"""Tier capability matrix — every free-vs-Sales-Navigator difference, in one place.

The free-tier contract: free accounts get everything — campaigns, invites
(200-char notes), DMs, engagement warm-ups, classic search (50 x 15 pages),
and zero-cost InMails to Open Profile members. Sales Navigator adds 300-char
invitation notes, SN search depth and filters (100 x 25 pages = 2,500
results via api=sales_navigator), credit InMail, and the SN invite pace
(hundreds of connection requests a day). Premium raises the daily and
weekly invite ceilings above the classic-free ~100/week. Open Profile
InMail is not tier-varying: both tiers may always send Open Profile InMails.

`caps_for` is pure; the numbers it reports are read from their owning modules
(`constants` for note lengths and daily caps, `services.icp_search` for page
sizes) so each number keeps a single home. `get_caps` is the one place the
stored `has_sales_navigator` setting is read and coerced — legacy rows hold
"true"/"false" strings, so callers must never truth-test the raw value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import constants
from .services import icp_search


def as_bool(value: Any) -> bool:
    """Coerce a stored setting to bool — legacy rows hold "true"/"false" strings.

    Delegates to flags.py's truth tables so 'on'/'off' — the edit_campaign
    idiom used everywhere else in this codebase — coerce correctly instead of
    silently reading as False.
    """
    from .flags import _FALSE, _TRUE

    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        return False
    return bool(value)


@dataclass(frozen=True)
class TierCaps:
    """What one tier may do. Frozen: capabilities are facts, not knobs.

    Only fields with production readers live here. Search depth deliberately
    does NOT: it is governed by the *search* account's tier
    (premium_search_has_sn, via resolve_search_account), which is a different
    account by design — a field keyed on the sending account's flag could
    never be correct for it and would silently downgrade pooled-search setups.
    """

    invite_note_max_chars: int
    can_send_credit_inmail: bool
    profile_view_daily_cap: int
    invite_daily_cap: int


# Learned InMail capability, persisted: "" / "unknown" (never tried),
# "works" (a send landed), "refused" (LinkedIn declined on entitlement
# grounds). Whether Unipile can send a Premium-seat InMail is not answered by
# any endpoint we have found — inmail_balance reports null on every product —
# so this records what an actual attempt proved rather than guessing.
INMAIL_CAPABILITY_KEY = "inmail_send_capability"
INMAIL_WORKS = "works"
INMAIL_REFUSED = "refused"


def caps_for(
    has_sales_navigator: bool,
    has_premium: bool | None = None,
    inmail_capability: str = "",
) -> TierCaps:
    """The capability row for a tier. Pure — pass the *stored, coerced* flag.

    Open Profile InMail is not a field because it is not tier-varying:
    both tiers may always send them (they cost zero credits).

    Premium is deliberately NOT treated as Sales Navigator anywhere else: it
    grants InMail credits, not 300-char invitation notes and not SN search
    depth.

    ``has_premium`` is tri-state and the None case is the common one, not an
    edge: hosted installs have no way to read entitlements at all, since the
    backend's only tier endpoint reports the seat. Defaulting it to False
    would answer "no premium" on behalf of a client that was never able to
    ask — which is what kept a premium account's credits unreachable through
    the release meant to free them.
    """
    sn = bool(has_sales_navigator)
    return TierCaps(
        invite_note_max_chars=constants.invitation_note_max_chars(sn),
        # Entitlement decides whether an InMail is worth ATTEMPTING —
        # Premium grants InMail credits too, and gating on the Sales
        # Navigator seat alone meant a Premium account could never spend
        # them. Unknown permits the attempt; only a *confirmed* absence of
        # both refuses it, and a recorded refusal overrides either way
        # because LinkedIn has already answered.
        can_send_credit_inmail=(
            (sn or has_premium is not False)
            and inmail_capability != INMAIL_REFUSED
        ),
        profile_view_daily_cap=(
            constants.DAILY_CAP_PROFILE_VIEWS_SALES_NAV
            if sn
            else constants.DAILY_CAP_PROFILE_VIEWS
        ),
        # Confirmed-free stays under ~100/week. SN uses the SN daily pace.
        # Unknown premium uses the paid daily pace — hosted installs often
        # cannot read the flag.
        invite_daily_cap=(
            constants.DAILY_CAP_INVITATIONS_FREE
            if (not sn and has_premium is False)
            else (
                constants.DAILY_CAP_INVITATIONS_SALES_NAV
                if sn
                else constants.DAILY_CAP_INVITATIONS
            )
        ),
    )


# The stored flag changes daily at most (SN_REDETECT_SECONDS) or on account
# switch; a short TTL keeps hot paths (per-tick planning, per-action cap
# checks) off the single-worker DB thread without invalidation coupling —
# staleness is bounded to one tick.
_CAPS_TTL_SECONDS = 60
_caps_cache: dict = {}


async def get_caps() -> TierCaps:
    """Capability row for the *sending* account, from the stored tier flag."""
    import time

    from .db.async_bridge import run_db
    from .db.queries import get_setting

    now = time.time()
    if _caps_cache.get("caps") is not None and (
        now - _caps_cache.get("at", 0)
    ) < _CAPS_TTL_SECONDS:
        return _caps_cache["caps"]

    stored = await run_db(get_setting, "has_sales_navigator", False)
    # None means no client has ever been able to report it — kept distinct
    # from a stored False, which is an answer /users/me actually gave.
    premium_raw = await run_db(get_setting, "has_linkedin_premium", None)
    premium = None if premium_raw is None else as_bool(premium_raw)
    capability = str(await run_db(get_setting, INMAIL_CAPABILITY_KEY, "") or "")
    caps = caps_for(as_bool(stored), premium, capability)
    _caps_cache["caps"] = caps
    _caps_cache["at"] = now
    return caps
