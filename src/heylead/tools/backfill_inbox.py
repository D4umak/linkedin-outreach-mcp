"""Backfill unreplied inbox messages — one-time catch-up script.

Scans the LinkedIn inbox, finds conversations where the prospect
messaged but never got a reply, and processes them through the
inbound pipeline (classify → send discovery DM).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..timeutil import signal_sent_at, to_epoch
from ..ai.inbound_qualifier import (
    InboundQualification,
    generate_discovery_question,
    qualify_inbound,
)
from ..db.async_bridge import run_db
from ..db.queries import (
    enroll_prospect,
    get_inbound_signal_by_sender,
    get_setting,
    list_campaigns,
    list_icps,
    list_inbound_signals,
    log_action,
    save_inbound_signal,
    save_message,
    update_inbound_signal,
    update_outreach,
)
from ..db.signal_queries import get_contact_by_linkedin_id
from ..linkedin.message_sender import message_is_ours
from ..tools.inbox import _extract_chat_info, _fetch_raw_chats, _resolve_profile

logger = logging.getLogger(__name__)


def _dm_rate_limited(result: dict) -> bool:
    """True when LinkedIn told us to stop. Unknown outcomes are not this."""
    if result.get("blocked") or result.get("status_code") == 429:
        return True
    err = str(result.get("error") or "").lower()
    return "rate limit" in err or "429" in err


async def run_backfill_inbox(
    limit: int = 50,
    dry_run: bool = False,
    min_confidence: float = 0.0,
    send_only: bool = False,
) -> str:
    """Scan inbox for unreplied messages and process them.

    Args:
        limit: Max conversations to scan.
        dry_run: If True, classify only — don't send DMs.
        min_confidence: Only send DMs for signals >= this confidence.
        send_only: If True, skip detection — send DMs for already-classified
            signals in the DB. Avoids API rate limits from re-scanning.

    Returns:
        Summary string.
    """
    from ..linkedin import UnipileError, get_account_id, get_linkedin_client

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected."

    logger.info(
        "backfill_inbox started: limit=%d dry_run=%s min_confidence=%.2f send_only=%s",
        limit, dry_run, min_confidence, send_only,
    )

    try:
        client = get_linkedin_client()
    except UnipileError as e:
        logger.error("LinkedIn client init failed: %s", e)
        return f"LinkedIn client error: {e}"

    try:
        if send_only:
            return await _send_classified_signals(
                client, account_id, limit, min_confidence,
            )
        return await _backfill(
            client, account_id, limit, dry_run, min_confidence,
        )
    except Exception as e:
        logger.error("Backfill error: %s", e, exc_info=True)
        return f"Backfill error: {e}"
    finally:
        await client.close()


async def _send_classified_signals(
    client: Any,
    account_id: str,
    limit: int,
    min_confidence: float,
) -> str:
    """Send DMs for already-classified inbound signals. No API scanning needed."""
    classified_signals = await run_db(
        list_inbound_signals, status="classified", signal_type="message", limit=limit,
    )
    if not classified_signals:
        logger.info("send_classified_signals: no classified signals found")
        return "No classified signals waiting to be actioned. Run `backfill_inbox()` first to detect and classify."

    logger.info("send_classified_signals: %d classified signals loaded", len(classified_signals))

    voice = await run_db(get_setting, "voice_signature", {})
    lines = [f"**Processing {len(classified_signals)} classified signals**\n"]
    sent = 0
    skipped = 0
    errors = 0

    for signal in classified_signals:
        intent = signal.get("intent", "unknown")
        confidence = signal.get("confidence", 0) or 0
        sender_id = signal.get("sender_id", "")
        sender_name = signal.get("sender_name", "Unknown")

        from ..services.inbound_policy import decide_inbound_reply

        reply_decision = decide_inbound_reply(signal)
        if reply_decision == "dismiss":
            logger.info(
                "Signal %s (%s): dismissed — intent=%s confidence=%.2f",
                signal["id"], sender_name, intent, confidence,
            )
            await run_db(update_inbound_signal, signal["id"], status="dismissed")
            lines.append(f"- {sender_name}: skipped ({intent})")
            skipped += 1
            continue

        if confidence < min_confidence:
            logger.info(
                "Signal %s (%s): skipped — confidence %.2f below threshold %.2f",
                signal["id"], sender_name, confidence, min_confidence,
            )
            lines.append(f"- {sender_name}: skipped (confidence {confidence:.0%} < {min_confidence:.0%})")
            skipped += 1
            continue

        if not sender_id:
            logger.warning("Signal %s (%s): skipped — no sender_id", signal["id"], sender_name)
            skipped += 1
            continue

        # Skip if already a campaign contact
        existing_contact = await run_db(get_contact_by_linkedin_id, sender_id)
        if existing_contact:
            logger.info(
                "Signal %s (%s): dismissed — already a contact (id=%s)",
                signal["id"], sender_name, existing_contact.get("id", "?"),
            )
            await run_db(update_inbound_signal, signal["id"], status="dismissed")
            lines.append(f"- {sender_name}: skipped (already a contact)")
            skipped += 1
            continue

        sender_profile = {
            "name": sender_name,
            "headline": signal.get("sender_headline", ""),
            "company": signal.get("sender_company", ""),
        }
        qual = InboundQualification(
            intent=intent,
            matched_icp_id=signal.get("matched_icp_id"),
            confidence=confidence,
            recommended_action=signal.get("recommended_action", "ask_purpose"),
            reasoning=signal.get("reasoning", ""),
        )

        try:
            from ..linkedin.rate_limiter import check_daily_cap

            # Peek first so we don't generate a DM we cannot send. The real
            # booking happens immediately before the LinkedIn call below.
            can, current, cap, _ = await check_daily_cap("dm", reserve=False)
            if not can:
                lines.append(
                    f"- daily DM cap reached ({current}/{cap}) — stopping"
                )
                break

            # Throttle: small delay between sends
            if sent > 0:
                await asyncio.sleep(3)

            dm_type = "counter_pitch" if reply_decision == "counter_pitch" else "discovery"
            logger.info(
                "Signal %s (%s): generating %s DM — intent=%s confidence=%.2f",
                signal["id"], sender_name, dm_type, intent, confidence,
            )
            if reply_decision == "counter_pitch":
                from ..ai.inbound_qualifier import generate_counter_pitch
                dm_result = await generate_counter_pitch(
                    sender_profile=sender_profile,
                    content=signal.get("content", ""),
                    voice=voice,
                )
            else:
                dm_result = await generate_discovery_question(
                    sender_profile=sender_profile,
                    signal_type="message",
                    content=signal.get("content", ""),
                    voice=voice,
                    qualification=qual,
                )
            dm_text = dm_result.get("message", "")
            logger.info(
                "Signal %s (%s): DM generated (%s) — %d chars, reasoning=%s",
                signal["id"], sender_name, dm_type,
                len(dm_text), dm_result.get("reasoning", "")[:100],
            )
            if not dm_text:
                logger.warning("Signal %s (%s): empty DM generated, skipping", signal["id"], sender_name)
                lines.append(f"- {sender_name}: skipped (no DM generated)")
                skipped += 1
                continue

            # Find chat and send
            can, current, cap, _ = await check_daily_cap("dm")
            if not can:
                lines.append(
                    f"- daily DM cap reached ({current}/{cap}) — stopping"
                )
                break

            chat_id = await client.find_chat_for_user(account_id, sender_id)
            logger.info(
                "Signal %s (%s): sending via %s (chat_id=%s)",
                signal["id"], sender_name,
                "existing_chat" if chat_id else "new_message",
                chat_id or "N/A",
            )
            if chat_id:
                result = await client.send_message(
                    account_id=account_id, chat_id=chat_id, text=dm_text,
                )
            else:
                result = await client.send_new_message(
                    account_id=account_id, provider_id=sender_id, text=dm_text,
                )

            if result.get("success"):
                logger.info(
                    "Signal %s (%s): DM sent successfully — text='%s'",
                    signal["id"], sender_name, dm_text[:120],
                )
                campaign_id = signal.get("campaign_id", "")
                now = int(time.time())
                try:
                    outreach_id = await run_db(
                        enroll_prospect,
                        campaign_id,
                        {
                            "name": sender_name,
                            "title": signal.get("sender_headline", ""),
                            "company": signal.get("sender_company", ""),
                            "linkedin_url": signal.get("sender_url", ""),
                            "linkedin_id": sender_id,
                        },
                        source="inbound_dm",
                        status="connected",
                        signal_id=signal["id"],
                    )
                    if not outreach_id:
                        continue
                    await run_db(update_outreach, outreach_id, accepted_at=now)
                    await run_db(save_message, outreach_id, role="sdr", text=dm_text)
                    await run_db(
                        save_message, outreach_id, role="prospect",
                        text=signal.get("content", ""),
                        # Backfill runs long after the fact: stamp the message
                        # when they SENT it (the signal's provider time) --
                        # the signal's own created_at is only when we noticed.
                        timestamp=signal_sent_at(signal),
                        external_message_id=signal.get("message_id") or None,
                    )
                    await run_db(
                        update_inbound_signal, signal["id"],
                        status="engaged",
                        outreach_id=outreach_id,
                        actioned_at=now,
                    )
                except Exception as e:
                    logger.warning("Failed to create contact/outreach for %s: %s", sender_name, e)

                await run_db(
                    log_action,
                    "inbound_discovery_dm_sent",
                    result="success",
                    details={
                        "signal_id": signal["id"],
                        "sender_name": sender_name,
                        "intent": intent,
                        "confidence": confidence,
                        "dm_text": dm_text[:200],
                        "backfill": True,
                    },
                )
                lines.append(f"- **{sender_name}**: sent DM")
                sent += 1
            else:
                error_msg = result.get("error", "unknown error")
                logger.warning(
                    "Signal %s (%s): DM send failed — error=%s full_result=%s",
                    signal["id"], sender_name, error_msg, result,
                )
                if _dm_rate_limited(result):
                    lines.append(f"- {sender_name}: rate limited — stopping")
                    errors += 1
                    break  # Stop on rate limit
                lines.append(f"- {sender_name}: failed ({error_msg})")
                errors += 1

        except Exception as e:
            logger.warning("Signal %s (%s): DM exception — %s", signal["id"], sender_name, e, exc_info=True)
            lines.append(f"- {sender_name}: error ({e})")
            errors += 1

    logger.info(
        "send_classified_signals complete: sent=%d skipped=%d errors=%d",
        sent, skipped, errors,
    )
    lines.append(f"\n**Result**: {sent} sent, {skipped} skipped, {errors} failed")
    return "\n".join(lines)


async def _our_provider_id() -> str:
    """Our own LinkedIn provider_id, as stored by setup_profile."""
    import json

    try:
        raw = await run_db(get_setting, "profile", "")
        profile = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
        if isinstance(profile, dict):
            return str(profile.get("provider_id", "") or "")
    except Exception:
        pass
    return ""


async def _backfill(
    client: Any,
    account_id: str,
    limit: int,
    dry_run: bool,
    min_confidence: float,
) -> str:
    """Core backfill logic."""
    # A message's sender_id is the sender's LinkedIn provider_id, never the
    # Unipile account id — comparing the two marks every chat unreplied and
    # lets our own last message be answered as if the prospect wrote it.
    our_provider_id = await _our_provider_id()
    if not our_provider_id:
        return (
            "Can't tell your own messages apart without your LinkedIn "
            "provider_id — run setup_profile, then retry backfill_inbox."
        )

    # ── 1. Fetch all inbox conversations ─────────────────────────────────
    logger.info("Phase 1: fetching inbox conversations (limit=%d)", limit)
    raw_chats = await _fetch_raw_chats(client, account_id, limit)
    if not raw_chats:
        logger.info("Phase 1: no conversations found in inbox")
        return "No conversations found in inbox."

    chat_infos = [_extract_chat_info(c) for c in raw_chats]
    logger.info("Phase 1: fetched %d conversations from inbox", len(chat_infos))

    # ── 2. Find unreplied conversations ──────────────────────────────────
    logger.info("Phase 2: scanning for unreplied conversations")
    unreplied: list[dict[str, Any]] = []
    skipped_replied = 0
    skipped_no_text = 0
    skipped_existing = 0
    skipped_error = 0
    BATCH = 10

    for start in range(0, len(chat_infos), BATCH):
        batch = chat_infos[start : start + BATCH]
        tasks = [
            client.get_chat_messages(account_id, ci["chat_id"], limit=20)
            for ci in batch
            if ci["chat_id"]
        ]
        msg_results = await asyncio.gather(*tasks, return_exceptions=True)

        task_idx = 0
        for ci in batch:
            if not ci["chat_id"]:
                continue
            msgs = msg_results[task_idx]
            task_idx += 1

            if isinstance(msgs, Exception):
                logger.debug(
                    "Phase 2: chat %s — message fetch error: %s",
                    ci["chat_id"], msgs,
                )
                skipped_error += 1
                continue
            if not msgs:
                skipped_error += 1
                continue

            # Check if we (account owner) ever replied in this chat
            our_messages = [
                m for m in msgs
                if message_is_ours(m, our_provider_id)
            ]
            our_last_message = ""
            if our_messages:
                latest_ours = max(
                    our_messages,
                    key=lambda m: m.get("timestamp") or 0,
                )
                our_last_message = (latest_ours.get("text") or "").strip()
                skipped_replied += 1
                continue  # We already replied — skip

            # Collect the prospect's messages
            prospect_msgs = [
                m for m in msgs
                if m.get("sender_id") and not message_is_ours(m, our_provider_id)
            ]
            if not prospect_msgs:
                skipped_no_text += 1
                continue

            # Get the most recent prospect message
            prospect_msgs.sort(key=lambda m: m.get("timestamp", 0), reverse=True)
            latest = prospect_msgs[0]
            text = (latest.get("text") or "").strip()
            if not text:
                skipped_no_text += 1
                continue

            sender_id = latest.get("sender_id", "")
            if not sender_id:
                skipped_no_text += 1
                continue

            # Skip if already a campaign contact
            existing_contact = await run_db(get_contact_by_linkedin_id, sender_id)
            if existing_contact:
                logger.debug(
                    "Phase 2: chat %s — sender %s already a contact, skipping",
                    ci["chat_id"], ci.get("contact_name", sender_id),
                )
                skipped_existing += 1
                continue

            unreplied.append({
                "chat_id": ci["chat_id"],
                "sender_id": sender_id,
                "sender_name": ci.get("contact_name") or latest.get("sender_name", ""),
                "sender_headline": ci.get("contact_headline", ""),
                "provider_id": ci.get("contact_provider_id") or sender_id,
                "text": text,
                "all_texts": "\n".join(
                    (m.get("text") or "") for m in prospect_msgs if m.get("text")
                ),
                "message_id": latest.get("message_id", ""),
                "timestamp": latest.get("timestamp", 0),
                "our_last_message": our_last_message,
            })

    logger.info(
        "Phase 2: %d unreplied found — %d already replied, %d no text, %d existing contacts, %d errors (of %d total)",
        len(unreplied), skipped_replied, skipped_no_text, skipped_existing, skipped_error, len(chat_infos),
    )

    if not unreplied:
        return "All inbox conversations have replies. Nothing to backfill."

    # Resolve missing names/headlines
    needs_profile = [u for u in unreplied if not u["sender_name"]]
    if needs_profile:
        logger.info("Phase 2b: resolving %d missing profiles", len(needs_profile))
    for start in range(0, len(needs_profile), BATCH):
        batch = needs_profile[start : start + BATCH]
        tasks = [
            _resolve_profile(client, account_id, u["provider_id"])
            for u in batch
        ]
        profiles = await asyncio.gather(*tasks, return_exceptions=True)
        for u, profile in zip(batch, profiles):
            if isinstance(profile, dict):
                u["sender_name"] = profile.get("name", "")
                u["sender_headline"] = profile.get("headline", "")
            else:
                logger.debug("Profile resolve failed for %s: %s", u["provider_id"], profile)

    # ── 3. Classify each unreplied message ───────────────────────────────
    logger.info("Phase 3: classifying %d unreplied conversations", len(unreplied))
    active_icps = await run_db(list_icps, status="active")
    logger.info("Phase 3: %d active ICPs loaded for matching", len(active_icps))
    classified: list[dict[str, Any]] = []

    for u in unreplied:
        sender_id = u["sender_id"]

        # Check if signal already exists
        existing = await run_db(get_inbound_signal_by_sender, sender_id, "message")
        if existing:
            status = existing.get("status", "")
            if status in ("engaged",):
                logger.debug(
                    "Phase 3: %s (sender=%s) — already engaged, skipping",
                    u.get("sender_name", "?"), sender_id,
                )
                continue  # Already handled
            if status in ("new", "classified", "accepted"):
                logger.info(
                    "Phase 3: %s — reusing existing signal %s (status=%s, intent=%s)",
                    u.get("sender_name", "?"), existing.get("id"),
                    status, existing.get("intent", "?"),
                )
                # Use existing signal
                classified.append({**u, "signal": existing})
                continue

        # Save new signal
        signal_id = await run_db(
            save_inbound_signal,
            signal_type="message",
            sender_name=u["sender_name"],
            sender_id=sender_id,
            sender_headline=u.get("sender_headline", ""),
            content=u["text"],
            message_id=u.get("message_id", ""),
            sent_at=to_epoch(u.get("timestamp")),
        )

        # Classify
        profile = {
            "name": u["sender_name"],
            "headline": u.get("sender_headline", ""),
            "company": "",
            "title": u.get("sender_headline", ""),
        }
        logger.info(
            "Phase 3: classifying %s — headline='%s', text='%s'",
            u["sender_name"], u.get("sender_headline", "")[:60],
            u["text"][:80],
        )
        try:
            qual = await qualify_inbound(
                profile=profile,
                content=u["all_texts"] or u["text"],
                signal_type="message",
                active_icps=active_icps,
                our_last_message=u.get("our_last_message") or "",
            )
            logger.info(
                "Phase 3: %s classified — intent=%s confidence=%.2f action=%s icp=%s reason='%s'",
                u["sender_name"], qual.intent, qual.confidence,
                qual.recommended_action, qual.matched_icp_id or "none",
                qual.reasoning[:100],
            )
        except Exception as e:
            logger.warning("Phase 3: classify failed for %s: %s", u["sender_name"], e, exc_info=True)
            qual = InboundQualification(
                intent="unknown",
                matched_icp_id=None,
                confidence=0.3,
                recommended_action="ask_purpose",
                reasoning=f"Classification error: {e}",
            )

        # Resolve campaign
        try:
            from ..services.inbound_helpers import resolve_campaign_for_signal as _resolve_campaign_for_signal

            sig_dict = {
                "content": u["text"],
                "sender_headline": u.get("sender_headline", ""),
                "sender_company": "",
            }
            # Sync helper, reads the DB — must not be called straight from
            # this coroutine or it raises and every backfilled signal loses
            # its campaign, silently, via the except below.
            campaign_id = await run_db(_resolve_campaign_for_signal, sig_dict)
        except Exception:
            campaign_id = ""

        try:
            await run_db(
                update_inbound_signal,
                signal_id,
                intent=qual.intent,
                matched_icp_id=qual.matched_icp_id or "",
                confidence=qual.confidence,
                recommended_action=qual.recommended_action,
                reasoning=qual.reasoning,
                status="classified",
                qualified_at=int(time.time()),
                **({"campaign_id": campaign_id} if campaign_id else {}),
            )
        except Exception as e:
            # FK constraint or other DB error — retry without campaign_id
            logger.debug("Update with campaign_id failed: %s, retrying without", e)
            await run_db(
                update_inbound_signal,
                signal_id,
                intent=qual.intent,
                confidence=qual.confidence,
                recommended_action=qual.recommended_action,
                reasoning=qual.reasoning,
                status="classified",
                qualified_at=int(time.time()),
            )

        signal = {
            "id": signal_id,
            "signal_type": "message",
            "sender_name": u["sender_name"],
            "sender_id": sender_id,
            "sender_headline": u.get("sender_headline", ""),
            "sender_company": "",
            "sender_url": "",
            "content": u["text"],
            "message_id": u.get("message_id", ""),
            "intent": qual.intent,
            "confidence": qual.confidence,
            "matched_icp_id": qual.matched_icp_id,
            "recommended_action": qual.recommended_action,
            "reasoning": qual.reasoning,
            "campaign_id": campaign_id,
        }
        classified.append({**u, "signal": signal, "qual": qual})

    logger.info("Phase 3: %d conversations classified", len(classified))

    if not classified:
        return "No unreplied messages to process after dedup."

    # ── 4. Build summary of classified signals ───────────────────────────
    lines = [f"**Backfill: {len(classified)} unreplied conversations found**\n"]

    for i, item in enumerate(classified, 1):
        sig = item.get("signal", {})
        name = item.get("sender_name") or "Unknown"
        headline = item.get("sender_headline", "")
        intent = sig.get("intent", "?")
        conf = sig.get("confidence", 0) or 0
        text_preview = (item.get("text") or "")[:80].replace("\n", " ")

        line = f"{i}. **{name}**"
        if headline:
            line += f" — {headline[:50]}"
        line += f"\n   Intent: {intent} ({conf:.0%})"
        line += f"\n   Message: {text_preview}"
        if len(item.get("text", "")) > 80:
            line += "..."
        lines.append(line)

    if dry_run:
        logger.info("Phase 4: dry run — %d classified, no DMs sent", len(classified))
        lines.append(
            f"\n**Dry run** — no DMs sent. "
            f"Run with `dry_run=False` to send discovery DMs "
            f"(off-ICP vendor/partnership pitches stay dismissed)."
        )
        return "\n".join(lines)

    # ── 5. Send discovery DMs ────────────────────────────────────────────
    logger.info("Phase 5: sending discovery DMs for %d classified conversations", len(classified))

    voice = await run_db(get_setting, "voice_signature", {})
    sent = 0
    skipped = 0
    errors = 0

    lines.append(f"\n**Sending discovery DMs** ({len(classified)} conversations)\n")

    for item in classified:
        sig = item.get("signal", {})
        intent = sig.get("intent", "unknown")
        confidence = sig.get("confidence", 0) or 0

        from ..services.inbound_policy import decide_inbound_reply

        reply_decision = decide_inbound_reply(sig)
        if reply_decision == "dismiss":
            logger.info(
                "Phase 5: %s — dismissed (intent=%s)",
                item.get("sender_name"), intent,
            )
            await run_db(update_inbound_signal, sig["id"], status="dismissed")
            lines.append(f"- {item.get('sender_name')}: skipped ({intent})")
            skipped += 1
            continue

        # Skip below min confidence
        if confidence < min_confidence:
            logger.info(
                "Phase 5: %s — skipped (confidence %.2f < threshold %.2f)",
                item.get("sender_name"), confidence, min_confidence,
            )
            lines.append(
                f"- {item.get('sender_name')}: skipped (confidence {confidence:.0%} < {min_confidence:.0%})"
            )
            skipped += 1
            continue

        sender_id = item["sender_id"]
        sender_profile = {
            "name": item.get("sender_name", ""),
            "headline": item.get("sender_headline", ""),
            "company": "",
        }

        qual = InboundQualification(
            intent=intent,
            matched_icp_id=sig.get("matched_icp_id"),
            confidence=confidence,
            recommended_action=sig.get("recommended_action", "ask_purpose"),
            reasoning=sig.get("reasoning", ""),
        )

        try:
            from ..linkedin.rate_limiter import check_daily_cap

            can, current, cap, _ = await check_daily_cap("dm", reserve=False)
            if not can:
                lines.append(
                    f"- daily DM cap reached ({current}/{cap}) — stopping"
                )
                break

            if sent > 0:
                await asyncio.sleep(3)

            content_text = item.get("all_texts") or item.get("text", "")
            dm_type = "counter_pitch" if reply_decision == "counter_pitch" else "discovery"
            logger.info(
                "Phase 5: %s — generating %s DM (intent=%s confidence=%.2f)",
                item.get("sender_name"), dm_type, intent, confidence,
            )
            if reply_decision == "counter_pitch":
                from ..ai.inbound_qualifier import generate_counter_pitch
                dm_result = await generate_counter_pitch(
                    sender_profile=sender_profile,
                    content=content_text,
                    voice=voice,
                )
            else:
                dm_result = await generate_discovery_question(
                    sender_profile=sender_profile,
                    signal_type="message",
                    content=content_text,
                    voice=voice,
                    qualification=qual,
                )
            dm_text = dm_result.get("message", "")
            logger.info(
                "Phase 5: %s — DM generated (%s, %d chars): '%s' reasoning='%s'",
                item.get("sender_name"), dm_type, len(dm_text),
                dm_text[:120], dm_result.get("reasoning", "")[:80],
            )
            if not dm_text:
                logger.warning("Phase 5: %s — empty DM generated, skipping", item.get("sender_name"))
                lines.append(f"- {item.get('sender_name')}: skipped (no DM generated)")
                skipped += 1
                continue

            # Send to existing chat
            can, current, cap, _ = await check_daily_cap("dm")
            if not can:
                lines.append(
                    f"- daily DM cap reached ({current}/{cap}) — stopping"
                )
                break

            chat_id = item.get("chat_id", "")
            logger.info(
                "Phase 5: %s — sending via %s (chat_id=%s, sender_id=%s)",
                item.get("sender_name"),
                "existing_chat" if chat_id else "new_message",
                chat_id or "N/A", sender_id,
            )
            if chat_id:
                result = await client.send_message(
                    account_id=account_id, chat_id=chat_id, text=dm_text,
                )
            else:
                result = await client.send_new_message(
                    account_id=account_id, provider_id=sender_id, text=dm_text,
                )

            if result.get("success"):
                logger.info(
                    "Phase 5: %s — DM sent successfully",
                    item.get("sender_name"),
                )
                # Create contact + outreach for tracking
                campaign_id = sig.get("campaign_id", "")
                now = int(time.time())
                try:
                    outreach_id = await run_db(
                        enroll_prospect,
                        campaign_id,
                        {
                            "name": item.get("sender_name", ""),
                            "title": item.get("sender_headline", ""),
                            "linkedin_id": sender_id,
                        },
                        source="inbound_dm",
                        status="connected",
                        signal_id=sig["id"],
                    )
                    if not outreach_id:
                        continue
                    await run_db(update_outreach, outreach_id, accepted_at=now)
                    await run_db(save_message, outreach_id, role="sdr", text=dm_text)
                    # Also save their original message, keeping the provider's
                    # own timestamp — the chat scan already collected it.
                    await run_db(
                        save_message, outreach_id, role="prospect",
                        text=item.get("text", ""),
                        timestamp=to_epoch(item.get("timestamp")),
                        external_message_id=item.get("message_id") or None,
                    )
                    await run_db(
                        update_inbound_signal, sig["id"],
                        status="engaged",
                        outreach_id=outreach_id,
                        actioned_at=now,
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to create contact/outreach for %s: %s",
                        item.get("sender_name"), e,
                    )

                await run_db(
                    log_action,
                    "inbound_discovery_dm_sent",
                    result="success",
                    details={
                        "signal_id": sig["id"],
                        "sender_name": item.get("sender_name", ""),
                        "intent": intent,
                        "confidence": confidence,
                        "dm_text": dm_text[:200],
                        "backfill": True,
                    },
                )
                lines.append(f"- **{item.get('sender_name')}**: sent DM")
                sent += 1
            else:
                error_msg = result.get("error", "unknown error")
                logger.warning(
                    "Phase 5: %s — DM send failed: error=%s full_result=%s",
                    item.get("sender_name"), error_msg, result,
                )
                if _dm_rate_limited(result):
                    lines.append(
                        f"- {item.get('sender_name')}: rate limited — stopping"
                    )
                    errors += 1
                    break
                lines.append(f"- {item.get('sender_name')}: failed ({error_msg})")
                errors += 1

        except Exception as e:
            logger.warning(
                "Phase 5: %s — DM exception: %s",
                item.get("sender_name"), e, exc_info=True,
            )
            lines.append(f"- {item.get('sender_name')}: error ({e})")
            errors += 1

    logger.info(
        "backfill complete: %d classified, %d sent, %d skipped, %d errors",
        len(classified), sent, skipped, errors,
    )
    lines.append(f"\n**Result**: {sent} sent, {skipped} skipped, {errors} failed")
    return "\n".join(lines)
