"""Proactive rate limiting — enforces daily caps to prevent LinkedIn bans.

Each action type has a hard daily cap defined in constants.py.
The scheduler checks caps BEFORE executing any action.
A total daily budget limits cumulative visible LinkedIn actions.

Also provides:
- Pacing helpers (delay between sends)
- Pending invitation cache + withdrawal utility
- Ban risk monitoring with graduated warnings
"""

from __future__ import annotations

import logging
import random
import time
from datetime import date
from typing import Any

from .. import constants
from ..db import aio as db
from ..db.async_bridge import run_db
from ..timeutil import to_epoch

logger = logging.getLogger(__name__)

# Block type constants
BLOCK_NONE = ""
BLOCK_TIME = "time"
BLOCK_DAILY = "daily"

# Cap / ban-risk warnings once per (action, calendar day), then DEBUG.
# The in-memory set dies on each engine stop (44 times in the log window);
# persist to a file so a daemon restart does not reprint BAN RISK CRITICAL.
_cap_warn_logged: set[tuple[str, str]] = set()
_cap_warn_loaded = False
_CAP_WARN_FILE = "cap_warn_logged.json"


def _cap_warn_path():
    from ..config import _heylead_home

    return _heylead_home() / _CAP_WARN_FILE


def _load_persisted_cap_tokens() -> set[tuple[str, str]]:
    import json as _json

    path = _cap_warn_path()
    try:
        data = _json.loads(path.read_text())
    except Exception:
        return set()
    today = date.today().isoformat()
    if not isinstance(data, dict):
        return set()
    return {(str(k), str(v)) for k, v in data.items() if v == today}


def _persist_cap_token(key: str, day: str) -> None:
    import json as _json

    path = _cap_warn_path()
    try:
        data = _json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[key] = day
    data = {k: v for k, v in data.items() if v == day}
    try:
        path.write_text(_json.dumps(data))
    except OSError:
        pass


def _log_cap_once(key: str, msg: str, *args: object) -> None:
    global _cap_warn_loaded
    if not _cap_warn_loaded:
        _cap_warn_logged.update(_load_persisted_cap_tokens())
        _cap_warn_loaded = True
    token = (key, date.today().isoformat())
    if token in _cap_warn_logged:
        logger.debug(msg, *args)
        return
    _cap_warn_logged.add(token)
    _persist_cap_token(key, token[1])
    logger.warning(msg, *args)
BLOCK_WEEKLY = "weekly"
BLOCK_PENDING = "pending"
BLOCK_TOTAL = "total_daily"

# Map action types to their daily cap constant and action_log patterns
_ACTION_CAP_MAP: dict[str, tuple[int, list[str]]] = {
    "invite": (
        constants.DAILY_CAP_INVITATIONS,
        # `invitation_sent` is what generate_send logs on a successful invite.
        # `outreach_status_change` is deliberately NOT counted: it is logged with
        # result=<new_status>, never "success", and every invite writes one, so
        # counting it would double-count.
        ["invitation_sent", "engagement_invite"],
    ),
    "follow": (
        constants.DAILY_CAP_FOLLOWS,
        ["engagement_follow"],
    ),
    "profile_view": (
        constants.DAILY_CAP_PROFILE_VIEWS,
        ["engagement_profile_view"],
    ),
    "comment": (
        constants.DAILY_CAP_COMMENTS,
        ["engagement_comment"],
    ),
    "react": (
        constants.DAILY_CAP_REACTIONS,
        ["engagement_react", "engagement_react_fallback"],
    ),
    "engage": (
        # engage = comment + react combined
        constants.DAILY_CAP_COMMENTS + constants.DAILY_CAP_REACTIONS,
        ["engagement_comment", "engagement_react", "engagement_react_fallback"],
    ),
    "dm": (
        constants.DAILY_CAP_DMS,
        [
            "dm_sent", "reply_sent", "followup_sent",
            # Inbound / backfill log their own names. Leaving them out of this
            # list made those sends invisible: after the reservation TTL the
            # next tick saw 0/12 and fired another batch.
            "inbound_contextual_reply_sent", "inbound_discovery_dm_sent",
        ],
    ),
    "auto_reply": (
        constants.DAILY_CAP_AUTO_REPLIES,
        ["auto_reply_sent"],
    ),
    "email": (
        # Not a LinkedIn action, so deliberately absent from _VISIBLE_PATTERNS —
        # it must not spend the DAILY_CAP_TOTAL_ACTIONS ceiling, which exists to
        # bound what LinkedIn itself can observe.
        constants.DAILY_CAP_EMAILS,
        # Every path that sends an email logs one of these. send_followup logs
        # email_followup_sent, so counting only email_sent would let the
        # highest-volume path run outside the ceiling it is supposed to be in.
        ["email_sent", "email_followup_sent"],
    ),
    "followup": (
        constants.DAILY_CAP_DMS,
        [
            "dm_sent", "reply_sent", "followup_sent",
            "inbound_contextual_reply_sent", "inbound_discovery_dm_sent",
        ],
    ),
    "withdraw": (
        constants.DAILY_CAP_WITHDRAWALS,
        ["stale_invite_withdrawn", "invite_withdrawn_to_free_spot"],
    ),
    "inmail": (
        constants.DAILY_CAP_INMAILS,
        ["inmail_sent"],
    ),
}

