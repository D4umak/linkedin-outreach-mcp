"""Partner follow-up tracking — CRM-style pipeline for business relationships.

Tracks follow-ups with partners, vendors, investors, etc. and sends
email reminders when follow-ups are due. Uses the existing scheduler
and Unipile email infrastructure.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from ..db.async_bridge import run_db
from ..db.queries import (
    advance_partner_followup,
    create_partner_followup,
    get_due_partner_reminders,
    get_partner_followup,
    get_partner_followups,
    update_partner_followup,
)
from ..constants import PARTNER_DEFAULT_SCHEDULE_DAYS, PARTNER_MAX_AUTO_FOLLOWUPS

logger = logging.getLogger(__name__)


def _ts_to_date(ts: int | None) -> str:
    """Convert unix timestamp to readable date string."""
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _ts_to_relative(ts: int | None) -> str:
    """Convert unix timestamp to relative time (e.g. 'in 2 days', '3 days ago')."""
    if not ts:
        return "—"
    now = int(time.time())
    diff = ts - now
    days = abs(diff) // 86400
    if diff > 0:
        if days == 0:
            return "today"
        elif days == 1:
            return "tomorrow"
        else:
            return f"in {days} days"
    else:
        if days == 0:
            return "today"
        elif days == 1:
            return "yesterday"
        else:
            return f"{days} days ago"


def _parse_date(date_str: str) -> int:
    """Parse YYYY-MM-DD string to unix timestamp (start of day UTC)."""
    try:
        dt = datetime.strptime(date_str.strip(), "%Y-%m-%d")
        dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        raise ValueError(f"Invalid date format: {date_str!r}. Use YYYY-MM-DD.")


async def run_partner(
    action: str = "list",
    name: str = "",
    company: str = "",
    email: str = "",
    context: str = "",
    next_followup: str = "",
    partner_id: str = "",
    note: str = "",
    days: int = 0,
) -> str:
    """Route partner follow-up actions."""
    action = action.strip().lower()

    # Every handler below is a sync function that hits the DB, and get_db()
    # refuses to run on the event loop thread — this router is awaited from
    # the `partner` MCP tool, so calling one directly raised RuntimeError and
    # the whole tool was dead.  run_db() hands the *whole handler* to the DB
    # worker thread: the read-modify-write handlers (update/complete read the
    # record, then write it back) stay on one thread with no await point in
    # between, exactly as they behaved when the tool ran in sync context.
    if action == "add":
        return await run_db(_handle_add, name, company, email, context, next_followup)
    elif action == "list":
        return await run_db(_handle_list)
    elif action == "update":
        return await run_db(_handle_update, partner_id, note, name, company, email, context)
    elif action == "complete":
        return await run_db(_handle_complete, partner_id, note)
    elif action == "snooze":
        return await run_db(_handle_snooze, partner_id, days or 7)
    elif action == "cancel":
        return await run_db(_handle_cancel, partner_id)
    else:
        return (
            f"Unknown action: {action!r}\n\n"
            "Available actions: add, list, update, complete, snooze, cancel"
        )


def _handle_add(
    name: str,
    company: str,
    email: str,
    context: str,
    next_followup: str,
) -> str:
    """Add a new partner to track."""
    if not name:
        return "Name is required. Usage: partner(action='add', name='...', company='...', context='...')"

    # Parse next follow-up date
    if next_followup:
        try:
            next_ts = _parse_date(next_followup)
        except ValueError as e:
            return str(e)
    else:
        next_ts = int(time.time()) + 86400  # tomorrow

    partner_id = create_partner_followup(
        name=name,
        company=company,
        email=email,
        context=context,
        next_followup_ts=next_ts,
    )

    # Build schedule preview
    schedule_lines = []
    base_ts = next_ts
    for i, gap_days in enumerate(PARTNER_DEFAULT_SCHEDULE_DAYS[:PARTNER_MAX_AUTO_FOLLOWUPS]):
        if i == 0:
            schedule_lines.append(f"  1. {_ts_to_date(base_ts)} ({_ts_to_relative(base_ts)})")
        else:
            base_ts += gap_days * 86400
            schedule_lines.append(f"  {i + 1}. {_ts_to_date(base_ts)}")

    lines = [
        f"Partner follow-up added!",
        "",
        f"  ID:       {partner_id}",
        f"  Name:     {name}",
    ]
    if company:
        lines.append(f"  Company:  {company}")
    if email:
        lines.append(f"  Email:    {email}")
    if context:
        lines.append(f"  Context:  {context}")
    lines += [
        "",
        f"Follow-up schedule (auto-reminders via email):",
        *schedule_lines,
        "",
        f"You'll receive an email reminder when each follow-up is due.",
    ]
    return "\n".join(lines)


def _handle_list() -> str:
    """List all active partner follow-ups."""
    active = get_partner_followups(status="active")
    paused = get_partner_followups(status="paused")
    all_items = active + paused

    if not all_items:
        return (
            "No active partner follow-ups.\n\n"
            "Add one: partner(action='add', name='...', company='...', context='...')"
        )

    now = int(time.time())
    lines = ["Partner Follow-Ups", "=" * 50, ""]

    for p in all_items:
        overdue = p.get("next_followup") and p["next_followup"] <= now
        status_icon = {
            "active": "🟢",
            "paused": "⏸️",
            "completed": "✅",
            "cancelled": "❌",
        }.get(p["status"], "❓")

        due_str = _ts_to_relative(p.get("next_followup"))
        if overdue and p["status"] == "active":
            due_str = f"⚠️ OVERDUE ({due_str})"

        lines.append(f"{status_icon} {p['name']}" + (f" @ {p['company']}" if p.get("company") else ""))
        lines.append(f"   ID: {p['id']}  |  Follow-ups: {p['followup_count']}/{PARTNER_MAX_AUTO_FOLLOWUPS}  |  Due: {due_str}")
        if p.get("context"):
            lines.append(f"   Context: {p['context']}")
        if p.get("email"):
            lines.append(f"   Email: {p['email']}")

        # Show latest note if any
        try:
            notes_list = json.loads(p.get("notes", "[]") or "[]")
            if notes_list:
                latest = notes_list[-1]
                lines.append(f"   Latest note: {latest.get('text', '')[:80]}")
        except (json.JSONDecodeError, TypeError):
            pass

        lines.append("")

    lines.append(f"Total: {len(active)} active, {len(paused)} paused")
    return "\n".join(lines)


def _handle_update(
    partner_id: str,
    note: str,
    name: str = "",
    company: str = "",
    email: str = "",
    context: str = "",
) -> str:
    """Update partner info or add a note."""
    if not partner_id:
        return "partner_id is required for 'update'. Run partner(action='list') to find IDs."

    record = get_partner_followup(partner_id)
    if not record:
        return f"Partner {partner_id!r} not found."

    updates: dict = {}
    if name:
        updates["name"] = name
    if company:
        updates["company"] = company
    if email:
        updates["email"] = email
    if context:
        updates["context"] = context

    if note:
        try:
            notes_list = json.loads(record.get("notes", "[]") or "[]")
        except (json.JSONDecodeError, TypeError):
            notes_list = []
        notes_list.append({
            "text": note,
            "at": int(time.time()),
        })
        updates["notes"] = json.dumps(notes_list)

    if not updates:
        return "Nothing to update. Provide note=, name=, company=, email=, or context=."

    update_partner_followup(partner_id, **updates)

    changes = ", ".join(updates.keys())
    return f"Updated {record['name']}: {changes}"


def _handle_complete(partner_id: str, note: str = "") -> str:
    """Mark a partner follow-up as completed."""
    if not partner_id:
        return "partner_id is required. Run partner(action='list') to find IDs."

    record = get_partner_followup(partner_id)
    if not record:
        return f"Partner {partner_id!r} not found."

    updates: dict = {"status": "completed"}
    if note:
        try:
            notes_list = json.loads(record.get("notes", "[]") or "[]")
        except (json.JSONDecodeError, TypeError):
            notes_list = []
        notes_list.append({"text": f"[COMPLETED] {note}", "at": int(time.time())})
        updates["notes"] = json.dumps(notes_list)

    update_partner_followup(partner_id, **updates)

    return (
        f"Completed: {record['name']}"
        + (f" @ {record['company']}" if record.get("company") else "")
        + f"\n  Follow-ups sent: {record['followup_count']}"
        + f"\n  Tracked since: {_ts_to_date(record.get('created_at'))}"
    )


def _handle_snooze(partner_id: str, days: int) -> str:
    """Push the next follow-up by N days."""
    if not partner_id:
        return "partner_id is required. Run partner(action='list') to find IDs."

    record = get_partner_followup(partner_id)
    if not record:
        return f"Partner {partner_id!r} not found."

    if days < 1:
        days = 7

    new_ts = int(time.time()) + days * 86400
    update_partner_followup(partner_id, next_followup=new_ts, status="active")

    return (
        f"Snoozed {record['name']}: next follow-up {_ts_to_date(new_ts)} ({_ts_to_relative(new_ts)})"
    )


def _handle_cancel(partner_id: str) -> str:
    """Stop tracking a partner."""
    if not partner_id:
        return "partner_id is required. Run partner(action='list') to find IDs."

    record = get_partner_followup(partner_id)
    if not record:
        return f"Partner {partner_id!r} not found."

    update_partner_followup(partner_id, status="cancelled")

    return f"Cancelled tracking for {record['name']}" + (f" @ {record['company']}" if record.get("company") else "")


def build_partner_reminder_email(partner: dict) -> str:
    """Build HTML email body for a partner follow-up reminder."""
    name = partner.get("name", "Unknown")
    company = partner.get("company", "")
    ctx = partner.get("context", "")
    count = partner.get("followup_count", 0) + 1
    partner_email = partner.get("email", "")

    subject_line = f"Follow-up #{count} with {name}" + (f" ({company})" if company else "")

    body_parts = [
        f"<h2>🔔 Partner Follow-Up Reminder</h2>",
        f"<p><strong>{name}</strong>",
    ]
    if company:
        body_parts.append(f" at <strong>{company}</strong>")
    body_parts.append("</p>")

    if ctx:
        body_parts.append(f"<p><strong>Context:</strong> {ctx}</p>")
    if partner_email:
        body_parts.append(f"<p><strong>Email:</strong> <a href='mailto:{partner_email}'>{partner_email}</a></p>")

    body_parts.append(f"<p>This is follow-up <strong>#{count}</strong> of {PARTNER_MAX_AUTO_FOLLOWUPS}.</p>")
    body_parts.append("<hr>")
    body_parts.append("<p style='color: #888; font-size: 12px;'>Sent by HeyLead Partner Tracker</p>")

    return "".join(body_parts)
