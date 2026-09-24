"""Unified Inbound Pipeline v2 — Classify-First Architecture.

Single-pass pipeline that handles 100% of inbound traffic:
1. DETECT  — Fetch invitations + messages, save as inbound_signals
2. CLASSIFY — AI qualification on every new signal BEFORE any action
3. ACT     — Accept/ignore/decline invitations, respond/react to messages
4. TRACK   — Create outreaches, log actions

Replaces the old accept-first approach where all invitations were
accepted immediately and qualified later.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..ai.inbound_qualifier import (
    InboundQualification,
    generate_discovery_question,
    qualify_inbound,
)
from ..ai.reply_pipeline import run_reply_pipeline
from ..ai.sentiment import classify_fast, classify_sentiment
from ..timeutil import signal_sent_at, to_epoch
from ..constants import (
    INBOUND_MAX_DM_ATTEMPTS,
)
from ..db.async_bridge import run_db
from ..linkedin.message_sender import message_is_ours
from ..db.queries import (
    count_inbound_dms_today,
    enroll_prospect,
    get_inbound_signal_by_sender,
    get_messages_for_outreach,
    get_setting,
    list_icps,
    list_inbound_signals,
    log_action,
    save_inbound_signal,
    save_message,
    save_setting,
    update_inbound_signal,
    update_outreach,
)
from ..db.signal_queries import (
    get_contact_by_name_company,
)
from ..db.global_contact_queries import upsert_global_contact
from ..db.schema import get_db
from ..services.inbox_match import find_best_inbox_match

logger = logging.getLogger(__name__)

# ── Configurable thresholds ──────────────────────────────────────────────────

INBOUND_ACCEPT_CONFIDENCE = 0.4   # Min confidence to accept an invitation
INBOUND_DECLINE_SPAM = False       # If True, actively decline spam (default: ignore)
DAILY_INBOUND_ACCEPT_LIMIT = 50   # Max invitations to accept per day


def _profile_display_name(profile: dict[str, Any]) -> str:
    """Compose a name from a Unipile / normalized profile dict."""
    name = (profile.get("name") or "").strip()
    if name:
        return name
    return " ".join(
        part for part in (
            (profile.get("first_name") or "").strip(),
            (profile.get("last_name") or "").strip(),
        ) if part
    ).strip()


async def _resolve_inbound_identity(
    client: Any,
    account_id: str,
    sender_id: str,
    name: str,
    headline: str,
    company: str,
    url: str,
) -> tuple[str, str, str, str]:
    """Fill blank Unipile identity from a profile fetch."""
    if (name or "").strip() and (headline or "").strip():
        return name, headline, company, url
    if not client or not account_id or not sender_id:
        return name, headline, company, url
    try:
        profile = await client.get_profile(account_id, sender_id)
    except Exception:
        logger.debug("inbound identity fetch failed for %s", sender_id, exc_info=True)
        return name, headline, company, url
    if not profile:
        return name, headline, company, url
    resolved_name = (name or "").strip() or _profile_display_name(profile)
    resolved_headline = (
        (headline or "").strip()
        or (profile.get("headline") or profile.get("occupation") or profile.get("title") or "")
    )
    resolved_company = (company or "").strip() or (profile.get("company") or "")
    resolved_url = (
        (url or "").strip()
        or (profile.get("profile_url") or profile.get("linkedin_url") or "")
    )
    return resolved_name, resolved_headline, resolved_company, resolved_url


def get_our_last_outbound_text(sender_id: str) -> str:
    """Last SDR text we sent this person, from messages or inbound action log."""
    lid = (sender_id or "").strip()
    if not lid:
        return ""
    db = get_db()
    try:
        row = db.execute(
            """SELECT m.text FROM messages m
               JOIN outreaches o ON o.id = m.outreach_id
               JOIN contacts c ON c.id = o.contact_id
               WHERE c.linkedin_id = ? AND m.role = 'sdr'
                 AND m.text IS NOT NULL AND trim(m.text) != ''
               ORDER BY m.timestamp DESC LIMIT 1""",
            (lid,),
        ).fetchone()
        if row and row[0]:
            return str(row[0]).strip()

        row = db.execute(
            """SELECT al.details_json FROM actions_log al
               JOIN inbound_signals s
                 ON json_extract(al.details_json, '$.signal_id') = s.id
               WHERE s.sender_id = ?
                 AND al.action_type IN (
                     'inbound_contextual_reply_sent',
                     'inbound_discovery_dm_sent'
                 )
               ORDER BY al.timestamp DESC LIMIT 1""",
            (lid,),
        ).fetchone()
        if row and row[0]:
            try:
                details = json.loads(row[0])
            except (TypeError, ValueError):
                details = {}
            text = (details.get("dm_text") or "").strip()
            if text:
                return text
        return ""
    finally:
        db.close()


# ── Main Entry Point ─────────────────────────────────────────────────────────

async def process_inbound_pipeline() -> str:
    """Single-pass unified inbound processor.

    1. DETECT: Fetch all inbound signals (invitations, messages)
    2. CLASSIFY: Run AI qualification on every new signal BEFORE any action
    3. ACT: Accept/ignore/decline invitations, respond/react to messages
    4. TRACK: Update DB state, create outreaches, log everything

    Returns a summary string.
    """
    from ..linkedin import UnipileError, get_account_id, get_linkedin_client

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected."

    try:
        client = get_linkedin_client()
    except UnipileError as e:
        return f"LinkedIn client error: {e}"

    try:
        # The watermark has to be the clock reading from before detection:
        # classify/act can run for minutes, and anything arriving in that
        # window would otherwise be older than the mark and never detected.
        scan_started = int(time.time())

        # ── DETECT ──
        inv_count, inv_ok = 0, True
        try:
            inv_count = await _detect_invitations(client, account_id)
        except Exception as e:
            inv_ok = False
            logger.warning("Failed to fetch invitations: %s", e)
        msg_count, msg_ok = 0, True
        try:
            msg_count = await _detect_messages(client, account_id)
        except Exception as e:
            msg_ok = False
            logger.warning("Failed to list all messages: %s", e)

        # ── CLASSIFY ──
        classified = await _classify_all_new()

        # ── ACT ──
        # JOB_PROCESS_INBOUND stays always-on so detect/classify still run in
        # observe and off. Sends and invitation accepts are full-mode only.
        from ..config import get_scheduler_mode

        mode = get_scheduler_mode()
        inv_summary = ""
        msg_summary = ""
        act_withheld = ""
        if mode != "full":
            act_withheld = f"Act withheld (scheduler={mode})"
        else:
            inv_summary = await _act_on_invitations(client, account_id)
            msg_summary = await _act_on_messages(client, account_id)

        # ── Record scan timestamp for next run's time filter ──
        # Only when detection actually read the window. Both detectors
        # swallow their own errors and return 0, which is indistinguishable
        # from "nothing new" — so an outage used to produce a clean-looking
        # run that moved the mark straight over the window it had failed to
        # read, making every invitation and message in it invisible for
        # ever. Re-reading a window is cheap and deduped downstream; missing
        # one is permanent.
        if inv_ok and msg_ok:
            await run_db(save_setting, "last_inbound_scan_ts", scan_started)
        else:
            logger.warning(
                "Inbound detection incomplete (invitations_ok=%s messages_ok=%s) "
                "— holding the watermark so the next run re-reads this window",
                inv_ok, msg_ok,
            )

        # ── Summary ──
        parts = []
        if inv_count or msg_count:
            parts.append(f"Detected: {inv_count} invitations, {msg_count} messages")
        if classified:
            parts.append(f"Classified: {classified}")
        if act_withheld:
            parts.append(act_withheld)
        if inv_summary:
            parts.append(f"Invitations: {inv_summary}")
        if msg_summary:
            parts.append(f"Messages: {msg_summary}")

        if not (inv_ok and msg_ok):
            # Never "No inbound activity." — an operator reads that as all
            # clear, and this run read nothing at all.
            failed = []
            if not inv_ok:
                failed.append("invitations")
            if not msg_ok:
                failed.append("messages")
            parts.append(
                f"⚠️ Could not read {' and '.join(failed)} — this window will "
                "be re-read next run"
            )
        return " | ".join(parts) if parts else "No inbound activity."

    except Exception as e:
        logger.error("Inbound pipeline error: %s", e, exc_info=True)
        return f"Inbound pipeline error: {e}"
    finally:
        await client.close()


# ── DETECT ───────────────────────────────────────────────────────────────────

async def _detect_invitations(client: Any, account_id: str) -> int:
    """Fetch received invitations and save new ones as inbound_signals.

    RAISES when the window cannot be read. It used to swallow and return 0 —
    indistinguishable from "nothing new" — which let the caller advance the
    watermark past a window it never saw. An unreachable API is an error,
    not an answer.
    """
    invitations = await client.get_received_invitations(
        account_id, raise_on_error=True,
    )

    saved = 0
    for inv in invitations:
        inv_id = inv.get("id", "")
        sender_id = inv.get("sender_id", "")
        if not inv_id or not sender_id:
            continue

        # Skip while this sender still has an in-flight invitation, or when
        # the provider is reprinting the same invitation_id. A terminal row
        # (ignored / dismissed / declined / …) must not block a later
        # re-invite — notes are often empty, so the twin of the message
        # path's content check is invitation_id, not text.
        existing = await run_db(get_inbound_signal_by_sender, sender_id, "invitation")
        if existing:
            status = existing.get("status", "")
            if status in ("new", "classified", "accepted", "engaged"):
                continue
            if (existing.get("invitation_id") or "") == inv_id:
                continue

        await run_db(
            save_inbound_signal,
            signal_type="invitation",
            sender_name=inv.get("sender_name", ""),
            sender_id=sender_id,
            sender_headline=inv.get("headline", ""),
            content=inv.get("message", ""),
            invitation_id=inv_id,
            sent_at=to_epoch(inv.get("timestamp")),
        )
        saved += 1

    if saved:
        logger.info("Detected %d new inbound invitations", saved)
    return saved


async def _detect_messages(client: Any, account_id: str) -> int:
    """Fetch recent messages and save unsolicited ones as inbound_signals.

    Filters out:
    - Our own sent messages (by LinkedIn provider_id)
    - Messages older than last scan (time-based dedup)
    - Senders with existing active inbound signals
    - Known campaign contacts (by linkedin_id or provider_id)
    - Empty messages
    """
    # Raises rather than swallowing: see _detect_invitations.
    messages = await client.list_all_messages(
        account_id, limit=50, raise_on_error=True,
    )

    if not messages:
        return 0

    logger.info("Inbound detect: %d messages fetched from API", len(messages))

    # Resolve our LinkedIn provider_id for own-message filtering.
    # account_id is a Unipile UUID — sender_id is a LinkedIn provider_id (ACoAAA format).
    profile = await run_db(get_setting, "profile", {})
    our_provider_id = ""
    if isinstance(profile, dict):
        our_provider_id = profile.get("provider_id", "")
    if not our_provider_id:
        logger.warning(
            "Inbound detect: no provider_id in profile setting — own messages "
            "are told by Unipile's is_sender alone"
        )

    # Time-based filtering: skip messages older than last scan.
    last_scan_ts = await run_db(get_setting, "last_inbound_scan_ts", 0)
    if not last_scan_ts:
        last_scan_ts = int(time.time()) - 3600  # Default: last hour

    # Skip counters for logging
    skip_own = 0
    skip_old = 0
    skip_signal = 0
    skip_contact = 0
    skip_empty = 0

    saved = 0
    for msg in messages:
        sender_id = msg.get("sender_id", "")
        if not sender_id:
            continue

        # Skip our own sent messages
        if message_is_ours(msg, our_provider_id):
            skip_own += 1
            continue

        # Skip messages older than last scan. Providers report ISO-8601 strings
        # as often as epochs — comparing the raw value would raise TypeError and
        # disable this filter, re-detecting old messages on every scan.
        msg_ts = to_epoch(msg.get("timestamp"))
        if msg_ts and msg_ts <= last_scan_ts:
            skip_old += 1
            continue

        # Skip if this sender already has an inbound signal in ANY active state
        # (check without signal_type filter — a sender who came in as an
        # invitation and was already engaged should NOT be re-detected as a
        # new "message" signal, which caused duplicate discovery DMs).
        existing = await run_db(get_inbound_signal_by_sender, sender_id)
        if existing:
            status = existing.get("status", "")
            if status in ("new", "classified", "accepted", "engaged"):
                skip_signal += 1
                await _maybe_referral_from_inbound_msg(msg, sender_id)
                continue
            # For dismissed/ignored/declined: allow re-detection if message is NEW
            existing_content = (existing.get("content") or "").strip()
            new_content = (msg.get("text") or "").strip()
            if existing_content == new_content:
                skip_signal += 1
                continue

        # Known campaign contact — re-activate instead of skipping.
        # Prefer the outreach we actually invited, not a skipped duplicate.
        contact = await run_db(find_best_inbox_match, sender_id)
        if contact:
            text = msg.get("text", "")
            if not text:
                skip_empty += 1
                continue
            handled = await _reactivate_existing_contact(contact, sender_id, text,
                                                         timestamp=msg_ts)
            if handled:
                skip_contact += 1
                continue
            # Answers nothing we sent: fall through and save it as an inbound
            # signal, so the classify/act pipeline hears it instead of nobody.

        text = msg.get("text", "")
        if not text:
            skip_empty += 1
            continue

        message_id = msg.get("message_id", "")
        from .referral_enroll import fill_sender_name

        sender_name = await run_db(
            fill_sender_name, sender_id, msg.get("sender_name") or "",
        )
        headline = msg.get("sender_headline") or msg.get("headline") or ""
        company = msg.get("sender_company") or ""
        url = msg.get("sender_url") or msg.get("profile_url") or ""
        sender_name, headline, company, url = await _resolve_inbound_identity(
            client, account_id, sender_id, sender_name, headline, company, url,
        )
        if sender_name or headline:
            try:
                await run_db(
                    upsert_global_contact,
                    linkedin_id=sender_id,
                    name=sender_name or "",
                    title=headline,
                    company=company,
                    linkedin_url=url,
                    source="inbound_dm",
                )
            except Exception:
                logger.debug(
                    "inbound identity upsert failed for %s", sender_id, exc_info=True,
                )

        await run_db(
            save_inbound_signal,
            signal_type="message",
            sender_name=sender_name,
            sender_id=sender_id,
            sender_headline=headline,
            sender_company=company,
            sender_url=url,
            content=text,
            message_id=message_id,
            sent_at=msg_ts,
        )
        saved += 1
        from .live_thread_enroll import enrich_and_enroll_live_thread

        await enrich_and_enroll_live_thread(
            sender_id=sender_id,
            name=sender_name,
            text=text,
            headline=headline,
            company=company,
            linkedin_url=url,
            message_id=message_id,
            timestamp=msg_ts,
            client=client,
            account_id=account_id,
        )
        await _maybe_referral_from_inbound_msg(msg, sender_id)

    total_skipped = skip_own + skip_old + skip_signal + skip_contact + skip_empty
    logger.info(
        "Inbound detect: %d from API, %d skipped "
        "(own=%d old=%d signal=%d contact=%d empty=%d), %d new signals saved",
        len(messages), total_skipped,
        skip_own, skip_old, skip_signal, skip_contact, skip_empty, saved,
    )
    return saved


async def _maybe_referral_from_inbound_msg(msg: dict[str, Any], sender_id: str) -> None:
    """Enroll a referred colleague even when we skip a second inbound row."""
    from .referral_enroll import fill_sender_name, maybe_enroll_from_inbound

    text = (msg.get("text") or "").strip()
    if not text:
        return
    try:
        sender_name = await run_db(
            fill_sender_name, sender_id, msg.get("sender_name") or "",
        )
        await maybe_enroll_from_inbound(
            sender_id=sender_id,
            text=text,
            name=sender_name,
            company=msg.get("sender_company") or "",
        )
    except Exception:
        logger.debug("Referral enroll from inbound detect failed", exc_info=True)


async def _reactivate_existing_contact(
    contact: dict[str, Any],
    sender_id: str,
    text: str,
    timestamp: int | None = None,
) -> bool:
    """Re-activate an existing campaign contact who sent a new message.

    Finds their outreach, updates status to 'replied' or 'hot_lead',
    saves the prospect message, and queues an auto-reply job so the
    scheduler sends a response within 5-15 minutes.

    Returns True when the message was consumed here (reactivated, deduped,
    or an honored opt-out). False means it answers nothing we sent — the
    caller should save it as an inbound signal rather than drop it.
    """
    import random

    from ..constants import AUTO_REPLY_DELAY_MAX, AUTO_REPLY_DELAY_MIN, JOB_AUTO_REPLY
    from ..db.queries import create_scheduler_job, get_pending_outreach_job, log_action
    from ..db.schema import get_db

    contact_id = contact.get("contact_id") or contact.get("id", "")
    outreach_id_hint = contact.get("outreach_id") or ""
    if not contact_id and not outreach_id_hint:
        return False

    def _find_outreach() -> dict | None:
        db = get_db()
        if outreach_id_hint:
            row = db.execute(
                """SELECT id, status, campaign_id, invited_at FROM outreaches
                   WHERE id = ?""",
                (outreach_id_hint,),
            ).fetchone()
        else:
            row = db.execute(
                """SELECT id, status, campaign_id, invited_at FROM outreaches
                   WHERE contact_id = ?
                   ORDER BY updated_at DESC LIMIT 1""",
                (contact_id,),
            ).fetchone()
        db.close()
        return dict(row) if row else None

    outreach = await run_db(_find_outreach)
    if not outreach:
        # Campaign contact with no outreach: still an unsolicited inbound.
        return False

    outreach_id = outreach["id"]
    old_status = outreach["status"]

    # Don't re-activate opted_out contacts
    if old_status == "opted_out":
        return True

    # Dedup: check if we already saved this exact message
    existing_msgs = await run_db(get_messages_for_outreach, outreach_id)
    if existing_msgs:
        for m in reversed(existing_msgs):
            if m.get("role") == "prospect":
                if m.get("text", "").strip() == text.strip():
                    return True  # Already processed
                break

    # A reply has to answer something we actually sent — same rule as
    # check_replies. An inbox thread that predates our first touch is history
    # that happens to involve a campaign contact, not a reply; reactivating
    # on it mints the phantom replied/hot_lead rows the startup repair then
    # has to revert.
    from ..tools.check_replies import _answers_our_outreach

    if not _answers_our_outreach(
        {"invited_at": outreach.get("invited_at")},
        existing_msgs,
        {"timestamp": timestamp},
    ):
        # Opt-outs are honored even when they answer nothing we sent —
        # dropping "remove me" means the planner later invites the one
        # person who explicitly asked us not to. Keyword check only: no
        # LLM spend on messages the gate rejects.
        if classify_fast(text) == "opt_out":
            # A pre-outreach opt-out is not a reply: explicit NULLs keep the
            # flip from minting accepted_at/first_reply_at for a prospect we
            # never touched.
            await run_db(
                update_outreach, outreach_id, status="opted_out",
                accepted_at=None, first_reply_at=None,
            )
            await run_db(
                log_action, "opt_out_detected", outreach_id=outreach_id,
                details={"text": text[:200], "note": "pre-outreach opt-out"},
            )
            logger.info(
                "Pre-outreach opt-out honored: %s (outreach=%s)",
                contact.get("name", ""), outreach_id,
            )
            return True
        logger.debug(
            "Not a reply — no outreach of ours precedes it: outreach=%s",
            outreach_id,
        )
        return False

    # Classify sentiment
    sentiment = "neutral"
    try:
        sentiment = await classify_sentiment(text, outreach_id=outreach_id)
    except Exception:
        pass

    # Map sentiment to status
    if sentiment == "opt_out":
        await run_db(update_outreach, outreach_id, status="opted_out")
        return True

    new_status = "hot_lead" if sentiment == "positive" else "replied"

    # Re-activate if in a dormant/terminal status
    REACTIVATABLE = (
        "pending", "error", "closed_happy", "closed_unhappy", "exhausted",
        "messaged", "connected", "invited", "sending", "sending_followup",
        "replied", "hot_lead",
    )
    if old_status in REACTIVATABLE:
        await run_db(update_outreach, outreach_id, status=new_status)

    # Save the prospect message
    await run_db(save_message, outreach_id, role="prospect", text=text,
                 sentiment=sentiment, timestamp=timestamp)

    try:
        from .referral_enroll import maybe_enroll_from_reply
        referral_row = dict(contact)
        referral_row.setdefault("campaign_id", outreach.get("campaign_id", ""))
        await maybe_enroll_from_reply(referral_row, text)
    except Exception:
        logger.debug("Referral enroll from inbound reply failed", exc_info=True)

    # Queue auto-reply job immediately (don't wait for planner to discover it).
    # Meeting-intent (positive / calendar) used to skip this and wait for a
    # human — that left booking-link hot leads unanswered for days.
    has_pending = await run_db(get_pending_outreach_job, outreach_id, JOB_AUTO_REPLY)
    if not has_pending:
        delay = random.randint(AUTO_REPLY_DELAY_MIN, AUTO_REPLY_DELAY_MAX)
        scheduled_at = int(time.time()) + delay
        await run_db(
            create_scheduler_job,
            campaign_id=outreach.get("campaign_id", ""),
            job_type=JOB_AUTO_REPLY,
            scheduled_at=scheduled_at,
            outreach_id=outreach_id,
        )
        logger.info(
            "Queued auto-reply for reactivated outreach %s (delay=%ds)",
            outreach_id[:8], delay,
        )

    await run_db(
        log_action,
        "outreach_reactivated_inbound",
        outreach_id=outreach_id,
        result="success",
        details={
            "contact_name": contact.get("name", ""),
            "previous_status": old_status,
            "new_status": new_status,
            "sentiment": sentiment,
            "text_preview": text[:100],
        },
    )
    logger.info(
        "Re-activated outreach %s for contact %s (%s -> %s, sentiment=%s)",
        outreach_id[:8], contact.get("name", ""), old_status, new_status, sentiment,
    )
    return True


# ── SELLER DETECTION ─────────────────────────────────────────────────────────


async def _detect_seller_signal(signal: dict, content: str) -> None:
    """Check if an inbound prospect is a LinkedIn seller.

    If detected, creates a linkedin_seller signal in the signals table
    so the signal scorer can factor it into lead prioritization.
    """
    try:
        from ..linkedin import get_linkedin_client

        client = get_linkedin_client()
        messages = [content] if isinstance(content, str) else content
        result = await client.classify_seller(
            messages=messages,
            author_name=signal.get("sender_name", ""),
            author_headline=signal.get("sender_headline", ""),
        )

        if result.get("is_seller") and result.get("confidence", 0) >= 0.4:
            from ..constants import SIGNAL_LINKEDIN_SELLER
            from ..db.signal_queries import save_signal

            linkedin_id = signal.get("sender_linkedin_id", "")
            if not linkedin_id:
                return

            import json as _json

            await run_db(
                save_signal,
                SIGNAL_LINKEDIN_SELLER,
                "inbound_pipeline",
                linkedin_id=linkedin_id,
                prospect_name=signal.get("sender_name", ""),
                confidence=result["confidence"],
                metadata_json=_json.dumps({
                    "seller_type": result.get("seller_type", ""),
                    "reasoning": result.get("reasoning", ""),
                    "sender_headline": signal.get("sender_headline", ""),
                }),
            )
            logger.info(
                "Seller signal detected for %s (type=%s, conf=%.2f)",
                signal.get("sender_name", "Unknown"),
                result.get("seller_type", ""),
                result["confidence"],
            )
    except Exception as e:
        # Non-critical — don't block inbound pipeline
        logger.debug(
            "Seller detection failed for sender=%s: %s",
            signal.get("sender_linkedin_id") or "unknown", e,
        )


# ── CLASSIFY ─────────────────────────────────────────────────────────────────

async def _classify_all_new() -> str:
    """Run AI qualification on all new (unclassified) inbound signals.

    Returns summary string.
    """
    new_signals = await run_db(list_inbound_signals, status="new", limit=20)
    if not new_signals:
        return ""

    active_icps = await run_db(list_icps, status="active")

    classified_count = 0
    high_conf = 0

    for signal in new_signals:
        profile = {
            "name": signal.get("sender_name", ""),
            "headline": signal.get("sender_headline", ""),
            "company": signal.get("sender_company", ""),
            "title": signal.get("sender_headline", ""),
        }
        content = signal.get("content", "")
        signal_type = signal.get("signal_type", "invitation")
        our_last = await run_db(
            get_our_last_outbound_text, signal.get("sender_id") or "",
        )

        try:
            qual = await qualify_inbound(
                profile=profile,
                content=content,
                signal_type=signal_type,
                active_icps=active_icps,
                our_last_message=our_last,
            )

            # Resolve best-matching campaign. The helper is sync and reads the
            # DB, so it has to go through run_db like inbound_service.py does:
            # called directly it raises on the daemon's loop thread, and the
            # except below turns that into a warning that discards the
            # qualification we just paid an LLM for.
            from .inbound_relationship import persist_hold, resolve_inbound_relationship

            rel = await run_db(resolve_inbound_relationship, signal)
            if rel.action != "stranger" and rel.campaign_id:
                campaign_id = rel.campaign_id
            else:
                campaign_id = await run_db(_resolve_campaign_for_signal, signal)

            await run_db(
                update_inbound_signal,
                signal["id"],
                intent=qual.intent,
                matched_icp_id=qual.matched_icp_id,
                confidence=qual.confidence,
                recommended_action=qual.recommended_action,
                reasoning=qual.reasoning,
                status="classified",
                qualified_at=int(time.time()),
                **({"campaign_id": campaign_id} if campaign_id else {}),
            )
            if rel.action == "hold":
                await run_db(persist_hold, signal["id"], rel)
            classified_count += 1
            if qual.confidence >= 0.7:
                high_conf += 1

            # Seller detection — check if prospect does LinkedIn outreach
            if content and len(content) > 50:
                await _detect_seller_signal(signal, content)

        except Exception as e:
            logger.warning(
                "Failed to classify signal %s (%s): %s",
                signal["id"], signal.get("sender_name"), e,
            )

    summary = f"{classified_count}/{len(new_signals)} classified"
    if high_conf:
        summary += f", {high_conf} high-confidence"
    return summary


# ── ACT: Invitations ────────────────────────────────────────────────────────

async def _act_on_invitations(client: Any, account_id: str) -> str:
    """Accept, ignore, or decline invitations based on classification.

    Decision matrix:
    - buying_signal (conf >= 0.4) → Accept + queue for DM
    - networking (conf >= 0.4) → Accept (no DM)
    - partnership (any) → Accept (no DM)
    - unknown (conf >= 0.3) → Accept + monitor
    - unknown (conf < 0.3) → Ignore (leave pending)
    - job_seeking → Dismiss (ignore)
    - spam → Ignore (or decline if INBOUND_DECLINE_SPAM=True)
    - vendor_pitch (conf >= 0.8) → Ignore (or decline)
    - vendor_pitch (conf < 0.8) → Ignore
    """
    classified = await run_db(
        list_inbound_signals, status="classified", signal_type="invitation", limit=DAILY_INBOUND_ACCEPT_LIMIT,
    )
    if not classified:
        return ""

    accepted = 0
    ignored = 0
    declined = 0
    dismissed = 0
    errors = 0

    for signal in classified:
        intent = signal.get("intent", "unknown")
        confidence = signal.get("confidence", 0) or 0
        invitation_id = signal.get("invitation_id", "")
        now = int(time.time())

        # Determine action
        action = _decide_invitation_action(intent, confidence)

        if action == "accept":
            if not invitation_id:
                # No invitation_id — can't accept, mark as accepted anyway
                # (may have been auto-accepted by LinkedIn)
                await run_db(
                    update_inbound_signal, signal["id"],
                    status="accepted", actioned_at=now,
                )
                accepted += 1
                continue

            try:
                result = await client.handle_invitation(
                    account_id, invitation_id, action="accept",
                )
                if result.get("success"):
                    await run_db(
                        update_inbound_signal, signal["id"],
                        status="accepted", actioned_at=now,
                    )
                    await run_db(
                        log_action,
                        "inbound_invitation_accepted",
                        result="success",
                        details={
                            "signal_id": signal["id"],
                            "sender_name": signal.get("sender_name", ""),
                            "intent": intent,
                            "confidence": confidence,
                        },
                    )
                    accepted += 1
                    logger.info(
                        "Accepted invitation from %s (intent=%s, conf=%.2f)",
                        signal.get("sender_name", "Unknown"), intent, confidence,
                    )
                else:
                    error = result.get("error", "unknown")
                    if "not found" in error.lower() or "already" in error.lower():
                        # Already handled — mark as accepted
                        await run_db(
                            update_inbound_signal, signal["id"],
                            status="accepted", actioned_at=now,
                        )
                        accepted += 1
                    else:
                        errors += 1
                        logger.warning("Failed to accept invitation %s: %s", invitation_id, error)
            except Exception as e:
                errors += 1
                logger.warning("Error accepting invitation %s: %s", invitation_id, e)

        elif action == "decline":
            if invitation_id:
                try:
                    result = await client.handle_invitation(
                        account_id, invitation_id, action="decline",
                    )
                    if result.get("success"):
                        await run_db(
                            update_inbound_signal, signal["id"],
                            status="declined",
                            actioned_at=now,
                            decline_reason=f"Auto-declined: {intent} (conf={confidence:.2f})",
                        )
                        await run_db(
                            log_action,
                            "inbound_invitation_declined",
                            result="success",
                            details={
                                "signal_id": signal["id"],
                                "sender_name": signal.get("sender_name", ""),
                                "intent": intent,
                            },
                        )
                        declined += 1
                    else:
                        # Decline failed — fall back to ignore
                        await run_db(
                            update_inbound_signal, signal["id"],
                            status="ignored", actioned_at=now,
                        )
                        ignored += 1
                except Exception:
                    await run_db(
                        update_inbound_signal, signal["id"],
                        status="ignored", actioned_at=now,
                    )
                    ignored += 1
            else:
                await run_db(
                    update_inbound_signal, signal["id"],
                    status="dismissed", actioned_at=now,
                )
                dismissed += 1

        elif action == "dismiss":
            await run_db(
                update_inbound_signal, signal["id"],
                status="dismissed", actioned_at=now,
            )
            dismissed += 1

        else:  # ignore
            await run_db(
                update_inbound_signal, signal["id"],
                status="ignored", actioned_at=now,
            )
            ignored += 1

    parts = []
    if accepted:
        parts.append(f"{accepted} accepted")
    if ignored:
        parts.append(f"{ignored} ignored")
    if declined:
        parts.append(f"{declined} declined")
    if dismissed:
        parts.append(f"{dismissed} dismissed")
    if errors:
        parts.append(f"{errors} errors")
    return ", ".join(parts)


def _decide_invitation_action(intent: str, confidence: float) -> str:
    """Return 'accept', 'ignore', 'decline', or 'dismiss' for an invitation."""
    if intent in ("spam",):
        return "decline" if INBOUND_DECLINE_SPAM else "dismiss"

    if intent in ("vendor_pitch",):
        return "accept"  # Accept vendors — we counter-pitch our product

    if intent in ("job_seeking",):
        return "dismiss"

    if intent in ("buying_signal",):
        return "accept" if confidence >= INBOUND_ACCEPT_CONFIDENCE else "ignore"

    if intent in ("partnership",):
        return "accept"

    if intent in ("networking",):
        return "accept" if confidence >= INBOUND_ACCEPT_CONFIDENCE else "ignore"

    # unknown
    return "accept" if confidence >= 0.3 else "ignore"


# ── ACT: Messages ───────────────────────────────────────────────────────────

async def _act_on_messages(client: Any, account_id: str) -> str:
    """Send discovery DMs and/or reactions for classified inbound signals.

    Handles both invitation-based signals (after acceptance) and
    unsolicited DM signals.

    - High confidence (>= 0.7): send discovery DM immediately
    - Medium confidence (0.4-0.7): send reaction first, then discovery DM
    - Off-ICP vendor/partnership pitches: dismissed (see decide_inbound_reply)
    - Vendor pitch with an ICP match: counter-pitch
    """
    # Process accepted invitations that need DMs
    accepted_inv = await run_db(
        list_inbound_signals, status="accepted", limit=20,
    )
    # Process classified messages (unsolicited DMs)
    classified_msg = await run_db(
        list_inbound_signals, status="classified", signal_type="message", limit=20,
    )

    all_signals = (accepted_inv or []) + (classified_msg or [])
    if not all_signals:
        return ""

    voice = await run_db(get_setting, "voice_signature", {})

    sent = 0
    skipped = 0
    errors = 0

    for signal in all_signals:
        confidence = signal.get("confidence", 0) or 0
        sender_id = signal.get("sender_id", "")

        from .inbound_relationship import persist_hold, resolve_inbound_relationship
        from .inbound_policy import decide_inbound_reply

        rel = await run_db(resolve_inbound_relationship, signal)
        if rel.action == "hold":
            await run_db(persist_hold, signal["id"], rel)
            skipped += 1
            continue
        if rel.action == "continue" and rel.campaign_id:
            signal = dict(signal)
            signal["campaign_id"] = rel.campaign_id
            signal["_relationship_continue"] = True

        if decide_inbound_reply(signal) == "dismiss":
            await run_db(update_inbound_signal, signal["id"], status="dismissed")
            skipped += 1
            continue

        if not sender_id:
            skipped += 1
            continue

        # Skip if already a known campaign contact (re-activation handled in _detect_messages)
        # Check both linkedin_id (public_id format) AND provider_id (ACoAAA format)
        # since sender_id from Unipile is provider_id but contacts.linkedin_id
        # stores public_id — format mismatch caused dedup misses.
        existing_contact = await run_db(find_best_inbox_match, sender_id)
        if not existing_contact:
            sender_name = signal.get("sender_name", "")
            sender_company = signal.get("sender_company", "")
            if sender_name and sender_company:
                existing_contact = await run_db(
                    get_contact_by_name_company, sender_name, sender_company,
                )
        if existing_contact:
            from .live_thread_enroll import enroll_matching_live_thread

            await run_db(
                enroll_matching_live_thread,
                sender_id=sender_id,
                name=signal.get("sender_name") or existing_contact.get("name") or "",
                text=signal.get("content") or "",
                headline=signal.get("sender_headline") or existing_contact.get("title") or "",
                company=signal.get("sender_company") or existing_contact.get("company") or "",
                message_id=signal.get("message_id") or "",
                timestamp=signal_sent_at(signal),
            )
            skipped += 1
            continue

        # NOTE: Prior conversation guard removed (v0.10.48).
        # Warm inbound leads from existing connections are the most valuable
        # signals — dismissing them because old messages exist was wrong.
        # Off-ICP vendor/partnership pitches are dismissed by decide_inbound_reply.

        # For medium confidence DMs, send a reaction first
        message_id = signal.get("message_id", "")
        if message_id and 0.4 <= confidence < 0.7:
            try:
                react_result = await client.add_message_reaction(
                    account_id, message_id,
                )
                if react_result.get("success"):
                    await run_db(
                        update_inbound_signal, signal["id"], reaction_sent=1,
                    )
            except Exception as e:
                logger.debug("Reaction failed for message %s: %s (continuing)", message_id, e)

        # Generate and send discovery DM
        result = await _send_discovery_dm(
            client, account_id, signal, voice,
        )
        if result == "sent":
            sent += 1
        elif result == "capped":
            skipped += 1
            break
        elif result == "rate_limited":
            errors += 1
            break
        elif result == "error":
            errors += 1
        else:
            skipped += 1

    parts = []
    if sent:
        parts.append(f"{sent} DMs sent")
    if skipped:
        parts.append(f"{skipped} skipped")
    if errors:
        parts.append(f"{errors} failed")
    return ", ".join(parts)


async def _send_discovery_dm(
    client: Any,
    account_id: str,
    signal: dict[str, Any],
    voice: dict[str, Any],
) -> str:
    """Generate and send a contextual reply to an inbound signal.

    Instead of a generic discovery question, generates a reply that
    addresses what the person actually said using the user's voice.

    Returns 'sent', 'skipped', 'capped', 'rate_limited', or 'error'.
    """
    from ..linkedin.rate_limiter import check_daily_cap

    # Peek so a spent cap does not create a contact or burn an LLM call
    # for a message we will not send. The real booking is immediately
    # before the LinkedIn call below.
    can, _current, _cap, _ = await check_daily_cap("dm", reserve=False)
    if not can:
        return "capped"

    if (signal.get("status") or "") == "held":
        return "skipped"

    from .inbound_relationship import (
        persist_hold,
        resolve_inbound_relationship,
        stamp_matched_identity,
    )

    rel = await run_db(resolve_inbound_relationship, signal)
    if rel.action == "hold":
        await run_db(persist_hold, signal.get("id") or "", rel)
        return "skipped"
    if rel.action == "continue" and rel.campaign_id:
        signal = dict(signal)
        signal["campaign_id"] = rel.campaign_id
        signal["_relationship_continue"] = True

    # Backstop: a signal already tied to a job-search campaign is never answered
    # here, whatever the relationship resolver decided.
    _js_campaign_id = signal.get("campaign_id") or ""
    if _js_campaign_id:
        from ..db.queries import get_campaign as _get_campaign
        from .inbound_relationship import Relationship
        from .job_search_guard import is_job_search_config_json

        _js_camp = await run_db(_get_campaign, _js_campaign_id) or {}
        if is_job_search_config_json(_js_camp.get("config_json")):
            await run_db(
                persist_hold,
                signal.get("id") or "",
                Relationship(
                    kind="job_search",
                    action="hold",
                    campaign_id=_js_campaign_id,
                    reason="job-search campaign, held for operator",
                ),
            )
            return "skipped"

    from .inbound_policy import decide_inbound_reply

    reply_decision = decide_inbound_reply(signal)
    if reply_decision == "dismiss":
        signal_id = signal.get("id") or ""
        if signal_id:
            await run_db(update_inbound_signal, signal_id, status="dismissed")
        return "skipped"

    sender_id = signal.get("sender_id", "")
    intent = signal.get("intent", "unknown")
    confidence = signal.get("confidence", 0) or 0
    content = signal.get("content", "")
    if rel.action == "continue" and rel.contact_id and sender_id:
        await run_db(stamp_matched_identity, rel.contact_id, sender_id)

    campaign_id = signal.get("campaign_id") or ""
    if campaign_id:
        from ..db.queries import get_campaign
        from .project_brief import campaign_has_project_brief
        camp = await run_db(get_campaign, campaign_id)
        if not campaign_has_project_brief(camp):
            return "skipped"

    prospect_profile = {
        "name": signal.get("sender_name", ""),
        "headline": signal.get("sender_headline", ""),
        "company": signal.get("sender_company", ""),
        "title": signal.get("sender_headline", ""),
    }

    # Resolve the chat and pull the thread BEFORE anything reads the message.
    # Until 9 Sep the chat was fetched only for the send-side dedup check —
    # after the reply had already been written from the single inbound line.
    chat_id = ""
    try:
        chat_id = await client.find_chat_for_user(account_id, sender_id) or ""
    except Exception as e:
        logger.debug("Could not resolve chat for %s: %s", sender_id, e)

    inbound_history: list[dict[str, Any]] = []
    try:
        inbound_history = await build_inbound_history(
            client, account_id, chat_id, "", content,
            timestamp=signal_sent_at(signal),
        )
    except Exception as e:
        if getattr(getattr(e, "response", None), "status_code", None) == 404:
            # The chat is gone. Generating anyway is what #450 set out to
            # stop: the reply cannot be delivered into a chat that does not
            # exist, so stop here, before the contact, the sentiment call and
            # the generation.
            return await _dismiss_for_gone_chat(signal, chat_id)
        logger.warning("Inbound history assembly failed: %s", e)

    # ── Step 1: Create contact + outreach BEFORE generating reply ──
    campaign_id = signal.get("campaign_id") or ""
    now = int(time.time())
    outreach_id = ""
    sentiment = "neutral"
    try:
        _sig_type = signal.get("signal_type", "invitation")
        _inbound_src = {
            "invitation": "inbound_invitation",
            "dm": "inbound_dm",
            "message": "inbound_dm",
            "comment": "inbound_comment",
        }.get(_sig_type, "inbound_invitation")

        # Classify sentiment of the inbound message
        if content:
            try:
                sentiment = await classify_sentiment(
                    content,
                    prior_turns=inbound_history[:-1],
                    message_id=str(signal.get("id") or ""),
                )
            except Exception as e:
                logger.debug("Sentiment classification failed: %s", e)

        # Skip opt-outs entirely
        if sentiment == "opt_out":
            await run_db(update_inbound_signal, signal["id"], status="dismissed")
            return "skipped"

        # Map sentiment to outreach status
        status_map = {
            "positive": "hot_lead",
            "question": "replied",
            "negative": "replied",
            "neutral": "replied",
            "out_of_office": "replied",
        }
        initial_status = status_map.get(sentiment, "replied")

        from .referral_enroll import fill_sender_name

        sender_name = await run_db(
            fill_sender_name, sender_id, signal.get("sender_name") or "",
        )
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
            source=_inbound_src,
            status=initial_status,
            signal_id=signal["id"],
        )
        if outreach_id:
            await run_db(update_outreach, outreach_id, accepted_at=now)

            # Save the inbound prospect message with sentiment
            if content:
                await run_db(
                    save_message, outreach_id,
                    role="prospect", text=content, sentiment=sentiment,
                    # When they SENT it (the signal's provider time), never
                    # when we noticed it: a backlog or a backfill is hours
                    # to years behind, and the sync push carries this stamp
                    # to the cloud as the message's time.
                    timestamp=signal_sent_at(signal),
                    external_message_id=signal.get("message_id") or None,
                )
    except Exception as e:
        from ..ops_log import record_inbound_enrol_failure

        record_inbound_enrol_failure(
            campaign_id=campaign_id,
            source=_inbound_src,
            exc=e,
        )
        # Continue anyway — try to send the reply even without tracking

    # ── Step 2: Generate contextual reply ──
    try:
        sender_profile = await run_db(get_setting, "profile", {})

        # For ICP-matched vendor pitches, generate a counter-pitch.
        # A known thread / warm referral must stay on that campaign's reply path.
        if reply_decision == "counter_pitch" and content:
            from ..ai.inbound_qualifier import generate_counter_pitch
            # Pull campaign context for counter-pitch
            _cp_ctx: dict[str, Any] = {"target_description": "", "relevance_hook": "", "booking_link": ""}
            _cp_campaign_id = signal.get("campaign_id", "")
            if _cp_campaign_id:
                try:
                    from ..db.queries import get_campaign
                    _cp_campaign = await run_db(get_campaign, _cp_campaign_id)
                    if _cp_campaign:
                        import json as _json
                        _cp_raw = _cp_campaign.get("context_json", "")
                        _cp_parsed = {}
                        if _cp_raw:
                            try:
                                _cp_parsed = _json.loads(_cp_raw)
                            except Exception:
                                pass
                        _cp_ctx["target_description"] = _cp_campaign.get("target_description", "")
                        _cp_brief = (_cp_parsed.get("project_brief") or "").strip()
                        _cp_ctx["project_brief"] = _cp_brief
                        _cp_ctx["relevance_hook"] = (
                            _cp_brief
                            or _cp_parsed.get("offerings", "")
                            or _cp_parsed.get("company_context_raw", "")
                        )
                        _cp_ctx["booking_link"] = _cp_parsed.get("booking_link", "")
                except Exception:
                    pass
            cp_result = await generate_counter_pitch(
                sender_profile=prospect_profile,
                content=content,
                voice=voice,
                campaign_context=_cp_ctx,
                conversation_history=inbound_history,
            )
            dm_text = cp_result.get("message", "")
        else:
            dm_text = await _generate_contextual_reply(
                prospect=prospect_profile,
                sender_profile=sender_profile,
                voice_signature=voice,
                content=content,
                sentiment=sentiment if content else "neutral",
                signal=signal,
                conversation_history=inbound_history,
            )
        if not dm_text:
            return "skipped"

        # Strip any trailing email-style signatures ("- Alex", "Best, Alex").
        # Real people don't sign LinkedIn DMs — and counter-pitch / fallback
        # discovery don't go through run_reply_pipeline, so the strip wouldn't
        # otherwise be applied on those paths.
        from ..ai.reply_pipeline import strip_signature
        dm_text = strip_signature(dm_text, sender_profile)
        if not dm_text:
            return "skipped"

        # ── Step 3: Send the reply (with pre-send dedup) ──
        # chat_id was resolved above, before generation.
        if chat_id:
            # Pre-send guard: check if we already sent a message in this chat
            # recently. This is the last line of defense against duplicate
            # discovery DMs from concurrent pipeline runs or signal-type mismatches.
            try:
                recent_msgs = await client.get_chat_messages(
                    account_id, chat_id, limit=5,
                )
                our_provider_id = (await run_db(get_setting, "profile", {})).get("provider_id", "")
                if recent_msgs:
                    for rmsg in recent_msgs:
                        if message_is_ours(rmsg, our_provider_id):
                            msg_ts = rmsg.get("timestamp", 0)
                            if msg_ts and (int(time.time()) - msg_ts) < 86400:
                                logger.info(
                                    "Pre-send dedup: already sent message to %s in last 24h, skipping",
                                    signal.get("sender_name", sender_id),
                                )
                                await run_db(
                                    update_inbound_signal, signal["id"],
                                    status="engaged", actioned_at=int(time.time()),
                                )
                                return "skipped"
            except Exception as e:
                if getattr(getattr(e, "response", None), "status_code", None) == 404:
                    # The chat went away after the history read above found it,
                    # so no message of ours can be in it: nothing to dedup
                    # against. The send below reports the gone chat itself.
                    logger.info("Pre-send dedup: chat %s no longer exists (404)", chat_id)
                else:
                    logger.debug("Pre-send dedup check failed: %s (continuing)", e)

        can, _current, _cap, _ = await check_daily_cap("dm")
        if not can:
            return "capped"

        if chat_id:
            result = await client.send_message(
                account_id=account_id,
                chat_id=chat_id,
                text=dm_text,
            )
        else:
            result = await client.send_new_message(
                account_id=account_id,
                provider_id=sender_id,
                text=dm_text,
            )

        if result.get("success"):
            # Save SDR reply and update tracking
            if outreach_id:
                await run_db(save_message, outreach_id, role="sdr", text=dm_text)
                await run_db(
                    update_inbound_signal, signal["id"],
                    status="engaged",
                    outreach_id=outreach_id,
                    actioned_at=now,
                )
            else:
                # No outreach was created, so nothing downstream can see this
                # conversation. 'engaged' would make _detect_messages skip the
                # sender forever; 'ignored' still lets a new message re-detect.
                await run_db(
                    update_inbound_signal, signal["id"],
                    status="ignored", actioned_at=now,
                )

            await run_db(
                log_action,
                "inbound_contextual_reply_sent",
                result="success",
                details={
                    "signal_id": signal["id"],
                    "sender_id": sender_id,
                    "sender_name": signal.get("sender_name", ""),
                    "intent": intent,
                    "confidence": confidence,
                    "sentiment": sentiment if content else "unknown",
                    "dm_text": dm_text[:200],
                },
            )
            return "sent"

        err = str(result.get("error") or "").lower()
        if (
            result.get("blocked")
            or result.get("status_code") == 429
            or "rate limit" in err
            or "429" in err
        ):
            return "rate_limited"

        # Send failed — handle DM attempt tracking
        dm_attempts = (signal.get("dm_attempts") or 0) + 1
        if dm_attempts >= INBOUND_MAX_DM_ATTEMPTS:
            await run_db(
                update_inbound_signal, signal["id"],
                status="dismissed", dm_attempts=dm_attempts,
            )
            return "skipped"
        else:
            await run_db(
                update_inbound_signal, signal["id"],
                dm_attempts=dm_attempts,
            )
            return "skipped"

    except Exception as e:
        if getattr(getattr(e, "response", None), "status_code", None) == 404:
            # A send that raised on a gone chat. "error" leaves the signal
            # live, and the next pass would write the same reply again.
            return await _dismiss_for_gone_chat(signal, chat_id)
        logger.warning(
            "Failed to send contextual reply to %s: %s",
            signal.get("sender_name"), e,
        )
        return "error"


async def _dismiss_for_gone_chat(signal: dict[str, Any], chat_id: str) -> str:
    """Stop the inbound lane for a signal whose chat no longer exists.

    Dismissed, not left live: a live signal is offered again on every pass and
    the chat will not come back. Re-detection treats a dismissed signal as
    terminal, so a new message from the person still opens a new one.
    """
    logger.info(
        "Inbound reply not written: chat %s for %s no longer exists (404)",
        chat_id, signal.get("sender_name") or signal.get("sender_id"),
    )
    await run_db(
        update_inbound_signal, signal["id"],
        status="dismissed", actioned_at=int(time.time()),
    )
    await run_db(
        log_action,
        "inbound_chat_not_found",
        result="skipped",
        details={
            "signal_id": signal["id"],
            "sender_id": signal.get("sender_id", ""),
            "chat_id": chat_id,
        },
    )
    return "skipped"


def assemble_inbound_campaign_context(campaign: dict[str, Any] | None) -> dict[str, Any]:
    """Merge full context_json (offerings, facts, preferences) plus campaign_intent.

    Same assembly the live inbound reply path uses after loading the campaign
    row — equivalent to ``get_campaign_context`` plus the config_json intent.
    """
    out: dict[str, Any] = {
        "target_description": "",
        "relevance_hook": "",
        "booking_link": "",
    }
    if not campaign:
        return out
    ctx_raw = campaign.get("context_json", "")
    ctx: dict[str, Any] = {}
    if isinstance(ctx_raw, dict):
        ctx = ctx_raw
    elif ctx_raw:
        try:
            parsed = json.loads(ctx_raw)
            if isinstance(parsed, dict):
                ctx = parsed
        except Exception:
            ctx = {}
    out.update(ctx)
    out["target_description"] = (
        campaign.get("target_description", "") or out.get("target_description", "")
    )
    brief = str(out.get("project_brief") or "").strip()
    if not out.get("relevance_hook"):
        out["relevance_hook"] = (
            brief or out.get("offerings", "") or out.get("company_context_raw", "")
        )
    try:
        cfg = json.loads(campaign.get("config_json") or "{}")
    except Exception:
        cfg = {}
    if isinstance(cfg, dict):
        out["campaign_intent"] = cfg.get("campaign_intent", "")
    return out


async def build_inbound_history(
    client: Any,
    account_id: str,
    chat_id: str | None,
    outreach_id: str,
    content: str,
    timestamp: int = 0,
) -> list[dict[str, Any]]:
    """Assemble the full thread behind an inbound signal.

    Until 9 Sep this path built ``[{"role": "prospect", "text": content}]`` —
    a single message — even though the chat was one API call away and had
    already been resolved for the send-dedup check. Everything the person had
    said before, and everything we had said, was invisible to the prompt.
    """
    history: list[dict[str, Any]] = []
    if chat_id:
        sender_provider_id = ""
        try:
            sender_provider_id = (
                await run_db(get_setting, "profile", {})
            ).get("provider_id", "")
        except Exception:
            sender_provider_id = ""
        if sender_provider_id:
            try:
                if outreach_id:
                    from .conversation_enricher import get_enriched_conversation

                    history = await get_enriched_conversation(
                        client, account_id, chat_id, outreach_id,
                        sender_provider_id,
                    )
                else:
                    from .conversation_enricher import fetch_linkedin_history

                    history = await fetch_linkedin_history(
                        client, account_id, chat_id, sender_provider_id,
                    )
            except Exception as e:
                if getattr(getattr(e, "response", None), "status_code", None) == 404:
                    # A gone chat is not "no history": the caller must not
                    # answer from the signal alone into a chat that is gone.
                    raise
                logger.warning(
                    "Inbound history fetch failed, using the signal alone: %s", e,
                )
                history = []

    text = (content or "").strip()
    if text and not any(
        (m.get("text") or "").strip() == text for m in history
    ):
        history.append({
            "role": "prospect",
            "text": text,
            "timestamp": int(timestamp or 0),
        })
    return history


async def _generate_contextual_reply(
    prospect: dict[str, Any],
    sender_profile: dict[str, Any],
    voice_signature: dict[str, Any],
    content: str,
    sentiment: str,
    signal: dict[str, Any],
    conversation_history: list[dict[str, Any]] | None = None,
) -> str:
    """Generate a contextual reply using the shared reply pipeline.

    Falls back to generate_discovery_question() if reply generation fails.
    """
    if not content:
        return await _fallback_discovery(
            prospect, voice_signature, signal, conversation_history,
        )

    if not conversation_history:
        conversation_history = [{"role": "prospect", "text": content}]

    # Pull campaign context so replies can reference the user's product/company
    campaign_context: dict[str, Any] = {"target_description": "", "relevance_hook": "", "booking_link": ""}
    campaign_id = signal.get("campaign_id", "")
    if campaign_id:
        try:
            from ..db.queries import get_campaign
            campaign = await run_db(get_campaign, campaign_id)
            if campaign:
                campaign_context = assemble_inbound_campaign_context(campaign)
        except Exception:
            pass

    try:
        dm_text, _, _ = await run_reply_pipeline(
            prospect=prospect,
            sender_profile=sender_profile,
            voice_signature=voice_signature,
            campaign_context=campaign_context,
            conversation_history=conversation_history,
            reply_text=content,
            sentiment=sentiment,
            max_chars=500,
        )
        if not dm_text:
            return await _fallback_discovery(
                prospect, voice_signature, signal, conversation_history,
            )
        return dm_text

    except Exception as e:
        logger.warning("Contextual reply generation failed: %s — falling back to discovery", e)
        return await _fallback_discovery(
            prospect, voice_signature, signal, conversation_history,
        )


async def _fallback_discovery(
    prospect: dict[str, Any],
    voice: dict[str, Any],
    signal: dict[str, Any],
    conversation_history: list[dict[str, Any]] | None = None,
) -> str:
    """Fall back to generic discovery question when contextual reply fails."""
    qual = InboundQualification(
        intent=signal.get("intent", "unknown"),
        matched_icp_id=signal.get("matched_icp_id"),
        confidence=signal.get("confidence", 0) or 0,
        recommended_action=signal.get("recommended_action", "ask_purpose"),
        reasoning=signal.get("reasoning", ""),
    )
    try:
        result = await generate_discovery_question(
            sender_profile=prospect,
            signal_type=signal.get("signal_type", "invitation"),
            content=signal.get("content", ""),
            voice=voice,
            qualification=qual,
            conversation_history=conversation_history,
        )
        return result.get("message", "")
    except Exception as e:
        logger.warning("Discovery question fallback also failed: %s", e)
        return ""


# ── Helpers ──────────────────────────────────────────────────────────────────

# Re-export shared helpers for backward compatibility (used internally and by backfill_inbox)
from .inbound_helpers import has_prior_conversation as _has_prior_conversation  # noqa: E402,F811
from .inbound_helpers import resolve_campaign_for_signal as _resolve_campaign_for_signal  # noqa: E402,F811
