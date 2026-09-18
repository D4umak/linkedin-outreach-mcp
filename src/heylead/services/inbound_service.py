"""Inbound lead qualification pipeline service.

DEPRECATED: process_inbound_signals() and send_discovery_dms() are superseded
by the unified pipeline in inbound_pipeline.py (process_inbound_pipeline).
The scheduler calls inbound_pipeline.process_inbound_pipeline() via
_execute_process_inbound().

check_post_comments() is still active and called by the scheduler.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..db.async_bridge import run_db
from ..ai.inbound_qualifier import (
    InboundQualification,
    generate_discovery_question,
    qualify_inbound,
)
from ..constants import (
    INBOUND_ASK_PURPOSE_CONFIDENCE,
    INBOUND_ENGAGE_CONFIDENCE,
    INBOUND_MAX_DM_ATTEMPTS,
)
from ..db.queries import (
    count_inbound_dms_today,
    enroll_prospect,
    get_setting,
    list_campaigns,
    list_icps,
    list_inbound_signals,
    list_published_posts,
    log_action,
    save_inbound_signal,
    save_message,
    update_inbound_signal,
    update_outreach,
)
from ..db.signal_queries import get_contact_by_linkedin_id, get_contact_by_name_company

logger = logging.getLogger(__name__)


# Re-export shared helpers for backward compatibility
from .inbound_helpers import has_prior_conversation as _has_prior_conversation  # noqa: E402,F811
from .inbound_helpers import resolve_campaign_for_signal as _resolve_campaign_for_signal  # noqa: E402,F811


async def process_inbound_signals() -> str:
    """Process all new inbound signals through the qualification pipeline.

    Loads unqualified signals, runs LLM qualification, updates DB.
    Returns a summary string.
    """
    new_signals = await run_db(list_inbound_signals, status="new", limit=20)
    if not new_signals:
        return "No new inbound signals to qualify."

    active_icps = await run_db(list_icps, status="active")

    qualified_count = 0
    results: list[dict[str, Any]] = []

    for signal in new_signals:
        profile = {
            "name": signal.get("sender_name", ""),
            "headline": signal.get("sender_headline", ""),
            "company": signal.get("sender_company", ""),
            "title": signal.get("sender_headline", ""),
        }
        content = signal.get("content", "")
        signal_type = signal.get("signal_type", "invitation")

        try:
            our_last = ""
            try:
                from .inbound_pipeline import get_our_last_outbound_text
                our_last = await run_db(
                    get_our_last_outbound_text, signal.get("sender_id") or "",
                )
            except Exception:
                logger.debug("Could not load last outbound for inbound qualify")

            qual = await qualify_inbound(
                profile=profile,
                content=content,
                signal_type=signal_type,
                active_icps=active_icps,
                our_last_message=our_last,
            )

            # Resolve best-matching campaign for this signal
            campaign_id = await run_db(_resolve_campaign_for_signal, signal)

            await run_db(update_inbound_signal,
                signal["id"],
                intent=qual.intent,
                matched_icp_id=qual.matched_icp_id,
                confidence=qual.confidence,
                recommended_action=qual.recommended_action,
                reasoning=qual.reasoning,
                status="qualified",
                qualified_at=int(time.time()),
                **({"campaign_id": campaign_id} if campaign_id else {}),
            )
            qualified_count += 1
            results.append({
                "signal_id": signal["id"],
                "name": signal.get("sender_name", ""),
                "intent": qual.intent,
                "confidence": qual.confidence,
                "action": qual.recommended_action,
            })

        except Exception as e:
            logger.warning(
                "Failed to qualify signal %s (%s): %s",
                signal["id"], signal.get("sender_name"), e,
            )

    summary = f"Qualified {qualified_count}/{len(new_signals)} inbound signals."
    high_conf = [r for r in results if r["confidence"] >= INBOUND_ENGAGE_CONFIDENCE]
    if high_conf:
        summary += f" {len(high_conf)} high-confidence leads detected."

    return summary


def _is_sender_excluded(sender_id: str) -> bool:
    """Check if a sender is excluded from automation by linkedin_id."""
    from ..db.schema import get_db
    db = get_db()
    row = db.execute(
        """SELECT id FROM global_contacts
           WHERE linkedin_id = ?
             AND (lifecycle_stage = 'do_not_contact'
                  OR tags_json LIKE '%"do-not-automate"%')
           LIMIT 1""",
        (sender_id,),
    ).fetchone()
    db.close()
    return row is not None


async def send_discovery_dms() -> str:
    """Send discovery DMs to qualified inbound connections.

    Full autopilot — no copilot review. Sends to all qualified signals
    that haven't been engaged yet, except spam/job_seeking.

    Returns summary string.
    """
    from ..linkedin import UnipileError, get_account_id, get_linkedin_client

    qualified = await run_db(list_inbound_signals, status="qualified", limit=50)
    if not qualified:
        return "No qualified signals pending engagement."

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected."

    voice = await run_db(get_setting, "voice_signature", {})
    profile = await run_db(get_setting, "profile", {})

    try:
        client = get_linkedin_client()
    except UnipileError as e:
        return f"LinkedIn client error: {e}"

    sent = 0
    skipped = 0
    errors = 0

    try:
        from .job_search_guard import job_search_campaign_ids
        job_search_ids = await run_db(job_search_campaign_ids)
    except Exception:
        logger.warning("Job-search campaign lookup failed, discovery DMs skipped", exc_info=True)
        return "Discovery DMs skipped: job-search campaigns could not be checked."

    for signal in qualified:
        # Never DM anyone tied to a job-search campaign.
        if (signal.get("campaign_id") or "") in job_search_ids:
            await run_db(update_inbound_signal, signal["id"], status="held")
            skipped += 1
            continue

        intent = signal.get("intent", "unknown")
        confidence = signal.get("confidence", 0) or 0

        # Skip spam/recruiters/vendors — accept silently, no DM
        if intent in ("spam", "job_seeking", "vendor_pitch"):
            await run_db(update_inbound_signal, signal["id"], status="dismissed")
            skipped += 1
            continue

        sender_id = signal.get("sender_id", "")

        # Skip if already a known contact in our campaigns (linkedin_id or name+company)
        existing_contact = await run_db(get_contact_by_linkedin_id, sender_id) if sender_id else None
        if not existing_contact:
            sender_name = signal.get("sender_name", "")
            sender_company = signal.get("sender_company", "")
            if sender_name and sender_company:
                existing_contact = await run_db(get_contact_by_name_company, sender_name, sender_company)
        if existing_contact:
            logger.info(
                "Skipping discovery DM to %s — already a campaign contact",
                signal.get("sender_name", sender_id),
            )
            await run_db(update_inbound_signal, signal["id"], status="dismissed")
            skipped += 1
            continue

        # Exclusion gate — skip contacts excluded from automation
        if sender_id:
            excluded = await run_db(_is_sender_excluded, sender_id)
            if excluded:
                logger.info(
                    "Skipping discovery DM to %s — excluded from automation",
                    signal.get("sender_name", sender_id),
                )
                await run_db(update_inbound_signal, signal["id"], status="dismissed")
                skipped += 1
                continue

        # Friend / existing-conversation guard — skip if there's a
        # pre-existing chat (messages before we detected this signal)
        if sender_id:
            signal_created_at = signal.get("created_at") or int(time.time())
            is_friend = await _has_prior_conversation(
                client, account_id, sender_id, signal_created_at,
            )
            if is_friend:
                logger.info(
                    "Skipping discovery DM to %s — prior conversation detected",
                    signal.get("sender_name", sender_id),
                )
                await run_db(update_inbound_signal, signal["id"], status="dismissed")
                skipped += 1
                continue

        # Generate discovery DM
        sender_profile = {
            "name": signal.get("sender_name", ""),
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
            dm_result = await generate_discovery_question(
                sender_profile=sender_profile,
                signal_type=signal.get("signal_type", "invitation"),
                content=signal.get("content", ""),
                voice=voice,
                qualification=qual,
            )
            dm_text = dm_result.get("message", "")
            if not dm_text:
                skipped += 1
                continue

            # Find the chat/conversation with this person and send the DM
            sender_id = signal.get("sender_id", "")
            if sender_id:
                try:
                    chat_id = await client.find_chat_for_user(account_id, sender_id)
                    if chat_id:
                        result = await client.send_message(
                            account_id=account_id,
                            chat_id=chat_id,
                            text=dm_text,
                        )
                        if result.get("success"):
                            # Verify the DM was actually delivered
                            dm_verified = await client.verify_message_sent(
                                account_id, chat_id, dm_text,
                            )
                            if not dm_verified:
                                logger.warning(
                                    "Discovery DM to %s not confirmed in conversation (chat_id=%s)",
                                    signal.get("sender_name", sender_id), chat_id,
                                )
                                await run_db(log_action,
                                    "inbound_dm_delivery_failed",
                                    result="unverified",
                                    details={
                                        "signal_id": signal["id"],
                                        "sender_name": signal.get("sender_name", ""),
                                        "chat_id": chat_id,
                                    },
                                )
                                skipped += 1
                                continue

                            # Create contact + outreach so the DM is
                            # tracked in the normal funnel and replies
                            # are detected by check_replies.
                            campaign_id = signal.get("campaign_id") or ""
                            try:
                                _sig_type = signal.get("signal_type", "invitation")
                                _inbound_src = {
                                    "invitation": "inbound_invitation",
                                    "dm": "inbound_dm",
                                    "message": "inbound_dm",
                                    "comment": "inbound_comment",
                                }.get(_sig_type, "inbound_invitation")
                                _content_snip = (signal.get("content") or "")[:80]
                                _src_detail = f"{_sig_type}: {_content_snip}" if _content_snip else _sig_type
                                outreach_id = await run_db(
                                    enroll_prospect,
                                    campaign_id,
                                    {
                                        "name": signal.get("sender_name", ""),
                                        "title": signal.get("sender_headline", ""),
                                        "company": signal.get("sender_company", ""),
                                        "linkedin_url": signal.get("sender_url", ""),
                                        "linkedin_id": sender_id,
                                    },
                                    source=_inbound_src,
                                    source_detail=_src_detail,
                                    status="connected",
                                    signal_id=signal["id"],
                                )
                                if not outreach_id:
                                    continue
                                # Inbound leads are already connected — set accepted_at
                                # so follow-up timing calculates correctly.
                                await run_db(update_outreach, outreach_id, accepted_at=int(time.time()))
                                await run_db(save_message, outreach_id, role="sdr", text=dm_text)
                                await run_db(update_inbound_signal,
                                    signal["id"],
                                    status="engaged",
                                    outreach_id=outreach_id,
                                )
                            except Exception as e:
                                from ..ops_log import record_inbound_enrol_failure

                                record_inbound_enrol_failure(
                                    campaign_id=campaign_id,
                                    source=_inbound_src,
                                    exc=e,
                                )
                                # Do not mark engaged — that status is skipped
                                # by re-detection, so a failed enroll would
                                # drop every later message from this person.

                            await run_db(log_action,
                                "inbound_discovery_dm_sent",
                                result="success",
                                details={
                                    "signal_id": signal["id"],
                                    "sender_name": signal.get("sender_name", ""),
                                    "intent": intent,
                                    "confidence": confidence,
                                    "dm_text": dm_text[:200],
                                },
                            )
                            sent += 1
                            continue
                    # No chat found — increment attempts and check limit
                    dm_attempts = (signal.get("dm_attempts") or 0) + 1
                    if dm_attempts >= INBOUND_MAX_DM_ATTEMPTS:
                        await run_db(update_inbound_signal,
                            signal["id"],
                            status="dismissed",
                            dm_attempts=dm_attempts,
                        )
                        await run_db(log_action,
                            "inbound_dm_chat_not_found",
                            result="dismissed",
                            details={
                                "signal_id": signal["id"],
                                "sender_name": signal.get("sender_name", ""),
                                "dm_attempts": dm_attempts,
                            },
                        )
                        logger.info(
                            "Dismissed inbound signal %s — chat not found after %d attempts",
                            signal.get("sender_name"), dm_attempts,
                        )
                    else:
                        await run_db(update_inbound_signal, signal["id"], dm_attempts=dm_attempts)
                        logger.info(
                            "Chat not found for %s — attempt %d/%d, will retry",
                            signal.get("sender_name"), dm_attempts, INBOUND_MAX_DM_ATTEMPTS,
                        )
                    skipped += 1
                except Exception as e:
                    logger.warning("Failed to send discovery DM to %s: %s", signal.get("sender_name"), e)
                    errors += 1
            else:
                skipped += 1

        except Exception as e:
            logger.warning("Failed to generate discovery DM for %s: %s", signal.get("sender_name"), e)
            errors += 1

    await client.close()

    summary = f"Discovery DMs: {sent} sent"
    if skipped:
        summary += f", {skipped} skipped"
    if errors:
        summary += f", {errors} failed"
    return summary


async def check_post_comments() -> str:
    """Check comments on recently published posts and save new commenters as inbound signals.

    Returns summary string.
    """
    from ..constants import PUBLISHED_POST_MONITOR_DAYS
    from ..linkedin import UnipileError, get_account_id, get_linkedin_client

    posts = await run_db(list_published_posts, days=PUBLISHED_POST_MONITOR_DAYS)
    if not posts:
        return "No recent posts to monitor for comments."

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected."

    try:
        client = get_linkedin_client()
    except UnipileError as e:
        return f"LinkedIn client error: {e}"

    # Get our own profile to filter out our own comments
    our_profile = await run_db(get_setting, "profile", {})
    our_name = (our_profile.get("name") or "").lower()

    new_commenters = 0
    posts_checked = 0

    for post in posts:
        post_id = post.get("post_id", "")
        if not post_id:
            continue

        try:
            comments = await client.get_post_comments(account_id, post_id, limit=50)
            posts_checked += 1

            for comment in comments:
                author_name = comment.get("author_name", "")
                author_id = comment.get("author_id", "")
                comment_text = comment.get("text", "")

                # Skip our own comments
                if our_name and author_name.lower() == our_name:
                    continue

                # Dedup: check if this commenter already has a signal for this post
                if author_id:
                    from ..db.queries import get_inbound_signal_by_sender
                    existing = await run_db(get_inbound_signal_by_sender, author_id, "comment")
                    if existing:
                        continue

                # Save as inbound signal
                await run_db(save_inbound_signal,
                    signal_type="comment",
                    sender_name=author_name,
                    sender_id=author_id,
                    content=comment_text,
                    post_id=post_id,
                )
                new_commenters += 1

                # ── Cross-reference: check if commenter is an existing campaign contact ──
                if author_id:
                    try:
                        from ..db.signal_queries import (
                            get_contact_by_linkedin_id,
                            save_signal,
                            signal_exists,
                            upsert_signal_account,
                        )
                        from ..constants import SIGNAL_COMMENTER_MATCH, SIGNAL_TTL_COMMENTER_MATCH

                        contact = await run_db(get_contact_by_linkedin_id, author_id)
                        if contact and not await run_db(signal_exists, SIGNAL_COMMENTER_MATCH, linkedin_id=author_id, post_id=post_id):
                            import json as _json

                            await run_db(save_signal,
                                signal_type=SIGNAL_COMMENTER_MATCH,
                                source="comment_xref",
                                prospect_id=contact.get("id"),
                                prospect_name=contact.get("name", author_name),
                                prospect_title=contact.get("title", ""),
                                linkedin_id=author_id,
                                campaign_id=contact.get("campaign_id"),
                                content=comment_text,
                                post_id=post_id,
                                metadata_json=_json.dumps({
                                    "match_type": "campaign_contact",
                                    "contact_name": contact.get("name", author_name),
                                    "contact_company": contact.get("company", ""),
                                    "contact_fit_score": contact.get("fit_score", 0),
                                }),
                                expires_at=int(time.time()) + SIGNAL_TTL_COMMENTER_MATCH,
                            )
                            await run_db(upsert_signal_account,
                                linkedin_id=author_id,
                                prospect_name=contact.get("name", author_name),
                                company=contact.get("company", ""),
                            )
                            logger.info(
                                "Commenter cross-reference: %s is campaign contact (campaign=%s)",
                                author_name, contact.get("campaign_id", "")[:8],
                            )
                    except Exception as e:
                        logger.debug("Commenter cross-reference failed for %s: %s", author_name, e)

            # Update monitoring state
            from ..db.queries import update_published_post
            await run_db(update_published_post,
                post_id=post_id,
                last_checked=int(time.time()),
                comment_count=len(comments),
            )

        except Exception as e:
            logger.warning("Failed to check comments for post %s: %s", post_id, e)

    await client.close()
    return f"Checked {posts_checked} posts, found {new_commenters} new commenters."
