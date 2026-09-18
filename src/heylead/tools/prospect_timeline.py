"""Tool helper: prospect timeline — chronological journey view for a prospect."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from ..db.async_bridge import run_db

logger = logging.getLogger(__name__)


async def run_prospect_timeline(outreach_id: str, days: int = 30) -> str:
    """Format a day-by-day timeline of all actions for a prospect."""
    from ..db.queries import get_prospect_timeline

    events = await run_db(get_prospect_timeline, outreach_id, days=days)
    if not events:
        return f"No events found for outreach `{outreach_id}` in the last {days} days."

    lines = [f"# Prospect Timeline — `{outreach_id[:12]}...`", ""]

    # Group by day
    current_day = ""
    for ev in events:
        ts = ev["timestamp"]
        if not ts:
            continue
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        day_str = dt.strftime("%Y-%m-%d")
        time_str = dt.strftime("%H:%M")

        if day_str != current_day:
            if current_day:
                lines.append("")
            lines.append(f"## {day_str}")
            current_day = day_str

        icon = _event_icon(ev["event_type"], ev["action"])
        details = ev.get("details", "")
        lines.append(f"- `{time_str}` {icon} **{ev['action']}** {details}")

    lines.append("")
    lines.append(f"_{len(events)} events over {days} days_")
    return "\n".join(lines)


def _event_icon(event_type: str, action: str) -> str:
    """Return a compact text label for the event type."""
    icons = {
        "milestone": "[M]",
        "engagement": "[E]",
        "job": "[J]",
        "action": "[A]",
    }
    return icons.get(event_type, "[?]")