# Cache for today's action counts, refreshed every _CACHE_TTL seconds.
_daily_counts_cache: dict[str, Any] = {}
_CACHE_TTL = 300  # seconds

# Slots handed out live in the daily_slot_reservations table, not in module
# state. The cached counts cannot see an action taken moments ago — the row is
# written after the action completes, and engage jobs run concurrently, so
# every check in the window read the same number and all of them passed; four
# reactions landed inside one second that way, ending the day at 24 against a
# cap of 20. An in-memory ledger fixed that within a process and nothing
# across them, so the daemon and each MCP session spent a full cap apiece.

# How long a booked slot keeps counting before it is forgiven.
#
# A reservation stands in for an action whose actions_log row has not appeared
# yet, so it must outlive the window in which the logged counts could still be
# blind to it — that window is _CACHE_TTL. Past this, a slot booked for an
# action that then failed is returned, exactly as clearing the in-memory ledger
# used to do.
#
# The overlap is deliberate and one-directional: for a few minutes after an
# action lands, it is counted both as a logged action and as a live
# reservation. That biases the cap towards sending less, which is the safe
# direction for a limit whose purpose is avoiding account restriction.
_RESERVATION_TTL_SECONDS = _CACHE_TTL + 120


def _today_key() -> str:
    """Local calendar day, stamped on a reservation row for readability.

    Nothing decides anything from it. Caps are measured over a rolling 24-hour
    window at both ends — see _get_daily_action_counts — so this is a label for
    whoever reads the table by hand, not a boundary.
    """
    import datetime as dt
    return dt.date.today().isoformat()


