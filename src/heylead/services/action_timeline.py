"""One-story outreach: prior touches for copy, and legal email/first-touch."""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

EMAIL_SEQUENCE_FALLBACK_DAYS = 14

_KIND_LABELS = {
    "profile_view": "Viewed their profile",
    "follow": "Followed them",
    "comment": "Commented on a post",
    "react": "Reacted to a post",
    "invite": "Sent a LinkedIn invitation",
    "inmail": "Sent InMail",
    "dm": "Sent a LinkedIn DM",
    "followup": "Sent a follow-up",
    "email": "Sent an email",
    "withdraw": "Withdrew the invitation",
}

_MESSAGE_KIND = {
    "invitation": "invite",
    "invite": "invite",
    "inmail": "inmail",
    "dm": "dm",
    "followup": "followup",
    "email": "email",
}

_ENGAGEMENT_KIND = {
    "profile_view": "profile_view",
    "follow": "follow",
    "comment": "comment",
    "react": "react",
    "engage_comment": "comment",
    "engage_react": "react",
}


def append_action_timeline(prompt: str, timeline: str) -> str:
    """Attach the sequence block when prior touches exist."""
    if not (timeline or "").strip():
        return prompt
    return (
        f"{prompt.rstrip()}\n\n## SEQUENCE SO FAR\n{timeline.strip()}\n"
    )


