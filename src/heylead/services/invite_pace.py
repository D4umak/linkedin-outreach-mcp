"""How HeyLead words an invitation pace, in one place.

A seat has two ceilings: invitations a week and invitations in one day. The
plan quoted both side by side ("up to 168 invitations a day and 320 a week,
Monday to Friday"), and at 168 a day the week's 320 run out on day two, so a
reader who planned by the daily figure was misled (heylead-api#1782, UI QA of
#1696, 28 Sep 2026). The weekly cap is the pace: HeyLead spreads it over the
sending days, and the daily figure is only a ceiling on any one day.

Twin of heylead-api app/services/invite_pace.py (pure, no imports, so the
two files can stay identical below this docstring); both repos' semgrep rule
``a-daily-invite-figure-outside-the-pace-helper`` sends every pace sentence
through here.
"""

from __future__ import annotations


def per_day(daily: int, weekly: int, sending_days: int) -> int:
    """About how many invitations go out on a sending day: the week spread
    evenly over its sending days, never above the daily ceiling."""
    days = max(int(sending_days or 0), 1)
    return max(0, min(int(daily or 0), int(weekly or 0) // days))


def pace_clause(daily: int, weekly: int, sending_days: int) -> str:
    """"up to 320 invitations a week, spread to about 64 each sending day
    (never more than 168 in one day)". The ceiling is named only when it is
    above the pace, so a free seat reads "…about 20 each sending day"."""
    pace = per_day(daily, weekly, sending_days)
    clause = f"up to {int(weekly)} invitations a week, spread to about {pace} each sending day"
    if int(daily or 0) > pace:
        clause += f" (never more than {int(daily)} in one day)"
    return clause