def _reserve_slot(
    patterns: list[str],
    cap: int,
    logged_current: int,
    visible_patterns: list[str],
    total_cap: int,
    logged_total: int,
    ttl_seconds: int,
    reserve: bool = True,
) -> tuple[bool, int, int, str]:
    """Decide and book in one serialized transaction.

    Returns (allowed, current, effective_cap, block_reason). Sync on purpose:
    call it through ``run_db``.

    BEGIN IMMEDIATE takes SQLite's write lock, so concurrent processes queue
    here rather than all reading the same remaining slot. Checking and booking
    on either side of that lock is what makes the cap hold across processes.
    """
    from ..db.schema import get_db

    day = _today_key()
    db = get_db()
    try:
        # The connection is a process singleton whose close() is a no-op. A
        # prior write that raised before commit leaves it inside a failed
        # transaction, and BEGIN IMMEDIATE cannot nest. That leftover is
        # not ours to finish — roll it back so the cap check can take the
        # write lock instead of raising and looking like a send error.
        if db.in_transaction:
            db.rollback()
        db.execute("BEGIN IMMEDIATE")

        # Expire by age only. The counts these reservations top up come from
        # get_actions_taken(24), a rolling 24-hour window, and the two halves of
        # one cap have to measure the same thing. Clearing on `day != ?` threw
        # a reservation away the moment the local date rolled, however fresh it
        # was — so for the first minutes of every local day the cover for
        # logging lag vanished and each process could re-spend slots it had
        # already booked. The `day` column is still written because it makes
        # the table readable by hand; nothing decides anything from it.
        cutoff = int(time.time()) - ttl_seconds
        db.execute(
            "DELETE FROM daily_slot_reservations WHERE created_at < ?",
            (cutoff,),
        )

        def _reserved(for_patterns: list[str]) -> int:
            if not for_patterns:
                return 0
            placeholders = ",".join("?" for _ in for_patterns)
            row = db.execute(
                f"SELECT COUNT(*) AS n FROM daily_slot_reservations "
                f"WHERE created_at >= ? AND pattern IN ({placeholders})",
                (cutoff, *for_patterns),
            ).fetchone()
            return int(row["n"] if row else 0)

        current = logged_current + _reserved(patterns)
        if current >= cap:
            db.commit()
            return False, current, cap, BLOCK_DAILY

        total = logged_total + _reserved(visible_patterns)
        if total >= total_cap:
            db.commit()
            return False, total, total_cap, BLOCK_TOTAL

        if reserve and patterns:
            db.execute(
                "INSERT INTO daily_slot_reservations (day, pattern, created_at) "
                "VALUES (?, ?, ?)",
                (day, patterns[0], int(time.time())),
            )
        db.commit()
        # `current` excludes the slot just booked: callers report it as usage
        # so far, and the old in-memory ledger had the same contract.
        return True, current, cap, BLOCK_NONE
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def _get_daily_action_counts() -> dict[str, int]:
    """Action counts over the last 24 hours, with a short cache.

    "Daily" here is a rolling 24-hour window, not the calendar day the name
    suggests — ``get_actions_taken(24)`` counts back from now. That is
    deliberate and is the stricter of the two readings: the actions taken since
    local midnight are always a subset of those taken in the last 24 hours, so
    a calendar-day count can never exceed this one. Switching to a calendar day
    would let a burst just before midnight be followed by a full fresh
    allowance just after it, which is the wrong direction for a limit whose
    purpose is avoiding an account restriction.

    The visible consequence is that slots free up 24 hours after they were
    spent rather than at midnight, so a day's work stays in whatever band of
    the clock it first occupied. That costs no throughput.
    """
    now = time.time()
    if (
        _daily_counts_cache.get("data") is not None
        and (now - _daily_counts_cache.get("fetched_at", 0)) < _CACHE_TTL
    ):
        return _daily_counts_cache["data"]

    from ..db.queries import get_actions_taken
    actions = await run_db(get_actions_taken, 24)

    # Flatten to {action_type: success_count}
    counts: dict[str, int] = {}
    for action_type, breakdown in actions.items():
        # Count only successful actions
        success = breakdown.get("success", 0)
        counts[action_type] = success

    # Also get engagement counts from engagements table (more accurate)
    from ..db.queries import get_daily_engagement_counts_all
    eng_counts = await run_db(get_daily_engagement_counts_all)
    for action_type, count in eng_counts.items():
        key = f"engagement_{action_type}"
        counts[key] = max(counts.get(key, 0), count)

    # Pending engagements are already in actions_log — they are logged when
    # sent, verification happens later. Adding them on top of the log count
    # spends the daily budget twice for every unverified action, so take the
    # larger of the two views instead.
    from ..db.queries import get_daily_engagement_counts_pending
    pend_counts = await run_db(get_daily_engagement_counts_pending)
    for action_type, count in pend_counts.items():
        key = f"engagement_{action_type}"
        verified = eng_counts.get(action_type, 0)
        counts[key] = max(counts.get(key, 0), verified + count)

    _daily_counts_cache["data"] = counts
    _daily_counts_cache["fetched_at"] = now
    return counts


def _count_for_action(
    counts: dict[str, int],
    patterns: list[str],
    reserved: dict[str, int] | None = None,
) -> int:
    """Sum counts matching any of the given patterns, plus unreflected slots."""
    total = 0
    for pattern in patterns:
        total += counts.get(pattern, 0)
        if reserved:
            total += reserved.get(pattern, 0)
    return total


def _active_reservation_counts() -> dict[str, int]:
    """Live bookings per pattern. Sync: call through ``run_db``."""
    from ..db.schema import get_db

    db = get_db()
    try:
        cutoff = int(time.time()) - _RESERVATION_TTL_SECONDS
        rows = db.execute(
            "SELECT pattern, COUNT(*) AS n FROM daily_slot_reservations "
            "WHERE day = ? AND created_at >= ? GROUP BY pattern",
            (_today_key(), cutoff),
        ).fetchall()
        return {r["pattern"]: int(r["n"]) for r in rows}
    finally:
        db.close()


# Every action a person could see on LinkedIn, for the combined daily budget.
_VISIBLE_PATTERNS = [
    "engagement_follow", "engagement_profile_view",
    "engagement_comment", "engagement_react", "engagement_react_fallback",
    "invitation_sent", "engagement_invite",
    "dm_sent", "reply_sent", "followup_sent", "auto_reply_sent",
    "inbound_contextual_reply_sent", "inbound_discovery_dm_sent",
    "stale_invite_withdrawn", "invite_withdrawn_to_free_spot",
    "inmail_sent",
]


