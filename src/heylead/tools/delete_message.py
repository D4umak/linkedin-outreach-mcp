"""Tool handler: delete a recently sent LinkedIn message.

Resolves the Unipile message ID from an outreach (by matching chat messages
against the local DB) or accepts a raw message_id, then calls
DELETE /api/v1/messages/{message_id}.

LinkedIn only allows deletion within 60 minutes of sending.
"""

from __future__ import annotations

import json
import logging
import time

from ..db.queries import (
    get_last_sdr_message,
    get_messages_for_outreach,
    get_outreach,
    get_setting,
    log_action,
    mark_message_deleted,
    set_message_external_id,
    update_outreach,
)
from ..linkedin import (
    UnipileError,
    get_account_id,
    get_linkedin_client,
)
from ..db.async_bridge import run_db

logger = logging.getLogger(__name__)

# LinkedIn enforces a 60-minute window for message deletion.
DELETE_WINDOW_SECONDS = 3600


async def run_delete_message(
    campaign_id: str = "",
    outreach_id: str = "",
    message_id: str = "",
) -> str:
    """Delete a recently sent LinkedIn message.

    Resolution paths:
      1. If ``message_id`` is provided, use it directly (precision path).
      2. If ``outreach_id`` is provided, resolve by fetching the chat and
         matching the last SDR message by text.

    Args:
        campaign_id: Campaign context (unused directly, kept for consistency).
        outreach_id: The outreach whose last SDR message should be deleted.
        message_id: A raw Unipile message ID to delete directly.

    Returns:
        Human-readable result string.
    """
    # ── Pre-checks ──
    setup_done = await run_db(get_setting, "setup_complete", False)
    if not setup_done:
        return "Setup required. Please run setup_profile first."

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected. Run setup_profile first."

    try:
        client = get_linkedin_client()
    except UnipileError as e:
        return str(e)

    # ── Resolve the Unipile message ID ──
    local_message: dict | None = None
    unipile_message_id: str = message_id.strip() if message_id else ""

    if unipile_message_id:
        # Precision path: raw message_id provided.
        # Try to find the matching local message for DB bookkeeping.
        if outreach_id:
            local_message = await run_db(get_last_sdr_message, outreach_id)
    elif outreach_id:
        # Convenience path: resolve from outreach.
        resolved = await _resolve_from_outreach(client, account_id, outreach_id)
        if resolved.get("error"):
            return resolved["error"]
        unipile_message_id = resolved["unipile_message_id"]
        local_message = resolved.get("local_message")
    else:
        return (
            "Please provide an outreach_id or a message_id to delete.\n\n"
            "Use `inbox(action='read', ...)` to find a specific message_id,\n"
            "or pass the outreach_id of the prospect whose last message to delete."
        )

    if not unipile_message_id:
        return "Could not resolve a Unipile message ID. Try passing the message_id directly."

    # ── Check 60-minute window (if we have local timing data) ──
    if local_message:
        sent_at = local_message.get("timestamp", 0)
        elapsed = int(time.time()) - sent_at
        if elapsed > DELETE_WINDOW_SECONDS:
            mins = elapsed // 60
            return (
                f"This message was sent {mins} minutes ago.\n"
                "LinkedIn only allows deleting messages within 60 minutes of sending."
            )

        # Check if already deleted
        if local_message.get("deleted_at"):
            return "This message was already deleted."

    # ── Execute deletion ──
    result = await client.delete_message(account_id, unipile_message_id)

    if not result.get("success"):
        error = result.get("error", "Unknown error")
        await run_db(log_action, "message_delete_failed",
            outreach_id=outreach_id,
            result="error",
            details={"message_id": unipile_message_id, "error": error},)
        return f"Failed to delete message: {error}"

    # ── Update local DB ──
    prospect_name = "the prospect"
    if local_message:
        await run_db(mark_message_deleted, local_message["id"])
        await run_db(set_message_external_id, local_message["id"], unipile_message_id)
        deleted_text = (local_message.get("text") or "")[:80]
    else:
        deleted_text = ""

    # If this was tied to an outreach, adjust followup_count
    if outreach_id:
        outreach = await run_db(get_outreach, outreach_id)
        if outreach:
            followup_count = outreach.get("followup_count", 0)
            if followup_count > 0:
                await run_db(update_outreach, outreach_id, followup_count=followup_count - 1)
            # If outreach was in 'messaged' status and we deleted the only message,
            # revert to 'connected' so it re-enters the follow-up queue.
            if outreach.get("status") == "messaged" and followup_count <= 1:
                remaining = [
                    m for m in await run_db(get_messages_for_outreach, outreach_id)
                    if m.get("role") == "sdr" and not m.get("deleted_at")
                ]
                if not remaining:
                    await run_db(update_outreach, outreach_id, status="connected")

    await run_db(log_action, "message_deleted",
        outreach_id=outreach_id,
        result="success",
        details={
            "unipile_message_id": unipile_message_id,
            "deleted_text_preview": deleted_text,
        },)

    preview = f'"{deleted_text}..."' if deleted_text else f"ID: {unipile_message_id}"
    return f"Message deleted successfully: {preview}"


