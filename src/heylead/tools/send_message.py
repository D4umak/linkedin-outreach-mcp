"""Tool: send_message — Send follow-ups, replies, or InMail.

Thin dispatcher that routes to existing run_* functions based on the action parameter.
"""

from __future__ import annotations

import logging

from .. import hosted_writes
from ..db.async_bridge import run_db
from ..db.queries import get_outreach_with_contact
from ..linkedin import get_linkedin_client

logger = logging.getLogger(__name__)


async def _send_in_cloud(outreach_id: str, text: str) -> str:
    """reply/followup for a hosted account: POST /inbox/reply in the outreach's chat.

    The same route and sentences as answer_inbox (heylead-api #2318). Until
    17 Oct 2026 this answered HOSTED_SEND_BLOCKED, which pointed at a
    dashboard send that does not exist (#3279). The words are the user's: the
    laptop drafts nothing. Without a chat id it does not fall back to the
    contact's name, because a name can match another conversation; it names
    the call that does.
    """
    from .inbox import reply_in_cloud

    if not (text or "").strip():
        return hosted_writes.SEND_MESSAGE_NEEDS_TEXT
    row = await run_db(get_outreach_with_contact, outreach_id) if outreach_id else None
    if not row:
        return hosted_writes.send_message_no_outreach(outreach_id)
    chat_id = str(row.get("chat_id") or "").strip()
    name = str(row.get("name") or "").strip()
    if not chat_id:
        return hosted_writes.send_message_no_chat(name)
    out = await reply_in_cloud(get_linkedin_client(), chat_id, "", text)
    if out == hosted_writes.reply_sent("") and name:
        # The api names the recipient only for a reply found by name.
        from ..textutil import first_name

        return hosted_writes.reply_sent(first_name(name))
    return out


async def run_send_message(
    action: str = "followup",
    campaign_id: str = "",
    outreach_id: str = "",
    format: str = "text",
    text: str = "",
) -> str:
    """Send a message to a prospect.

    Actions:
      followup — Send a follow-up DM after connection accepted
      reply    — Reply to a prospect who has messaged you
      delete   — Delete a recently sent message (within 60 min on LinkedIn)
      inmail   — Send an InMail to a non-connection (escalation; pending invite OK)

    Args:
        action: What to do: 'followup', 'reply', 'delete', 'inmail'. 'voice' is
            refused: voice memos are off.
        campaign_id: Which campaign to send from. Uses active campaign if empty.
        outreach_id: Specific outreach to target. Auto-picks next if empty.
        format: Always 'text'; anything else is sent as text. For followup/reply.
        text: For followup/reply on a hosted account: the message, sent as
            is by the cloud in the outreach's chat. For delete: optionally
            pass a Unipile message_id directly.
    """
    action = action.lower().strip()

    # Voice memos are off for every user (heylead-api #1527).
    if action == "voice":
        from ..constants import VOICE_MEMOS_OFF
        return VOICE_MEMOS_OFF
    format = "text"

    # 'delete' is absent on purpose: it removes a message already sent, so it
    # is a remedy rather than a send, and it never reads the sender profile.
    if action in ("followup", "reply", "inmail"):
        from .organization import refuse_if_hosted_send

        blocked = await refuse_if_hosted_send()
        if blocked and action in ("followup", "reply"):
            return await _send_in_cloud(outreach_id, text)
        if blocked:
            return blocked

    if action == "followup":
        from .send_followup import run_send_followup
        return await run_send_followup(campaign_id, outreach_id, format=format)

    if action == "reply":
        from .reply_to_prospect import run_reply_to_prospect
        return await run_reply_to_prospect(outreach_id=outreach_id, format=format)

    if action == "delete":
        from .delete_message import run_delete_message
        return await run_delete_message(campaign_id, outreach_id, message_id=text)

    if action == "inmail":
        from .send_inmail import run_send_inmail
        return await run_send_inmail(campaign_id, outreach_id)

    return f"Unknown action: '{action}'. Use 'followup', 'reply', 'delete', or 'inmail'."
