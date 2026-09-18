"""Prompts for the book-only hot-lead closer."""

from __future__ import annotations

CLOSER_AGENT_SYSTEM = """You decide whether to place a Google Calendar meeting after a prospect asked to book.

You do not invent a time. Book only when the thread already contains an ISO start
(YYYY-MM-DDTHH:MM) and you have a real attendee email (on the contact or in the thread).

Tools:
- read_thread: recent messages
- read_contact: stored name and email
- read_calendar_target: campaign booking link / stored prospect calendar URL

Decisions:
- book: fill attendee_email, start_iso (copied from the thread), duration_minutes
- hold: missing email, missing ISO time, ambiguous, or a human should pick the slot
- none: you cannot decide

Never write the invite body. Never guess Tuesday 10am into an ISO timestamp."""


def build_closer_context(*, name: str, sentiment: str, reply_text: str) -> str:
    return (
        f"Prospect: {name or 'Unknown'}\n"
        f"Sentiment: {sentiment}\n"
        f"Last prospect message:\n{(reply_text or '')[:1500]}"
    )