def _total_visible_actions(
    counts: dict[str, int], reserved: dict[str, int] | None = None
) -> int:
    """Sum all visible LinkedIn actions for today."""
    visible_patterns = _VISIBLE_PATTERNS
    # NOTE: do not read rate_limits here. This runs inside an async caller, and
    # the sync get_rate_limit_today() raises on the event-loop thread — the old
    # try/except silently swallowed it, so invitations never reached the total.
    # `invitation_sent` above is the accurate source.
    return _count_for_action(counts, visible_patterns, reserved)


def _synced_invites_today() -> int:
    """Today's invite count as reported by the cloud, read-only.

    Deliberately NOT get_rate_limit_today(): that helper is a get-or-CREATE and
    its INSERT/commit collides with the BEGIN IMMEDIATE that _reserve_slot
    takes moments later — "cannot start a transaction within a transaction",
    which broke the multi-process contention test. A cap check must never write.
    """
    import datetime

    from ..db.schema import get_db

    # The two counters measure different windows and cannot be reconciled
    # exactly: this cap counts a ROLLING 24 hours (get_actions_taken(24)),
    # while rate_limits is keyed by CALENDAR day. Reading only today's row
    # left a blind spot for the first hours after local midnight — cloud
    # invites sent late yesterday are absent from actions_log entirely and
    # from a freshly-reset calendar row, so both counters saw zero.
    #
    # MAX over the two days that a rolling 24h window can touch, not SUM:
    # summing would block a legitimate day's sends whenever two partial days
    # overlap. Max still under-counts a split cloud day, so this narrows the
    # gap rather than closing it. Closing it properly means cloud-executed
    # sends writing actions_log rows so one counter covers everything — the
    # same "two tallies for one ceiling" problem the email cap had.
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)

    db = get_db()
    try:
        rows = db.execute(
            "SELECT sent FROM rate_limits WHERE date IN (?, ?)",
            (today.isoformat(), yesterday.isoformat()),
        ).fetchall()
        return max((int((r["sent"] or 0)) for r in rows), default=0)
    finally:
        db.close()


