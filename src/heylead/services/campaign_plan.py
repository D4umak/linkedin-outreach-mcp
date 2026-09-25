"""What happens after launch, step by step: the client twin of the api's plan.

heylead-api ``app/services/campaign_plan.py`` computes the plan a hosted
campaign shows (``GET /api/v1/campaigns/{id}/plan``). This module computes the
same steps, with the same ``PlanStep``, ``campaign_plan`` and ``render_plan``
signatures, for self-hosted installs and for a hosted account whose api call
failed. ``plan_text_for`` picks between them.

Every number in a line is read from ``constants.py``, never typed here: the
two hand-written "Once launched" blocks this replaced quoted warm-up gaps and
follow-up days that matched neither the api nor a Free seat (24 Sep 2026, a
new user: "I do not understand how it works"). tests/test_campaign_plan.py
compares every number with its constant, and the step order and titles with
the api's fixture.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .. import constants as c
from .. import facts
from .outreach_channel import exclude_connections_enabled

logger = logging.getLogger(__name__)

HEADING = "What happens after launch"

ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
_DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_DAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

MODE_AUTOPILOT = "autopilot"
MODE_REQUIRE_APPROVAL = "require_approval"

# Window sources whose stored hours stand as chosen (heylead-api honours
# "dashboard"; "local" is a self-hosted install's own config).
_CHOSEN_SOURCES = ("dashboard", "local")

NEVER_LINE = (
    "HeyLead never posts on your profile, never messages your existing "
    "connections in this campaign, and stops the moment you pause."
)
# A campaign with exclude_connections off can enrol people the seat is
# already connected to, so the plan must not promise it will leave them alone
# (D4umak/heylead-api#1414; same words as the api's).
NEVER_LINE_INCLUDES_CONNECTIONS = (
    "HeyLead never posts on your profile and stops the moment you pause. This "
    "campaign can include people you are already connected to."
)
NEVER_LINE_CONNECTIONS_ONLY = (
    "HeyLead never posts on your profile, writes only to the people this "
    "campaign found, and stops the moment you pause."
)
# The founder cohort, with its n and date (facts.FOUNDER_COHORT). A hosted
# workspace with enough history of its own gets its own numbers in the api's
# plan, which plan_text_for renders as it comes (Outcome api#1140).
ACCEPT_LINE = facts.with_expectation_label(facts.ACCEPT_EXPECTATION)
REPLY_EXPECTATION_LINE = facts.with_expectation_label(facts.REPLY_EXPECTATION)
_MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"]

# heylead-api campaign_flags: every spelling of on and off the product writes.
_ON_WORDS = frozenset({"on", "true", "1", "yes", "y", "enabled", "enable"})
_OFF_WORDS = frozenset({"off", "false", "0", "no", "n", "disabled", "disable"})


@dataclass(frozen=True)
class PlanStep:
    key: str
    title: str
    line: str
    day_hint: str


def _json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _flag(cfg: dict[str, Any], key: str, default: bool) -> bool:
    value = cfg.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _ON_WORDS:
            return True
        if text in _OFF_WORDS:
            return False
    return default


def _minutes(seconds: int) -> int:
    return int(seconds) // 60


def _days_label(days: list[int]) -> str:
    days = sorted(set(int(d) for d in days))
    if days == ALL_DAYS:
        return "every day"
    if days == [0, 1, 2, 3, 4]:
        return "Monday to Friday"
    if len(days) > 1 and days == list(range(days[0], days[-1] + 1)):
        return f"{_DAY_ABBR[days[0]]} to {_DAY_ABBR[days[-1]]}"
    return ", ".join(_DAY_ABBR[d] for d in days) or "every day"


def _zone(name: str) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def _is_client_default(hours: dict[str, Any]) -> bool:
    """The chat client's 0-24-every-day default is not a chosen window."""
    try:
        return (int(hours.get("start", 0)) <= 0 and int(hours.get("end", 24)) >= 24
                and sorted(int(d) for d in hours.get("days") or ALL_DAYS) == ALL_DAYS)
    except (TypeError, ValueError):
        return True


