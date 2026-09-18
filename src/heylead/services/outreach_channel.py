"""First-touch channel picker — one rule for planner and send.

Regular LinkedIn DMs still require a 1st-degree connection. First-touch
InMail is Open Profile only (zero credit). Credit InMail is the 14-day
fallback after an invite, not the first touch for an unknown-open stranger.
"""

from __future__ import annotations

from typing import Any, Literal

FirstTouch = Literal["dm", "inmail", "invite", "skip"]

# Campaign setting key, shared verbatim with heylead-api (9 Sep 2026).
# Default OFF: turning it on for every existing campaign would silently stop
# outreach to people the customer meant to reach.
EXCLUDE_CONNECTIONS_KEY = "exclude_connections"
# The one refusal string. Client `enrol_rejected` reason and the
# `last_attempt_error` written at send time; the backend funnel key is
# `refused_first_degree`, built from this same word.
FIRST_DEGREE_REASON = "first_degree"


def exclude_connections_enabled(config: dict[str, Any] | None) -> bool:
    """True when this campaign must never reach a pre-existing connection."""
    from ..flags import flag_enabled

    return flag_enabled(config or {}, EXCLUDE_CONNECTIONS_KEY, default=False)


def first_touch_inmail_enabled(config: dict[str, Any] | None) -> bool:
    """First-touch InMail, defaulting to inmail_fallback when unset."""
    cfg = config or {}
    if "inmail_first_touch" in cfg:
        return cfg.get("inmail_first_touch") in (True, "on", "true", "1", 1)
    raw = cfg.get("inmail_fallback", True)
    return raw not in (False, "off", "false", "0", 0)


def fallback_inmail_enabled(config: dict[str, Any] | None) -> bool:
    raw = (config or {}).get("inmail_fallback", True)
    return raw not in (False, "off", "false", "0", 0)


def choose_first_touch(
    *,
    is_first_degree: bool,
    can_send_credit_inmail: bool,
    is_open_profile: bool = False,
    has_provider_id: bool = True,
    exclude_first_degree: bool = False,
) -> FirstTouch:
    """Pick dm / inmail / invite for a prospect's first LinkedIn touch.

    Open Profile InMails cost zero credits, so both tiers may use them as
    first touch. A Premium seat alone is not enough — credit InMail to an
    unknown-open stranger 422s and parked the invite queue. Missing
    provider_id cannot InMail. ``can_send_credit_inmail`` is kept so callers
    do not break; it no longer selects first-touch InMail.
    """
    if is_first_degree:
        # `exclude_first_degree` is the campaign's exclude_connections setting
        # already resolved against *this* prospect: pre-existing connection,
        # not merely 1st-degree today. Acme, 9 Sep 2026 — before this,
        # "connected" only ever meant "DM instead of invite", so a campaign
        # that was supposed to leave connections alone still messaged them,
        # just through a different channel.
        return "skip" if exclude_first_degree else "dm"
    if not has_provider_id:
        return "invite"
    if is_open_profile:
        return "inmail"
    return "invite"
