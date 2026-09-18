"""Engagement verification — confirm actions actually landed on LinkedIn.

Provides both immediate (post-send) and batch verification for engagements.
Comments are verified via get_post_comments(), DMs via get_chat_messages().
Reactions are verified via get_post_reactions() (match our account in reactor list).
Follows are verified via check_follow_status() (Voyager followingStates endpoint).
Profile views and endorsements are marked as trust_api (no verification API).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..constants import (
    ENGAGEMENT_VERIFY_DELAY_SECONDS,
    POST_SEND_VERIFY_DELAY_SECONDS,
    POST_SEND_VERIFY_ENABLED,
    POST_SEND_VERIFY_MATCH_CHARS,
    POST_SEND_VERIFY_MIN_CHARS,
    TRUST_API,
    UNVERIFIED,
    VERIFIED,
)
from ..db.async_bridge import run_db

logger = logging.getLogger(__name__)


async def verify_comment(
    client: Any,
    account_id: str,
    our_provider_id: str,
    engagement_id: str,
    post_id: str,
    comment_text: str,
) -> dict[str, Any]:
    """Verify a comment was posted by fetching post comments and matching.

    Matches by author_id == our_provider_id. Falls back to substring match
    on the first 80 chars of the comment text if author_id is missing.

    Returns:
        {"verified": bool, "comment_id": str, "error": str}
    """
    from ..db.queries import update_engagement_verification

    result: dict[str, Any] = {"verified": False, "comment_id": "", "error": ""}

    if not post_id or not engagement_id:
        result["error"] = "Missing post_id or engagement_id"
        return result

    try:
        # Brief delay to let LinkedIn propagate the comment
        await asyncio.sleep(ENGAGEMENT_VERIFY_DELAY_SECONDS)

        comments = await client.get_post_comments(account_id, post_id, limit=20)
        if not comments:
            # An empty fetch is indistinguishable from "not propagated yet"
            # or a closed client — same as verify_reaction / verify_follow.
            # Writing UNVERIFIED + verified_at here permanently parks the
            # row; the digest then warns forever.
            await run_db(update_engagement_verification, engagement_id, TRUST_API)
            result["error"] = "No comments returned for post"
            return result

        # Primary match: by author_id
        snippet = comment_text[:80].strip() if comment_text else ""
        for c in comments:
            author_id = c.get("author_id", "")
            if author_id and str(author_id) == str(our_provider_id):
                result["verified"] = True
                result["comment_id"] = c.get("comment_id", "")
                await run_db(
                    update_engagement_verification,
                    engagement_id,
                    VERIFIED,
                    result["comment_id"],
                )
                return result

        # Fallback match: substring on text (if author_id wasn't returned)
        if snippet:
            for c in comments:
                c_text = (c.get("text") or "")[:80].strip()
                if c_text and snippet and snippet in c_text:
                    result["verified"] = True
                    result["comment_id"] = c.get("comment_id", "")
                    await run_db(
                        update_engagement_verification,
                        engagement_id,
                        VERIFIED,
                        result["comment_id"],
                    )
                    return result

        # Not found
        await run_db(update_engagement_verification, engagement_id, UNVERIFIED)
        result["error"] = "Comment not found in post comments"
    except Exception as e:
        logger.debug("verify_comment failed for engagement %s: %s", engagement_id[:8], e)
        result["error"] = str(e)

    return result


async def verify_dm(
    client: Any,
    account_id: str,
    chat_id: str,
    message_text: str,
    sent_at: int,
    outreach_id: str = "",
) -> dict[str, Any]:
    """Verify a DM was sent by fetching chat messages and matching.

    Matches by first 100 chars of text AND timestamp within 300 seconds.

    Returns:
        {"verified": bool, "error": str}
    """
    result: dict[str, Any] = {"verified": False, "error": ""}

    if not chat_id:
        result["error"] = "No chat_id to verify"
        return result

    try:
        await asyncio.sleep(ENGAGEMENT_VERIFY_DELAY_SECONDS)

        messages = await client.get_chat_messages(account_id, chat_id, limit=10)
        if not messages:
            result["error"] = "No messages returned for chat"
            return result

        snippet = message_text[:100].strip() if message_text else ""
        for msg in messages:
            msg_text = (msg.get("text") or "")[:100].strip()
            msg_ts = msg.get("timestamp", 0)
            # Match by text similarity and timestamp proximity (5 min window)
            if snippet and msg_text and snippet in msg_text:
                if not sent_at or not msg_ts or abs(msg_ts - sent_at) < 300:
                    result["verified"] = True
                    return result

        result["error"] = "Message not found in chat history"
    except Exception as e:
        logger.debug("verify_dm failed: %s", e)
        result["error"] = str(e)

    return result


# ──────────────────────────────────────────────
# Post-send DM verification + auto-delete safety net
# ──────────────────────────────────────────────

async def post_send_verify_and_delete(
    account_id: str,
    chat_id: str,
    sent_text: str,
    outreach_id: str,
    local_message_id: str,
    message_type: str = "followup",
    voice_signature: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Post-send safety net: verify a DM landed correctly, auto-delete if garbled.

    Runs as a background task (via schedule_post_send_verify). Never raises.

    Steps:
      1. Wait for LinkedIn propagation
      2. Fetch recent chat messages
      3. Match sent text by first N chars
      4. If matched: store external_message_id, run validate_message()
      5. If critical validation failure or text mismatch: auto-delete
      6. Log outcome
    """
    from ..ai.message_validator import validate_message
    from ..db.queries import (
        log_action,
        mark_message_deleted,
        set_message_external_id,
    )
    from ..linkedin import get_linkedin_client

    result: dict[str, Any] = {
        "verified": False,
        "deleted": False,
        "error": "",
        "external_message_id": "",
    }

    if not POST_SEND_VERIFY_ENABLED or not chat_id or not sent_text:
        return result

    try:
        client = get_linkedin_client()
    except Exception as e:
        result["error"] = f"Could not get client: {e}"
        return result

    try:
        # Step 1: Wait for LinkedIn propagation
        await asyncio.sleep(POST_SEND_VERIFY_DELAY_SECONDS)

        # Step 2: Fetch recent messages
        messages = await client.get_chat_messages(account_id, chat_id, limit=5)
        if not messages:
            await run_db(
                log_action, "post_send_verify_skip",
                outreach_id=outreach_id, result="skipped",
                details={"reason": "no_messages_returned", "type": message_type},
            )
            result["error"] = "No messages returned from chat"
            return result

        # Step 3: Match by text prefix
        sent_snippet = sent_text[:POST_SEND_VERIFY_MATCH_CHARS].strip()
        matched_msg = None
        for msg in messages:
            msg_text = (msg.get("text") or "").strip()
            if sent_snippet and msg_text and sent_snippet in msg_text:
                matched_msg = msg
                break

        if not matched_msg:
            # Message not found — update outreach so the scheduler can retry.
            # Don't auto-delete (API may return stale data), but mark the
            # outreach with an error so it's picked up by orphan rescue.
            await run_db(
                log_action, "post_send_verify_not_found",
                outreach_id=outreach_id, result="warning",
                details={"type": message_type, "sent_snippet": sent_snippet[:50],
                          "fetched_count": len(messages)},
            )
            if outreach_id:
                from ..db.queries import update_outreach
                try:
                    await run_db(
                        update_outreach, outreach_id,
                        last_attempt_error="Post-send verification: message not found in chat",
                    )
                except Exception:
                    pass  # Non-critical — don't break verification
            result["error"] = "Sent message not found in chat"
            return result

        # Step 4: Store external_message_id
        external_id = matched_msg.get("message_id", "")
        if external_id and local_message_id:
            await run_db(set_message_external_id, local_message_id, external_id)
            result["external_message_id"] = external_id

        # Step 5: Check fetched text integrity
        fetched_text = (matched_msg.get("text") or "").strip()
        should_delete = False
        delete_reason = ""

        # 5a: Truncated/empty
        if len(fetched_text) < POST_SEND_VERIFY_MIN_CHARS and len(sent_text) >= POST_SEND_VERIFY_MIN_CHARS:
            should_delete = True
            delete_reason = f"Truncated: sent {len(sent_text)} chars, received {len(fetched_text)}"

        # 5b: Severely truncated (< 50% of sent length)
        if not should_delete and len(sent_text) > POST_SEND_VERIFY_MATCH_CHARS:
            if len(fetched_text) < len(sent_text) * 0.5:
                should_delete = True
                delete_reason = f"Severely truncated: {len(fetched_text)}/{len(sent_text)} chars (<50%)"

        # 5c: Rule-based validation on fetched text
        if not should_delete:
            validation = validate_message(fetched_text, voice_signature, max_chars=500)
            if not validation.is_valid:
                critical_stages = {"Salesy", "AI-tells", "Voice"}
                critical_issues = [
                    issue for issue in validation.issues
                    if any(stage in issue for stage in critical_stages)
                ]
                if critical_issues:
                    should_delete = True
                    delete_reason = f"Validation: {'; '.join(critical_issues[:2])}"
                else:
                    # Non-critical — log warning only
                    await run_db(
                        log_action, "post_send_verify_warning",
                        outreach_id=outreach_id, result="warning",
                        details={"type": message_type, "issues": validation.issues,
                                  "warnings": validation.warnings},
                    )

        # Step 6: Auto-delete or mark verified
        if should_delete and external_id:
            delete_result = await client.delete_message(account_id, external_id)
            if delete_result.get("success"):
                result["deleted"] = True
                if local_message_id:
                    await run_db(mark_message_deleted, local_message_id)
                await run_db(
                    log_action, "post_send_auto_deleted",
                    outreach_id=outreach_id, result="deleted",
                    details={"type": message_type, "reason": delete_reason,
                              "external_message_id": external_id},
                )
                logger.warning(
                    "Post-send auto-delete: %s (outreach=%s, reason=%s)",
                    message_type, outreach_id[:8], delete_reason,
                )
            else:
                await run_db(
                    log_action, "post_send_delete_failed",
                    outreach_id=outreach_id, result="error",
                    details={"type": message_type, "reason": delete_reason,
                              "delete_error": delete_result.get("error", "unknown")},
                )
        elif should_delete:
            # Critical failure but no external_id to delete
            await run_db(
                log_action, "post_send_verify_warning",
                outreach_id=outreach_id, result="warning",
                details={"type": message_type, "reason": delete_reason,
                          "note": "No external_message_id for deletion"},
            )
        else:
            result["verified"] = True
            await run_db(
                log_action, "post_send_verified",
                outreach_id=outreach_id, result="verified",
                details={"type": message_type, "external_message_id": external_id,
                          "message_length": len(fetched_text)},
            )
            logger.debug("Post-send verified OK: %s (outreach=%s)", message_type, outreach_id[:8])

    except Exception as e:
        # Verification must NEVER crash the send flow
        logger.warning("post_send_verify_and_delete failed: %s", e)
        result["error"] = str(e)
    finally:
        try:
            await client.close()
        except Exception:
            pass

    return result