def format_action_timeline(
    events: list[dict[str, Any]],
    *,
    now: int | None = None,
) -> str:
    """Prompt block for generators. Empty when there are no prior touches."""
    if not events:
        return ""
    clock = int(now if now is not None else time.time())
    lines = [
        "PRIOR TOUCHES IN THIS SEQUENCE (do not pretend this is first contact):",
    ]
    last_at: int | None = None
    for event in sorted(events, key=lambda row: int(row.get("at") or 0)):
        kind = str(event.get("kind") or "")
        label = _KIND_LABELS.get(kind, kind.replace("_", " "))
        at = int(event.get("at") or 0)
        if at:
            last_at = at
            days = max(0, (clock - at) // 86400)
            age = f" ({days}d ago)"
        else:
            age = ""
        text = " ".join(str(event.get("text") or "").split())
        if text:
            lines.append(f"- {label}{age}: {text[:180]}")
        else:
            lines.append(f"- {label}{age}")
    if last_at:
        lines.append(f"Days since last touch: {max(0, (clock - last_at) // 86400)}")
    lines.append(
        "Continue the same ask. Mention LinkedIn once, lightly, if writing email. "
        "Do not re-introduce yourself as if you never wrote them."
    )
    return "\n".join(lines)


def sequence_email_instructions(*, sender_name: str = "there") -> str:
    """Email copy rules: next chapter, not a standalone cold blast."""
    return (
        "Generate the NEXT email in an existing outreach sequence "
        "(NOT a cold first email unless PRIOR TOUCHES is empty). "
        "Return JSON with 'subject' (max 60 chars, no emojis) and 'body' "
        f"(max 800 chars, professional email with greeting and sign-off). "
        f"Sign off as {sender_name}. "
        "Use PRIOR TOUCHES: continue the same ask; mention LinkedIn once, "
        "lightly; do not pretend you never contacted them."
    )


def email_is_legal(
    *,
    has_mailbox: bool,
    has_address: bool,
    enable_email: bool | None,
    status: str,
    linkedin_first_touch_attempted: bool,
    linkedin_unreachable: bool,
    has_replied: bool,
    is_first_degree: bool,
    invited_at: int | None,
    now: int,
    fallback_days: int = EMAIL_SEQUENCE_FALLBACK_DAYS,
    referral_handoff: bool = False,
) -> bool:
    """Email is a later chapter, never a day-0 parallel blast.

    ``referral_handoff`` is the one exception: the referrer named this
    address as the channel, so email is legal immediately.
    """
    if not has_mailbox or not has_address:
        return False
    if enable_email is False:
        return False
    if has_replied:
        return False
    if status in {
        "replied", "hot_lead", "opted_out", "skipped",
        "closed_happy", "closed_unhappy",
    }:
        return False
    if referral_handoff:
        return True
    if is_first_degree:
        return False
    if not linkedin_first_touch_attempted and not linkedin_unreachable:
        return False
    if linkedin_unreachable:
        return True
    if status in {"withdrawn", "expired"}:
        return True
    if status == "invited" and invited_at:
        return (now - int(invited_at)) >= fallback_days * 86400
    return False


def constrain_planned_actions(
    actions: list[dict[str, Any]],
    *,
    status: str,
    first_touch: str,
    email_allowed: bool,
) -> list[dict[str, Any]]:
    """Rewrite invite/InMail/DM/email so they match the legal picker."""
    out: list[dict[str, Any]] = []
    for raw in actions:
        action = dict(raw)
        kind = str(action.get("action_type") or "")
        if kind in {"invite", "inmail", "send_dm"}:
            if status == "pending":
                action["action_type"] = (
                    "send_dm" if first_touch == "dm" else first_touch
                )
            elif status in {"connected", "messaged"}:
                if kind in {"invite", "inmail"}:
                    action["action_type"] = "followup" if status == "messaged" else "send_dm"
            elif status == "invited" and kind in {"invite", "send_dm"}:
                continue
        if kind in {"email", "email_fallback", "email_invite"}:
            if not email_allowed:
                continue
            action["action_type"] = "email"
        out.append(action)
    return out


def kind_from_message(
    *,
    text: str = "",
    message_type: str = "",
) -> str:
    """Map a stored SDR message to a timeline kind."""
    raw = (text or "").lstrip()
    if raw.startswith("[EMAIL]"):
        return "email"
    if raw.startswith("[INMAIL]"):
        return "inmail"
    mapped = _MESSAGE_KIND.get((message_type or "").lower())
    if mapped:
        return mapped
    return "dm"


def kind_from_engagement(action_type: str) -> str | None:
    if action_type in {
        "no_posts_skipped", "endorse_skipped", "engage_422_skipped",
    }:
        return None
    return _ENGAGEMENT_KIND.get(action_type)


def collect_action_timeline(outreach_id: str) -> list[dict[str, Any]]:
    """Client: SDR messages + real engagements + withdraw, oldest first."""
    from ..db.queries import get_messages_for_outreach, get_outreach
    from ..db.schema import get_db

    events: list[dict[str, Any]] = []
    for msg in get_messages_for_outreach(outreach_id):
        if msg.get("role") != "sdr":
            continue
        events.append({
            "at": int(msg.get("timestamp") or 0),
            "kind": kind_from_message(text=msg.get("text") or ""),
            "text": msg.get("text") or "",
        })
    db = get_db()
    rows = db.execute(
        """SELECT action_type, text, created_at FROM engagements
           WHERE outreach_id = ? ORDER BY created_at""",
        (outreach_id,),
    ).fetchall()
    db.close()
    for row in rows:
        kind = kind_from_engagement(row["action_type"])
        if not kind:
            continue
        events.append({
            "at": int(row["created_at"] or 0),
            "kind": kind,
            "text": row["text"] or "",
        })
    outreach = get_outreach(outreach_id) or {}
    if outreach.get("status") in {"withdrawn", "expired"}:
        events.append({
            "at": int(outreach.get("updated_at") or outreach.get("invited_at") or 0),
            "kind": "withdraw",
            "text": "",
        })
    events.sort(key=lambda row: int(row.get("at") or 0))
    return events


async def action_timeline_text(outreach_id: str, *, now: int | None = None) -> str:
    """The prompt block for a prospect's prior touches, collected off the loop.

    collect_action_timeline() reads the database and get_db() raises
    RuntimeError on the event loop thread, so the read has to hop to a worker.
    Every caller used to make the call inline inside an async function and
    swallow the raise — generate_send returned "", followup_generator and
    send_inmail used a bare `except Exception: pass` — which turned a hard,
    always-thrown failure into an empty timeline that reads exactly like
    "this prospect has never been contacted". Nothing downstream could tell the
    two apart, and no invite, follow-up or InMail ever carried a sequence.

    The swallow is kept, because an unavailable timeline must not fail a send,
    but it lives in one place now and it logs. A warning here means prompts are
    going out as first contact.
    """
    if not outreach_id:
        return ""
    from ..db.async_bridge import run_db

    try:
        events = await run_db(collect_action_timeline, outreach_id)
    except Exception as exc:
        logger.warning(
            "Action timeline unavailable for outreach %s — the prompt will read "
            "as first contact: %s", outreach_id, exc,
        )
        return ""
    return format_action_timeline(events, now=now)