def effective_working_hours(state: dict[str, Any] | None) -> dict[str, Any]:
    """The window sends happen in, the way heylead-api decides it.

    A window from the dashboard (or a self-hosted install's own config)
    stands. Anything else, the client's no-restriction default included, is
    ``constants.DEFAULT_SENDING_WINDOW`` in the state's ``timezone`` (London
    when none).
    """
    state = state or {}
    hours = _json_dict(state.get("working_hours"))
    source = str(state.get("working_hours_source") or "")
    zone = str(state.get("timezone") or hours.get("timezone") or "").strip()
    if hours and (source in _CHOSEN_SOURCES or not _is_client_default(hours)):
        out = {
            "start": int(hours.get("start", 0) or 0),
            "end": int(hours.get("end", 24) or 24),
            "days": list(hours.get("days") or ALL_DAYS),
            "timezone": zone or "UTC",
        }
        return out
    window = dict(c.DEFAULT_SENDING_WINDOW)
    window["days"] = list(window["days"])
    if zone:
        window["timezone"] = zone
    return window


def first_send_day(hours: dict[str, Any], now: datetime) -> str:
    """"today", "tomorrow" or a weekday name: the first day the window opens."""
    days = set(int(d) for d in hours.get("days") or ALL_DAYS)
    end = int(hours.get("end", 24))
    local = now.astimezone(_zone(str(hours.get("timezone") or "UTC")))
    for offset in range(0, 8):
        day = local + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        if offset == 0 and local.hour >= end:
            continue
        if offset == 0:
            return "today"
        if offset == 1:
            return "tomorrow"
        return _DAY_NAMES[day.weekday()]
    return "the next day your window opens"


def _seat_caps(seat: dict[str, Any] | None) -> tuple[int, int]:
    """(daily, weekly) invitation ceilings for this seat; a free seat when unknown."""
    seat = seat or {}
    try:
        daily = int(seat.get("daily_invite_cap") or 0) or c.HOSTED_DAILY_INVITE_CAP_FREE
        weekly = int(seat.get("weekly_cap") or 0) or c.HOSTED_WEEKLY_INVITE_CAP
    except (TypeError, ValueError):
        return c.HOSTED_DAILY_INVITE_CAP_FREE, c.HOSTED_WEEKLY_INVITE_CAP
    return daily, weekly


def effective_max_followups(cfg: dict[str, Any] | str | None, tier: str) -> int:
    """How many follow-ups this campaign can actually send one person.

    0 when follow-ups are switched off (planner.py skips every follow-up
    when ``enable_followups`` is off). Otherwise the campaign's own
    max_followups below the tier ceiling, the tier ceiling when it is unset,
    the way the scheduler's state machine reads it. Twin of heylead-api's
    ``campaign_plan.effective_max_followups`` (#1414: a header said "Up to
    4" next to a plan that said "up to 2").
    """
    cfg = _json_dict(cfg)
    if not _flag(cfg, "enable_followups", True):
        return 0
    tier_max = c.PRO_MAX_FOLLOWUPS if str(tier or c.TIER_FREE) == c.TIER_PRO else c.FREE_MAX_FOLLOWUPS
    try:
        cfg_max = int(cfg.get("max_followups") or 0)
    except (TypeError, ValueError):
        cfg_max = 0
    return min(cfg_max, tier_max) if cfg_max > 0 else tier_max


def _fu_word(n: int) -> str:
    return "follow-up" if n == 1 else "follow-ups"


def _followup_line(cfg: dict[str, Any], tier: str) -> tuple[str, str]:
    custom = cfg.get("followup_delay_days")
    schedule: list[int] = []
    if isinstance(custom, str):
        schedule = [int(p) for p in custom.split(",") if p.strip().isdigit()]
    elif isinstance(custom, (list, tuple)):
        for item in custom:
            try:
                schedule.append(int(item))
            except (TypeError, ValueError):
                continue
    pro = tier == c.TIER_PRO
    max_fu = effective_max_followups(cfg, tier)
    if max_fu <= 0:
        return (
            "Follow-ups are off: if they do not reply to the opening message, HeyLead stops.",
            "Off",
        )
    if not schedule and pro:
        schedule = list(c.PRO_FOLLOWUP_SCHEDULE_DAYS)
    # Only the gaps the cap lets fire: a 1,3,7,14 cadence under a cap of 2
    # printed four gaps next to 'up to 2 follow-ups' (24 Sep prod check).
    schedule = schedule[:max_fu]
    if schedule:
        spaced = (", ".join(str(d) for d in schedule[:-1]) + f" and {schedule[-1]}"
                  if len(schedule) > 1 else str(schedule[0]))
        return (
            f"If they do not reply: up to {max_fu} {_fu_word(max_fu)}, spaced {spaced} "
            f"{'day' if schedule == [1] else 'days'} after "
            "the last message. Then HeyLead stops.",
            f"Days {spaced}",
        )
    return (
        f"If they do not reply: up to {max_fu} {_fu_word(max_fu)}, at least a day apart. "
        "Then HeyLead stops.",
        "A day apart",
    )