async def _resolve_from_outreach(
    client,
    account_id: str,
    outreach_id: str,
) -> dict:
    """Resolve the Unipile message ID from an outreach.

    Looks up the contact, finds the chat, fetches recent messages,
    and matches the last SDR message by text.

    Returns:
        {"unipile_message_id": str, "local_message": dict} on success,
        {"error": str} on failure.
    """
    # Get the outreach and its last SDR message from local DB
    outreach = await run_db(get_outreach, outreach_id)
    if not outreach:
        return {"error": f"Outreach '{outreach_id}' not found."}

    local_message = await run_db(get_last_sdr_message, outreach_id)
    if not local_message:
        return {"error": "No sent SDR message found for this outreach."}

    # Check if we already have the external ID cached
    cached_ext_id = local_message.get("external_message_id")
    if cached_ext_id:
        return {"unipile_message_id": cached_ext_id, "local_message": local_message}

    # Resolve contact's LinkedIn ID
    contact_id = outreach.get("contact_id", "")
    if not contact_id:
        return {"error": "Outreach has no contact_id."}

    from ..db.schema import get_db

    def _get_contact_row():
        db = get_db()
        row = db.execute(
            "SELECT linkedin_id, profile_json FROM contacts WHERE id = ?",
            (contact_id,),
        ).fetchone()
        db.close()
        return row
    contact_row = await run_db(_get_contact_row)

    if not contact_row:
        return {"error": "Contact not found in database."}

    contact = dict(contact_row)
    linkedin_id = contact.get("linkedin_id", "")

    # Try to get provider_id from profile_json (more reliable for chat lookup)
    profile_json = contact.get("profile_json")
    if profile_json:
        try:
            profile = json.loads(profile_json) if isinstance(profile_json, str) else profile_json
            provider_id = profile.get("provider_id", "")
            if provider_id:
                linkedin_id = provider_id
        except (json.JSONDecodeError, TypeError):
            pass

    if not linkedin_id:
        return {"error": "No LinkedIn ID found for this contact."}

    # Find the chat
    try:
        chat_id = await client.find_chat_for_user(account_id, linkedin_id)
    except Exception as e:
        logger.error(f"Failed to find chat for {linkedin_id}: {e}")
        return {"error": f"Could not find chat: {e}"}

    if not chat_id:
        return {"error": "No LinkedIn chat found with this prospect."}

    # Fetch recent messages and match by text
    try:
        chat_messages = await client.get_chat_messages(account_id, chat_id, limit=10)
    except Exception as e:
        if getattr(getattr(e, "response", None), "status_code", None) == 404:
            return {
                "error": "This LinkedIn chat no longer exists (deleted, or the "
                "person disconnected), so there is no message to delete.",
            }
        logger.error(f"Failed to fetch chat messages: {e}")
        return {"error": f"Could not fetch chat messages: {e}"}

    local_text = (local_message.get("text") or "").strip()
    if not local_text:
        return {"error": "Local message has no text to match against."}

    # Match by text prefix (first 100 chars) — same strategy as engagement_verifier.
    # Empty remote text must not match: `"" in local_text` is True.
    # Do not cache the id here — a later failed delete would poison retries.
    match_snippet = local_text[:100]
    for msg in chat_messages:
        msg_text = (msg.get("text") or "").strip()
        if not msg_text:
            continue
        if match_snippet in msg_text or msg_text[:100] in local_text:
            mid = msg.get("message_id", "")
            if mid:
                return {"unipile_message_id": mid, "local_message": local_message}

    return {
        "error": (
            "Could not match the message in the LinkedIn conversation.\n"
            "Try passing the message_id directly (use `inbox(action='read')` to find it)."
        )
    }