async def check_daily_cap(
    action_type: str, *, reserve: bool = True,
) -> tuple[bool, int, int, str]:
    """Check if an action type is within its daily cap.

    Args:
        action_type: One of "invite", "follow", "profile_view", "comment",
                     "react", "engage", "dm", "auto_reply", "followup",
                     "withdraw", "inmail"
        reserve: When True (the send-gate default) book a slot if one
                 remains. When False, only read — informational probes
                 must not consume a slot they will never spend.

    Returns:
        (can_proceed, current_count, cap, block_reason)
    """
    cap_info = _ACTION_CAP_MAP.get(action_type)
    if not cap_info:
        # Unknown action type — allow but log
        logger.debug("No daily cap defined for action type: %s", action_type)
        return True, 0, 999, BLOCK_NONE

    cap, patterns = cap_info
    if action_type in ("profile_view", "invite"):
        # These two are tier-dependent. _ACTION_CAP_MAP is built at import
        # time, so the paid-seat allowance has to be looked up per call.
        from ..tier import get_caps

        caps = await get_caps()
        cap = (
            caps.profile_view_daily_cap
            if action_type == "profile_view"
            else caps.invite_daily_cap
        )
    counts = await _get_daily_action_counts()

    # Decide and book together, under SQLite's write lock, so the daemon and
    # every MCP session contend for the same slots instead of each spending a
    # full cap from its own memory. Only the logged counts come from this
    # process; the reservations that cover the logging lag are shared.
    logged_current = sum(counts.get(p, 0) for p in patterns)
    logged_total = sum(counts.get(p, 0) for p in _VISIBLE_PATTERNS)

    # Belt and braces, no longer the load-bearing guard.
    #
    # This floor was added (v0.10.250) because cloud-executed invites wrote
    # rate_limits.sent but NO actions_log row, so the local cap counted zero and
    # re-approved a full DAILY_CAP_INVITATIONS on top of what the cloud had
    # already sent. PR #141 (v0.10.251) closed that at the source: pull_changes
    # now writes an `invitation_sent` row for each cloud-executed invite, so the
    # normal path is covered by the same counter as everything else.
    #
    # Kept because it costs one indexed read and covers the window where a cloud
    # send has been reported but its actions_log backfill has not landed. Taken
    # as a FLOOR, never a replacement: whichever source reports more wins.
    #
    # The calendar-day/rolling-window mismatch below is therefore a narrowing
    # detail on a backup guard, not an open hole in the cap.
    if action_type == "invite":
        try:
            synced = await run_db(_synced_invites_today)
            if synced > logged_current:
                logger.debug(
                    "invite cap: cloud reports %d sends, local log has %d — "
                    "using the cloud figure", synced, logged_current,
                )
                logged_current = synced
        except Exception as e:
            # Unreadable is not zero, but it is also not a reason to block a
            # send outright; the logged count still applies.
            logger.debug("could not read the synced invite counter: %s", e)
    allowed, current, effective_cap, block = await run_db(
        _reserve_slot,
        patterns,
        cap,
        logged_current,
        _VISIBLE_PATTERNS,
        constants.DAILY_CAP_TOTAL_ACTIONS,
        logged_total,
        _RESERVATION_TTL_SECONDS,
        reserve,
    )

    if not allowed:
        if block == BLOCK_DAILY:
            _log_cap_once(
                f"daily:{action_type}",
                "Daily cap reached for %s: %d/%d — blocking action",
                action_type, current, effective_cap,
            )
        else:
            _log_cap_once(
                f"total:{action_type}",
                "Total daily action budget exhausted: %d/%d — blocking %s",
                current, effective_cap, action_type,
            )
        return False, current, effective_cap, block

    total = logged_total

    # Ban risk warnings
    pct = current / cap if cap > 0 else 0
    if pct >= constants.BAN_RISK_CRITICAL_PCT:
        _log_cap_once(
            f"ban:{action_type}",
            "BAN RISK CRITICAL: %s at %.0f%% of daily cap (%d/%d)",
            action_type, pct * 100, current, cap,
        )
    elif pct >= constants.BAN_RISK_WARNING_PCT:
        logger.info(
            "Ban risk warning: %s at %.0f%% of daily cap (%d/%d)",
            action_type, pct * 100, current, cap,
        )

    total_pct = total / constants.DAILY_CAP_TOTAL_ACTIONS if constants.DAILY_CAP_TOTAL_ACTIONS > 0 else 0
    if total_pct >= constants.BAN_RISK_CRITICAL_PCT:
        _log_cap_once(
            "ban:total",
            "BAN RISK CRITICAL: total actions at %.0f%% (%d/%d)",
            total_pct * 100, total, constants.DAILY_CAP_TOTAL_ACTIONS,
        )
    elif total_pct >= constants.BAN_RISK_WARNING_PCT:
        logger.info(
            "Ban risk warning: total actions at %.0f%% (%d/%d)",
            total_pct * 100, total, constants.DAILY_CAP_TOTAL_ACTIONS,
        )

    # The slot is already booked: _reserve_slot did it inside the transaction
    # that approved this call.
    return True, current, cap, BLOCK_NONE


async def get_daily_cap_summary() -> dict[str, dict[str, int]]:
    """Get a summary of all daily caps and current usage.

    Returns: {action_type: {"current": N, "cap": N, "remaining": N, "pct": N}}
    """
    counts = await _get_daily_action_counts()
    reserved = await run_db(_active_reservation_counts)
    summary: dict[str, dict[str, int]] = {}

    from ..tier import get_caps
    tier_caps = await get_caps()

    for action_type, (cap, patterns) in _ACTION_CAP_MAP.items():
        if action_type == "engage":
            continue  # Skip composite — show comment/react separately
        if action_type == "profile_view":
            # Report the number check_daily_cap actually enforces — the
            # operator-facing safety table must not show 35 while the
            # scheduler runs to a Sales Navigator 50.
            cap = tier_caps.profile_view_daily_cap
        elif action_type == "invite":
            cap = tier_caps.invite_daily_cap
        current = _count_for_action(counts, patterns, reserved)
        summary[action_type] = {
            "current": current,
            "cap": cap,
            "remaining": max(0, cap - current),
            "pct": int((current / cap * 100) if cap > 0 else 0),
        }

    total = _total_visible_actions(counts, reserved)
    summary["_total"] = {
        "current": total,
        "cap": constants.DAILY_CAP_TOTAL_ACTIONS,
        "remaining": max(0, constants.DAILY_CAP_TOTAL_ACTIONS - total),
        "pct": int((total / constants.DAILY_CAP_TOTAL_ACTIONS * 100)
                    if constants.DAILY_CAP_TOTAL_ACTIONS > 0 else 0),
    }
    return summary


