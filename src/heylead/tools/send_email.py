"""Tool: send_email — send a message via a connected Unipile mailbox.

The only supported outbound email path. Do not use Mail.app or osascript.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def run_send_email(
    to: str = "",
    subject: str = "",
    body: str = "",
    to_name: str = "",
) -> str:
    """Send an email through the connected Unipile mailbox.

    Args:
        to: Recipient address.
        subject: Subject line.
        body: Plain-text body. The mailbox renders paragraphs; pass HTML
            through the client as body_html, not here.
        to_name: Optional display name.
    """
    to = (to or "").strip()
    subject = (subject or "").strip()
    body = (body or "").strip()
    if not to or not subject or not body:
        return "Error: to, subject, and body are required."

    from ..db.async_bridge import run_db
    from ..db.global_contact_queries import is_excluded_by_email

    # Exclusion lives on the global contact, and this tool is handed only an
    # address — so without an explicit lookup a 'do-not-automate' tag was
    # honoured on LinkedIn and silently ignored here. An unknown address is not
    # excluded; the user named it deliberately.
    if await run_db(is_excluded_by_email, to):
        from ..ops_log import message_hash, record_channel_skip
        await record_channel_skip(
            "email_skipped",
            skip_reason="excluded",
            details={"to_hash": message_hash(to)},
        )
        return (
            f"⏭️ Skipped {to} — excluded from automation.\n\n"
            "This contact has the 'do-not-automate' tag or 'do_not_contact' "
            "lifecycle. Remove it with "
            "contacts(action='tag', tag='-do-not-automate') to re-enable."
        )

    # The same daily ceiling the campaign email path obeys. Without it this
    # tool was an unbounded send loop an agent could be talked into.
    from ..linkedin.rate_limiter import can_send_email_now

    allowed, why = await can_send_email_now()
    if not allowed:
        from ..ops_log import message_hash, record_channel_skip
        await record_channel_skip(
            "email_skipped",
            skip_reason="daily_cap",
            details={"to_hash": message_hash(to), "why": why},
        )
        return f"⏸️ Not sending to {to} — {why}. Try again tomorrow."

    from ..services.unipile_email import send_unipile_email

    result = await send_unipile_email(
        to_email=to,
        subject=subject,
        body=body,
        to_name=to_name.strip(),
    )
    if result.get("success"):
        email_id = result.get("email_id") or ""
        # Record it. An unlogged send is invisible to analytics *and* weakens
        # every later cap check, because the cap reads this very counter.
        from ..db.queries import log_action
        from ..linkedin.rate_limiter import increment_email_sent

        try:
            await increment_email_sent()
            await run_db(
                log_action, "email_sent", result="success",
                details={"to": to, "subject": subject, "email_id": email_id,
                         "source": "send_email_tool"},
            )
        except Exception as e:  # recording must never lose a delivered send
            logger.warning("send_email: could not record the send to %s: %s", to, e)
        suffix = f" (id {email_id})" if email_id else ""
        return f"Sent via Unipile to {to}{suffix}"

    err = result.get("error") or "send failed"
    url = result.get("connect_url") or ""
    msg = f"Could not send via Unipile: {err}"
    if url:
        msg += f"\n\nConnect a mailbox: {url}"
    return msg
