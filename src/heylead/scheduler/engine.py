"""Scheduler engine — asyncio-based background loop for autonomous outreach.

Runs alongside the MCP server via FastMCP's lifespan hook.
Ticks every 60 seconds, checking for ready work and executing it.

Key responsibilities:
1. Execute ready jobs from the DB-backed job queue
2. Schedule new work (invites, follow-ups) for autopilot campaigns
3. Periodically check for replies (every 5 min)
4. Periodically schedule follow warm-ups (every 15 min)
5. Periodically schedule engagement warm-ups (every 30 min)
6. Retry failed jobs (up to 3 attempts)
7. Cleanup old completed jobs (7 days)
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback as _tb
from typing import Any

from ..config import (
    get_scheduler_mode,
    is_scheduler_always_on,
    is_scheduler_enabled,
    load_config,
    set_scheduler_mode,
)
from ..constants import (
    JOB_SCAN_NETWORK_POSTS,
    BRAND_ENGAGE_CHECK_SECONDS,
    BRAND_LIFECYCLE_CHECK_SECONDS,
    BRAND_POST_CHECK_SECONDS,
    BRAND_PROFILE_CHECK_SECONDS,
    INBOUND_CHECK_SECONDS,
    INBOUND_PIPELINE_SECONDS,
    INBOUND_QUALIFY_SECONDS,
    POST_COMMENT_CHECK_SECONDS,
    SIGNAL_KEYWORD_POLL_SECONDS,
    SIGNAL_PROSPECT_SCAN_SECONDS,
    SIGNAL_CLASSIFY_SECONDS,
    SIGNAL_PROFILE_VIEW_SECONDS,
    SIGNAL_JOB_CHANGE_SCAN_SECONDS,
    SIGNAL_COMPETITOR_POLL_SECONDS,
    SIGNAL_ACTIVATE_SECONDS,
    SIGNAL_COMPOUND_DETECT_SECONDS,
    SIGNAL_DECAY_CYCLE_SECONDS,
    SIGNAL_REMATCH_SECONDS,
    SIGNAL_BACKFILL_SECONDS,
    SIGNAL_HIRING_SCAN_SECONDS,
    SIGNAL_NEWS_SCAN_SECONDS,
    SIGNAL_WATCHLIST_WEB_SCAN_SECONDS,
    SIGNAL_COMPANY_PAGE_POLL_SECONDS,
    SIGNAL_COMPANY_FOLLOWER_POLL_SECONDS,
    SIGNAL_COMMENT_MINING_SECONDS,
    POST_INTENT_CLASSIFY_SECONDS,
    JOB_ACCEPT_INBOUND,
    JOB_ACTIVATE_SIGNALS,
    JOB_CHECK_POST_COMMENTS,
    JOB_CHECK_REPLIES,
    JOB_CLASSIFY_POST_INTENT,
    JOB_CLASSIFY_SIGNALS,
    JOB_COMPLETED,
    JOB_ENGAGE,
    JOB_ENDORSE,
    JOB_FAILED,
    JOB_FOLLOW,
    JOB_FOLLOWUP,
    JOB_INVITE,
    JOB_MINE_COMMENTS,
    JOB_PROCESS_INBOUND,
    JOB_PROFILE_VIEW,
    JOB_DAILY_DIGEST,
    JOB_DAILY_STRATEGY,
    JOB_EXECUTE_STRATEGY_PLANS,
    JOB_PARTNER_REMINDER,
    JOB_COLLECT_COMPANY_FOLLOWERS,
    JOB_COLLECT_COMPANY_PAGE,
    JOB_QUALIFY_INBOUND,
    JOB_CAMPAIGN_REFILL,
    JOB_SYNC_CONNECTIONS,
    JOB_WITHDRAW_INVITE,
    SCHEDULER_CLEANUP_DAYS,
    SCHEDULER_DAILY_STRATEGY_SECONDS,
    SCHEDULER_DAILY_DIGEST_SECONDS,
    SCHEDULER_ACTION_HEALTH_SECONDS,
    SCHEDULER_EXECUTE_PLANS_SECONDS,
    SCHEDULER_ENDORSE_SECONDS,
    SCHEDULER_ENGAGEMENT_SECONDS,
    SCHEDULER_FOLLOW_SECONDS,
    SCHEDULER_PROFILE_VIEW_WARMUP_SECONDS,
    SCHEDULER_MAX_RETRIES,
    SCHEDULER_AUTO_REPLY_SECONDS,
    SCHEDULER_AUTO_RESUME_CHECK_SECONDS,
    SCHEDULER_REPLY_CHECK_SECONDS,
    SCHEDULER_RETRY_DELAY,
    SCHEDULER_AB_EVAL_SECONDS,
    SCHEDULER_EXPERIMENT_SECONDS,
    SCHEDULER_PARTNER_REMINDER_SECONDS,
    SCHEDULER_STRATEGY_SECONDS,
    SCHEDULER_BACKFILL_PROFILES_SECONDS,
    SCHEDULER_CAMPAIGN_REFILL_SECONDS,
    SCHEDULER_TICK_SECONDS,
    ANOMALY_SCAN_SECONDS,
    UNANSWERED_LEAD_SCAN_SECONDS,
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    WITHDRAW_CHECK_SECONDS,
    # Distributed post intelligence
    DISTRIBUTED_SCAN_SECONDS,
    VIRAL_DETECTION_SECONDS,
    WATCHLIST_TUNE_SECONDS,
    JOB_COLLECT_POSTS_DISTRIBUTED,
    JOB_RESEARCH_CONTACTS,
    JOB_BACKFILL_POST_ANALYSIS,
    JOB_DETECT_VIRAL_POSTS,
    JOB_TUNE_WATCHLISTS,
    JOB_OPTIMIZE_SIGNALS,
    SIGNAL_OPTIMIZE_SECONDS,
)
from ..correlation import new_correlation_id, clear_correlation_id, get_correlation_id
from ..tracing import tracer
from ..db.async_bridge import run_db
from ..db.queries import (
    claim_job,
    cleanup_old_jobs,
    cleanup_scheduler_events,
    complete_job,
    count_stale_ready_jobs,
    get_ready_jobs,
    get_setting,
    list_campaigns,
    log_scheduler_event,
    reschedule_job,
    restagger_stale_jobs,
    retry_job,
    save_setting,
)

logger = logging.getLogger(__name__)

# How long the run loop lets one tick run before cancelling it.
_TICK_BUDGET_SECONDS = 55

# Grace before the first tick, so an MCP server finishes starting up first.
_STARTUP_DELAY_SECONDS = 5

# Held back from the job-execution phase so the planning and bookkeeping that
# follow it still fit inside the budget. Without it, filling the tick with jobs
# right up to the cancellation point starves the scheduling half of the tick.
_TICK_PLANNING_RESERVE_SECONDS = 10

# The ceiling on ONE job, and the number a job's own internal budget has to be
# written against — not _TICK_BUDGET_SECONDS, which a job never gets. This is
# an upper bound: _process_ready_jobs clamps to int(deadline - now), so what a
# job is actually granted is this less whatever the tick has already spent
# (~1s of startup for the first wave, more for a job that waited for a slot).
# Named because a self-stop set against the tick budget instead of this one is
# a self-stop that never fires — see SIGNAL_CLASSIFY_MAX_PER_RUN.
_TICK_JOB_DEADLINE_SECONDS = _TICK_BUDGET_SECONDS - _TICK_PLANNING_RESERVE_SECONDS

# Outreach must run before collectors. A collector that starts late in the
# tick is clamped to the leftover seconds and writes nothing.
_OUTREACH_JOB_TYPES = frozenset({
    "invite", "send_dm", "followup", "auto_reply", "inmail", "email_invite",
})

# One of these per tick, and only when more than 20s remain. They share the
# same scheduler_jobs table and Semaphore(5) as invites — starting several
# eats the 44s job window.
_HEAVY_COLLECTOR_JOBS = frozenset({
    "check_replies",
    "collect_company_followers",
    "collect_posts_distributed",
    "collect_company_page",
    "collect_keyword_signals",
    "collect_competitor_signals",
    "collect_hiring_signals",
    "collect_news_signals",
    "collect_watchlist_web_signals",
    "collect_profile_views",
    "research_contacts",
    "campaign_refill",
    "backfill_profiles",
    "sync_connections",
    "backfill_post_analysis",
    "scan_prospect_posts",
    "scan_network_posts",
})
_HEAVY_MIN_REMAINING_SECONDS = 20.0

# Long-running collectors get a 5 min nominal timeout (then clamped to the
# remaining tick). research/refill/backfill used to sit at 120s and still
# lost the race to the 44s clamp; they stay in this set so the intent is
# visible, even though the clamp is what actually applies.
_SLOW_JOBS = frozenset({
    "check_replies",
    "collect_company_followers",
    "collect_posts_distributed",
    "collect_company_page",
    "collect_keyword_signals",
    "collect_competitor_signals",
    "collect_hiring_signals",
    "collect_news_signals",
    "collect_watchlist_web_signals",
    "collect_profile_views",
    "research_contacts",
    "campaign_refill",
    "backfill_profiles",
    "sync_connections",
})


def _job_start_priority(job_type: str, *, refill_critical: bool = False) -> int:
    if job_type in _OUTREACH_JOB_TYPES:
        return 0
    if refill_critical and job_type == "campaign_refill":
        return 0
    if job_type in _HEAVY_COLLECTOR_JOBS:
        return 2
    return 1

# Inbound job types run even when the outbound scheduler is toggled off.
# Job types that run even when the outbound scheduler is toggled off.
#
# The bar is deliberately narrow: handling messages other people sent us, plus
# work that never leaves this machine. Everything else waits for the scheduler.
#
# This set used to include the whole signal-intelligence pipeline on the
# reasoning that analysis is "passive". It is not passive from LinkedIn's side —
# scanning posts, mining comments and collecting company pages are all reads
# against the user's account, and they carried on for hours after an emergency
# stop. It also included campaign_refill, which enrols new prospects, so
# campaigns kept growing while the user believed everything was halted.
# Turning the scheduler off must make the account go quiet.
_INBOUND_JOB_TYPES = frozenset({
    JOB_ACCEPT_INBOUND,       # DEPRECATED: kept for in-flight jobs
    JOB_QUALIFY_INBOUND,      # DEPRECATED: kept for in-flight jobs
    JOB_PROCESS_INBOUND,      # Unified classify-first pipeline
    JOB_DAILY_DIGEST,         # Local reporting, no network calls
})


def runs_while_scheduler_off(job_type: str) -> bool:
    """Whether a job may execute with the outbound scheduler disabled.

    Unknown job types are blocked: a newly added job must opt in explicitly
    rather than inherit always-on behaviour by omission.
    """
    return job_type in _INBOUND_JOB_TYPES


from . import leader
from .. import constants as _c  # job types not in the explicit import list above

# How long to stay in standby after handing leadership over before contending
# again. A requester polls for the lock for up to _HANDOVER_WAIT_SECONDS after
# asking, so waiting out its whole window means a real handover always wins the
# race and we only come back when nobody actually took over.
_YIELD_GRACE_SECONDS = leader._HANDOVER_WAIT_SECONDS

# After re-acquiring, ignore yield requests for this long. The request that stood
# us down stays readable for its whole TTL whether or not its author is still
# waiting, so honouring it again immediately would put us straight back into
# standby, tick after tick, scheduling nothing — the very outcome standby exists
# to prevent. Kept strictly under _HANDOVER_WAIT_SECONDS so a genuinely new
# requester still takes the lock inside the window it is willing to poll for.
_YIELD_REPEAT_HOLD_SECONDS = SCHEDULER_TICK_SECONDS

# Reading and thinking: collecting signals, classifying them, checking replies.
# These touch LinkedIn but only to read, and they leave no trace another person
# sees. They are what "observe" mode buys — the product stays informed while
# nothing goes out.
_OBSERVE_JOB_TYPES = frozenset({
    _c.JOB_SCAN_PROSPECT_POSTS,
    _c.JOB_SCAN_NETWORK_POSTS,
    JOB_CLASSIFY_POST_INTENT,
    JOB_CLASSIFY_SIGNALS,
    # NOT activate_signals: it converts scored signals into outreaches and adds
    # people to campaigns as warm leads. That is enrolment, not observation.
    JOB_CHECK_POST_COMMENTS,
    JOB_CHECK_REPLIES,
    JOB_MINE_COMMENTS,
    JOB_COLLECT_COMPANY_PAGE,
    JOB_COLLECT_COMPANY_FOLLOWERS,
    _c.JOB_COLLECT_PROFILE_VIEWS,
    _c.JOB_DETECT_JOB_CHANGES,
    JOB_SYNC_CONNECTIONS,
    _c.JOB_BACKFILL_PROFILES,
    _c.JOB_VERIFY_ACTIONS,
    # Watchlist-driven collectors. Searching posts, job ads and news reads
    # public surfaces; nobody is contacted and nobody learns they were read.
    # Without these, watchlists stop polling entirely the moment the scheduler
    # leaves 'full' — the planner keeps queuing the jobs and the stale-pending
    # sweeper fails them 30 min later, so the queue looks busy while no signal
    # is collected.
    _c.JOB_COLLECT_KEYWORD_SIGNALS,
    _c.JOB_COLLECT_COMPETITOR_SIGNALS,
    _c.JOB_COLLECT_HIRING_SIGNALS,
    _c.JOB_COLLECT_NEWS_SIGNALS,
    _c.JOB_COLLECT_WATCHLIST_WEB_SIGNALS,
    # Local scoring maintenance: DB and LLM only, no network calls to a person.
    # These keep collected signals scored, linked and expired; starving them
    # leaves observe mode collecting data it never finishes processing.
    _c.JOB_DETECT_COMPOUND_INTENT,
    _c.JOB_SIGNAL_DECAY_CYCLE,
    _c.JOB_REMATCH_SIGNALS,
    _c.JOB_BACKFILL_ORPHAN_SIGNALS,
    # Tier re-detection reads account metadata and runs one limit-1 search;
    # it writes local settings and contacts nobody. Observe mode must keep
    # the stored has_sales_navigator flag honest or every later send runs on
    # a tier that lapsed (or was bought) months ago.
    _c.JOB_REDETECT_SALES_NAV,
})


def _local_datetime(now: float):
    """Datetime in the configured timezone (UTC when unset or invalid)."""
    from datetime import datetime, timezone as _tz

    from ..config import get_timezone

    tz_name = get_timezone()
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(now, ZoneInfo(tz_name))
    except Exception:
        pass
    return datetime.fromtimestamp(now, _tz.utc)


def _daily_strategy_due(last_run: float, now: float) -> bool:
    """True once per local day, from STRATEGIST_PLANNING_HOUR onwards.

    A daemon that boots after the planning hour still plans that day (the
    windows are "not before", so the plan is usable); a second run on the
    same local date is never scheduled.
    """
    from ..constants import STRATEGIST_PLANNING_HOUR

    local_now = _local_datetime(now)
    if local_now.hour < STRATEGIST_PLANNING_HOUR:
        return False
    if not last_run:
        return True
    return _local_datetime(last_run).date() < local_now.date()


def _outside_working_hours(now: float) -> bool:
    """True when local working_hours say we should not be sending."""
    from datetime import datetime

    cfg = load_config()
    wh = cfg.get("working_hours") or {}
    start = int(wh.get("start", 0) or 0)
    end = int(wh.get("end", 24) or 24)
    days = wh.get("days") or list(range(7))
    from ..config import get_timezone

    tz_name = get_timezone()
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(now, ZoneInfo(tz_name))
    except Exception:
        dt = datetime.fromtimestamp(now)
    if dt.weekday() not in days:
        return True
    if start <= 0 and end >= 24:
        return False
    return not (start <= dt.hour < end)


def _stall_is_expected(now: float) -> bool:
    """Daily cap, empty queue, or after-hours is not a stuck sender."""
    if _outside_working_hours(now):
        return True
    from ..db.schema import get_db
    db = get_db()
    try:
        skip_row = db.execute(
            """SELECT action_type, result FROM actions_log
               WHERE action_type IN
                 ('skip_invite', 'skip_inmail', 'invitation_sent',
                  'inmail_sent', 'inmail_unreachable')
               ORDER BY timestamp DESC LIMIT 1"""
        ).fetchone()
        if skip_row and str(skip_row[0]).startswith("skip_") and (
            skip_row[1] in ("daily_cap", "none_eligible", "skipped")
        ):
            return True
    except Exception:
        return False
    finally:
        db.close()
    return False


def runs_in_mode(job_type: str, mode: str) -> bool:
    """Whether a job may execute in the given scheduler mode.

    Unknown job types run only in 'full'. Anything that reaches a person, or
    enrols them into a campaign, is excluded from 'observe' by construction:
    membership is an allow-list, never a deny-list.
    """
    if job_type in _INBOUND_JOB_TYPES:
        return True
    if mode == "full":
        return True
    if mode == "observe":
        return job_type in _OBSERVE_JOB_TYPES
    return False


class SchedulerEngine:
    """Asyncio-based scheduler for autonomous outreach execution.

    Runs as a background task in the same event loop as the MCP server.
    All outreach logic is delegated to executors (which reuse existing tool internals).
    """

    # Setting key for persisting timers to SQLite
    _TIMERS_SETTING_KEY = "scheduler_timers"
    # Deprecated timer names to clean up on startup
    _DEPRECATED_TIMERS = {"inbound_check", "qualify_inbound"}

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task[None] | None = None
        # All periodic timers stored in a dict, persisted to SQLite.
        # Loaded on start() to survive MCP server restarts.
        self._timers: dict[str, float] = {}
        self._tick_count: int = 0
        self._errors_in_row: int = 0
        self._started_at: float = 0
        self._reported_stuck_jobs: set[str] = set()  # dedup watchdog events
        # Standby state after handing leadership over: monotonic deadline for
        # the next contention attempt, None while we hold the lock.
        self._recontend_at: float | None = None
        self._ignore_yield_until: float = 0.0
        # Read off the lock before releasing it, so we come back as the same
        # kind we were leading as rather than mislabelling a daemon as mcp.
        self._leader_kind: str = "mcp"

    def _timer(self, name: str) -> float:
        """Get a timer value (last execution timestamp). Returns 0 if never run."""
        return self._timers.get(name, 0)

    def _set_timer(self, name: str, value: float) -> None:
        """Set a timer value. Persisted to SQLite every 5 ticks."""
        self._timers[name] = value

    async def _send_scheduler_disable_alert(self, toggle_info: dict) -> None:
        """Send immediate email alert when scheduler was disabled while always-on is active."""
        try:
            from ..config import is_backend_mode
            if not is_backend_mode():
                logger.debug("No backend configured, skipping scheduler disable alert")
                return

            import httpx
            from ..services.cloud_sync import _base_url, _headers

            base = _base_url()
            payload = {
                "alert_type": "scheduler_disabled",
                "disabled_by": toggle_info.get("caller", "unknown"),
                "reason": toggle_info.get("reason", ""),
                "disabled_at": toggle_info.get("timestamp", 0),
                "auto_reenabled": True,
            }

            async with httpx.AsyncClient(timeout=15.0) as http:
                resp = await http.post(
                    f"{base}/api/v1/alerts/scheduler-disabled",
                    json=payload,
                    headers=_headers(),
                )
                if resp.status_code == 200:
                    logger.info("Scheduler disable alert sent to backend")
                else:
                    logger.warning(
                        "Scheduler disable alert failed: HTTP %d — %s",
                        resp.status_code, resp.text[:100],
                    )
        except Exception as e:
            logger.warning("Failed to send scheduler disable alert: %s", e)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def tick_count(self) -> int:
        return self._tick_count

    async def start(self) -> None:
        """Start the scheduler background loop.

        Loads persisted timer state from SQLite so periodic tasks resume
        where they left off instead of all firing immediately on restart.
        """
        if self._running:
            logger.warning("Scheduler already running")
            return
        # Load persisted timers from DB (survives restarts)
        try:
            saved = await run_db(get_setting, self._TIMERS_SETTING_KEY, {})
            if isinstance(saved, dict):
                self._timers = saved
                logger.info(
                    "Loaded %d persisted scheduler timers", len(self._timers),
                )
                # Clean up deprecated timer keys
                removed = [k for k in self._DEPRECATED_TIMERS if k in self._timers]
                if removed:
                    for k in removed:
                        del self._timers[k]
                    await run_db(save_setting, self._TIMERS_SETTING_KEY, self._timers)
                    logger.info("Cleaned up deprecated timers: %s", removed)
        except Exception as e:
            logger.debug("Failed to load persisted timers: %s", e)

        # Recover jobs stuck in 'running' from a previous crashed session
        try:
            from ..db.queries import (
                recover_stuck_running_jobs,
                recover_stuck_sending_outreaches,
                repair_phantom_replies,
            )
            recovered = await run_db(recover_stuck_running_jobs, 360)
            if recovered > 0:
                logger.warning("Startup: recovered %d stuck running jobs", recovered)
            recovered_out = await run_db(recover_stuck_sending_outreaches, 360)
            if recovered_out > 0:
                logger.warning("Startup: recovered %d stuck sending outreaches", recovered_out)
            phantom = await run_db(repair_phantom_replies)
            if phantom > 0:
                logger.warning("Startup: reverted %d phantom replied outreaches", phantom)
            from ..services.own_identity import park_own_account_outreaches
            parked = await run_db(park_own_account_outreaches)
            if parked:
                logger.info("Startup: parked %d own-account outreach(es)", parked)
        except Exception as e:
            logger.debug("Startup stuck-job recovery failed: %s", e)

        # Purge old completed/failed jobs on startup (don't wait for hourly cleanup)
        try:
            from ..db.queries import cleanup_old_jobs
            deleted = await run_db(cleanup_old_jobs, days=SCHEDULER_CLEANUP_DAYS)
            if deleted > 0:
                logger.info("Startup: purged %d old completed/failed jobs", deleted)
        except Exception as e:
            logger.debug("Startup cleanup failed: %s", e)

        self._running = True
        self._started_at = time.time()
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Scheduler engine started")

        # Non-blocking: verify cloud fallback is active, and move existing
        # hosted campaigns onto the cloud if they still sit on this machine.
        asyncio.create_task(self._check_cloud_on_startup())
        asyncio.create_task(self._ensure_cloud_default_on_startup())
        asyncio.create_task(self._cancel_cloud_owned_jobs_on_startup())
        # Seat / Premium can change while the laptop is closed — probe on start
        # even if the persisted daily timer would have skipped this tick.
        asyncio.create_task(self._schedule_sn_redetect())

    async def stop(self) -> None:
        """Gracefully stop the scheduler."""
        uptime = time.time() - self._started_at if self._started_at else 0
        logger.warning(
            "Scheduler engine stopping — uptime=%.0fs, ticks=%d, reason=process_shutdown",
            uptime, self._tick_count,
        )
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

        try:
            await run_db(save_setting, self._TIMERS_SETTING_KEY, self._timers)
        except Exception as e:
            logger.debug("Timer persistence on stop failed: %s", e)

        # Check if cloud scheduler is picking up the slack
        await self._verify_cloud_fallback(uptime)

        logger.warning("Scheduler engine stopped")

    async def _verify_cloud_fallback(self, uptime: float) -> None:
        """Check if backend cloud scheduling covers for us. Alert if not.

        Asks /scheduler/status, the user-auth endpoint. /scheduler/health is
        authenticated by the cron system's scheduler key — a user JWT gets
        403 there unconditionally, which made every engine stop log a
        CRITICAL and alert for the wrong reason. /status carries no tick
        age, so 'enabled' is the strongest confirmation a user token can
        get; staleness detection needs a backend change.
        """
        try:
            from .. import config
            if not config.is_backend_mode():
                logger.warning("No backend configured — no cloud fallback available")
                return

            import httpx
            from ..services.cloud_sync import _base_url, _headers

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{_base_url()}/api/v1/scheduler/status",
                    headers=_headers(),
                )

            if resp.status_code == 200:
                data = resp.json()
                if data.get("enabled"):
                    logger.info(
                        "Cloud scheduling is enabled (%d pending jobs) — fallback active",
                        data.get("pending_jobs", 0),
                    )
                    return
                # Disabled-by-choice is a configuration state, not a fault.
                # Startup already logs this at INFO; a launchd restart or
                # MCP exit used to fire a both-down email every time.
                logger.info(
                    "Cloud scheduling is disabled for this account — local scheduler only"
                )
                return
            logger.error(
                "CRITICAL: Cloud scheduler status check failed (HTTP %d). "
                "Cannot confirm fallback.",
                resp.status_code,
            )

            # Status check failed — cannot confirm fallback
            await self._send_both_down_alert(uptime)

        except Exception as e:
            logger.error("Failed to verify cloud fallback: %s", e)
            try:
                await self._send_both_down_alert(uptime)
            except Exception:
                pass

    async def _send_both_down_alert(self, uptime: float) -> None:
        """Tell backend to send a 'both schedulers down' alert email."""
        try:
            from .. import config
            if not config.is_backend_mode():
                return

            import httpx
            from ..services.cloud_sync import _base_url, _headers

            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{_base_url()}/api/v1/scheduler/alert-down",
                    headers=_headers(),
                    json={"local_uptime_seconds": uptime, "local_ticks": self._tick_count},
                )
            logger.info("Both-schedulers-down alert sent via backend")
        except Exception as e:
            logger.warning("Failed to send both-down alert: %s", e)

    async def _outreach_health_watchdog(self, now: float) -> None:
        """Detect stuck jobs and outreach stalls, recover and alert via email.

        Runs every 5 min. Two checks:
        1. Stuck jobs: running for >6 min → recover + alert
        2. Outreach stall: pending outreaches exist but no DM/invite completed
           in last 30 min → alert
        """
        try:
            from ..db.queries import recover_stuck_running_jobs, recover_stuck_sending_outreaches

            # ── Check 1: Stuck jobs ──
            # Use 600s (10 min) threshold — some jobs legitimately take 5+ min
            _STUCK_THRESHOLD = 600

            def _query_stuck(threshold: int) -> list:
                from ..db.schema import get_db
                _db = get_db()
                rows = _db.execute(
                    """SELECT id, job_type, campaign_id, started_at
                       FROM scheduler_jobs
                       WHERE status = 'running' AND started_at < ?""",
                    (int(now) - threshold,),
                ).fetchall()
                _db.close()
                return rows

            stuck_rows = await run_db(_query_stuck, _STUCK_THRESHOLD)

            if stuck_rows:
                recovered = await run_db(recover_stuck_running_jobs, _STUCK_THRESHOLD)
                recovered_out = await run_db(recover_stuck_sending_outreaches, _STUCK_THRESHOLD)
                total_recovered = recovered + recovered_out
                logger.warning(
                    "Watchdog: %d stuck jobs detected, recovered %d jobs + %d outreaches",
                    len(stuck_rows), recovered, recovered_out,
                )

                # Only log event for NEW stuck jobs (dedup across watchdog cycles)
                stuck_ids = {r["id"] for r in stuck_rows}
                new_stuck = stuck_ids - self._reported_stuck_jobs
                self._reported_stuck_jobs = stuck_ids  # update for next cycle

                # Send alert (debounced — max 1 per cooldown period)
                from ..constants import STALL_ALERT_COOLDOWN_SECONDS
                last_stuck_alert = self._timer("last_stuck_alert")
                if now - last_stuck_alert >= STALL_ALERT_COOLDOWN_SECONDS:
                    await self._send_outreach_stall_alert({
                        "stall_type": "stuck_jobs",
                        "minutes_since_last_action": (now - min(r["started_at"] for r in stuck_rows)) / 60,
                        "stuck_jobs": len(stuck_rows),
                        "recovered_jobs": total_recovered,
                        "pending_outreaches": 0,
                        "campaigns": [
                            {"name": r["job_type"], "pending": 1}
                            for r in stuck_rows[:5]
                        ],
                    })
                    self._set_timer("last_stuck_alert", now)

                if new_stuck:
                    await run_db(
                        log_scheduler_event, "watchdog_stuck_jobs",
                        context={
                            "stuck_count": len(stuck_rows),
                            "new_stuck": len(new_stuck),
                            "recovered": total_recovered,
                            "job_types": [r["job_type"] for r in stuck_rows],
                        },
                    )
            else:
                # Clear reported set when no stuck jobs
                self._reported_stuck_jobs.clear()

            # ── Check 2: Outreach stall ──
            # If there are pending outreaches but no DM/invite completed in 30 min
            def _query_stall_info() -> tuple:
                from ..db.queries import count_due_sendable_work
                from ..db.schema import get_db
                _db = get_db()
                _pending = count_due_sendable_work()
                _last = None
                _camps = []
                if _pending > 0:
                    _last = _db.execute(
                        """SELECT max(completed_at) FROM scheduler_jobs
                           WHERE job_type IN ('send_dm', 'invite', 'followup')
                           AND status = 'completed'"""
                    ).fetchone()[0]
                    _camps = _db.execute(
                        """SELECT c.name,
                                  (SELECT count(*) FROM outreaches o
                                   WHERE o.campaign_id = c.id
                                   AND o.status IN ('pending', 'connected')) as pending
                           FROM campaigns c
                           WHERE c.status = 'active'"""
                    ).fetchall()
                _db.close()
                return _pending, _last, _camps

            pending_count, last_action, campaign_info = await run_db(_query_stall_info)

            if pending_count > 0:
                minutes_since_action = (now - last_action) / 60 if last_action else 999

                if minutes_since_action >= 30:
                    if await run_db(_stall_is_expected, now):
                        logger.info(
                            "Watchdog: no stall — last invite action was a gate "
                            "or we are outside working hours",
                        )
                    else:
                        logger.warning(
                            "Watchdog: outreach stall — %d pending, last action %.0f min ago",
                            pending_count, minutes_since_action,
                        )

                        # Only alert once per stall (use timer to debounce)
                        from ..constants import STALL_ALERT_COOLDOWN_SECONDS
                        last_stall_alert = self._timer("last_stall_alert")
                        if now - last_stall_alert >= STALL_ALERT_COOLDOWN_SECONDS:
                            await self._send_outreach_stall_alert({
                                "stall_type": "outreach_stall",
                                "minutes_since_last_action": minutes_since_action,
                                "stuck_jobs": 0,
                                "pending_outreaches": pending_count,
                                "recovered_jobs": 0,
                                "campaigns": [
                                    {"name": r["name"], "pending": r["pending"]}
                                    for r in campaign_info[:5]
                                ],
                            })
                            self._set_timer("last_stall_alert", now)

                        await run_db(
                            log_scheduler_event, "watchdog_outreach_stall",
                            context={
                                "pending_outreaches": pending_count,
                                "minutes_since_action": round(minutes_since_action, 1),
                            },
                        )
        except Exception as e:
            logger.warning("Outreach health watchdog failed: %s", e)

    async def _send_outreach_stall_alert(self, details: dict) -> None:
        """Send outreach stall/stuck alert to backend for email delivery."""
        try:
            from ..config import is_backend_mode
            if not is_backend_mode():
                logger.debug("No backend configured, skipping outreach stall alert")
                return

            import httpx
            from ..services.cloud_sync import _base_url, _headers

            async with httpx.AsyncClient(timeout=15.0) as http:
                resp = await http.post(
                    f"{_base_url()}/api/v1/alerts/outreach-stall",
                    json=details,
                    headers=_headers(),
                )
                if resp.status_code == 200:
                    logger.info("Outreach stall alert sent to backend (type: %s)", details.get("stall_type"))
                else:
                    logger.warning(
                        "Outreach stall alert failed: HTTP %d — %s",
                        resp.status_code, resp.text[:100],
                    )
        except Exception as e:
            logger.warning("Failed to send outreach stall alert: %s", e)

    async def _check_cloud_on_startup(self) -> None:
        """Verify cloud scheduler fallback is active on startup (non-blocking)."""
        await asyncio.sleep(10)  # Let MCP server finish initializing
        try:
            from .. import config
            if not config.is_backend_mode():
                logger.info("No backend mode — cloud fallback not available")
                return

            import httpx
            from ..services.cloud_sync import _base_url, _headers

            # /scheduler/status, not /scheduler/health: health wants the cron
            # system's scheduler key and 403s every user JWT unconditionally.
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{_base_url()}/api/v1/scheduler/status",
                    headers=_headers(),
                )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("enabled"):
                    logger.info(
                        "Cloud scheduling is enabled (%d pending jobs) — fallback available",
                        data.get("pending_jobs", 0),
                    )
                else:
                    # Disabled-by-choice is a configuration state, not a fault.
                    logger.info(
                        "Cloud scheduling is disabled for this account — local scheduler only"
                    )
            else:
                logger.warning(
                    "Cloud scheduler status check returned HTTP %d — fallback may not be active",
                    resp.status_code,
                )
        except Exception as e:
            logger.debug("Cloud startup check failed (non-fatal): %s", e)

    async def _ensure_cloud_default_on_startup(self) -> None:
        """Commission cloud sending so existing campaigns do not stay local."""
        try:
            from ..services.cloud_sync import ensure_hosted_sending_default
            commissioned, detail = await ensure_hosted_sending_default()
            if commissioned:
                logger.info("Hosted sending default: cloud commissioned (%s)", detail)
            else:
                logger.debug("Hosted sending default skipped: %s", detail)
        except Exception as e:
            logger.debug("Hosted sending default on startup failed (non-fatal): %s", e)

    async def _cancel_cloud_owned_jobs_on_startup(self) -> None:
        """Drop leftover local send/inbound jobs once the cloud owns them."""
        try:
            from ..services.cloud_sync import cancel_cloud_owned_pending_jobs
            await run_db(cancel_cloud_owned_pending_jobs)
        except Exception as e:
            logger.debug("Cancel cloud-owned jobs failed (non-fatal): %s", e)

    def _check_leadership(self) -> bool:
        """Whether this process may run the tick, handling both halves of a
        handover: standing down for a newer build, and coming back when nobody
        took over.

        Stepping down and stopping was only half of it. It assumes the requester
        starts scheduling; a requester that dies, or that is itself an MCP server
        which later stands down for a third process, left an MCP-only install
        with no scheduler anywhere until a process restarted. So a stood-down
        engine goes to standby and contends again through the normal
        try_acquire_leader path — the lock file stays the single arbiter, and
        losing means someone else leads, which is the right outcome.
        """
        now = time.monotonic()

        if self._recontend_at is not None:
            if now < self._recontend_at:
                return False
            if leader.try_acquire_leader(self._leader_kind):
                logger.warning(
                    "Re-acquired scheduler leadership after standing down — "
                    "nobody else took the lock"
                )
                self._recontend_at = None
                self._ignore_yield_until = now + _YIELD_REPEAT_HOLD_SECONDS
                return True
            # Someone leads. Keep contending on later ticks: that holder can
            # still die, and nothing else would notice.
            self._recontend_at = now + SCHEDULER_TICK_SECONDS
            return False

        if now < self._ignore_yield_until:
            return True

        # A newer build has asked for leadership. Stepping down here rather
        # than at process exit is the whole point: an orphaned old-version
        # process used to hold the lock for hours while a fixed one idled as
        # a follower, so the bug stayed live because stale code owned the
        # scheduler. Requests only come from strictly newer versions.
        if leader.should_yield():
            logger.warning(
                "Newer scheduler requested leadership — releasing the lock "
                "after %d ticks", self._tick_count,
            )
            # Read before releasing: release truncates the record.
            self._leader_kind = str(
                leader.read_leader_info().get("kind") or self._leader_kind
            )
            leader.release_leader()
            # Do not clear the request here — it belongs to the requester,
            # which clears it once it holds the lock. Deleting it from this
            # side used to erase a third process's pending request too.
            self._recontend_at = now + _YIELD_GRACE_SECONDS
            return False

        return True

    async def _run_loop(self) -> None:
        """Main tick loop — runs every SCHEDULER_TICK_SECONDS."""
        # Small initial delay to let the MCP server finish startup
        await asyncio.sleep(_STARTUP_DELAY_SECONDS)

        while self._running:
            if not self._check_leadership():
                await asyncio.sleep(SCHEDULER_TICK_SECONDS)
                continue

            try:
                await asyncio.wait_for(self._tick(), timeout=_TICK_BUDGET_SECONDS)
                self._errors_in_row = 0
            except asyncio.TimeoutError:
                finished = getattr(self, "_last_tick_jobs_finished", 0)
                if finished:
                    logger.warning(
                        "Scheduler tick hit 55s after %s jobs finished — "
                        "remaining work continues next tick",
                        finished,
                    )
                    self._errors_in_row = 0
                else:
                    logger.error("Scheduler tick timed out after 55s — skipping")
                    self._errors_in_row += 1
            except asyncio.CancelledError:
                logger.warning("Scheduler run loop cancelled after %d ticks", self._tick_count)
                break
            except Exception as e:
                self._errors_in_row += 1
                logger.error("Scheduler tick error (%d in a row): %s", self._errors_in_row, e)
                # Back off if too many consecutive errors
                if self._errors_in_row >= 5:
                    logger.warning("Too many scheduler errors, backing off for 5 minutes")
                    await asyncio.sleep(300)
                    self._errors_in_row = 0

            await asyncio.sleep(SCHEDULER_TICK_SECONDS)

        # Loop exited normally (not via CancelledError)
        if not self._running:
            logger.warning("Scheduler run loop exited — _running=False after %d ticks", self._tick_count)

    async def _tick(self) -> None:
        """Single scheduler tick — the heart of the autonomous system."""
        self._tick_count += 1

        # The run loop cancels this tick at _TICK_BUDGET_SECONDS. Job execution
        # stops starting new work a little before that, so the tick keeps
        # enough budget to finish the planning and bookkeeping that follow it.
        tick_deadline = time.monotonic() + _TICK_JOB_DEADLINE_SECONDS

        # Guard: setup must be complete for ANY processing
        if not await run_db(get_setting, "setup_complete", False):
            return

        # The account id and email account are memoized for the life of the
        # process. This daemon outlives account switches made from MCP
        # sessions, so re-read them here — on the DB thread, where a sync read
        # is legal — rather than sending from an account the user disconnected.
        from ..services.process_caches import refresh_process_caches
        await run_db(refresh_process_caches)

        now = time.time()
        mode = get_scheduler_mode()
        scheduler_on = mode == "full"
        tick_processed = 0
        tick_failed = 0
        tick_planned = 0

        # ── ALWAYS-ON GUARD: auto-re-enable if always_on mode is active ──
        # Fires on 'off' only. In 'observe' the user asked for collection
        # without sending, and always-on undoes accidental stops, not
        # deliberate ones. Firing there also re-fired every tick — observe
        # keeps scheduler_on False by design — writing the mode and emailing a
        # disable alert once a minute, forever.
        if mode == "off" and is_scheduler_always_on():
            cfg = load_config()
            last_toggle = cfg.get("_last_scheduler_toggle", {})

            set_scheduler_mode(
                "full",
                caller="always_on_guard",
                reason=(
                    f"Auto-re-enabled by always-on guard. "
                    f"Was disabled by '{last_toggle.get('caller', 'unknown')}': "
                    f"{last_toggle.get('reason', 'no reason given')}"
                ),
            )
            # `mode` is deliberately left as read at the top of the tick: the
            # re-enable takes effect from the next tick, exactly as it did when
            # this wrote the boolean and the next tick's get_scheduler_mode()
            # picked it up.
            scheduler_on = True

            logger.warning(
                "Scheduler was disabled but always_on is active — auto-re-enabled. "
                "Disabled by: %s, reason: %s",
                last_toggle.get("caller", "unknown"),
                last_toggle.get("reason", "unknown"),
            )

            await self._send_scheduler_disable_alert(last_toggle)

            await run_db(
                log_scheduler_event, "scheduler_always_on_reenable",
                context={
                    "disabled_by": last_toggle.get("caller", "unknown"),
                    "disable_reason": last_toggle.get("reason", ""),
                    "disabled_at": last_toggle.get("timestamp", 0),
                    "reenabled_at": int(now),
                },
            )

        # ── ALWAYS-ON: execute ready jobs from the DB queue ──
        # Inbound jobs run regardless of scheduler_enabled.
        # Outbound jobs are skipped when the scheduler is off.
        self._last_tick_jobs_finished = 0
        processed, failed = await self._process_ready_jobs(
            scheduler_on=scheduler_on,
            mode=mode,
            deadline=tick_deadline,
        )
        self._last_tick_jobs_finished = max(
            getattr(self, "_last_tick_jobs_finished", 0), processed,
        )
        tick_processed += processed
        tick_failed += failed

        # ── ALWAYS-ON: inbound pipeline (independent of scheduler toggle) ──

        # Unified classify-first inbound pipeline (every 15 min)
        if now - self._timer("inbound_pipeline") >= INBOUND_PIPELINE_SECONDS:
            await self._schedule_process_inbound()
            self._set_timer("inbound_pipeline", now)

        # ── STOP HERE IN 'off' MODE ──
        # Everything below reads LinkedIn on our own initiative or spends money
        # on classification. In 'off' that is unwanted; in 'observe' it is the
        # entire point, and nothing below sends because the outbound section is
        # gated separately further down.
        if mode == "off":
            logger.info(
                "tick#%d: processed=%d failed=%d planned=%d mode=off",
                self._tick_count, tick_processed, tick_failed, tick_planned,
            )
            return

        # Check post comments for inbound leads (every 30 min)
        if now - self._timer("post_comment_check") >= POST_COMMENT_CHECK_SECONDS:
            await self._schedule_check_post_comments()
            self._set_timer("post_comment_check", now)

        # ── Signal intelligence pipeline ──
        # Classification and activation are analysis rather than outreach, but
        # they still read LinkedIn, so they follow the scheduler toggle.

        # Classify pending signals (every 5 min)
        if now - self._timer("signal_classify") >= SIGNAL_CLASSIFY_SECONDS:
            await self._schedule_signal_classification()
            self._set_timer("signal_classify", now)

        # Activate classified signals (every 15 min)
        if now - self._timer("signal_activate") >= SIGNAL_ACTIVATE_SECONDS:
            await self._schedule_signal_activation()
            self._set_timer("signal_activate", now)

        # Re-match homeless signals to campaigns (every 1 hour)
        if now - self._timer("signal_rematch") >= SIGNAL_REMATCH_SECONDS:
            await self._schedule_signal_rematch()
            self._set_timer("signal_rematch", now)

        # Backfill orphan signals to known contacts (every 2 hours)
        if now - self._timer("orphan_backfill") >= SIGNAL_BACKFILL_SECONDS:
            await self._schedule_orphan_backfill()
            self._set_timer("orphan_backfill", now)

        # Classify post intents for granular buyer signals (every 30 min)
        if now - self._timer("post_intent_classify") >= POST_INTENT_CLASSIFY_SECONDS:
            await self._schedule_post_intent_classification()
            self._set_timer("post_intent_classify", now)

        # Mine competitor/industry post comments for leads (every 2 hours)
        if now - self._timer("comment_mining") >= SIGNAL_COMMENT_MINING_SECONDS:
            await self._schedule_comment_mining()
            self._set_timer("comment_mining", now)

        # Backfill empty profiles (every 2 hours)
        if now - self._timer("backfill_profiles") >= SCHEDULER_BACKFILL_PROFILES_SECONDS:
            await self._schedule_backfill_profiles()
            self._set_timer("backfill_profiles", now)

        # Signal collection (blocks 6d-6n below) runs in observe as well as
        # full. The outbound work that used to sit here has moved below them,
        # behind the mode guard, because observation and outreach were
        # interleaved and a single early return blocked both.

        # Periodic: auto-resume limit-paused campaigns (every 5 min).
        # 'full' only, despite sitting among the collectors: un-pausing a
        # campaign exists to restart sending, and _check_auto_resume's cloud
        # sync POSTs /campaigns/{id}/resume — the backend's instruction to send
        # from that campaign every 5 minutes. Left ungated it fired in observe
        # with no user action and repeated every tick, which is precisely the
        # door launch and monitor close. Nothing accumulates by waiting:
        # 'weekly_limit' is a *send* cap, and observe never sends into it.
        if (
            mode == "full"
            and now - self._timer("auto_resume_check") >= SCHEDULER_AUTO_RESUME_CHECK_SECONDS
        ):
            await self._check_auto_resume()
            self._set_timer("auto_resume_check", now)

        # Periodic: check replies (every 5 min)
        if now - self._timer("reply_check") >= SCHEDULER_REPLY_CHECK_SECONDS:
            await self._schedule_reply_checks()
            self._set_timer("reply_check", now)

        # Periodic: auto-reply to detected messages (every 5 min)
        if now - self._timer("auto_reply_plan") >= SCHEDULER_AUTO_REPLY_SECONDS:
            await self._schedule_auto_replies()
            self._set_timer("auto_reply_plan", now)

        # Periodic: profile view warm-ups (every 10 min — lightest touch)
        if now - self._timer("profile_view_plan") >= SCHEDULER_PROFILE_VIEW_WARMUP_SECONDS:
            await self._schedule_profile_views()
            self._set_timer("profile_view_plan", now)

        # Periodic: follow warm-ups (every 15 min)
        if now - self._timer("follow_plan") >= SCHEDULER_FOLLOW_SECONDS:
            await self._schedule_follows()
            self._set_timer("follow_plan", now)

        # Periodic: engagement warm-ups (every 30 min)
        if now - self._timer("engagement_plan") >= SCHEDULER_ENGAGEMENT_SECONDS:
            await self._schedule_engagements()
            self._set_timer("engagement_plan", now)

        # 6d. Periodic: collect keyword signals (every 30 min)
        if now - self._timer("keyword_signal_collect") >= SIGNAL_KEYWORD_POLL_SECONDS:
            await self._schedule_keyword_signal_collection()
            self._set_timer("keyword_signal_collect", now)

        # 6e. Periodic: scan prospect posts for signals (every 1 hour)
        if now - self._timer("prospect_post_scan") >= SIGNAL_PROSPECT_SCAN_SECONDS:
            await self._schedule_prospect_post_scan()
            self._set_timer("prospect_post_scan", now)

        # 6e-2. Periodic: scan the wider network's posts (every 1 hour).
        # Campaign contacts are the wrong surface for spotting a connection who
        # announces a role; this rotates through 1st-degree connections instead.
        if now - self._timer("network_post_scan") >= SIGNAL_PROSPECT_SCAN_SECONDS:
            await self._schedule_network_post_scan()
            self._set_timer("network_post_scan", now)

        # 6g. Periodic: collect profile views (every 1 hour)
        if now - self._timer("profile_view_collect") >= SIGNAL_PROFILE_VIEW_SECONDS:
            await self._schedule_profile_view_collection()
            self._set_timer("profile_view_collect", now)

        # 6h. Periodic: detect job changes (every 4 hours)
        if now - self._timer("job_change_scan") >= SIGNAL_JOB_CHANGE_SCAN_SECONDS:
            await self._schedule_job_change_detection()
            self._set_timer("job_change_scan", now)

        # 6i. Periodic: collect competitor mentions (every 30 min)
        if now - self._timer("competitor_signal_collect") >= SIGNAL_COMPETITOR_POLL_SECONDS:
            await self._schedule_competitor_signal_collection()
            self._set_timer("competitor_signal_collect", now)

        # 6k. Periodic: detect hiring surges (every 4 hours)
        if now - self._timer("hiring_signal_collect") >= SIGNAL_HIRING_SCAN_SECONDS:
            await self._schedule_hiring_signal_collection()
            self._set_timer("hiring_signal_collect", now)

        # 6l. Periodic: collect news/funding signals (every 4 hours)
        if now - self._timer("news_signal_collect") >= SIGNAL_NEWS_SCAN_SECONDS:
            await self._schedule_news_signal_collection()
            self._set_timer("news_signal_collect", now)

        # 6l-1. Periodic: collect off-LinkedIn watchlist web signals (every 4 hours)
        if now - self._timer("watchlist_web_collect") >= SIGNAL_WATCHLIST_WEB_SCAN_SECONDS:
            await self._schedule_watchlist_web_collection()
            self._set_timer("watchlist_web_collect", now)

        # 6l-2. Periodic: collect company page engagement signals (Phase 2)
        if now - self._timer("company_page_collect") >= SIGNAL_COMPANY_PAGE_POLL_SECONDS:
            await self._schedule_company_page_collection()
            self._set_timer("company_page_collect", now)

        # 6l-3. Periodic: collect company follower signals (Phase 2)
        if now - self._timer("company_follower_collect") >= SIGNAL_COMPANY_FOLLOWER_POLL_SECONDS:
            await self._schedule_company_follower_collection()
            self._set_timer("company_follower_collect", now)

        # 6m. Periodic: detect compound intent events (every 30 min)
        if now - self._timer("compound_intent_detect") >= SIGNAL_COMPOUND_DETECT_SECONDS:
            await self._schedule_compound_intent_detection()
            self._set_timer("compound_intent_detect", now)

        # 6n. Periodic: signal decay cycle (every 6 hours)
        if now - self._timer("decay_cycle") >= SIGNAL_DECAY_CYCLE_SECONDS:
            await self._schedule_decay_cycle()
            self._set_timer("decay_cycle", now)

        # 6o. Periodic: connection sync (every 4 hours).
        # Above the outbound cutoff because runs_in_mode() classifies
        # JOB_SYNC_CONNECTIONS as observation — it reads the relations API and
        # leaves no trace another person sees. Queued below the cutoff, that
        # allow-list entry was unreachable: observe mode ran indefinitely on a
        # connections table that never refreshed, and it is the table the
        # 1st-degree DM guard and invite dedup both read.
        from ..constants import SCHEDULER_SYNC_CONNECTIONS_SECONDS, JOB_SYNC_CONNECTIONS
        if now - self._timer("sync_connections") >= SCHEDULER_SYNC_CONNECTIONS_SECONDS:
            try:
                from ..db.queries import create_scheduler_job
                await run_db(
                    create_scheduler_job,
                    campaign_id=None,
                    job_type=JOB_SYNC_CONNECTIONS,
                    scheduled_at=now,
                )
                tick_planned += 1
            except Exception as e:
                logger.debug("Connection sync scheduling failed: %s", e)
            self._set_timer("sync_connections", now)

        # 6p. Periodic: Sales Navigator / Premium re-detection (daily). Above the
        # outbound cutoff because the probe only reads — one metadata GET and
        # a limit-1 search, nobody contacted — and the stored tier flag must
        # keep healing even when the scheduler never leaves 'observe'.
        from ..constants import SN_REDETECT_SECONDS
        if now - self._timer("sn_redetect") >= SN_REDETECT_SECONDS:
            await self._schedule_sn_redetect()
            self._set_timer("sn_redetect", now)

        # Operator alert: unanswered hot leads. Does not message prospects —
        # emails the account owner — so it stays above the outbound cutoff.
        if now - self._timer("unanswered_lead_scan") >= UNANSWERED_LEAD_SCAN_SECONDS:
            try:
                from ..services.unanswered_lead_alerts import run_unanswered_lead_scan

                await run_unanswered_lead_scan()
            except Exception as e:
                logger.debug("Unanswered-lead scan failed: %s", e)
            self._set_timer("unanswered_lead_scan", now)

        # Mirror heal: sending + invitation_sent must become invited even when
        # this machine is not the sender (observe, or cloud-owned leftover ticks).
        if now - self._timer("recover_sending") >= 900:
            try:
                from ..db.queries import recover_stuck_sending_outreaches
                recovered_outreaches = await run_db(
                    recover_stuck_sending_outreaches, 360,
                )
                if recovered_outreaches > 0:
                    logger.warning(
                        "Recovered %d stuck sending outreaches",
                        recovered_outreaches,
                    )
            except Exception as e:
                logger.debug("Stuck outreach recovery failed: %s", e)
            self._set_timer("recover_sending", now)

        # ── OUTBOUND: everything from here reaches people or enrols them ──
        # 'observe' stops here, having collected and classified everything
        # above. That is what makes it safe to leave running indefinitely.
        if mode != "full":
            await self._finish_tick_bookkeeping(
                tick_processed, tick_failed, tick_planned, mode,
            )
            return

        # Schedule new work for all active autopilot campaigns
        await self._schedule_new_work()

        # Auto-refill campaigns running low on prospects (every 1 hour).
        # NOT always-on: refill enrolls new outreach recipients, so it must
        # stay behind the scheduler toggle (21 silent enrollments, 6 Aug 2026).
        if now - self._timer("campaign_refill") >= SCHEDULER_CAMPAIGN_REFILL_SECONDS:
            await self._schedule_campaign_refill()
            self._set_timer("campaign_refill", now)

        # 7. Periodic: skill endorsement warm-ups (every 15 min)
        if now - self._timer("endorse_plan") >= SCHEDULER_ENDORSE_SECONDS:
            await self._schedule_endorsements()
            self._set_timer("endorse_plan", now)

        # 8. Periodic: stale invite withdrawal (every hour)
        if now - self._timer("withdraw_check") >= WITHDRAW_CHECK_SECONDS:
            await self._schedule_stale_withdrawals()
            self._set_timer("withdraw_check", now)

        if now - self._timer("recover_stuck") >= WITHDRAW_CHECK_SECONDS:
            await self._schedule_recover_stuck_outreaches()
            self._set_timer("recover_stuck", now)

        # 9a. Brand lifecycle (every hour)
        if now - self._timer("brand_lifecycle_check") >= BRAND_LIFECYCLE_CHECK_SECONDS:
            await self._schedule_brand_lifecycle()
            self._set_timer("brand_lifecycle_check", now)

        # 9b. Brand posts (every hour — planner decides if one is due)
        if now - self._timer("brand_post_plan") >= BRAND_POST_CHECK_SECONDS:
            await self._schedule_brand_posts()
            self._set_timer("brand_post_plan", now)

        # 9c. Brand engagement (every 4 hours)
        if now - self._timer("brand_engage_plan") >= BRAND_ENGAGE_CHECK_SECONDS:
            await self._schedule_brand_engagements()
            self._set_timer("brand_engage_plan", now)

        if now - self._timer("brand_profile_plan") >= BRAND_PROFILE_CHECK_SECONDS:
            await self._schedule_brand_profile()
            self._set_timer("brand_profile_plan", now)

        # 10. Cleanup old completed jobs (once per hour)
        if now - self._timer("cleanup") >= 3600:
            try:
                deleted = await run_db(cleanup_old_jobs, days=SCHEDULER_CLEANUP_DAYS)
                if deleted > 0:
                    logger.debug("Cleaned up %d old scheduler jobs", deleted)
            except Exception as e:
                logger.debug("Cleanup failed: %s", e)

            # Auto-cleanup failed engage & invite jobs
            try:
                from ..db.queries import (
                    cleanup_failed_engage_jobs,
                    cleanup_failed_invite_jobs,
                )
                engage_result = await run_db(cleanup_failed_engage_jobs)
                invite_result = await run_db(cleanup_failed_invite_jobs)
                if engage_result["cleaned"] or invite_result["cleaned"]:
                    logger.info(
                        "Auto-cleanup: %d engage + %d invite failed jobs",
                        engage_result["cleaned"], invite_result["cleaned"],
                    )
            except Exception as e:
                logger.debug("Failed job cleanup failed: %s", e)

            # Recover jobs stuck in 'running' state (process crash recovery)
            try:
                from ..db.queries import recover_stuck_running_jobs
                recovered = await run_db(recover_stuck_running_jobs, 360)
                if recovered > 0:
                    logger.warning("Recovered %d stuck running jobs", recovered)
            except Exception as e:
                logger.debug("Stuck job recovery failed: %s", e)

            # Recover outreaches stuck in 'sending'/'sending_followup' state
            try:
                from ..db.queries import recover_stuck_sending_outreaches
                recovered_outreaches = await run_db(recover_stuck_sending_outreaches, 360)
                if recovered_outreaches > 0:
                    logger.warning("Recovered %d stuck sending outreaches", recovered_outreaches)
            except Exception as e:
                logger.debug("Stuck outreach recovery failed: %s", e)

            # Revert phantom replied/hot_lead rows. The mint paths are gated,
            # but the daemon runs for weeks — a phantom minted mid-flight must
            # not wait for the next restart to be repaired.
            try:
                from ..db.queries import repair_phantom_replies
                phantom = await run_db(repair_phantom_replies)
                if phantom > 0:
                    logger.warning("Reverted %d phantom replied outreaches", phantom)
            except Exception as e:
                logger.debug("Phantom reply repair failed: %s", e)

            self._set_timer("cleanup", now)

        # 11. Periodic: A/B test evaluation (every hour)
        if now - self._timer("ab_eval") >= SCHEDULER_AB_EVAL_SECONDS:
            try:
                from ..services.experiment_service import evaluate_ab_tests
                results = await run_db(evaluate_ab_tests)
                for r in results:
                    logger.info("A/B test result: %s", r)
                from ..services.experiment_service import finish_headline_tests
                await finish_headline_tests()
            except Exception as e:
                logger.debug("A/B test evaluation failed: %s", e)
            self._set_timer("ab_eval", now)

        # 11b. Account-wide action-health snapshot (every hour)
        if now - self._timer("action_health") >= SCHEDULER_ACTION_HEALTH_SECONDS:
            try:
                from ..services.action_health import persist_action_health_snapshot, summarize_action_health
                health = await run_db(summarize_action_health)
                wrote = await run_db(persist_action_health_snapshot, health, now=int(now))
                if wrote:
                    logger.info(
                        "Action health (24h): %s — %d skip rows / %d reasons, %d sends",
                        health.verdict,
                        health.skip_rows,
                        health.skip_reasons,
                        sum(health.sends.values()),
                    )
            except Exception as e:
                logger.debug("Action health snapshot failed: %s", e)
            self._set_timer("action_health", now)

        # 12. Periodic: experiment analysis (every 6 hours)
        if now - self._timer("experiment") >= SCHEDULER_EXPERIMENT_SECONDS:
            try:
                from ..services.experiment_service import run_experiment_analysis
                result = await run_experiment_analysis()
                if result:
                    logger.info("Experiment analysis completed: %s", result.get("health_check", "")[:100])
            except Exception as e:
                logger.debug("Experiment analysis failed: %s", e)
            self._set_timer("experiment", now)

        # 13. Periodic: strategy engine cycle (every 4 hours)
        if now - self._timer("strategy_cycle") >= SCHEDULER_STRATEGY_SECONDS:
            try:
                from ..services.strategy_engine import run_strategy_cycle
                result = await run_strategy_cycle()
                if result:
                    logger.info(
                        "Strategy cycle: %d patterns, %d optimizations, %d spawned",
                        result.get("patterns_detected", 0),
                        result.get("optimizations", 0),
                        result.get("campaigns_spawned", 0),
                    )
            except Exception as e:
                logger.debug("Strategy cycle failed: %s", e)
            self._set_timer("strategy_cycle", now)

        # 13b. Periodic: communication strategist — once per local day, at or
        # after STRATEGIST_PLANNING_HOUR. The old "24h since last run" timer
        # drifted, fired at whatever hour the daemon last booted, and a plan
        # written mid-afternoon lost its morning window entirely.
        if _daily_strategy_due(self._timer("daily_strategy"), now):
            try:
                from ..db.queries import create_scheduler_job, get_pending_job_count
                pending = await run_db(get_pending_job_count, None, JOB_DAILY_STRATEGY)
                if pending == 0:
                    await run_db(
                        create_scheduler_job, None, JOB_DAILY_STRATEGY, now
                    )
                    tick_planned += 1
                    logger.info("Scheduled daily strategy planning")
            except Exception as e:
                logger.debug("Daily strategy scheduling failed: %s", e)
            self._set_timer("daily_strategy", now)

        # 13c. Periodic: execute strategy plans (every 15 min)
        if now - self._timer("execute_strategy_plans") >= SCHEDULER_EXECUTE_PLANS_SECONDS:
            try:
                campaigns = await run_db(list_campaigns, status=STATUS_ACTIVE)
                for campaign in campaigns:
                    if campaign.get("mode") != "autopilot":
                        continue
                    campaign_id = campaign["id"]
                    from ..db.queries import create_scheduler_job, get_pending_job_count
                    pending = await run_db(
                        get_pending_job_count, campaign_id, JOB_EXECUTE_STRATEGY_PLANS
                    )
                    if pending == 0:
                        await run_db(
                            create_scheduler_job,
                            campaign_id,
                            JOB_EXECUTE_STRATEGY_PLANS,
                            now,
                        )
                        tick_planned += 1
            except Exception as e:
                logger.debug("Strategy plans execution scheduling failed: %s", e)
            self._set_timer("execute_strategy_plans", now)

        # 14. Periodic: partner follow-up reminders (every hour)
        if now - self._timer("partner_reminder") >= SCHEDULER_PARTNER_REMINDER_SECONDS:
            try:
                from ..db.queries import create_scheduler_job
                await run_db(
                    create_scheduler_job,
                    campaign_id=None,
                    job_type=JOB_PARTNER_REMINDER,
                    scheduled_at=now,
                )
            except Exception as e:
                logger.debug("Partner reminder scheduling failed: %s", e)
            self._set_timer("partner_reminder", now)

        # 15. Periodic: daily digest (once per day)
        if now - self._timer("daily_digest") >= SCHEDULER_DAILY_DIGEST_SECONDS:
            try:
                from ..db.queries import create_scheduler_job
                await run_db(
                    create_scheduler_job,
                    campaign_id=None,
                    job_type=JOB_DAILY_DIGEST,
                    scheduled_at=now,
                )
                tick_planned += 1
            except Exception as e:
                logger.debug("Daily digest scheduling failed: %s", e)
            self._set_timer("daily_digest", now)

        # 16a. Periodic: action verification (every 30 min)
        from ..constants import VERIFICATION_CHECK_SECONDS, JOB_VERIFY_ACTIONS
        if now - self._timer("verify_actions") >= VERIFICATION_CHECK_SECONDS:
            try:
                from ..db.queries import create_scheduler_job
                await run_db(
                    create_scheduler_job,
                    campaign_id=None,
                    job_type=JOB_VERIFY_ACTIONS,
                    scheduled_at=now,
                )
                tick_planned += 1
            except Exception as e:
                logger.debug("Verification scheduling failed: %s", e)
            self._set_timer("verify_actions", now)

        # 16. Periodic: engagement anomaly scan (every hour)
        if now - self._timer("anomaly_scan") >= ANOMALY_SCAN_SECONDS:
            try:
                from ..services.anomaly_detector import run_anomaly_scan

                anomalies = await run_anomaly_scan()
                if anomalies:
                    logger.warning(
                        "Anomaly scan found %d issues: %s",
                        len(anomalies),
                        ", ".join(a.anomaly_type for a in anomalies),
                    )
            except Exception as e:
                logger.debug("Anomaly scan failed: %s", e)
            self._set_timer("anomaly_scan", now)

        # 18. Periodic: distributed post collection (every 30 min)
        if now - self._timer("distributed_post_scan") >= DISTRIBUTED_SCAN_SECONDS:
            try:
                from ..db.queries import create_scheduler_job, get_pending_job_count
                pending = await run_db(get_pending_job_count, None, JOB_COLLECT_POSTS_DISTRIBUTED)
                if pending == 0:
                    await run_db(create_scheduler_job, None, JOB_COLLECT_POSTS_DISTRIBUTED, now)
                    tick_planned += 1
            except Exception as e:
                logger.debug("Distributed post scan scheduling failed: %s", e)
            self._set_timer("distributed_post_scan", now)

        # 19. Periodic: contact research pipeline (every 15 min)
        if now - self._timer("research_contacts") >= 900:
            try:
                from ..db.queries import create_scheduler_job, get_pending_job_count
                pending = await run_db(get_pending_job_count, None, JOB_RESEARCH_CONTACTS)
                if pending == 0:
                    await run_db(create_scheduler_job, None, JOB_RESEARCH_CONTACTS, now)
                    tick_planned += 1
            except Exception as e:
                logger.debug("Contact research scheduling failed: %s", e)
            self._set_timer("research_contacts", now)

        # 20. Periodic: viral post detection (every 1 hour)
        if now - self._timer("viral_detection") >= VIRAL_DETECTION_SECONDS:
            try:
                from ..db.queries import create_scheduler_job, get_pending_job_count
                pending = await run_db(get_pending_job_count, None, JOB_DETECT_VIRAL_POSTS)
                if pending == 0:
                    await run_db(create_scheduler_job, None, JOB_DETECT_VIRAL_POSTS, now)
                    tick_planned += 1
            except Exception as e:
                logger.debug("Viral detection scheduling failed: %s", e)
            self._set_timer("viral_detection", now)

        # 21. Periodic: watchlist auto-tuning (once per day)
        if now - self._timer("watchlist_tune") >= WATCHLIST_TUNE_SECONDS:
            try:
                from ..db.queries import create_scheduler_job, get_pending_job_count
                pending = await run_db(get_pending_job_count, None, JOB_TUNE_WATCHLISTS)
                if pending == 0:
                    await run_db(create_scheduler_job, None, JOB_TUNE_WATCHLISTS, now)
                    tick_planned += 1
            except Exception as e:
                logger.debug("Watchlist tuning scheduling failed: %s", e)
            self._set_timer("watchlist_tune", now)

        # 22. Periodic: signal self-optimization (once per day)
        if now - self._timer("signal_optimize") >= SIGNAL_OPTIMIZE_SECONDS:
            try:
                from ..db.queries import create_scheduler_job, get_pending_job_count
                pending = await run_db(get_pending_job_count, None, JOB_OPTIMIZE_SIGNALS)
                if pending == 0:
                    await run_db(create_scheduler_job, None, JOB_OPTIMIZE_SIGNALS, now)
                    tick_planned += 1
            except Exception as e:
                logger.debug("Signal optimization scheduling failed: %s", e)
            self._set_timer("signal_optimize", now)

        # 23. One-shot: backfill unanalyzed posts (runs once on startup, then every 4 hours)
        if now - self._timer("backfill_analysis") >= 14400:
            try:
                from ..db.queries import create_scheduler_job, get_pending_job_count
                pending = await run_db(get_pending_job_count, None, JOB_BACKFILL_POST_ANALYSIS)
                if pending == 0:
                    await run_db(create_scheduler_job, None, JOB_BACKFILL_POST_ANALYSIS, now)
                    tick_planned += 1
            except Exception as e:
                logger.debug("Backfill analysis scheduling failed: %s", e)
            self._set_timer("backfill_analysis", now)

        # 23. Periodic: outreach health watchdog (every 5 min)
        if now - self._timer("outreach_watchdog") >= 300:
            await self._outreach_health_watchdog(now)
            self._set_timer("outreach_watchdog", now)

        # ── Flush Unipile API metrics every tick ──
        try:
            from ..linkedin.api_metrics import api_metrics
            from ..linkedin.voyager_health import voyager_health

            metrics_summary = api_metrics.summary()
            if metrics_summary:
                await run_db(
                    log_scheduler_event, "api_metrics",
                    context=metrics_summary,
                )
                api_metrics.reset()

            # API health snapshot every 30 min (every 6 ticks)
            if self._tick_count % 6 == 0:
                await run_db(
                    log_scheduler_event, "api_health_snapshot",
                    context={
                        "voyager_health": voyager_health.summary(),
                    },
                )
        except Exception as e:
            logger.debug("API metrics flush failed: %s", e)

        await self._finish_tick_bookkeeping(
            tick_processed, tick_failed, tick_planned, mode,
        )

    async def _finish_tick_bookkeeping(
        self,
        tick_processed: int,
        tick_failed: int,
        tick_planned: int,
        mode: str,
    ) -> None:
        """Timer persistence, event cleanup, and tick_summary.

        Observe used to return before this ran, so a reboot replayed every
        collector and scheduler_events grew for the whole observe lifetime.
        """
        observing = mode != "full"
        logger.info(
            "tick#%d: processed=%d failed=%d planned=%d mode=%s%s",
            self._tick_count, tick_processed, tick_failed, tick_planned, mode,
            " (observing, not sending)" if observing else "",
        )
        await run_db(
            log_scheduler_event, "tick_summary",
            context={
                "tick": self._tick_count,
                "processed": tick_processed,
                "failed": tick_failed,
                "planned": tick_planned,
                "scheduler": "observe" if observing else "on",
            },
        )
        if self._tick_count % 5 == 0:
            try:
                await run_db(save_setting, self._TIMERS_SETTING_KEY, self._timers)
            except Exception as e:
                logger.debug("Timer persistence failed: %s", e)
            try:
                await run_db(cleanup_scheduler_events, days=7)
            except Exception as e:
                logger.debug("Event cleanup failed: %s", e)

    async def _process_ready_jobs(
        self, *, scheduler_on: bool = True, mode: str | None = None,
        deadline: float | None = None,
    ) -> tuple[int, int]:
        """Find and execute jobs that are ready (scheduled_at <= now).

        When ``scheduler_on`` is False, only inbound job types are executed;
        outbound jobs stay in the queue until the scheduler is re-enabled.

        Jobs are executed concurrently (up to 5 at a time) to avoid slow
        collectors blocking the entire queue.

        ``deadline`` is a time.monotonic() value past which no *new* job is
        started. It exists because the run loop caps a tick at 55s while this
        method would claim up to 20 jobs and run them in four waves of five —
        so the tick was routinely cancelled mid-flight, and every job claimed
        but not yet reached stayed marked `running` until the 30-minute
        stuck-job sweeper released it. Claiming happens per job, once it has a
        slot, so work that never starts is never claimed and simply waits for
        the next tick.

        It bounds the pile-up, not a job already in flight: a job that has
        started may still run to its own timeout (120s, 300s for the slow
        collectors), which is longer than the whole tick budget.

        Returns:
            (jobs_processed, jobs_failed) counts for tick summary.
        """
        from .executors import execute_job, categorize_error

        # ── Post-outage backlog detection ──
        # If many jobs are stale (scheduled_at > 10 min ago), the scheduler
        # was likely offline. Re-stagger them to avoid a burst that overwhelms
        # the LinkedIn API.
        _STALE_THRESHOLD = 600   # 10 min — jobs older than this are "stale"
        _BACKLOG_MIN = 20        # only act if 20+ stale jobs (normal queue is 0-10)
        _SPREAD_SECONDS = 30     # 1 job every 30s — 100 jobs spread over ~50 min

        stale_count = await run_db(count_stale_ready_jobs, _STALE_THRESHOLD)
        if stale_count >= _BACKLOG_MIN:
            restaggered = await run_db(restagger_stale_jobs, _STALE_THRESHOLD, _SPREAD_SECONDS)
            logger.warning(
                "Post-outage backlog detected: %d stale jobs re-staggered over %d min",
                restaggered, (restaggered * _SPREAD_SECONDS) // 60,
            )
            await run_db(
                log_scheduler_event, "backlog_restaggered",
                context={"stale_count": stale_count, "restaggered": restaggered,
                         "spread_seconds": _SPREAD_SECONDS},
            )

        # Recover stuck jobs (running > 30 min = likely crashed)
        from ..db.queries import recover_stuck_jobs
        stuck_count = await run_db(recover_stuck_jobs, 30)
        if stuck_count:
            logger.warning("Recovered %d stuck jobs (running > 30 min)", stuck_count)

        # Callers that predate modes pass only scheduler_on; map it faithfully.
        if mode is None:
            mode = "full" if scheduler_on else "off"

        ready = await run_db(get_ready_jobs, limit=20)

        # When outbound scheduler is off, only execute inbound/local jobs.
        eligible = [j for j in ready if runs_in_mode(j["job_type"], mode)]
        if not eligible:
            return 0, 0
        from ..services.cloud_sync import any_active_campaign_needs_refill

        refill_critical = await run_db(any_active_campaign_needs_refill)
        if refill_critical:
            # Empty sendable queue is a send-path failure. Do not start other
            # heavy collectors in the same tick — Semaphore(5) would otherwise
            # race them with refill and the 44s budget is lost again.
            eligible = [
                j for j in eligible
                if (j.get("job_type") or "") not in _HEAVY_COLLECTOR_JOBS
                or (j.get("job_type") or "") == "campaign_refill"
            ]
        if not eligible:
            return 0, 0
        eligible.sort(
            key=lambda j: (
                _job_start_priority(
                    j.get("job_type") or "", refill_critical=refill_critical,
                ),
                int(j.get("scheduled_at") or 0),
            ),
        )

        # Track results from concurrent execution
        # True=success, False=failure, None=never started (still pending)
        sem = asyncio.Semaphore(5)
        skipped = 0
        heavy_started = False
        heavy_lock = asyncio.Lock()

        async def _run_one(job: dict) -> bool | None:
            nonlocal skipped, heavy_started
            async with sem:
                job_id = job["id"]
                job_type = job["job_type"]

                # Budget is checked here, not upfront: a job only matters once
                # it has a slot. Past the deadline we leave it unclaimed, so it
                # stays `pending` and the next tick takes it — the ordinary
                # path, rather than the sweeper's recovery path.
                if deadline is not None and time.monotonic() >= deadline:
                    skipped += 1
                    return None

                if job_type in _HEAVY_COLLECTOR_JOBS:
                    async with heavy_lock:
                        remaining = (
                            deadline - time.monotonic()
                            if deadline is not None
                            else _HEAVY_MIN_REMAINING_SECONDS + 1
                        )
                        if heavy_started or remaining < _HEAVY_MIN_REMAINING_SECONDS:
                            skipped += 1
                            return None
                        heavy_started = True

                # Claim the job atomically (prevents double execution)
                if not await run_db(claim_job, job_id):
                    logger.debug("Job %s already claimed, skipping", job_id)
                    return None

                self._last_tick_jobs_finished = getattr(self, "_last_tick_jobs_finished", 0) + 1
                cid = new_correlation_id()
                logger.info("Executing scheduler job: %s (type=%s, cid=%s)", job_id, job_type, cid)

                start_ms = int(time.time() * 1000)
                scheduled_at = job.get("scheduled_at", 0)
                queue_wait_ms = int((time.time() - scheduled_at) * 1000) if scheduled_at else 0
                campaign_id = job.get("campaign_id")
                outreach_id = job.get("outreach_id")
                failed = False
                with tracer.start_as_current_span(f"job:{job_type}") as span:
                    span.set_attribute("job.id", job_id)
                    span.set_attribute("job.type", job_type)
                    span.set_attribute("correlation.id", cid)
                    if campaign_id:
                        span.set_attribute("campaign.id", campaign_id)
                    try:
                        # Long-running collectors get 5 min; everything else 2 min
                        job_timeout = 300 if job_type in _SLOW_JOBS else 120
                        # A job allowed to outlive the tick is cancelled by the
                        # run loop's wait_for, and CancelledError is not an
                        # Exception: the handler below never runs, so the row
                        # stays 'running' with no error and no retry recorded.
                        # Timing out inside the tick keeps that bookkeeping.
                        if deadline is not None:
                            job_timeout = max(1, min(job_timeout, int(deadline - time.monotonic())))
                        job["_job_timeout"] = job_timeout
                        result = await asyncio.wait_for(execute_job(job), timeout=job_timeout)
                        duration_ms = int(time.time() * 1000) - start_ms
                        span.set_attribute("job.duration_ms", duration_ms)

                        # Extract structured outcome from JobResult (if returned)
                        from .executors import JobResult
                        if isinstance(result, JobResult):
                            outcome = result.outcome
                            result_msg = result.message
                        else:
                            outcome = "success"
                            result_msg = str(result) if result else ""
                        # Trimmed like error_msg below, and for the same
                        # reason: one runaway summary should not decide the row
                        # size. 500 is generous against a measured p99 of 242
                        # chars; only the keyword collector's watchlist
                        # enumeration has ever exceeded it.
                        result_msg = result_msg[:500]

                        if outcome == "deferred":
                            delay = 3600
                            if isinstance(result, JobResult):
                                delay = max(int(result.retry_after_seconds or 3600), 60)
                            await run_db(
                                reschedule_job, job_id, int(time.time()) + delay,
                            )
                        else:
                            await run_db(complete_job, job_id, duration_ms=duration_ms)
                        event_type = f"job_{outcome}" if outcome != "success" else "job_completed"
                        span.set_attribute("job.outcome", outcome)
                        logger.info(
                            "Job %s %s in %dms (type=%s)",
                            job_id, outcome, duration_ms, job_type,
                        )
                        await run_db(
                            log_scheduler_event, event_type,
                            campaign_id=campaign_id, outreach_id=outreach_id,
                            job_id=job_id, duration_ms=duration_ms,
                            # "result" is the job's own summary of what it
                            # did. Without it a completed event records only
                            # that SOMETHING of this type finished, which
                            # cannot distinguish a run that drained rows from
                            # one that drained none — and bounded jobs now
                            # report their remaining work in this string and
                            # nowhere else. It was computed here and dropped;
                            # the text log was the only copy.
                            context={"job_type": job_type, "outcome": outcome, "correlation_id": cid, "queue_wait_ms": queue_wait_ms, "result": result_msg},
                        )
                    except asyncio.CancelledError:
                        # CancelledError is a BaseException — the handler
                        # below never saw it, so the row stayed `running`
                        # until the 30-minute sweeper. File it as a defer.
                        duration_ms = int(time.time() * 1000) - start_ms
                        span.set_attribute("job.duration_ms", duration_ms)
                        span.set_attribute("job.outcome", "deferred")
                        span.set_attribute("error.category", "tick_budget")
                        try:
                            await asyncio.shield(
                                run_db(complete_job, job_id, duration_ms=duration_ms),
                            )
                        except asyncio.CancelledError:
                            await run_db(complete_job, job_id, duration_ms=duration_ms)
                        from ..ops_log import log_event
                        log_event(
                            "job_executed",
                            job_id=job_id,
                            job_type=job_type,
                            outreach_id=outreach_id or "",
                            campaign_id=campaign_id or "",
                            outcome="deferred",
                            duration_ms=duration_ms,
                            error_category="tick_budget",
                        )
                        logger.info(
                            "Job %s deferred in %dms [cancelled]: tick cancelled",
                            job_id, duration_ms,
                        )
                        try:
                            await asyncio.shield(
                                run_db(
                                    log_scheduler_event, "job_deferred",
                                    campaign_id=campaign_id, outreach_id=outreach_id,
                                    job_id=job_id, duration_ms=duration_ms,
                                    context={
                                        "job_type": job_type,
                                        "outcome": "deferred",
                                        "result": "tick cancelled",
                                        "error_category": "tick_budget",
                                        "correlation_id": cid,
                                        "queue_wait_ms": queue_wait_ms,
                                    },
                                ),
                            )
                        except asyncio.CancelledError:
                            pass
                        return True
                    except Exception as e:
                        duration_ms = int(time.time() * 1000) - start_ms
                        # A bare asyncio.TimeoutError stringifies to "", and an
                        # empty message used to be filed as a success — every
                        # collector that blew its 300s budget was stored as
                        # status='completed', error=NULL. Substitute the class
                        # name ONLY when there is no message: categorize_error()
                        # pattern-matches this string, so a class-name prefix
                        # would hand it 'http'/'unipile'/'status' for free.
                        error_msg = (str(e) or type(e).__name__)[:500]
                        error_cat = categorize_error(error_msg)
                        # Tick-budget cancel is a defer, not a LinkedIn 5xx.
                        # Collectors already persisted what they finished;
                        # retrying like a network_error burns the next 3 ticks.
                        if isinstance(e, TimeoutError) or error_cat == "tick_budget":
                            error_cat = "tick_budget"
                            failed = False
                            span.set_attribute("job.duration_ms", duration_ms)
                            span.set_attribute("job.outcome", "deferred")
                            span.set_attribute("error.category", error_cat)
                            await run_db(complete_job, job_id, duration_ms=duration_ms)
                            from ..ops_log import log_event
                            log_event(
                                "job_executed",
                                job_id=job_id,
                                job_type=job_type,
                                outreach_id=outreach_id or "",
                                campaign_id=campaign_id or "",
                                outcome="deferred",
                                duration_ms=duration_ms,
                                error_category="tick_budget",
                            )
                            logger.info(
                                "Job %s deferred in %dms [tick_budget]: %s",
                                job_id, duration_ms, error_msg,
                            )
                            await run_db(
                                log_scheduler_event, "job_deferred",
                                campaign_id=campaign_id, outreach_id=outreach_id,
                                job_id=job_id, duration_ms=duration_ms,
                                context={
                                    "job_type": job_type,
                                    "outcome": "deferred",
                                    "result": error_msg[:200],
                                    "error_category": error_cat,
                                    "correlation_id": cid,
                                    "queue_wait_ms": queue_wait_ms,
                                },
                            )
                        else:
                            failed = True
                            error_tb = _tb.format_exc()[-1500:]
                            span.set_attribute("job.duration_ms", duration_ms)
                            span.set_attribute("error.category", error_cat)
                            span.record_exception(e)
                            await run_db(
                                complete_job, job_id, error=error_msg, duration_ms=duration_ms,
                            )
                            from ..ops_log import log_event
                            log_event(
                                "job_executed",
                                job_id=job_id,
                                job_type=job_type,
                                outreach_id=outreach_id or "",
                                campaign_id=campaign_id or "",
                                outcome="error",
                                duration_ms=duration_ms,
                                error_category=error_cat,
                            )
                            logger.warning(
                                "Job %s failed in %dms [%s]: %s",
                                job_id, duration_ms, error_cat, error_msg,
                                exc_info=True,
                            )
                            await run_db(
                                log_scheduler_event, "job_failed",
                                campaign_id=campaign_id, outreach_id=outreach_id,
                                job_id=job_id, duration_ms=duration_ms,
                                context={
                                    "job_type": job_type,
                                    "error": error_msg[:200],
                                    "error_category": error_cat,
                                    "error_traceback": error_tb,
                                    "retry_count": job.get("retry_count", 0) + 1,
                                    "correlation_id": cid,
                                    "queue_wait_ms": queue_wait_ms,
                                },
                            )
                    finally:
                        clear_correlation_id()

                    # Retry failed jobs if under max retries
                    if failed:
                        # Permanent failures (not connected, premium required,
                        # invalid profile) will never resolve — skip ALL retries
                        # to avoid wasting API calls and log noise.
                        if error_cat == "permanent_failure":
                            logger.info(
                                "Job %s permanent failure — no retry (category=%s)",
                                job_id, error_cat,
                            )
                        else:
                            retry_count = job.get("retry_count", 0) + 1
                            if retry_count < SCHEDULER_MAX_RETRIES:
                                new_time = int(time.time()) + SCHEDULER_RETRY_DELAY
                                await run_db(retry_job, job_id, new_time)
                                logger.info("Job %s scheduled for retry #%d", job_id, retry_count)
                            elif job_type in ("send_dm", "followup", "auto_reply", "invite"):
                                # All fast retries exhausted for critical job types.
                                # Back off further so the prospect isn't
                                # permanently abandoned. The SAME row is
                                # rescheduled: retry_count only survives on the
                                # original row, and a fresh row would start at 0
                                # and climb back to escalation index 0 forever,
                                # pinning the ladder to its first rung.
                                _ESCALATION_DELAYS = [3600, 14400, 86400]  # 1h, 4h, 24h
                                escalation_idx = retry_count - SCHEDULER_MAX_RETRIES
                                if escalation_idx < len(_ESCALATION_DELAYS):
                                    delay = _ESCALATION_DELAYS[escalation_idx]
                                    new_time = int(time.time()) + delay
                                    await run_db(retry_job, job_id, new_time)
                                    logger.info(
                                        "Escalated retry for %s job (outreach=%s) in %d min",
                                        job_type, outreach_id, delay // 60,
                                    )

                return not failed

        results = await asyncio.gather(*[_run_one(j) for j in eligible])

        # None means never started — neither processed nor failed, or the tick
        # summary would report unstarted work as failures.
        jobs_processed = sum(1 for ok in results if ok is True)
        jobs_failed = sum(1 for ok in results if ok is False)
        if skipped:
            logger.info(
                "Tick budget spent — %d ready job(s) left for the next tick", skipped,
            )
        return jobs_processed, jobs_failed

    async def _schedule_new_work(self) -> None:
        """Schedule new invite and follow-up jobs for active autopilot campaigns.

        Also schedules follow-ups for completed campaigns — prospects may accept
        invitations days after the campaign finished, and they still need their
        initial DM / follow-up sequence.
        """
        from ..correlation import new_correlation_id
        from .planner import plan_campaign_work, _plan_followups

        new_correlation_id()
        campaigns = await run_db(list_campaigns, status=STATUS_ACTIVE)
        for campaign in campaigns:
            # Only schedule for autopilot campaigns
            if campaign.get("mode") != "autopilot":
                continue

            try:
                await plan_campaign_work(campaign["id"])
            except Exception as e:
                logger.warning("Planning failed for campaign %s: %s", campaign["id"], e)

        # Completed campaigns: no new invites or engagements, but the sequence
        # already owed to someone who accepted still runs. _plan_followups
        # skips followup_count == 0 by design — it leaves the initial DM to
        # _plan_dms, which only runs for active campaigns — so a late accepter
        # had nothing scheduling their first message at all. _rescue_orphans
        # already knows how to queue it; the age bound keeps it to acceptances
        # recent enough that a first message still reads as a reply.
        from ..config import get_tier
        from ..constants import LATE_ACCEPTER_MAX_AGE_DAYS
        from .planner import _rescue_orphans
        completed = await run_db(list_campaigns, status=STATUS_COMPLETED)
        tier = get_tier()
        for campaign in completed:
            try:
                await _plan_followups(campaign["id"], int(time.time()), tier)
                await _rescue_orphans(
                    campaign["id"], int(time.time()), tier,
                    max_accepted_age_days=LATE_ACCEPTER_MAX_AGE_DAYS,
                )
            except Exception as e:
                logger.debug("Follow-up planning failed for completed campaign %s: %s", campaign["id"], e)

    async def _check_auto_resume(self) -> None:
        """Resume autopilot campaigns that were system-paused due to weekly limits.

        Copilot campaigns are not auto-resumed — they get a suggestion
        in suggest_next_action() for the user to resume manually.
        """
        import json as _json
        from ..db.queries import log_action, update_campaign
        from ..linkedin.rate_limiter import can_send_now, BLOCK_DAILY, BLOCK_WEEKLY

        paused = await run_db(list_campaigns, status="paused")
        if not paused:
            return

        eligible = []
        for campaign in paused:
            cfg = _json.loads(campaign.get("config_json") or "{}")
            # Only auto-resume campaigns paused by the system for weekly limits
            if cfg.get("pause_reason") != "weekly_limit":
                continue
            # Only auto-resume autopilot campaigns
            if campaign.get("mode") != "autopilot":
                continue
            eligible.append((campaign, cfg))

        if not eligible:
            return

        # All campaigns share the same LinkedIn account, so check limits once.
        # This is a probe, not a send — booking a slot here spent an invite
        # every tick for as long as any campaign sat paused.
        can_send, reason, block_type = await can_send_now(reserve=False)
        if block_type in (BLOCK_DAILY, BLOCK_WEEKLY):
            return  # Still limited — nothing to resume

        from .. import config as _config
        from ..services.cloud_sync import sync_campaign_status_explained

        for campaign, cfg in eligible:
            # Clear pause metadata and resume. Two keys and the status in one
            # statement: cfg was read before the awaited limit probe above, and
            # writing it all back reverted anything saved since (#482).
            cfg.pop("pause_reason", None)
            cfg.pop("paused_at", None)
            from ..db import queries as _queries

            await run_db(
                _queries.merge_campaign_config,
                campaign["id"],
                remove=["pause_reason", "paused_at"],
                status="active",
            )

            # Resume in the cloud through /resume, the only call that can move
            # a paused cloud campaign to active since heylead-api #328 (the
            # periodic push no longer repairs a failed one). The outcome goes
            # into the audit row, so a refusal is visible in status_history.
            details = {
                "campaign_id": campaign["id"],
                "campaign_name": campaign["name"],
                "old_status": "paused",
                "new_status": "active",
                "changed_by": "scheduler",
                "reason": "weekly_limit_cleared",
            }
            if _config.is_backend_mode():
                try:
                    resumed, cloud_detail = await sync_campaign_status_explained(
                        campaign["id"], "active",
                        caller="scheduler", reason="weekly_limit_cleared",
                    )
                except Exception as exc:
                    resumed, cloud_detail = False, str(exc)
                details["cloud_resumed"] = resumed
                if not resumed:
                    details["cloud_detail"] = cloud_detail
                    logger.warning(
                        "Auto-resume of %s was not resumed in the cloud (%s); "
                        "retry with campaign(action='resume')",
                        campaign["id"], cloud_detail or "no answer",
                    )
            await run_db(
                log_action,
                "campaign_status_change",
                result="active",
                details=details,
            )
            await run_db(
                log_scheduler_event,
                "campaign_auto_resumed",
                campaign_id=campaign["id"],
                context={
                    "campaign_name": campaign["name"],
                    "reason": "weekly_limit_cleared",
                },
            )

            logger.info(
                "Auto-resumed campaign %s: %s (weekly limit cleared)",
                campaign["id"], campaign["name"],
            )

    async def _schedule_reply_checks(self) -> None:
        """Schedule one account-wide reply check job.

        run_check_replies() takes no campaign argument: it pulls the 50 most
        recent chats once and matches them against every outreach. One job per
        campaign therefore repeated the identical fetch — six polled campaigns
        (four of them completed, some since March) meant six identical inbox
        fetches every five minutes for one inbox's worth of replies.

        Completed campaigns still keep the job scheduled: prospects may reply
        days after the last outreach, and run_check_replies also drives the
        silent-connection sync and the hosted-mode webhook drain, none of which
        are campaign-scoped either.
        """
        from ..db.queries import create_scheduler_job, get_pending_job_count

        active = await run_db(list_campaigns, status=STATUS_ACTIVE)
        completed = await run_db(list_campaigns, status=STATUS_COMPLETED)
        if not (active or completed):
            return

        if await run_db(get_pending_job_count, None, JOB_CHECK_REPLIES) > 0:
            logger.debug("Reply check already pending — not queuing another")
            return

        await run_db(
            create_scheduler_job,
            campaign_id=None,
            job_type=JOB_CHECK_REPLIES,
            scheduled_at=int(time.time()),  # Execute immediately
        )
        logger.debug(
            "Scheduled account-wide reply check (%d campaigns polled)",
            len(active) + len(completed),
        )

    async def _schedule_auto_replies(self) -> None:
        """Schedule auto-reply jobs for active and completed campaigns.

        Completed campaigns still need auto-replies — prospects may reply
        days after the last outreach was sent.
        """
        from .planner import plan_auto_replies, plan_inbound_auto_replies

        active = await run_db(list_campaigns, status=STATUS_ACTIVE)
        completed = await run_db(list_campaigns, status=STATUS_COMPLETED)
        campaigns = active + completed
        for campaign in campaigns:
            try:
                await plan_auto_replies(campaign["id"])
            except Exception as e:
                logger.debug(
                    "Auto-reply planning failed for campaign %s: %s",
                    campaign["id"], e,
                )

        # Also plan auto-replies for inbound (campaign-less) outreaches
        try:
            await plan_inbound_auto_replies()
        except Exception as e:
            logger.debug("Inbound auto-reply planning failed: %s", e)

    async def _schedule_profile_views(self) -> None:
        """Schedule profile view warm-up jobs for active autopilot campaigns.

        Profile views are the lightest warm-up touch. The sequence is:
        Profile View → Follow → Endorse → Engage → Invite → Follow-up DM.
        """
        from .planner import plan_profile_views

        campaigns = await run_db(list_campaigns, status=STATUS_ACTIVE)
        for campaign in campaigns:
            if campaign.get("mode") != "autopilot":
                continue

            try:
                await plan_profile_views(campaign["id"])
            except Exception as e:
                logger.debug("Profile view planning failed for campaign %s: %s", campaign["id"], e)

    async def _schedule_follows(self) -> None:
        """Schedule follow warm-up jobs for active autopilot campaigns.

        Follows prospects before engaging with posts to warm them up.
        The sequence is: Follow → Engage → Invite → Follow-up DM.
        """
        from .planner import plan_follows

        campaigns = await run_db(list_campaigns, status=STATUS_ACTIVE)
        for campaign in campaigns:
            if campaign.get("mode") != "autopilot":
                continue

            try:
                await plan_follows(campaign["id"])
            except Exception as e:
                logger.debug("Follow planning failed for campaign %s: %s", campaign["id"], e)

    async def _schedule_process_inbound(self) -> None:
        """Schedule unified classify-first inbound pipeline.

        Account-level (not campaign-specific). Detects invitations + messages,
        classifies all signals via AI, then acts (accept/ignore/DM).
        """
        from .planner import plan_process_inbound

        try:
            await plan_process_inbound()
        except Exception as e:
            logger.debug("Inbound pipeline planning failed: %s", e)

    async def _schedule_check_post_comments(self) -> None:
        """Schedule checking published posts for new comments."""
        from .planner import plan_check_post_comments

        try:
            await plan_check_post_comments()
        except Exception as e:
            logger.debug("Post comment check planning failed: %s", e)

    async def _schedule_keyword_signal_collection(self) -> None:
        """Schedule keyword signal collection from LinkedIn posts."""
        from .planner import plan_keyword_signal_collection

        try:
            await plan_keyword_signal_collection()
        except Exception as e:
            logger.debug("Keyword signal collection planning failed: %s", e)

    async def _schedule_network_post_scan(self) -> None:
        """Queue a scan of 1st-degree connections' posts (no campaign needed)."""
        from ..db.queries import create_scheduler_job, get_pending_job_count

        pending = await run_db(get_pending_job_count, None, JOB_SCAN_NETWORK_POSTS)
        if pending:
            return
        await run_db(create_scheduler_job, None, JOB_SCAN_NETWORK_POSTS, int(time.time()))
        logger.info("Scheduled network post scan")

    async def _schedule_prospect_post_scan(self) -> None:
        """Schedule scanning campaign contacts' posts for signals."""
        from .planner import plan_prospect_post_scan

        try:
            await plan_prospect_post_scan()
        except Exception as e:
            logger.debug("Prospect post scan planning failed: %s", e)

    async def _schedule_signal_classification(self) -> None:
        """Schedule classification of pending signals."""
        from .planner import plan_signal_classification

        try:
            await plan_signal_classification()
        except Exception as e:
            logger.debug("Signal classification planning failed: %s", e)

    async def _schedule_profile_view_collection(self) -> None:
        """Schedule collection of LinkedIn profile viewers."""
        from .planner import plan_profile_view_collection

        try:
            await plan_profile_view_collection()
        except Exception as e:
            logger.debug("Profile view collection planning failed: %s", e)

    async def _schedule_job_change_detection(self) -> None:
        """Schedule job change detection for campaign contacts."""
        from .planner import plan_job_change_detection

        try:
            await plan_job_change_detection()
        except Exception as e:
            logger.debug("Job change detection planning failed: %s", e)

    async def _schedule_competitor_signal_collection(self) -> None:
        """Schedule competitor mention collection from LinkedIn posts."""
        from .planner import plan_competitor_signal_collection

        try:
            await plan_competitor_signal_collection()
        except Exception as e:
            logger.debug("Competitor signal collection planning failed: %s", e)

    async def _schedule_signal_activation(self) -> None:
        """Schedule signal activation — convert high-scoring signals to outreach."""
        from .planner import plan_signal_activation

        try:
            await plan_signal_activation()
        except Exception as e:
            logger.debug("Signal activation planning failed: %s", e)

    async def _schedule_hiring_signal_collection(self) -> None:
        """Schedule hiring surge detection from LinkedIn job searches."""
        from .planner import plan_hiring_signal_collection

        try:
            await plan_hiring_signal_collection()
        except Exception as e:
            logger.debug("Hiring signal collection planning failed: %s", e)

    async def _schedule_news_signal_collection(self) -> None:
        """Schedule news/funding signal collection from SERPER API."""
        from .planner import plan_news_signal_collection

        try:
            await plan_news_signal_collection()
        except Exception as e:
            logger.debug("News signal collection planning failed: %s", e)

    async def _schedule_watchlist_web_collection(self) -> None:
        """Schedule off-LinkedIn watchlist web collection from SERPER search."""
        from .planner import plan_watchlist_web_collection

        try:
            await plan_watchlist_web_collection()
        except Exception as e:
            logger.debug("Watchlist web collection planning failed: %s", e)

    async def _schedule_company_page_collection(self) -> None:
        """Schedule company page engagement signal collection."""
        from .planner import plan_company_page_collection

        try:
            await plan_company_page_collection()
        except Exception as e:
            logger.debug("Company page collection planning failed: %s", e)

    async def _schedule_company_follower_collection(self) -> None:
        """Schedule company follower signal collection."""
        from .planner import plan_company_follower_collection

        try:
            await plan_company_follower_collection()
        except Exception as e:
            logger.debug("Company follower collection planning failed: %s", e)

    async def _schedule_post_intent_classification(self) -> None:
        """Schedule post intent classification for granular buyer signals."""
        from .planner import plan_post_intent_classification

        try:
            await plan_post_intent_classification()
        except Exception as e:
            logger.debug("Post intent classification planning failed: %s", e)

    async def _schedule_comment_mining(self) -> None:
        """Schedule competitor/industry comment mining for lead signals."""
        from .planner import plan_comment_mining

        try:
            await plan_comment_mining()
        except Exception as e:
            logger.debug("Comment mining planning failed: %s", e)

    async def _schedule_compound_intent_detection(self) -> None:
        """Schedule compound intent detection — stack signals into intent events."""
        from .planner import plan_compound_intent_detection

        try:
            await plan_compound_intent_detection()
        except Exception as e:
            logger.debug("Compound intent detection planning failed: %s", e)

    async def _schedule_decay_cycle(self) -> None:
        """Schedule signal decay cycle — expire old signals and recompute scores."""
        from .planner import plan_decay_cycle

        try:
            await plan_decay_cycle()
        except Exception as e:
            logger.debug("Decay cycle planning failed: %s", e)

    async def _schedule_signal_rematch(self) -> None:
        """Schedule re-matching of homeless signals to active campaigns."""
        from .planner import plan_signal_rematch

        try:
            await plan_signal_rematch()
        except Exception as e:
            logger.debug("Signal rematch planning failed: %s", e)

    async def _schedule_orphan_backfill(self) -> None:
        """Schedule backfill of orphan signals to known contacts."""
        from .planner import plan_orphan_signal_backfill

        try:
            await plan_orphan_signal_backfill()
        except Exception as e:
            logger.debug("Orphan signal backfill planning failed: %s", e)

    async def _schedule_endorsements(self) -> None:
        """Schedule skill endorsement warm-up jobs for active autopilot campaigns.

        Endorsements go between follow and engage in the warm-up sequence:
        Follow → Endorse → Engage → Invite → Follow-up DM.
        """
        from .planner import plan_endorsements

        campaigns = await run_db(list_campaigns, status=STATUS_ACTIVE)
        for campaign in campaigns:
            if campaign.get("mode") != "autopilot":
                continue

            try:
                await plan_endorsements(campaign["id"])
            except Exception as e:
                logger.debug("Endorsement planning failed for campaign %s: %s", campaign["id"], e)

    async def _schedule_stale_withdrawals(self) -> None:
        """Schedule stale invitation withdrawal checks.

        Withdraws sent invitations older than STALE_INVITE_DAYS to free up
        LinkedIn invitation quota.
        """
        from .planner import plan_stale_invite_withdrawals

        try:
            await plan_stale_invite_withdrawals()
        except Exception as e:
            logger.debug("Stale invite withdrawal planning failed: %s", e)

    async def _schedule_recover_stuck_outreaches(self) -> None:
        """Park proven-unsendable rows and reset first-degree DM errors."""
        from .planner import plan_recover_stuck_outreaches

        try:
            await plan_recover_stuck_outreaches()
        except Exception as e:
            logger.debug("Stuck-outreach recovery planning failed: %s", e)

    async def _schedule_sn_redetect(self) -> None:
        """Schedule the daily Sales Navigator / Premium re-detection probe.

        Keeps the stored has_sales_navigator / premium_search_has_sn flags
        honest against licence purchases and lapses after setup.
        """
        from .planner import plan_sales_nav_redetect

        try:
            await plan_sales_nav_redetect()
        except Exception as e:
            logger.debug("Sales Nav re-detect planning failed: %s", e)

    async def _schedule_engagements(self) -> None:
        """Schedule engagement warm-up jobs for active autopilot campaigns."""
        from .planner import plan_engagements

        campaigns = await run_db(list_campaigns, status=STATUS_ACTIVE)
        for campaign in campaigns:
            if campaign.get("mode") != "autopilot":
                continue

            try:
                await plan_engagements(campaign["id"])
            except Exception as e:
                logger.debug("Engagement planning failed for campaign %s: %s", campaign["id"], e)

    # ── Brand strategy scheduling ──

    async def _schedule_brand_lifecycle(self) -> None:
        """Check and manage brand strategy lifecycle (auto-analyze, auto-plan)."""
        from .planner import plan_brand_lifecycle

        try:
            await plan_brand_lifecycle()
        except Exception as e:
            logger.debug("Brand lifecycle planning failed: %s", e)

    async def _schedule_brand_posts(self) -> None:
        """Schedule brand post jobs if plan has pending post actions."""
        from .planner import plan_brand_post

        try:
            await plan_brand_post()
        except Exception as e:
            logger.debug("Brand post planning failed: %s", e)

    async def _schedule_brand_profile(self) -> None:
        """Schedule brand profile jobs if the plan has pending profile actions."""
        from .planner import plan_brand_profile

        try:
            await plan_brand_profile()
        except Exception as e:
            logger.debug("Brand profile planning failed: %s", e)

    async def _schedule_brand_engagements(self) -> None:
        """Schedule brand engagement jobs if plan has pending engagement actions."""
        from .planner import plan_brand_engagement

        try:
            await plan_brand_engagement()
        except Exception as e:
            logger.debug("Brand engagement planning failed: %s", e)

    async def _schedule_campaign_refill(self) -> None:
        """Schedule campaign refill — search for more prospects when running low."""
        from .planner import plan_campaign_refill

        try:
            await plan_campaign_refill()
        except Exception as e:
            logger.debug("Campaign refill planning failed: %s", e)

    async def _schedule_backfill_profiles(self) -> None:
        """Schedule profile backfill for contacts with empty profile_json."""
        from .planner import plan_backfill_profiles

        try:
            await plan_backfill_profiles()
        except Exception as e:
            logger.debug("Profile backfill planning failed: %s", e)