def invalidate_daily_cache() -> None:
    """Force refresh of daily counts on next check (call after action succeeds)."""
    _daily_counts_cache.clear()


def log_cloud_executed_send(
    action_type: str,
    outreach_id: str,
    *,
    timestamp: int | None = None,
    campaign_id: str = "",
    details: dict[str, Any] | None = None,
) -> str | None:
    """Write the actions_log row for a send the cloud already executed.

    ``check_daily_cap`` counts ``invitation_sent`` / ``dm_sent`` here, not
    ``rate_limits.sent``. A cloud invite that only incremented the backend
    counter was invisible to the local cap, so this machine could approve a
    full day on top. One row, one counter, both writers.

    Invites are one per outreach — a second call is a no-op. DMs may repeat;
    pass a ``cloud_id`` in ``details`` so a replay of the same message is not
    a second send. The timestamp is when the cloud sent, not when we heard.
    """
    from ..db.queries import log_action
    from ..db.schema import get_db

    payload = dict(details) if details else {}
    payload.setdefault("source", "cloud")
    cloud_id = str(payload.get("cloud_id") or "")

    db = get_db()
    try:
        if action_type == "invitation_sent" and outreach_id:
            existing = db.execute(
                "SELECT id FROM actions_log "
                "WHERE outreach_id = ? AND action_type = ? LIMIT 1",
                (outreach_id, action_type),
            ).fetchone()
            if existing:
                return None
        elif cloud_id and outreach_id:
            existing = db.execute(
                "SELECT id FROM actions_log "
                "WHERE outreach_id = ? AND action_type = ? "
                "AND details_json LIKE ? LIMIT 1",
                (outreach_id, action_type, f'%"cloud_id": "{cloud_id}"%'),
            ).fetchone()
            if existing:
                return None
    finally:
        db.close()

    action_id = log_action(
        action_type,
        outreach_id=outreach_id,
        result="success",
        details=payload,
        campaign_id=campaign_id,
        timestamp=timestamp,
    )
    invalidate_daily_cache()
    return action_id


def backfill_unlogged_cloud_invites(max_age_hours: int = 48) -> int:
    """Log invitation_sent for local invited_at rows the cloud wrote first.

    After the pull no-op guard, an invite the client already applied never
    comes back in ``outreach_updates``. Those rows still have no actions_log
    entry until something writes one. Called at the end of every pull.
    """
    from ..db.schema import get_db

    since = int(time.time()) - max_age_hours * 3600
    db = get_db()
    try:
        rows = db.execute(
            "SELECT o.id, o.invited_at, o.campaign_id FROM outreaches o "
            "WHERE o.invited_at IS NOT NULL AND o.invited_at >= ? "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM actions_log a "
            "  WHERE a.outreach_id = o.id AND a.action_type = 'invitation_sent'"
            ")",
            (since,),
        ).fetchall()
    finally:
        db.close()

    written = 0
    for row in rows:
        if log_cloud_executed_send(
            "invitation_sent",
            row["id"],
            timestamp=int(row["invited_at"]),
            campaign_id=row["campaign_id"] or "",
            details={"source": "cloud", "via": "invited_at_reconcile"},
        ):
            written += 1
    return written


# ──────────────────────────────────────────────
# Backwards-compatible API (used by existing callers)
# ──────────────────────────────────────────────

async def can_send_now(
    *, skip_time_check: bool = False, reserve: bool = True
) -> tuple[bool, str, str]:
    """Check if we can send an invitation now (respects daily cap)."""
    can, current, cap, block = await check_daily_cap("invite", reserve=reserve)
    if not can:
        return False, f"Daily invitation cap reached ({current}/{cap})", block
    return True, "", BLOCK_NONE


async def can_send_email_now(*, reserve: bool = True) -> tuple[bool, str]:
    """Whether today's email budget still has room.

    This used to be a stub returning True unconditionally. Email is the
    *overflow* channel — once the LinkedIn caps are reached, select_channel
    routes every remaining prospect with an address here and _send_gate_open
    judges the send on this answer alone — so an unconditional True meant
    hitting the LinkedIn cap at midday drained the rest of the pending queue as
    cold email, with nothing counting it and nothing stopping it.

    Counted through check_daily_cap("email") rather than a tally of its own.
    There used to be two: this function read email_rate_limits.sent while
    _ACTION_CAP_MAP["email"] counted actions_log rows that nothing consulted.
    Two counters for one ceiling disagree the moment a path updates one and not
    the other — which is what happened, since send_followup logged
    email_followup_sent and incremented neither.
    """
    ok, current, cap, _block = await check_daily_cap("email", reserve=reserve)
    if not ok:
        return False, f"Daily email cap reached ({current}/{cap})"
    return True, ""


