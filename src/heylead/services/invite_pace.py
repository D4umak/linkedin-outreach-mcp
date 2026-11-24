"""How HeyLead words an invitation pace, in one place.

A seat has two ceilings: invitations a week and invitations in one day. The
plan quoted both side by side ("up to 168 invitations a day and 320 a week,
Monday to Friday"), and at 168 a day the week's 320 run out on day two, so a
reader who planned by the daily figure was misled (heylead-api#1782, UI QA of
#1696, 28 Sep 2026). The weekly cap is the pace: HeyLead spreads it over the
sending days, and the daily figure is only a ceiling on any one day.

Twin of heylead-api app/services/invite_pace.py (pure, standard library
only, so the two files stay identical below this docstring, which
tests/test_invite_ramp_4315.py checks); both repos' semgrep rule
``a-daily-invite-figure-outside-the-pace-helper`` sends every pace sentence
through here.

A newly connected seat ramps (heylead-api#4315): 20 a day in its first week,
then 40, 80 and the tier cap. ``pace_clause`` takes the seat's
``invite_ramp`` (from the api's ``invite_caps_payload``) and says so while it
lasts, with the date the next pace starts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

_DAY = 86400
# scheduler_store.INVITE_RAMP_DAILY_STEPS, named again because this module is
# pure and has a client twin; test_invite_ramp_4315 pins the two together.
RAMP_DAILY_STEPS = (20, 40, 80)
_RAMP_DONE = len(RAMP_DAILY_STEPS) + 1


def per_day(daily: int, weekly: int, sending_days: int) -> int:
    """About how many invitations go out on a sending day: the week spread
    evenly over its sending days, never above the daily ceiling."""
    days = max(int(sending_days or 0), 1)
    return max(0, min(int(daily or 0), int(weekly or 0) // days))


def _date(ts: int) -> str:
    """"Mon 2 Nov", the UTC day the caps count by."""
    d = datetime.fromtimestamp(int(ts), timezone.utc)
    return f"{d:%a} {d.day} {d:%b}"


def _full_week_at(ramp: dict[str, Any]) -> int:
    """When the ramped weekly cap first equals the tier's, or 0 if not dated."""
    first = int(ramp.get("first_invite_at") or 0)
    if not first:
        return 0
    tier_weekly = int(ramp.get("tier_weekly") or 0)
    for step, daily in enumerate(RAMP_DAILY_STEPS, start=1):
        if min(tier_weekly, daily * 5) >= tier_weekly:
            return (first // _DAY + (step - 1) * 7) * _DAY
    return (first // _DAY + 21) * _DAY


def pace_clause(
    daily: int, weekly: int, sending_days: int, ramp: dict[str, Any] | None = None,
) -> str:
    """"up to 320 invitations a week, spread to about 64 each sending day
    (never more than 168 in one day)". The ceiling is named only when it is
    above the pace, so a free seat reads "…about 20 each sending day".

    ``ramp`` is the seat's ``invite_ramp``. While a paid seat ramps, the
    clause says when the full week starts, or that LinkedIn's pushback holds
    it (#4315); ``daily`` and ``weekly`` are already the ramped caps."""
    pace = per_day(daily, weekly, sending_days)
    clause = f"up to {int(weekly)} invitations a week, spread to about {pace} each sending day"
    ramp = ramp or {}
    ramping = (str(ramp.get("tier") or "free") != "free"
               and int(ramp.get("step") or _RAMP_DONE) < _RAMP_DONE)
    if ramping and int(ramp.get("held_until") or 0):
        return (f"{clause}; held at this pace until {_date(int(ramp['held_until']))} "
                "because LinkedIn pushed back")
    tier_weekly = int(ramp.get("tier_weekly") or 0)
    if ramping and int(weekly or 0) < tier_weekly:
        full_at = _full_week_at(ramp)
        when = f"from {_date(full_at)}" if full_at else "from its third week"
        rises = "rises each week, to" if int(ramp.get("step") or 1) < 2 else "rises to"
        return f"{clause}; this account was connected recently, so that {rises} {tier_weekly} a week {when}"
    if int(daily or 0) > pace:
        until = (f" until {_date(int(ramp['next_step_at']))}"
                 if ramping and int(ramp.get("next_step_at") or 0) else "")
        clause += f" (never more than {int(daily)} in one day{until})"
    return clause


def public_pace_clause(daily: int, weekly: int) -> str:
    """A ceiling pair as the public pace sentence states it (api #7686, approved words 7100616867).

    When a week of the daily figure fits in the weekly one, both are a plannable pace: "at most 20
    invitations a day and 100 a week". Otherwise the week is the pace and the day only a ceiling:
    "at most 320 invitations a week, never more than 168 in one day" (#1782: 168 a day spends 320
    by day two)."""
    d, w = int(daily), int(weekly)
    if w >= d * 5:
        return f"at most {d} invitations a day and {w} a week"
    return f"at most {w} invitations a week, never more than {d} in one day"


def public_ramp_clause(step_dailies: list[int] | tuple[int, ...]) -> str:
    """"starts at 20 a day and steps up once a week, to at most 40, 80 and then 168 in one day".

    ``step_dailies`` are the daily caps of the ramp's steps, first to last (the public matrix's
    ``steps``). Repeated figures are said once; "" when the account never steps up."""
    dailies = [int(d) for d in step_dailies]
    if not dailies:
        return ""
    start, ups = dailies[0], []
    for d in dailies[1:]:
        if d > (ups[-1] if ups else start):
            ups.append(d)
    if not ups:
        return ""
    if len(ups) == 1:
        to = str(ups[0])
    else:
        to = ", ".join(str(d) for d in ups[:-2]) + (", " if len(ups) > 2 else "") + f"{ups[-2]} and then {ups[-1]}"
    return f"starts at {start} a day and steps up once a week, to at most {to} in one day"