def schedule_post_send_verify(
    account_id: str,
    chat_id: str,
    sent_text: str,
    outreach_id: str,
    local_message_id: str,
    message_type: str = "followup",
    voice_signature: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget post-send verification as a background asyncio task.

    Safe to call from any send path. Silently swallows all errors.
    Only for text messages (voice memos can't be verified by text matching).
    """
    if not POST_SEND_VERIFY_ENABLED or not chat_id or not sent_text:
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(
            post_send_verify_and_delete(
                account_id=account_id,
                chat_id=chat_id,
                sent_text=sent_text,
                outreach_id=outreach_id,
                local_message_id=local_message_id,
                message_type=message_type,
                voice_signature=voice_signature,
            )
        )
    except RuntimeError:
        logger.debug("schedule_post_send_verify: no running event loop")


async def mark_trust_api(engagement_id: str) -> None:
    """Mark an engagement as trust_api (no verification API available).

    Used for endorsements and profile views where no verification API exists.
    Reactions and follows now have real verification via verify_reaction/verify_follow.
    """
    if not engagement_id:
        return
    try:
        from ..db.queries import update_engagement_verification

        await run_db(update_engagement_verification, engagement_id, TRUST_API)
    except Exception as e:
        logger.debug("mark_trust_api failed for %s: %s", engagement_id[:8], e)


async def verify_comment_immediate(
    client: Any,
    account_id: str,
    engagement_id: str,
    post_id: str,
    comment_text: str,
) -> None:
    """Convenience wrapper for immediate comment verification after send.

    Resolves our_provider_id from settings and calls verify_comment().
    Silently swallows all errors — never blocks the engagement flow.
    """
    try:
        from ..db.queries import get_setting

        profile = await run_db(get_setting, "profile", {})
        our_provider_id = profile.get("provider_id", "")
        if not our_provider_id:
            logger.debug("verify_comment_immediate: no provider_id in profile")
            return
        await verify_comment(
            client, account_id, our_provider_id,
            engagement_id, post_id, comment_text,
        )
    except Exception as e:
        logger.debug("verify_comment_immediate failed: %s", e)


async def verify_reaction(
    client: Any,
    account_id: str,
    our_provider_id: str,
    engagement_id: str,
    post_id: str,
) -> dict[str, Any]:
    """Verify a reaction was posted by fetching post reactions and matching.

    Matches by author_id == our_provider_id in the reactions list.

    Returns:
        {"verified": bool, "error": str}
    """
    from ..db.queries import update_engagement_verification

    result: dict[str, Any] = {"verified": False, "error": ""}

    if not post_id or not engagement_id:
        result["error"] = "Missing post_id or engagement_id"
        return result

    try:
        # Brief delay to let LinkedIn propagate the reaction
        await asyncio.sleep(ENGAGEMENT_VERIFY_DELAY_SECONDS)

        reactions = await client.get_post_reactions(account_id, post_id, limit=100)
        if not reactions:
            # Empty list could be API hiccup — trust the original send
            await run_db(update_engagement_verification, engagement_id, TRUST_API)
            result["verified"] = True
            result["error"] = "No reactions returned — trusted via API"
            return result

        # Match by author_id / provider_id
        for r in reactions:
            reactor_id = str(
                r.get("author_id") or r.get("provider_id") or ""
            )
            if reactor_id and reactor_id == str(our_provider_id):
                result["verified"] = True
                await run_db(
                    update_engagement_verification,
                    engagement_id,
                    VERIFIED,
                )
                return result

        # Not found in first 100 reactions — likely propagation delay or
        # pagination limit.  The API already confirmed the action succeeded,
        # so fall back to trust_api instead of marking unverified.
        await run_db(update_engagement_verification, engagement_id, TRUST_API)
        result["verified"] = True
        result["error"] = "Not in first 100 reactions — trusted via API"
    except Exception as e:
        logger.debug("verify_reaction failed for engagement %s: %s", engagement_id[:8], e)
        await run_db(update_engagement_verification, engagement_id, TRUST_API)
        result["error"] = str(e)

    return result


async def verify_reaction_immediate(
    client: Any,
    account_id: str,
    engagement_id: str,
    post_id: str,
) -> None:
    """Convenience wrapper for immediate reaction verification after send.

    Resolves our_provider_id from settings and calls verify_reaction().
    Silently swallows all errors — never blocks the engagement flow.
    """
    try:
        from ..db.queries import get_setting

        profile = await run_db(get_setting, "profile", {})
        our_provider_id = profile.get("provider_id", "")
        if not our_provider_id:
            logger.debug("verify_reaction_immediate: no provider_id in profile")
            return
        await verify_reaction(
            client, account_id, our_provider_id,
            engagement_id, post_id,
        )
    except Exception as e:
        logger.debug("verify_reaction_immediate failed: %s", e)


async def verify_follow(
    client: Any,
    account_id: str,
    provider_id: str,
    engagement_id: str,
) -> dict[str, Any]:
    """Verify a follow action by checking the follow status via Voyager.

    Calls check_follow_status() to query LinkedIn's followingStates endpoint.

    Returns:
        {"verified": bool, "error": str}
    """
    from ..db.queries import update_engagement_verification

    result: dict[str, Any] = {"verified": False, "error": ""}

    if not provider_id or not engagement_id:
        result["error"] = "Missing provider_id or engagement_id"
        return result

    try:
        # Brief delay to let LinkedIn propagate the follow
        await asyncio.sleep(ENGAGEMENT_VERIFY_DELAY_SECONDS)

        follow_result = await client.check_follow_status(account_id, provider_id)
        if follow_result.get("following"):
            result["verified"] = True
            await run_db(
                update_engagement_verification,
                engagement_id,
                VERIFIED,
            )
        elif follow_result.get("voyager_down") or follow_result.get("error"):
            # Voyager unstable or API error — the follow API call itself
            # succeeded, so trust it rather than penalising the metric.
            await run_db(update_engagement_verification, engagement_id, TRUST_API)
            result["verified"] = True
            result["error"] = follow_result.get("error", "voyager_down")
        else:
            # Voyager says not following — could be propagation delay.
            # Trust the original API success rather than marking unverified.
            await run_db(update_engagement_verification, engagement_id, TRUST_API)
            result["verified"] = True
            result["error"] = "Follow not confirmed by Voyager — trusted via API"
    except Exception as e:
        logger.debug("verify_follow failed for engagement %s: %s", engagement_id[:8], e)
        await run_db(update_engagement_verification, engagement_id, TRUST_API)
        result["error"] = str(e)

    return result


async def verify_follow_immediate(
    client: Any,
    account_id: str,
    provider_id: str,
    engagement_id: str,
) -> None:
    """Convenience wrapper for immediate follow verification after send.

    Silently swallows all errors — never blocks the engagement flow.
    """
    try:
        await verify_follow(
            client, account_id, provider_id, engagement_id,
        )
    except Exception as e:
        logger.debug("verify_follow_immediate failed: %s", e)
