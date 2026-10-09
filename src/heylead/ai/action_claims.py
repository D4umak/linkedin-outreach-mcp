"""Does a message say HeyLead did something? (D4umak/heylead-api#2453; the hosted copy is heylead-api app/services/action_claims.py, keep the two alike)

A message we send in a person's name may say what was said, never that a
thing was done, unless the thing was done. On 7 Oct 2026 an auto-reply wrote
"Just sent an invite over to ... for Thursday at 8 pm PST" in a workspace
with no calendar, no mailbox and no booking link; nobody sent anything, and
the prospect asked the next day whether the meeting was on.

A promise of the same actions ("I'll send the invite", "what email should I
send it to") is caught too: it is the same untruth one message earlier.

Pure code, no model: three kinds of action a LinkedIn message could claim,
each a short list of patterns. ``performed`` names the kinds the caller can
prove for this thread (a ``calendar_events`` row, a sent email); a claim of a
kind not in it is returned. "Sent an invite 10 days ago" is a LinkedIn
invitation that was sent, and is not a claim of a calendar invite.
"""

from __future__ import annotations

import re

CALENDAR_INVITE = "calendar_invite"
MEETING_BOOKED = "meeting_booked"
EMAIL_SENT = "email_sent"
KINDS = (CALENDAR_INVITE, MEETING_BOOKED, EMAIL_SENT)

_INVITE = r"(?:calendar\s+|meeting\s+|google\s+meet\s+|zoom\s+)?invit(?:e|ation)"
# "Sent an invite 10 days ago / a few weeks back / last week": the LinkedIn
# connection request, which HeyLead did send.
_PAST_INVITATION = re.compile(
    r"\b(?:ago|back|last\s+(?:week|month)|earlier\s+on\s+linkedin|on\s+linkedin)\b", re.I,
)

_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    CALENDAR_INVITE: (
        re.compile(
            rf"\b(?:i\s*(?:'ve|\s+have)\s+|just\s+|already\s+)?(?:sent|shot|fired\s+off)\b"
            rf"[^.?!\n]{{0,30}}?\b{_INVITE}\b(?P<tail>[^.?!\n]{{0,40}})",
            re.I,
        ),
        re.compile(rf"\b{_INVITE}\s+(?:is\s+|has\s+been\s+)?(?:sent|out|on\s+its\s+way|in\s+your\s+inbox)\b", re.I),
        re.compile(rf"\b(?:check(?:ing)?|resen[dt]|resending|updat(?:e|ed|ing))\s+(?:the|your|my)\s+{_INVITE}\b", re.I),
        re.compile(r"\badded\s+(?:it|you|this|us|the\s+call|the\s+meeting)\s+to\s+(?:the|my|your|our)?\s*calendar\b", re.I),
        # A promise is the same untruth one message earlier: "What is the best
        # email to send the calendar invite to?" came an hour before "Just
        # sent an invite" (#2453, designer review).
        re.compile(rf"\b(?:send|sending|get|shoot|pop)\s+(?:you\s+|over\s+)?(?:the|a|an)\s+{_INVITE}\b", re.I),
        re.compile(rf"\b(?:i'?ll|i\s+will|let\s+me|will)\s+(?:send|shoot|get)\b[^.?!\n]{{0,20}}?\b{_INVITE}\b", re.I),
    ),
    MEETING_BOOKED: (
        re.compile(
            r"\b(?:i\s*(?:'ve|\s+have)\s+|just\s+|all\s+)?(?:booked|scheduled|locked\s+in)\s+"
            r"(?:us|you|it|that|this|the\s+(?:call|meeting|slot|time)|a\s+slot)\b",
            re.I,
        ),
        re.compile(r"\b(?:call|meeting)\s+is\s+(?:booked|scheduled|confirmed|in\s+the\s+calendar)\b", re.I),
    ),
    EMAIL_SENT: (
        re.compile(r"\b(?:i\s*(?:'ve|\s+have)\s+|just\s+)?emailed\s+(?:you|it|them|the)\b", re.I),
        re.compile(r"\b(?:sent|dropped)\s+(?:you\s+)?(?:an?\s+|the\s+)?(?:e-?mail|note\s+to\s+your\s+inbox)\b", re.I),
        re.compile(r"\bcheck\s+your\s+(?:inbox|e-?mail)\b", re.I),
        re.compile(r"\b(?:i'?ll|i\s+will|let\s+me)\s+(?:e-?mail|send)\s+(?:you|it|this|that|the\s+\w+)\b[^.?!\n]{0,20}?(?:\bover\b|\bto\s+your\s+(?:e-?mail|inbox)\b|\bby\s+e-?mail\b)", re.I),
        re.compile(r"\b(?:i'?ll|i\s+will|let\s+me)\s+e-?mail\s+you\b", re.I),
        re.compile(r"\b(?:best|which|what(?:'s| is))\s+(?:the\s+best\s+)?e-?mail\b[^.?!\n]{0,30}?\bto\s+send\b", re.I),
    ),
}


def claims_unperformed_action(text: str, *, performed: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """The kinds of action ``text`` says were done that ``performed`` does not hold."""
    found: list[str] = []
    body = str(text or "")
    for kind, patterns in _PATTERNS.items():
        if kind in performed:
            continue
        for pattern in patterns:
            match = pattern.search(body)
            if not match:
                continue
            tail = match.groupdict().get("tail")
            if kind == CALENDAR_INVITE and tail is not None and _PAST_INVITATION.search(tail):
                continue
            found.append(kind)
            break
    return found


def claim_error(text: str, *, performed: frozenset[str] | set[str] = frozenset()) -> str | None:
    """A validator error for a claim of an action not taken, or None."""
    kinds = claims_unperformed_action(text, performed=performed)
    if not kinds:
        return None
    return f"Claims an action that was not taken: {', '.join(kinds)}"