async def increment_email_sent() -> None:
    """Track an email send in the email rate limits table."""
    from ..db.queries import increment_email_sent as _inc
    await run_db(_inc)


def get_next_delay() -> int:
    """Get randomized delay in seconds for the next send."""
    min_secs = constants.MIN_DELAY_MINUTES * 60
    max_secs = constants.MAX_DELAY_MINUTES * 60
    return random.randint(min_secs, max_secs)


async def update_limits_after_send(blocked: bool = False) -> int:
    """Invalidate cache after a send. Returns 0."""
    invalidate_daily_cache()
    return 0


async def check_engagement_budget(
    engagement_type: str = "",
    *,
    reserve: bool = True,
) -> tuple[bool, int, int]:
    """Check engagement budget using daily caps.

    Maps engagement_type to the appropriate cap check.
    Returns (has_budget, current, limit).
    """
    # Map engagement_type to our cap action types
    type_map = {
        "comment": "comment",
        "react": "react",
        "follow": "follow",
        "profile_view": "profile_view",
        "view": "profile_view",
        "endorse": "follow",  # shares follow cap
        "engage": "engage",
        "": "engage",  # default
    }
    action = type_map.get(engagement_type, "engage")
    can, current, cap, _ = await check_daily_cap(action, reserve=reserve)
    return can, current, cap


async def check_pending_limit(
    client: Any, account_id: str
) -> tuple[bool, str, str]:
    """Always returns can_send=True — no proactive pending limit."""
    return True, "", BLOCK_NONE


async def estimate_weekly_limit_reset() -> tuple[bool, int, str]:
    """Always returns not-at-limit — no weekly cap."""
    return False, 0, ""


def hosted_weekly_invite_cap(rate_limits: dict[str, Any] | None) -> int:
    """The weekly invitation cap the hosted sender actually enforces.

    Read from the stats/sync payload's ``weekly_cap`` when it carries a usable
    positive number, else HOSTED_WEEKLY_INVITE_CAP. Hosted accounts used to be
    shown the self-hosted scoring denominator (200) — "Weekly: 93/200" on a
    seat the backend stops at 100 (10 Sep 2026).
    """
    raw = (rate_limits or {}).get("weekly_cap")
    try:
        cap = int(raw)
    except (TypeError, ValueError):
        return constants.HOSTED_WEEKLY_INVITE_CAP
    return cap if cap > 0 else constants.HOSTED_WEEKLY_INVITE_CAP


async def _get_effective_caps() -> tuple[int, int]:
    """(weekly_cap, daily_max) for the health score and the operator's display.

    ``daily_max`` is the cap ``check_daily_cap("invite")`` will actually refuse
    a send at, read per call from ``tier.get_caps()`` (60s TTL). It used to be
    a hardcoded 30, which was neither tier's figure; once #236 made it the
    fallback for the service's null ``daily_limit`` it became the number
    printed to the operator, so a premium seat whose scheduler runs to its
    daily invite cap was shown "28/30 today".

    ``weekly_cap`` stays 200 and stays a scoring denominator. Nothing enforces
    a weekly ceiling here — ``estimate_weekly_limit_reset`` reports none and
    ``can_send_now`` never returns ``BLOCK_WEEKLY`` — so lowering it toward
    LinkedIn's real ~100 would light "weekly limit reached" branches that
    print an empty ETA and claim a block that does not exist. That needs a
    real weekly signal first, not a smaller invented number.
    """
    from ..tier import get_caps

    try:
        caps = await get_caps()
    except Exception as e:
        # Every caller renders a dashboard inside a broad except that turns a
        # raise into "backend unreachable" — never let a settings read do that.
        logger.debug("Falling back to the default daily cap: %s", e)
        return 200, constants.DAILY_CAP_INVITATIONS_FREE
    return 200, caps.invite_daily_cap


# ──────────────────────────────────────────────
# Pending invitation cache + withdrawal (utility, not rate limiting)
# ──────────────────────────────────────────────

