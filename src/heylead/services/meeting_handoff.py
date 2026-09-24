"""A way to meet that only a person can finish: a number, or their own link.

``hot_lead_closer.booking_is_grounded`` answers one question — can I write a
calendar event? — and it needs an attendee email AND an ISO-8601 timestamp.
Until 22 Sep 2026 the product read that single answer as "is a meeting
arranged?" (the hosted twin carries the same module), so every yes that arrived in another shape was filed as a failure
to book. Volodymyr Nakvasiuk sent his phone number and asked to be rung; the
closer recorded "Provided a phone number for a call instead of an ISO booking
time or an email address" and the row sat in "unanswered" for six days.

This module reads the other two shapes off the prospect's own message. Pure:
no model, no database, no clock. Nothing here is guessed — a number is a
number or it is not — and nothing here dials or clicks anything.
"""

from __future__ import annotations

import re
from typing import Any

# Their own booking page. Defined here and imported by unanswered_leads, so
# the two cannot disagree about what a booking link is.
BOOKING_URL_RE = re.compile(
    r"https?://[^\s<>\"]*(?:"
    r"calendly\.com|cal\.com|acuityscheduling\.com|savvycal\.com|"
    r"hubspot\.com/meetings|/bookings?\b|/book-a-|/schedule"
    r"|outlook\.office\.com"
    r")[^\s<>\"]*",
    re.IGNORECASE,
)

# A run of digits somebody could ring, with the separators people type. Bounded
# at both ends by a non-word character so it cannot start mid-token, and the
# digit count is checked separately: the regex alone would take the front of an
# ISO timestamp ("2026-09-25T15:00" begins 2026-09-25) for a number.
_PHONE_RE = re.compile(r"(?<![\w.+])\+?\d[\d\s().-]{6,18}\d(?![\w.])")

# Fewest digits anyone's number has once the spaces come out (a UK mobile
# without its country code is 11, a US number 10, an extension-less landline 9).
_PHONE_MIN_DIGITS = 9
_PHONE_MAX_DIGITS = 15

# A date is not a phone number however many digits it carries.
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}[/.]\d{1,2}[/.]\d{2,4}")


def phone_in(text: str) -> str:
    """The first number in ``text`` a person could be called on, or ""."""
    body = text or ""
    if not body:
        return ""
    for match in _PHONE_RE.finditer(body):
        candidate = match.group(0).strip()
        if _DATE_RE.search(candidate):
            continue
        digits = [c for c in candidate if c.isdigit()]
        if not _PHONE_MIN_DIGITS <= len(digits) <= _PHONE_MAX_DIGITS:
            continue
        # A bare run of digits with no punctuation and no leading + is a
        # quantity far more often than a number ("we did 15000 in revenue").
        if not candidate.startswith("+") and candidate.isdigit() and len(digits) < 10:
            continue
        return candidate
    return ""


def booking_url_in(text: str) -> str:
    """Their own booking page in ``text``, or ""."""
    found = BOOKING_URL_RE.search(text or "")
    return found.group(0) if found else ""


def handoff_in(text: str) -> dict[str, str] | None:
    """The way to meet this message hands over, or None.

    A link beats a number when both are present: it costs the operator one
    click and no phone call, and it is the prospect's own availability.
    """
    url = booking_url_in(text)
    if url:
        return {"kind": "link", "value": url}
    number = phone_in(text)
    if number:
        return {"kind": "phone", "value": number}
    return None


def operator_action(handoff: dict[str, Any] | None, name: str = "") -> str:
    """What the operator has to do, in the words they would use themselves.

    Never "unanswered": nobody owes this person a message. They said yes in
    the one way the product cannot finish by itself.
    """
    kind = str((handoff or {}).get("kind") or "")
    value = str((handoff or {}).get("value") or "")
    who = (name or "").strip()
    if kind == "phone":
        return f"Call {who} on {value}".strip() if who else f"Call {value}"
    if kind == "link":
        return f"Book on {value}"
    return ""