def _warmup_line(cfg: dict[str, Any]) -> str:
    """The warm-up the scheduler arms: only the touches this campaign has on.

    planner.py reads enable_profile_views, enable_follows and
    enable_engagements (the comment) with a default of on; a campaign with
    engagements off was still promised "a comment on a recent post" (#1414).
    """
    touches = [
        name for key, name in (
            ("enable_profile_views", "a profile view"),
            ("enable_follows", "a follow"),
            ("enable_engagements", "a comment on a recent post"),
        ) if _flag(cfg, key, True)
    ]
    if not touches:
        return "No warm-up: the invitation is the first thing they see from you."
    listed = touches[0] if len(touches) == 1 else ", ".join(touches[:-1]) + f" and {touches[-1]}"
    gap = (
        f", {_minutes(c.ENGAGEMENT_DELAY_MIN)} to {_minutes(c.ENGAGEMENT_DELAY_MAX)} minutes apart"
        if len(touches) > 1 else ""
    )
    return f"Before each invitation: {listed}{gap}. Nothing is written on your own profile."


def _is_job_search(cfg: dict[str, Any], context: dict[str, Any]) -> bool:
    merged = {**cfg, **context}
    nested = cfg.get("campaign_context")
    if isinstance(nested, dict) and not merged.get("campaign_type"):
        merged["campaign_type"] = nested.get("campaign_type")
    return str(merged.get("campaign_type") or "").strip().lower() == "job_search"


