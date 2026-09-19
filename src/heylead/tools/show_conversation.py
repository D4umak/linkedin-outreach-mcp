"""Tool: show_conversation — View full message thread with a prospect.

Shows the complete conversation history including engagements (comments,
reactions) and DMs, with timestamps and sentiment labels.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..db.queries import (
    get_messages_for_outreach,
    get_outreach,
    get_setting,
)
from ..db.schema import get_db
from ..formatter import prospect_link, source_badge, stars
from ..db.async_bridge import run_db

logger = logging.getLogger(__name__)


def _format_timestamp(ts: int) -> str:
    """Format a Unix timestamp into a relative time string."""
    if not ts:
        return ""
    now = int(time.time())
    diff = now - ts
    if diff < 60:
        return "just now"
    elif diff < 3600:
        mins = diff // 60
        return f"{mins}m ago"
    elif diff < 86400:
        hours = diff // 3600
        return f"{hours}h ago"
    else:
        days = diff // 86400
        return f"{days}d ago"


async def run_show_conversation(outreach_id: str) -> str:
    """Show the full conversation thread with a prospect.

    Displays engagements (comments, reactions) and DM messages
    in chronological order with timestamps and sentiment.

    Args:
        outreach_id: The outreach ID to show conversation for (required).
    """

    # ── Pre-checks ──
    setup_done = await run_db(get_setting, "setup_complete", False)
    if not setup_done:
        return (
            "Setup required before viewing conversations.\n\n"
            "Please run setup_profile first."
        )

    if not outreach_id:
        return (
            "outreach_id is required.\n\n"
            "Use show_status(campaign_id='...') to find outreach IDs,\n"
            "or export_campaign() to see all prospects with IDs."
        )

    # ── Load outreach ──
    outreach = await run_db(get_outreach, outreach_id)
    if not outreach:
        return f"Outreach not found: {outreach_id}"

    # ── Load contact info + engagements ──
    def _load_contact_and_engagements():
        db = get_db()
        contact = db.execute(
            """SELECT name, title, company, fit_score, linkedin_url,
                      source, source_detail
               FROM contacts WHERE id = ?""",
            (outreach["contact_id"],),
        ).fetchone()
        engagements = db.execute(
            """SELECT action_type, post_id, post_text, text, reaction_type, created_at
               FROM engagements
               WHERE outreach_id = ?
               ORDER BY created_at ASC""",
            (outreach_id,),
        ).fetchall()
        db.close()
        return contact, engagements

    contact, engagements = await run_db(_load_contact_and_engagements)

    if not contact:
        return "Contact not found for this outreach."

    c = dict(contact)
    contact_name = c.get("name", "Unknown")
    title = c.get("title", "")
    company = c.get("company", "")
    fit_score = c.get("fit_score", 0)
    linkedin_url = c.get("linkedin_url", "")

    role_str = title
    if company:
        role_str += f" at {company}" if role_str else company

    # ── Load messages ──
    messages = await run_db(get_messages_for_outreach, outreach_id)

    # ── Build header ──
    status = outreach.get("status", "unknown")
    status_icons = {
        "pending": "Pending",
        "invited": "Invited",
        "connected": "Connected",
        "messaged": "Messaged",
        "replied": "Replied",
        "hot_lead": "Hot Lead",
        "skipped": "Skipped",
        "opted_out": "Opted Out",
        "review_pending": "Review Pending",
        "closed_happy": "Closed (Won)",
        "closed_unhappy": "Closed (Lost)",
        "error": "Error",
    }
    status_display = status_icons.get(status, status)

    output: list[str] = [
        f"**{prospect_link(contact_name, linkedin_url)}** ({role_str})",
        f"   Status: {status_display} | Fit: {stars(fit_score)}",
        f"   Source: {source_badge(c.get('source', 'search'), c.get('source_detail', ''))}",
        "",
    ]

    # ── Combine engagements + messages chronologically ──
    timeline: list[dict[str, Any]] = []

    for eng in engagements:
        e = dict(eng)
        timeline.append({
            "type": "engagement",
            "subtype": e.get("action_type", "comment"),
            "content": e.get("text", "") or e.get("post_text", ""),
            "reaction_type": e.get("reaction_type", ""),
            "timestamp": e.get("created_at", 0),
        })

    for msg in messages:
        m = dict(msg) if not isinstance(msg, dict) else msg
        timeline.append({
            "type": "message",
            "role": m.get("role", "unknown"),
            "text": m.get("text", ""),
            "sentiment": m.get("sentiment", ""),
            "read_at": m.get("read_at"),
            "timestamp": m.get("created_at", 0) or m.get("timestamp", 0),
        })

    # Sort by timestamp
    timeline.sort(key=lambda x: x.get("timestamp", 0))

    if not timeline:
        output.append("No conversation history yet.")
        output.append("")
        _add_next_action_hint(output, status)
        return "\n".join(output)

    # ── Format timeline ──
    output.append("Conversation:")

    for i, item in enumerate(timeline):
        is_last = i == len(timeline) - 1
        prefix = " " if is_last else " "
        ts_str = _format_timestamp(item.get("timestamp", 0))

        if item["type"] == "engagement":
            subtype = item.get("subtype", "comment")
            if subtype == "react":
                icon = f"like {item.get('reaction_type', 'LIKE')}"
            else:
                icon = "comment"
            content = item.get("content", "")
            if content:
                content_preview = content[:80] + "..." if len(content) > 80 else content
                output.append(f"{prefix}[{icon}] ({ts_str}) \"{content_preview}\"")
            else:
                output.append(f"{prefix}[{icon}] ({ts_str})")

        elif item["type"] == "message":
            role = item.get("role", "unknown")
            text = item.get("text", "")
            sentiment = item.get("sentiment", "")
            read_at = item.get("read_at")

            if role == "sdr":
                label = "You"
            elif role == "prospect":
                label = contact_name
            else:
                label = role

            sentiment_badge = _message_badge(role, sentiment)

            # Read receipt for SDR messages
            read_badge = ""
            if role == "sdr":
                if read_at:
                    read_badge = " (read)"
                else:
                    read_badge = " (unread)"

            text_preview = text[:120] + "..." if len(text) > 120 else text
            output.append(f"{prefix}[{label}]{sentiment_badge}{read_badge} ({ts_str})")
            output.append(f"{prefix}  \"{text_preview}\"")

    output.append("")
    _add_next_action_hint(output, status)

    return "\n".join(output)


def _add_next_action_hint(output: list[str], status: str) -> None:
    """Add a contextual hint for what to do next based on outreach status."""
    hints = {
        "pending": "Next: engage_prospect() to warm up, or generate_and_send() to invite.",
        "invited": "Next: check_replies() to detect acceptance, or engage_prospect() to warm up.",
        "connected": 'Next: send_message(action="followup") to send a follow-up DM.',
        "messaged": "Next: check_replies() to see if they responded.",
        "replied": "Next: Reply to them on LinkedIn directly.",
        "hot_lead": "Next: This is a hot lead! Reply on LinkedIn ASAP.",
        "skipped": "This prospect was skipped. No further actions.",
        "opted_out": "This prospect opted out. No further actions.",
    }
    hint = hints.get(status)
    if hint:
        output.append(hint)


_SENTIMENT_BADGES = {
    "positive": "+",
    "negative": "-",
    "question": "?",
    "out_of_office": "OOO",
    "opt_out": "X",
}
_MOVE_PREFIX = "move:"


def _message_badge(role: str, sentiment: str) -> str:
    """The label after a message. Our reply rows synced from the cloud carry
    the reply policy's move as ``move:<name>`` (api #757); it is ours, never a
    reading of the prospect, and says so."""
    sentiment = sentiment or ""
    if role == "sdr" and sentiment.startswith(_MOVE_PREFIX):
        return f" [our move: {sentiment[len(_MOVE_PREFIX):].replace('_', ' ')}]"
    if not sentiment or sentiment == "neutral":
        return ""
    return f" [{_SENTIMENT_BADGES.get(sentiment, sentiment)}]"
