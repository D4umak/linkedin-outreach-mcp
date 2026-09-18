"""Tool: book_meeting — put a meeting on the user's Google Calendar.

When a prospect agrees to a call, this creates the event on the calendar the
user connected, attaches a Google Meet link, and sends the prospect an
invitation. The backend holds the OAuth refresh token; nothing here ever sees
the user's Google credentials.

A person agreeing to a call thinks in durations ("half an hour on Tuesday"),
so this takes minutes and works out the end time itself.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _end_time(start: str, duration_minutes: int) -> str:
    return (datetime.fromisoformat(start) + timedelta(minutes=duration_minutes)).isoformat()


async def run_book_meeting(
    attendee_email: str,
    start: str,
    duration_minutes: int = 30,
    summary: str = "",
    description: str = "",
) -> str:
    """Book a meeting on your Google Calendar and invite a prospect.

    Use this when a reply agrees to a call. Creates the event on the calendar
    you connected, attaches a Google Meet link, and emails the attendee an
    invitation.

    Args:
        attendee_email: Who to invite — the prospect who replied.
        start: When it starts, ISO 8601, e.g. 2026-09-01T10:00:00.
        duration_minutes: How long the meeting runs. Defaults to 30.
        summary: Event title. Defaults to naming the attendee.
        description: Optional agenda or notes included in the invitation.
    """
    from ..config import get_backend_config
    from ..linkedin.backend_client import BackendClient

    if not attendee_email.strip():
        return "Error: 'attendee_email' is required — who should be invited?"

    try:
        end = _end_time(start, duration_minutes)
    except (ValueError, TypeError):
        return (
            f"Could not read '{start}' as a start time.\n"
            "Use ISO 8601, e.g. 2026-09-01T10:00:00 for 10am on 1 September."
        )

    backend_url, jwt_token = get_backend_config()
    if not jwt_token:
        return (
            "Not connected to the HeyLead backend. Run setup_profile with your "
            "token first, then try again."
        )

    title = summary.strip() or f"Intro call with {attendee_email.split('@')[0]}"
    client = BackendClient(backend_url, jwt_token)

    try:
        result = await client.create_calendar_event(
            summary=title,
            start_datetime=start,
            end_datetime=end,
            attendee_email=attendee_email,
            description=description,
        )
    except Exception as e:
        # The backend's 403 detail already carries the connect URL, so the user
        # gets a link they can act on rather than "booking failed".
        logger.warning("book_meeting failed: %s", e)
        return f"Could not book the meeting.\n{e}"
    finally:
        await client.close()

    if not result.get("success"):
        return f"Could not book the meeting: {result.get('error') or 'unknown error'}"

    lines = [
        f"Booked: {title}",
        f"  When:     {start} ({duration_minutes} min)",
        f"  Invited:  {attendee_email}",
    ]
    if result.get("meet_link"):
        lines.append(f"  Meet:     {result['meet_link']}")
    if result.get("event_link"):
        lines.append(f"  Calendar: {result['event_link']}")
    return "\n".join(lines)