_pending_cache: dict[str, Any] = {}


async def get_cached_pending_invitations(
    client: Any, account_id: str
) -> tuple[int, list[dict]]:
    """Return (count, invitation_list) with a 5-minute TTL cache."""
    now = time.time()
    fetched_at = _pending_cache.get("fetched_at", 0)
    # An entry belongs to the account it was fetched for. Serving it to another
    # account picks that account's invitation for withdrawal and gates its
    # sends on a count that is not its own.
    if (
        _pending_cache.get("invitations") is not None
        and _pending_cache.get("account_id") == account_id
        and (now - fetched_at) < constants.PENDING_CACHE_TTL_SECONDS
    ):
        invitations = _pending_cache["invitations"]
        return len(invitations), invitations

    try:
        invitations = await client.get_pending_invitations(account_id)
    except Exception as e:
        logger.warning("Failed to fetch pending invitations: %s", e)
        if (
            _pending_cache.get("invitations") is not None
            and _pending_cache.get("account_id") == account_id
        ):
            invitations = _pending_cache["invitations"]
            return len(invitations), invitations
        return 0, []

    _pending_cache["invitations"] = invitations
    _pending_cache["fetched_at"] = now
    _pending_cache["account_id"] = account_id
    return len(invitations), invitations


def invalidate_pending_cache() -> None:
    """Clear the pending invitation cache. Call after send or withdrawal."""
    _pending_cache.clear()


async def withdraw_oldest_to_free_spot(
    client: Any, account_id: str
) -> dict[str, Any]:
    """Withdraw the oldest pending invitation to free a spot."""
    # Check withdrawal daily cap first
    can, current, cap, _ = await check_daily_cap("withdraw")
    if not can:
        logger.warning("Daily withdrawal cap reached (%d/%d), skipping", current, cap)
        return {"success": False, "error": f"Daily withdrawal cap reached ({current}/{cap})"}

    count, invitations = await get_cached_pending_invitations(
        client, account_id
    )
    if not invitations:
        return {"success": False, "error": "No pending invitations to withdraw"}

    # Parse timestamps and sort oldest-first. An old invitation carries only a
    # fuzzy "Sent 5 months ago" string, which Unipile resolves against fetch
    # time, so a whole cohort lands on one timestamp and whole seconds separate
    # nothing at all — a stable sort then hands back the list order it was
    # given, which is newest-first. Bucket to the day, which is all the fuzzy
    # string supports, and break ties on the invitation id, a monotonic
    # LinkedIn snowflake and the only real age signal left.
    now_ts = int(time.time())
    candidates = []
    for inv in invitations:
        inv_time = to_epoch(
            inv.get("parsed_datetime")
            or inv.get("timestamp")
            or inv.get("created_at")
            or inv.get("date")
            or 0
        ) or 0
        inv_id = inv.get("id") or inv.get("invitation_id") or ""
        if inv_id and inv_time:
            raw_id = str(inv_id)
            candidates.append(
                (inv_time // 86400, int(raw_id) if raw_id.isdigit() else 0,
                 inv_time, inv_id, inv)
            )

    if not candidates:
        return {"success": False, "error": "No valid invitation candidates"}

    candidates.sort(key=lambda c: (c[0], c[1]))  # oldest first
    _day, _seq, oldest_time, oldest_id, oldest_inv = candidates[0]
    days_old = (now_ts - oldest_time) // 86400
    name = oldest_inv.get("invited_user") or oldest_inv.get("name") or ""

    try:
        result = await client.withdraw_invitation(account_id, oldest_id)
    except Exception as e:
        logger.warning("Failed to withdraw invitation %s: %s", oldest_id, e)
        return {"success": False, "error": str(e)}

    if result.get("success"):
        invalidate_pending_cache()
        invalidate_daily_cache()
        await db.log_action(
            "invite_withdrawn_to_free_spot",
            result="success",
            details={
                "invitation_id": oldest_id,
                "days_old": days_old,
                "name": name,
                "pending_count_before": count,
            },
        )
        logger.info(
            "Withdrew oldest invitation %s (%d days old, %s) to free spot",
            oldest_id,
            days_old,
            name,
        )
        return {
            "success": True,
            "invitation_id": oldest_id,
            "days_old": days_old,
            "name": name,
        }

    error = result.get("error", "Unknown error")
    logger.warning("Failed to withdraw invitation %s: %s", oldest_id, error)
    return {"success": False, "error": error}