def campaign_plan(
    campaign: dict[str, Any],
    workspace_state: dict[str, Any] | None,
    seat: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> list[PlanStep]:
    """The ordered steps this campaign will take once launched.

    ``campaign`` is a campaign row (``config_json`` and the optional
    ``context_json`` as JSON text or dicts). ``workspace_state`` carries
    ``tier``, ``send_approval_mode``, ``working_hours``,
    ``working_hours_source`` and ``timezone``; an unknown approval mode reads
    as require_approval, as in the api. ``seat`` carries
    ``daily_invite_cap`` and ``weekly_cap`` (the names the api's
    ``invite_caps_payload`` uses); None reads as a free LinkedIn account.
    ``now`` is for tests.
    """
    cfg = _json_dict((campaign or {}).get("config_json"))
    context = _json_dict((campaign or {}).get("context_json"))
    state = workspace_state or {}
    hours = effective_working_hours(state)
    tier = str(state.get("tier") or c.TIER_FREE)
    autopilot = str(state.get("send_approval_mode") or "").strip() == MODE_AUTOPILOT
    connections_only = _flag(cfg, "connections_only", False) or not _flag(cfg, "enable_invitations", True)
    job_search = _is_job_search(cfg, context)
    booking_link = str(cfg.get("booking_link") or "").strip()
    daily, weekly = _seat_caps(seat)
    day = first_send_day(hours, now or datetime.now(timezone.utc))
    start = int(hours.get("start", 0))
    window = f"{_days_label(hours.get('days') or ALL_DAYS)} {start:02d}:00-{int(hours.get('end', 24)):02d}:00"

    steps: list[PlanStep] = []
    if connections_only:
        find = (
            f"Today: HeyLead searches LinkedIn for people who match, up to {c.PLAN_DAILY_ADD_BUDGET} "
            "a day, keeps only those above the fit line, and in this campaign writes only to "
            "people you are already connected to. You can open every one."
        )
    else:
        find = (
            f"Today: HeyLead searches LinkedIn for people who match, up to {c.PLAN_DAILY_ADD_BUDGET} "
            "a day, and keeps only those above the fit line. You can open every one."
        )
    steps.append(PlanStep("find", "Find", find, "Today"))

    if not connections_only:
        steps.append(PlanStep("warmup", "Warm-up", _warmup_line(cfg), "Before each invitation"))
        steps.append(PlanStep(
            "invite", "Invite",
            f"From {day} at {start:02d}:00 your time: up to {daily} "
            f"invitations a day and {weekly} a week, {window}, "
            f"{_minutes(c.INVITE_DELAY_MIN)} to {_minutes(c.INVITE_DELAY_MAX)} minutes apart, "
            "each with a short note in your voice.",
            f"From {day}",
        ))
        steps.append(PlanStep("accept", "Accept", ACCEPT_LINE, "A few days later"))

    if connections_only:
        opener = (f"From {day} at {start:02d}:00 your time, {window}: an opening message "
                  "in your voice to each person, ")
        open_hint = f"From {day}"
    else:
        opener = "When someone accepts: an opening message in your voice, "
        open_hint = "On acceptance"
    if autopilot:
        opener += "sent inside your window, no review."
    else:
        opener += "held for your approval until you switch to autopilot. You get a notification."
    steps.append(PlanStep("open", "Open", opener, open_hint))

    fu_line, fu_hint = _followup_line(cfg, tier)
    steps.append(PlanStep("follow_up", "Follow up", fu_line, fu_hint))

    if job_search:
        reply = "Every reply is held for you; nothing answers on its own."
    elif _flag(cfg, "enable_auto_replies", True):
        reply = (
            "Replies are read and answered from your campaign's facts; anything unclear is "
            f"held for you; a 'not now' gets a check-in in {c.NOT_NOW_WAIT_DAYS} days."
        )
    else:
        reply = "Replies are read and held for you to answer; nothing answers on its own."
    steps.append(PlanStep("reply", "Reply", f"{reply} {REPLY_EXPECTATION_LINE}", "When they answer"))

    if booking_link:
        lead = (
            "A yes becomes a meeting hand-off with your booking link and a Slack or email "
            "notification."
        )
    else:
        lead = (
            "A yes becomes a lead in your dashboard with a Slack or email notification. "
            "Add a booking link to the campaign and HeyLead offers it."
        )
    steps.append(PlanStep("lead", "Lead", lead, "On a yes"))

    steps.append(PlanStep(
        "never", "Never",
        NEVER_LINE_CONNECTIONS_ONLY if connections_only
        # The reader planner.py enforces with: an unset key is off there, so
        # the promise is made only where the scheduler keeps it.
        else NEVER_LINE if exclude_connections_enabled(cfg)
        else NEVER_LINE_INCLUDES_CONNECTIONS,
        "Always",
    ))
    return steps


def render_plan(steps: list[PlanStep]) -> str:
    """The chat block: a heading and one numbered line per step."""
    lines = [HEADING]
    for i, step in enumerate(steps, start=1):
        lines.append(f"{i}. {step.title}: {step.line}")
    return "\n".join(lines)


def local_tier() -> str:
    """The tier this install's plan reads: config's ``tier``, Free when unset.

    Every client surface that quotes a follow-up count passes this to
    ``effective_max_followups``, so the plan, show_status and the scheduler
    diagnostics quote one number (#1414).
    """
    from .. import config

    return str(config.load_config().get("tier") or c.TIER_FREE)


def _local_state() -> dict[str, Any]:
    """This install's own settings, in the shape campaign_plan reads.

    Self-hosted sends in the window its own config names and has no approval
    hold. A hosted account whose api call failed gets the api's defaults: the
    default sending window and require_approval.
    """
    from .. import config

    cfg = config.load_config()
    hosted = config.is_backend_mode()
    return {
        "tier": local_tier(),
        "send_approval_mode": "" if hosted else MODE_AUTOPILOT,
        "working_hours": cfg.get("working_hours") or None,
        "working_hours_source": "" if hosted else "local",
        "timezone": str(cfg.get("timezone") or ""),
    }


def _local_seat() -> dict[str, Any]:
    from .. import config

    if config.is_backend_mode():
        return {}
    # The local sender enforces a daily ceiling and no weekly one; the api's
    # free-seat week stands in until a hosted payload says otherwise.
    return {"daily_invite_cap": c.DAILY_CAP_INVITATIONS_FREE}


CHECKPOINT_WORKING_DAYS = 5


def checkpoint_date(launched_at: datetime, zone: str) -> str:
    """The launch day plus five working days (Mon-Fri) in ``zone``, as
    "Friday 2 October". A weekend launch counts from the Monday."""
    day = launched_at.astimezone(_zone(zone or "UTC")).date()
    left = CHECKPOINT_WORKING_DAYS
    while left:
        day += timedelta(days=1)
        if day.weekday() < 5:
            left -= 1
    return f"{_DAY_NAMES[day.weekday()]} {day.day} {_MONTH_NAMES[day.month - 1]}"


def first_week_checkpoint(
    launched_at: datetime, *, autopilot: bool, zone: str, emails: bool = True,
) -> str:
    """What the first week shows, and by when. ``emails`` is False on a
    self-hosted install, which has no one to send the email."""
    held = "sent" if autopilot else "waiting for your approval"
    text = (
        f"By {checkpoint_date(launched_at, zone)} you will see invitations sent, the "
        f"first acceptances, and the first opening messages {held}."
    )
    if emails:
        text += " HeyLead emails you when the first person accepts."
    return text


async def _hosted_plan(campaign_id: str) -> dict[str, Any]:
    """The api's plan payload for a hosted account; {} when not hosted or it failed."""
    from .. import config

    if not (config.is_backend_mode() and campaign_id):
        return {}
    try:
        from urllib.parse import quote

        from .cloud_sync import get_hosted_json

        data = await get_hosted_json(
            f"/api/v1/campaigns/{quote(str(campaign_id), safe='')}/plan",
        )
    except Exception as exc:  # the plan must never cost the reply
        logger.info("Hosted plan for %s failed, rendering locally: %s", campaign_id, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _checkpoint_from(payload: dict[str, Any]) -> str:
    raw = payload.get("checkpoint")
    if isinstance(raw, dict):
        raw = raw.get("text")
    return str(raw or "").strip()


async def _local_plan_text(campaign_id: str, campaign: dict[str, Any] | None) -> str:
    row = campaign
    if row is None and campaign_id:
        try:
            from ..db.async_bridge import run_db
            from ..db.queries import get_campaign

            row = await run_db(get_campaign, campaign_id)
        except Exception as exc:
            logger.info("Campaign %s unreadable for the plan: %s", campaign_id, exc)
    try:
        state = _local_state()
        seat = _local_seat()
    except Exception as exc:
        logger.info("Local plan settings unreadable: %s", exc)
        state, seat = {}, {}
    return render_plan(campaign_plan(row or {}, state, seat))


def _local_checkpoint(now: datetime) -> str:
    from .. import config

    try:
        state = _local_state()
    except Exception as exc:
        logger.info("Local plan settings unreadable: %s", exc)
        state = {}
    hosted = config.is_backend_mode()
    zone = str(effective_working_hours(state).get("timezone") or "")
    if not hosted and not str(state.get("timezone") or "").strip():
        # A self-hosted install with no zone set: this machine's own.
        zone = str(getattr(datetime.now().astimezone().tzinfo, "key", "") or zone)
    autopilot = str(state.get("send_approval_mode") or "") == MODE_AUTOPILOT
    return first_week_checkpoint(now, autopilot=autopilot, zone=zone, emails=hosted)


async def plan_text_for(campaign_id: str, campaign: dict[str, Any] | None = None) -> str:
    """The rendered plan for one campaign: the api's when hosted, else the twin's.

    The api's text stands as it comes: on a workspace with history of its own
    its Accept and Reply lines quote that history instead of the founder
    cohort. Never raises: a plan that cannot be fetched falls back to the
    local computation, and a campaign this install cannot read gets the
    defaults.
    """
    text = str((await _hosted_plan(campaign_id)).get("text") or "").strip()
    return text or await _local_plan_text(campaign_id, campaign)


async def plan_and_checkpoint_for(
    campaign_id: str,
    campaign: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> tuple[str, str]:
    """The plan and the first-week checkpoint, for the launch result.

    Hosted: both from GET /campaigns/{id}/plan (``text`` and ``checkpoint``),
    each computed here when the api did not send it. ``now`` is for tests.
    """
    payload = await _hosted_plan(campaign_id)
    text = str(payload.get("text") or "").strip()
    if not text:
        text = await _local_plan_text(campaign_id, campaign)
    checkpoint = _checkpoint_from(payload)
    if not checkpoint:
        checkpoint = _local_checkpoint(now or datetime.now(timezone.utc))
    return text, checkpoint
