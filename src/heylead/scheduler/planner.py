"""Work planner — determines WHAT to schedule and WHEN.

Analyzes active autopilot campaigns and creates scheduler jobs for:
1. Follows: warm-up follows on prospect profiles (new in Sprint 28)
2. Invitations: pending outreaches that need to be sent
3. Follow-ups: connected outreaches that are due based on schedule
4. Engagements: warm-up comments/reactions before inviting

All decisions respect:
- Follow-up schedule (PRO_FOLLOWUP_SCHEDULE_DAYS: 1, 3, 7, 14 days)
- Per-campaign config toggles (enable_follows, enable_engagements, etc.)
- Deduplication (no duplicate pending jobs for same outreach)
- Randomized delays (10-25 min follows, etc.)
- Unused daily invite slots are due immediately (one in flight)
- Unipile/LinkedIn as the authority on rate limits (no proactive caps)
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any

from ..config import apply_free_monthly_caps, get_scheduler_mode, get_tier, is_backend_mode
from ..db.async_bridge import run_db
from ..flags import flag_enabled
from ..constants import (
    ENDORSE_DELAY_MAX,
    ENDORSE_DELAY_MIN,
    ENGAGEMENT_DELAY_MAX,
    ENGAGEMENT_DELAY_MIN,
    FOLLOW_DELAY_MAX,
    FOLLOW_DELAY_MIN,
    PROFILE_VIEW_DELAY_MAX,
    PROFILE_VIEW_DELAY_MIN,
    FOLLOWUP_DELAY_MAX,
    FOLLOWUP_DELAY_MIN,
    FREE_MAX_ENGAGEMENTS,
    FREE_MONTHLY_INVITATIONS,
    DM_DELAY_MAX,
    DM_DELAY_MIN,
    JOB_ACCEPT_INBOUND,
    JOB_AUTO_REPLY,
    JOB_CHECK_POST_COMMENTS,
    JOB_EMAIL_INVITE,
    JOB_ENDORSE,
    JOB_ENGAGE,
    JOB_FOLLOW,
    JOB_PROCESS_INBOUND,
    JOB_PROFILE_VIEW,
    JOB_FOLLOWUP,
    JOB_INMAIL,
    JOB_INVITE,
    JOB_QUALIFY_INBOUND,
    JOB_REDETECT_SALES_NAV,
    JOB_RECOVER_STUCK_OUTREACHES,
    JOB_SEND_DM,
    JOB_WITHDRAW_INVITE,
    PRO_FOLLOWUP_SCHEDULE_DAYS,
    STALE_INVITE_DAYS,
)
from ..db.queries import (
    create_scheduler_job,
    get_campaign,
    get_engagement_candidates,
    get_follow_candidates,
    get_followup_candidates,
    get_monthly_usage,
    get_profile_view_candidates,
    get_messages_for_outreach,
    get_outreach,
    get_pending_job_count,
    get_pending_outreach_job,
    list_campaigns,
)

logger = logging.getLogger(__name__)


def _create_gated_job(
    campaign_id: str | None,
    job_type: str,
    scheduled_at: int,
    outreach_id: str | None = None,
) -> str:
    """Create a scheduler job, unless the current mode would refuse to run it.

    The planner had no knowledge of the mode gate, so in 'observe' it happily
    queued work runs_in_mode() always refuses — 25 profile_view, 21 engage, 19
    follow and a batch of activate_signals in a single day — which the
    stale-pending sweeper then reaped as 'stale_pending_recovered', the same
    label a genuinely crashed job gets.

    Deliberately named differently from ``create_scheduler_job``: every gated
    creation in this module reads ``_create_gated_job``, so a call that bypasses
    the gate is visible at the call site instead of hiding behind a shadowed
    name.

    Returns "" when the job was suppressed, so callers can tell "not created"
    from a real job id.
    """
    # Imported here, not at module scope: engine imports planner lazily inside
    # its own functions, and a top-level import would close that cycle.
    from .engine import runs_in_mode

    # A malformed call must still hit create_scheduler_job's loud ValueError —
    # callers wrap this in `except Exception: logger.debug(...)`, so swallowing
    # it behind the mode gate would silently disable a whole subsystem.
    if isinstance(job_type, str) and job_type:
        # Resolved per creation rather than threaded down from _tick: the gate
        # must read the mode as of the moment the row is written. The cost is
        # one small JSON read against a SQLite connect + INSERT + commit, i.e.
        # noise next to the write it guards.
        mode = get_scheduler_mode()
        if not runs_in_mode(job_type, mode):
            logger.debug(
                "Not scheduling %s job — mode gate would refuse it (mode=%s)",
                job_type, mode,
            )
            return ""

        # Two schedulers, one campaign. Once the backend holds this campaign
        # and its scheduler is on, it is sending the four families it runs
        # every 5 minutes; queueing the same work here is how the same follow-up
        # reaches a prospect twice. Everything the cloud does not do — refill,
        # discovery, signals, brand, inbound — falls straight through.
        from ..services.cloud_sync import (
            CLOUD_SENT_ACCOUNT_JOB_TYPES,
            cloud_owns_account_sending,
            cloud_sends_this_job,
            log_ownership_decision,
        )

        if cloud_sends_this_job(job_type, campaign_id or "", outreach_id or ""):
            log_ownership_decision(
                campaign_id=campaign_id or "",
                job_type=job_type,
                outreach_id=outreach_id or "",
                owner="cloud",
                source="planner",
            )
            logger.info(
                "Not scheduling %s job for campaign %s — the cloud scheduler owns it",
                job_type, (campaign_id or "")[:8],
            )
            skip_action = {
                JOB_SEND_DM: "dm",
                JOB_EMAIL_INVITE: "email",
            }.get(job_type, job_type)
            try:
                from ..db.queries import log_action

                log_action(
                    f"skip_{skip_action}",
                    result="skipped",
                    campaign_id=campaign_id or "",
                    outreach_id=outreach_id or "",
                    details={"reason": "cloud_owned"},
                )
            except Exception:
                pass
            return ""

        # Brand work publishes under the user's name and answers to the account,
        # not to a campaign — there is no campaign_id to look up.
        if job_type in CLOUD_SENT_ACCOUNT_JOB_TYPES and cloud_owns_account_sending():
            logger.debug(
                "Not scheduling %s job — the cloud scheduler publishes for this account",
                job_type,
            )
            return ""

        # A coordinator hold: _execute_auto_reply skips the campaign's reply
        # while one is live, the engine closes that skip as completed, and
        # get_auto_reply_candidates offers a completed row again 30 minutes
        # later -- a reply queued and refused every half hour for as long as
        # the hold stands. The cloud did the same once a pass: 375 auto_reply
        # jobs in 24 hours (heylead-api #957, 23 Sep 2026). Planned actions
        # ask the hold before they get here (execute_planned_actions).
        if job_type == JOB_AUTO_REPLY and campaign_id:
            from ..services.coordinator import coordinator_blocks_send

            if coordinator_blocks_send(campaign_id):
                logger.debug(
                    "Not scheduling auto_reply for campaign %s — coordinator hold",
                    campaign_id[:8],
                )
                return ""
    from .enqueue_gate import GAP_JOB_TYPES, message_gap_blocks

    if job_type in GAP_JOB_TYPES and outreach_id:
        blocked, gap_details = message_gap_blocks(outreach_id)
        if blocked:
            logger.debug(
                "Not scheduling %s for outreach %s — 24h message gap",
                job_type, outreach_id[:8],
            )
            try:
                from ..db.queries import log_action

                log_action(
                    f"skip_{job_type}",
                    result="skipped",
                    campaign_id=campaign_id or "",
                    outreach_id=outreach_id or "",
                    details={"reason": "message_gap", **gap_details},
                )
            except Exception:
                pass
            return ""
    return create_scheduler_job(
        campaign_id=campaign_id,
        job_type=job_type,
        scheduled_at=scheduled_at,
        outreach_id=outreach_id,
    )


_SKIP_SUCCESS_BY_ACTION = {
    "invite": "invitation_sent",
    "inmail": "inmail_sent",
    "dm": "dm_sent",
    "send_dm": "dm_sent",
    "email": "email_sent",
    "email_invite": "email_sent",
    "followup": "dm_sent",
}

# Idle ticks — ops/debug only. Caps, credits, and none_eligible stay in
# actions_log so a 10-hour scan is not 28 empty-queue "failures".
_IDLE_SKIP_REASONS = frozenset({
    "no_candidates",
    "no_schedulable",
    "pending_jobs_full",
    "empty_queue",
})


def _should_write_skip(campaign_id: str, action: str, reason: str) -> bool:
    """True when this skip is a new reason or a send succeeded since the last one."""
    if not campaign_id:
        return True
    from ..db.schema import get_db

    db = get_db()
    row = db.execute(
        """SELECT details_json, timestamp FROM actions_log
           WHERE campaign_id = ? AND action_type = ?
           ORDER BY timestamp DESC LIMIT 1""",
        (campaign_id, f"skip_{action}"),
    ).fetchone()
    last_reason = None
    last_ts = 0
    if row:
        try:
            last_reason = json.loads(row["details_json"] or "{}").get("reason")
        except (json.JSONDecodeError, TypeError):
            last_reason = None
        last_ts = int(row["timestamp"] or 0)
    if last_reason != reason:
        db.close()
        return True
    success_type = _SKIP_SUCCESS_BY_ACTION.get(action)
    if not success_type:
        db.close()
        return False
    hit = db.execute(
        """SELECT 1 FROM actions_log
           WHERE campaign_id = ? AND action_type = ? AND timestamp >= ?
           LIMIT 1""",
        (campaign_id, success_type, last_ts),
    ).fetchone()
    db.close()
    return hit is not None


async def _log_skip(
    campaign_id: str | None,
    action: str,
    reason: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Log a scheduler skip once per (campaign, action, reason) until it clears.

    Idle reasons (empty queue, jobs already in flight) go to ops_log only.
    Real gates still write ``skip_*`` to actions_log.
    """
    try:
        if reason in _IDLE_SKIP_REASONS:
            from ..ops_log import log_event

            log_event(
                "planner_skip",
                campaign_id=campaign_id or "",
                action=action,
                skip_reason=reason,
            )
            return
        if campaign_id and not await run_db(
            _should_write_skip, campaign_id, action, reason,
        ):
            return
        from ..db.queries import log_action
        await run_db(
            log_action,
            action_type=f"skip_{action}",
            result="skipped",
            campaign_id=campaign_id or "",
            details={"reason": reason, **(details or {})},
        )
    except Exception:
        pass  # Never block the planner


async def _empty_or_ineligible(campaign_id: str, kind: str) -> str:
    """empty_queue when the pre-filter pool is vacant, else none_eligible."""
    from ..db.queries import count_first_touch_pool

    n = await run_db(count_first_touch_pool, campaign_id, kind)
    return "empty_queue" if n == 0 else "none_eligible"


def _has_daily_plan(outreach_id: str) -> bool:
    """Check if this outreach has a daily strategy plan for today.

    If a daily plan exists, the communication strategist owns scheduling
    for this prospect — individual plan_*() functions should skip it.
    Fails open: returns False on any error so hardcoded logic takes over.
    """
    try:
        from ..db.strategist_queries import has_daily_plan
        return has_daily_plan(outreach_id)
    except Exception:
        return False


def _skip_if_strategist_owned(outreach_id: str, job_type: str) -> bool:
    """True when today's strategist plan owns this prospect — and log why."""
    if not _has_daily_plan(outreach_id):
        return False
    plan_id = ""
    try:
        from ..db.strategist_queries import get_daily_plan

        plan = get_daily_plan(outreach_id)
        if plan:
            plan_id = plan.get("id") or ""
    except Exception:
        plan_id = ""
    from ..ops_log import log_event

    log_event(
        "skip_strategist_owned",
        outreach_id=outreach_id,
        job_type=job_type,
        skip_reason="daily_plan",
        plan_id=plan_id or None,
    )
    return True


def _has_recent_completed_job(outreach_id: str, job_type: str) -> bool:
    """Check if a job of this type completed recently for this outreach (within 1 hour).

    Prevents duplicate scheduling when the engagement record hasn't been
    written yet but the job already completed.
    """
    try:
        from ..db.schema import get_db
        db = get_db()
        row = db.execute(
            """SELECT 1 FROM scheduler_jobs
               WHERE outreach_id = ? AND job_type = ?
                 AND status = 'completed'
                 AND completed_at > ?
               LIMIT 1""",
            (outreach_id, job_type, int(time.time()) - 3600),
        ).fetchone()
        db.close()
        return row is not None
    except Exception:
        return False


async def _is_invite_blocked() -> bool:
    """Check if LinkedIn invitations are currently blocked (daily or weekly limit).

    Stubbed — no proactive rate limit checks; Unipile/LinkedIn is the authority.
    """
    return False


def _get_campaign_config(campaign_id: str) -> dict[str, Any]:
    """Load and parse config_json for a campaign. Returns empty dict on failure."""
    campaign = get_campaign(campaign_id)
    if not campaign:
        return {}
    try:
        return json.loads(campaign.get("config_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


async def plan_campaign_work(campaign_id: str) -> None:
    """Analyze an autopilot campaign and schedule invite + follow-up jobs.

    Called by the scheduler engine every tick for each active autopilot campaign.
    Creates jobs in the scheduler_jobs table with randomized future scheduled_at times.
    """
    from ..correlation import get_correlation_id, new_correlation_id

    if not get_correlation_id():
        new_correlation_id()
    now = int(time.time())
    tier = get_tier()

    # ── 1. First touch by entitlement ──
    # 1st degree → DM. Premium / Open Profile non-connections → InMail.
    # Invite is the fallback when InMail cannot send.
    config = await run_db(_get_campaign_config, campaign_id)
    if not flag_enabled(config, "enable_invitations"):
        await _plan_dms(campaign_id, now)
    else:
        await _plan_dms(campaign_id, now, only_connected=True)
        await _plan_inmail_first_touch(campaign_id, now, config=config)
        await _plan_invitations(campaign_id, now, config=config)

    # ── 2. InMail fallback ──
    # Escalate invitations that sat quiet past the cadence.
    await _plan_inmail_fallback(campaign_id, now, config=config)

    # ── 2b. Email overflow ──
    # First-touch email only after the LinkedIn invite budget is spent.
    if await _linkedin_invite_budget_spent():
        from ..services.channel_selector import has_email_channel
        if await run_db(has_email_channel):
            await _plan_email_overflow(campaign_id, now)

    # ── 3. Follow-ups ──
    await _plan_followups(campaign_id, now, tier)

    # ── 4. Orphan rescue ──
    # Safety net: find connected prospects with no pending jobs that fell
    # through the cracks (failed jobs, scheduler gaps, etc.) and re-schedule.
    await _rescue_orphans(campaign_id, now, tier)


async def _rescue_orphans(
    campaign_id: str, now: int, tier: str,
    *, max_accepted_age_days: int | None = None,
) -> None:
    """Re-schedule jobs for connected prospects that have no pending work.

    This is a safety net that catches prospects missed by _plan_dms() and
    _plan_followups() — e.g. due to prior failed jobs, scheduler gaps, or
    status transitions that happened outside the normal flow.

    *max_accepted_age_days* bounds how stale an acceptance may be. Left None
    for active campaigns, which are still running and keep the old reach. A
    completed campaign passes a bound, because the only thing it can still owe
    someone is a reply to their acceptance — past that window a first message
    stops reading as one. A NULL ``accepted_at`` fails the bound: it is a date
    we do not have, not a recent one.

    The bound is handed to the query rather than applied to its results,
    because ``find_orphaned_outreaches`` returns only the ten oldest — see its
    docstring. Bounded mode also restricts to ``status='connected'`` there:
    'messaged' means the opening DM already went out, and followup_count == 0
    below turns into a *send_dm*, so rescuing one would send a second opening
    message. A follow-up such a prospect may be owed is _plan_followups' job,
    and that already runs for completed campaigns.
    """
    from ..db import queries as _queries
    from ..services.campaign_plan import effective_max_followups

    config = await run_db(_get_campaign_config, campaign_id)
    # The count the plan promises (#1414): the campaign's max_followups under
    # the tier ceiling, 0 when follow-ups are off. followup_count 0 is the
    # opening message, which the rescue still owes with follow-ups off, so
    # the query bound never drops below 1. This was a literal 2 for Free and
    # the Pro ceiling whatever the campaign said.
    max_followups = max(effective_max_followups(config, tier), 1)
    since = (
        None if max_accepted_age_days is None
        else now - (max_accepted_age_days * 86400)
    )
    orphans = await run_db(
        _queries.find_orphaned_outreaches, campaign_id, max_followups, since,
    )
    if not orphans:
        return

    custom_schedule = config.get("followup_delay_days")
    if custom_schedule and not isinstance(custom_schedule, list):
        custom_schedule = None

    rescued = 0
    for orphan in orphans:
        outreach_id = orphan["outreach_id"]
        followup_count = orphan.get("followup_count", 0)

        # Between two scheduled follow-ups there is legitimately no pending job,
        # so without the cadence check every tick re-rescues the same prospect.
        if followup_count > 0 and not _is_followup_due(
            orphan, tier, custom_schedule=custom_schedule,
        ):
            continue

        if followup_count == 0:
            # Opening DM only. messaged / existing SDR belongs to _plan_followups.
            if (orphan.get("outreach_status") or "") != "connected":
                continue
            job_type = JOB_SEND_DM
        else:
            job_type = JOB_FOLLOWUP

        delay = random.randint(DM_DELAY_MIN, DM_DELAY_MAX) + (rescued * random.randint(300, 600))
        job_id = await run_db(
            _create_gated_job,
            campaign_id=campaign_id,
            job_type=job_type,
            scheduled_at=now + delay,
            outreach_id=outreach_id,
        )
        if not job_id:
            continue
        logger.info(
            "Rescued orphan outreach %s (followup_count=%d) → %s job in %d min",
            outreach_id, followup_count, job_type, delay // 60,
        )
        rescued += 1

    if rescued:
        await _log_skip(campaign_id, "rescue", "orphans_found", {"rescued": rescued})


async def plan_profile_views(campaign_id: str) -> None:
    """Schedule profile view jobs for pending outreaches that haven't been viewed yet.

    Called every 10 min by the scheduler engine. Profile views warm up
    prospects by triggering a "X viewed your profile" notification —
    the lightest possible warm-up signal before following.
    Skipped if enable_profile_views is off in campaign config.
    """
    # Check per-campaign toggle
    config = await run_db(_get_campaign_config, campaign_id)
    if not flag_enabled(config, "enable_profile_views"):
        logger.debug("Profile views disabled for campaign %s", campaign_id)
        await _log_skip(campaign_id, "profile_view", "feature_disabled")
        return
    if not flag_enabled(config, "enable_invitations"):
        await _log_skip(campaign_id, "profile_view", "dm_only_campaign")
        return

    now = int(time.time())

    # Check for existing pending profile view jobs for this campaign (dedup)
    pending_count = await run_db(get_pending_job_count, campaign_id, JOB_PROFILE_VIEW)
    if pending_count >= 5:
        logger.debug("Already %d pending profile view jobs for campaign %s", pending_count, campaign_id)
        await _log_skip(campaign_id, "profile_view", "pending_jobs_full", {"pending": pending_count})
        return

    # Find pending outreaches that haven't been viewed yet
    candidates = await run_db(get_profile_view_candidates, campaign_id)
    if not candidates:
        await _log_skip(campaign_id, "profile_view", "no_candidates")
        return

    if await _daily_cap_blocks("profile_view", campaign_id):
        return

    # Schedule up to 5 profile view jobs with staggered delays
    scheduled = 0
    strategist_skips = 0
    for candidate in candidates:
        outreach_id = candidate["outreach_id"]

        # Skip if this outreach already has a pending profile view job
        if await run_db(get_pending_outreach_job, outreach_id, JOB_PROFILE_VIEW):
            continue

        # Skip if communication strategist owns this prospect's schedule today
        if await run_db(_skip_if_strategist_owned, outreach_id, JOB_PROFILE_VIEW):
            strategist_skips += 1
            continue

        delay = random.randint(PROFILE_VIEW_DELAY_MIN, PROFILE_VIEW_DELAY_MAX)
        scheduled_at = now + delay + (scheduled * random.randint(180, 420))

        await run_db(
            _create_gated_job,
            campaign_id=campaign_id,
            job_type=JOB_PROFILE_VIEW,
            scheduled_at=scheduled_at,
            outreach_id=outreach_id,
        )
        logger.debug(
            "Scheduled profile view for outreach %s in %d min",
            outreach_id, (scheduled_at - now) // 60,
        )
        scheduled += 1
        if scheduled >= 5:
            break

    if scheduled == 0:
        await _log_skip(
            campaign_id, "profile_view", "no_schedulable",
            {"strategist": strategist_skips},
        )


async def plan_follows(campaign_id: str) -> None:
    """Schedule follow jobs for pending outreaches that haven't been followed yet.

    Called every 15 min by the scheduler engine. Follows warm up prospects
    by triggering a "X started following you" notification before connecting.
    Skipped if enable_follows is off in campaign config.
    """
    # Check per-campaign toggle
    config = await run_db(_get_campaign_config, campaign_id)
    if not flag_enabled(config, "enable_follows"):
        logger.debug("Follows disabled for campaign %s", campaign_id)
        await _log_skip(campaign_id, "follow", "feature_disabled")
        return
    if not flag_enabled(config, "enable_invitations"):
        await _log_skip(campaign_id, "follow", "dm_only_campaign")
        return

    now = int(time.time())

    # Check for existing pending follow jobs for this campaign (dedup)
    pending_count = await run_db(get_pending_job_count, campaign_id, JOB_FOLLOW)
    if pending_count >= 4:
        logger.debug("Already %d pending follow jobs for campaign %s", pending_count, campaign_id)
        await _log_skip(campaign_id, "follow", "pending_jobs_full", {"pending": pending_count})
        return

    # Find pending outreaches that haven't been followed yet
    candidates = await run_db(get_follow_candidates, campaign_id)
    if not candidates:
        await _log_skip(campaign_id, "follow", "no_candidates")
        return

    if await _daily_cap_blocks("follow", campaign_id):
        return

    # Schedule up to 4 follow jobs with staggered delays
    scheduled = 0
    strategist_skips = 0
    for candidate in candidates:
        outreach_id = candidate["outreach_id"]

        # Skip if this outreach already has a pending follow job
        if await run_db(get_pending_outreach_job, outreach_id, JOB_FOLLOW):
            continue

        # Skip if a follow job completed recently (race condition guard)
        if await run_db(_has_recent_completed_job, outreach_id, JOB_FOLLOW):
            continue

        # Skip if communication strategist owns this prospect's schedule today
        if await run_db(_skip_if_strategist_owned, outreach_id, JOB_FOLLOW):
            strategist_skips += 1
            continue

        delay = random.randint(FOLLOW_DELAY_MIN, FOLLOW_DELAY_MAX)
        scheduled_at = now + delay + (scheduled * random.randint(300, 600))

        await run_db(
            _create_gated_job,
            campaign_id=campaign_id,
            job_type=JOB_FOLLOW,
            scheduled_at=scheduled_at,
            outreach_id=outreach_id,
        )
        logger.debug(
            "Scheduled follow for outreach %s in %d min",
            outreach_id, (scheduled_at - now) // 60,
        )
        scheduled += 1
        if scheduled >= 4:
            break

    if scheduled == 0:
        await _log_skip(
            campaign_id, "follow", "no_schedulable",
            {"strategist": strategist_skips},
        )


async def plan_engagements(campaign_id: str) -> None:
    """Schedule engagement warm-up jobs for an autopilot campaign.

    Called less frequently (every 30 min) since engagements are lower priority.
    Skipped if enable_engagements is off in campaign config.
    """
    # Check per-campaign toggle
    config = await run_db(_get_campaign_config, campaign_id)
    if await _daily_cap_blocks("comment", campaign_id):
        return
    if await _monthly_free_engagement_cap_blocks():
        await _log_skip(campaign_id, "engage", "monthly_free_cap")
        return
    if not flag_enabled(config, "enable_engagements"):
        logger.debug("Engagements disabled for campaign %s", campaign_id)
        await _log_skip(campaign_id, "engage", "feature_disabled")
        return
    if not flag_enabled(config, "enable_invitations"):
        await _log_skip(campaign_id, "engage", "dm_only_campaign")
        return

    now = int(time.time())
    max_per_outreach = 3

    # Check for existing pending engagement jobs for this campaign
    pending_count = await run_db(get_pending_job_count, campaign_id, JOB_ENGAGE)
    if pending_count >= 4:
        logger.debug("Already %d pending engagement jobs for campaign %s", pending_count, campaign_id)
        await _log_skip(campaign_id, "engage", "pending_jobs_full", {"pending": pending_count})
        return

    # Find engagement candidates
    candidates = await run_db(get_engagement_candidates, campaign_id, max_per_outreach=max_per_outreach)
    if not candidates:
        await _log_skip(campaign_id, "engage", "no_candidates")
        return

    # Schedule up to 4 engagement jobs with staggered delays
    scheduled = 0
    strategist_skips = 0
    for candidate in candidates:
        outreach_id = candidate["outreach_id"]

        # Skip if this outreach already has a pending engagement job
        if await run_db(get_pending_outreach_job, outreach_id, JOB_ENGAGE):
            continue

        # Skip if communication strategist owns this prospect's schedule today
        if await run_db(_skip_if_strategist_owned, outreach_id, JOB_ENGAGE):
            strategist_skips += 1
            continue

        delay = random.randint(ENGAGEMENT_DELAY_MIN, ENGAGEMENT_DELAY_MAX)
        scheduled_at = now + delay + (scheduled * random.randint(300, 600))

        await run_db(
            _create_gated_job,
            campaign_id=campaign_id,
            job_type=JOB_ENGAGE,
            scheduled_at=scheduled_at,
            outreach_id=outreach_id,
        )
        logger.debug(
            "Scheduled engagement for outreach %s in %d min",
            outreach_id, (scheduled_at - now) // 60,
        )
        scheduled += 1
        if scheduled >= 4:
            break

    if scheduled == 0:
        await _log_skip(
            campaign_id, "engage", "no_schedulable",
            {"strategist": strategist_skips},
        )


async def _plan_dms(
    campaign_id: str, now: int, *, only_connected: bool = False,
) -> None:
    """Schedule DM jobs for prospects needing their first message.

    When *only_connected* is False (DM-only campaigns): targets 'pending' or
    'connected' prospects — 'pending' ones are existing connections detected
    via silent-sync.

    When *only_connected* is True (invitation-based campaigns): targets only
    'connected' prospects — those who already accepted an invitation.  This
    prevents racing a DM against a pending invite, which would fail with
    403 subscription_required.

    Schedules up to 5 DM jobs per tick to avoid throughput bottlenecks.
    """
    if await _daily_cap_blocks("dm", campaign_id):
        return
    pending_dms = await run_db(get_pending_job_count, campaign_id, JOB_SEND_DM)
    if pending_dms >= 5:
        await _log_skip(campaign_id, "dm", "pending_jobs_full", {"pending": pending_dms})
        return

    from ..db.queries import get_next_dm_candidate
    from ..constants import MIN_FIT_SCORE_THRESHOLD

    config = await run_db(_get_campaign_config, campaign_id)
    try:
        min_fit = float(config.get("min_fit_score", MIN_FIT_SCORE_THRESHOLD))
    except (TypeError, ValueError):
        min_fit = MIN_FIT_SCORE_THRESHOLD

    # exclude_connections (9 Sep 2026): a pre-existing 1st-degree
    # connection must not be queued for a DM at all. This used to be the path
    # that reached them — the invite planner already excluded 1st-degree, so
    # "connected" simply meant "DM instead of invite".
    from ..services.outreach_channel import exclude_connections_enabled
    exclude_first = exclude_connections_enabled(config)
    campaign_created_at = None
    if exclude_first:
        from ..db.queries import get_campaign
        campaign_row = await run_db(get_campaign, campaign_id)
        campaign_created_at = (campaign_row or {}).get("created_at")

    slots = 5 - pending_dms
    scheduled = 0
    for _ in range(slots):
        outreach = await run_db(
            get_next_dm_candidate, campaign_id, JOB_SEND_DM,
            only_connected=only_connected,
            min_fit_score=min_fit,
            exclude_first_degree=exclude_first,
            campaign_created_at=campaign_created_at,
        )
        if not outreach:
            break

        delay = random.randint(DM_DELAY_MIN, DM_DELAY_MAX) + (scheduled * random.randint(300, 600))
        job_id = await run_db(
            _create_gated_job,
            campaign_id=campaign_id,
            job_type=JOB_SEND_DM,
            scheduled_at=now + delay,
            outreach_id=outreach["id"],
        )
        if not job_id:
            return
        logger.debug(
            "Scheduled DM for outreach %s in campaign %s in %d min",
            outreach["id"], campaign_id, delay // 60,
        )
        scheduled += 1

    if scheduled == 0:
        await _log_skip(
            campaign_id, "dm", await _empty_or_ineligible(campaign_id, "dm"),
        )


async def _plan_invitations(
    campaign_id: str, now: int, *, config: dict[str, Any] | None = None,
) -> None:
    """Schedule invitation jobs for pending outreaches.

    LinkedIn first; falls back to email when LinkedIn limits are hit.
    """
    if config is None:
        config = await run_db(_get_campaign_config, campaign_id)

    # Check LinkedIn pending invitation count — if at limit, auto-withdraw oldest
    try:
        from ..linkedin import get_account_id, get_linkedin_client
        from ..linkedin.rate_limiter import check_pending_limit, withdraw_oldest_to_free_spot
        _acct = await run_db(get_account_id)
        if _acct:
            _client = get_linkedin_client()
            can_send_pending, pending_reason, _ = await check_pending_limit(_client, _acct)
            if not can_send_pending:
                logger.info("Pending limit hit — auto-withdrawing oldest invite to free spot")
                wd_result = await withdraw_oldest_to_free_spot(_client, _acct)
                if wd_result.get("success"):
                    logger.info(
                        "Auto-withdrew invite for %s (%d days old) to free spot",
                        wd_result.get("name", "unknown"), wd_result.get("days_old", 0),
                    )
                else:
                    logger.warning("Auto-withdraw failed: %s — skipping invitations", wd_result.get("error"))
                    await _log_skip(campaign_id, "invite", "pending_limit", {"reason": pending_reason})
                    return
    except Exception as e:
        logger.warning("Pending limit check failed in planner (non-blocking): %s", e)

    if await _daily_cap_blocks("invite", campaign_id):
        return
    if await _monthly_free_invite_cap_blocks():
        await _log_skip(campaign_id, "invite", "monthly_free_cap")
        return

    # One in-flight invite: leftover daily slots are due now, so a backlog
    # of five would send as a burst on the next tick.
    pending_invites = await run_db(get_pending_job_count, campaign_id, JOB_INVITE)
    if pending_invites >= 1:
        logger.debug("Already %d pending invite jobs for campaign %s", pending_invites, campaign_id)
        await _log_skip(campaign_id, "invite", "pending_jobs_full", {"pending": pending_invites})
        return

    # Pick a specific outreach to bind the job to (prevents duplicate sends).
    # exclude_job_type filters out outreaches that already have a pending/running
    # invite job, so the planner doesn't get stuck on the same top-ranked prospect.
    from ..db.queries import get_next_invite_candidate
    from ..tier import get_caps
    from ..constants import MIN_FIT_SCORE_THRESHOLD
    from ..services.outreach_channel import first_touch_inmail_enabled
    caps = await get_caps()
    inmail_on = first_touch_inmail_enabled(config)
    try:
        min_fit = float((config or {}).get("min_fit_score", MIN_FIT_SCORE_THRESHOLD))
    except (TypeError, ValueError):
        min_fit = MIN_FIT_SCORE_THRESHOLD
    outreach = await run_db(
        get_next_invite_candidate,
        campaign_id,
        JOB_INVITE,
        exclude_first_degree=True,
        defer_inmail_first=inmail_on,
        can_send_credit_inmail=caps.can_send_credit_inmail,
        min_fit_score=min_fit,
    )
    if not outreach:
        await _log_skip(
            campaign_id, "invite",
            await _empty_or_ineligible(campaign_id, "invite"),
        )
        return

    # Unused daily room is due now — a Premium upgrade or a higher cap must
    # not sit behind INVITE_DELAY_MIN after the last send.
    scheduled_at = now

    job_id = await run_db(
        _create_gated_job,
        campaign_id=campaign_id,
        job_type=JOB_INVITE,
        scheduled_at=scheduled_at,
        outreach_id=outreach["id"],
    )
    if not job_id:
        return
    logger.debug(
        "Scheduled invitation for outreach %s in campaign %s in %d min",
        outreach["id"], campaign_id, max(0, scheduled_at - now) // 60,
    )


_CAP_ACTION_TO_JOB = {
    "invite": JOB_INVITE,
    "dm": JOB_SEND_DM,
    "followup": JOB_FOLLOWUP,
    "engage": JOB_ENGAGE,
    "comment": JOB_ENGAGE,
    "inmail": JOB_INMAIL,
    "profile_view": JOB_PROFILE_VIEW,
    "follow": JOB_FOLLOW,
}


async def _monthly_free_engagement_cap_blocks() -> bool:
    """True when self-hosted free has spent this month's engagement allowance.

    Hosted billing lives on the host — leftover local ``tier: free`` must not
    stop comments there. Pro never takes this branch.
    """
    if not apply_free_monthly_caps():
        return False
    usage = await run_db(get_monthly_usage)
    return int((usage or {}).get("engagements_sent", 0) or 0) >= FREE_MAX_ENGAGEMENTS


async def _monthly_free_invite_cap_blocks() -> bool:
    """True when self-hosted free has spent this month's invitation allowance.

    Hosted leftover ``tier: free`` and Pro use the LinkedIn daily seat cap
    (premium 50 / confirmed-free 15), not the 50/month product wall.
    """
    if not apply_free_monthly_caps():
        return False
    usage = await run_db(get_monthly_usage)
    return int((usage or {}).get("invitations_sent", 0) or 0) >= FREE_MONTHLY_INVITATIONS


async def _daily_cap_blocks(action_type: str, campaign_id: str = "") -> bool:
    """True when the daily cap has no room — do not enqueue another job.

    ``reserve=False`` so a planner tick cannot book a slot the executor
    will reserve again when it actually sends.
    """
    from ..linkedin.rate_limiter import check_daily_cap, get_daily_cap_summary

    can, current, cap, _block = await check_daily_cap(action_type, reserve=False)
    total_spent = False
    if can:
        summary = await get_daily_cap_summary()
        remaining = int((summary.get("_total") or {}).get("remaining", 1))
        if remaining > 0:
            return False
        total_spent = True
        current = int((summary.get("_total") or {}).get("current", current))
        cap = int((summary.get("_total") or {}).get("cap", cap))
    logger.debug(
        "Daily cap reached for %s (%d/%d) — not enqueueing",
        action_type, current, cap,
    )
    if campaign_id:
        await _log_skip(
            campaign_id, action_type,
            "total_daily_cap" if total_spent else "daily_cap",
            {"current": current, "cap": cap},
        )
    job_type = _CAP_ACTION_TO_JOB.get(action_type)
    if job_type:
        from ..db.queries import cancel_pending_jobs_of_type

        await run_db(cancel_pending_jobs_of_type, job_type)
    return True


async def _linkedin_invite_budget_spent() -> bool:
    """Whether today's LinkedIn invite cap has no remaining room.

    Uses ``get_daily_cap_summary`` so a planner tick cannot reserve a slot.
    """
    from ..linkedin.rate_limiter import get_daily_cap_summary

    summary = await get_daily_cap_summary()
    remaining = (summary.get("invite") or {}).get("remaining", 1)
    return int(remaining) <= 0


async def _plan_email_overflow(campaign_id: str, now: int) -> None:
    """Schedule email invitation jobs when LinkedIn limits are hit.

    Only targets prospects who:
    - Have status = 'pending' (not yet contacted on any channel)
    - Have an email address available
    - Email account is connected
    - Email rate limits are not exhausted
    """
    from ..constants import (
        EMAIL_INVITE_DELAY_MIN,
        EMAIL_INVITE_DELAY_MAX,
        JOB_EMAIL_INVITE,
    )
    from ..services.channel_selector import has_email_channel
    from ..linkedin.rate_limiter import can_send_email_now

    # Guard: email channel must be available
    if not await run_db(has_email_channel):
        logger.debug("Email overflow: no email channel configured")
        await _log_skip(campaign_id, "email", "no_email_channel")
        return

    # Guard: email budget — reserve=False so a planner tick cannot book a slot.
    can_email, email_reason = await can_send_email_now(reserve=False)
    if not can_email:
        logger.debug("Email overflow: %s", email_reason)
        await _log_skip(campaign_id, "email", "rate_limited", {"reason": email_reason})
        return

    # Guard: dedup — don't pile up email jobs
    pending_email_jobs = await run_db(get_pending_job_count, campaign_id, JOB_EMAIL_INVITE)
    if pending_email_jobs >= 2:
        logger.debug(
            "Already %d pending email invite jobs for campaign %s",
            pending_email_jobs, campaign_id,
        )
        await _log_skip(campaign_id, "email", "pending_jobs_full", {"pending": pending_email_jobs})
        return

    # Find pending prospects with email addresses
    from ..db.queries import get_email_eligible_pending_outreaches
    candidates = await run_db(get_email_eligible_pending_outreaches, campaign_id, 5)

    if not candidates:
        logger.debug("Email overflow: no email-eligible prospects in campaign %s", campaign_id)
        await _log_skip(campaign_id, "email", "no_candidates")
        return

    # Schedule one email invite job (highest fit_score first)
    best = candidates[0]
    delay = random.randint(EMAIL_INVITE_DELAY_MIN, EMAIL_INVITE_DELAY_MAX)
    scheduled_at = now + delay

    job_id = await run_db(
        _create_gated_job,
        campaign_id=campaign_id,
        job_type=JOB_EMAIL_INVITE,
        scheduled_at=scheduled_at,
        outreach_id=best["outreach_id"],
    )
    if not job_id:
        return
    logger.info(
        "Email overflow: scheduled email invite for %s (campaign %s) in %d min",
        best.get("name", "unknown"), campaign_id, delay // 60,
    )


async def _plan_inmail_first_touch(
    campaign_id: str, now: int, *, config: dict[str, Any] | None = None,
) -> None:
    """Schedule InMail as the first touch for pending non-connections.

    Premium / Open Profile strangers go here. Invite leftovers are
    ``_plan_invitations``. Credit gate and CAS live in the send tool.
    """
    from ..constants import INMAIL_DELAY_MAX, INMAIL_DELAY_MIN, JOB_INMAIL

    if config is None:
        config = await run_db(_get_campaign_config, campaign_id)
    from ..services.outreach_channel import first_touch_inmail_enabled
    if not first_touch_inmail_enabled(config):
        logger.debug("InMail first-touch disabled for campaign %s", campaign_id)
        return

    pending_inmail_jobs = await run_db(get_pending_job_count, campaign_id, JOB_INMAIL)
    if pending_inmail_jobs >= 2:
        await _log_skip(
            campaign_id, "inmail", "pending_jobs_full",
            {"pending": pending_inmail_jobs},
        )
        return

    from ..tier import get_caps
    caps = await get_caps()
    from ..constants import MIN_FIT_SCORE_THRESHOLD
    try:
        min_fit = float(config.get("min_fit_score", MIN_FIT_SCORE_THRESHOLD))
    except (TypeError, ValueError):
        min_fit = MIN_FIT_SCORE_THRESHOLD

    from ..db.queries import get_inmail_first_touch_candidates
    candidates = await run_db(
        get_inmail_first_touch_candidates,
        campaign_id,
        5,
        True,
        min_fit,
    )
    if not candidates:
        if not caps.can_send_credit_inmail:
            reason = "no_credits"
        else:
            reason = await _empty_or_ineligible(campaign_id, "inmail")
        await _log_skip(campaign_id, "inmail", reason)
        return

    if await _daily_cap_blocks("inmail", campaign_id):
        return

    best = candidates[0]
    delay = random.randint(INMAIL_DELAY_MIN, INMAIL_DELAY_MAX)
    job_id = await run_db(
        _create_gated_job,
        campaign_id=campaign_id,
        job_type=JOB_INMAIL,
        scheduled_at=now + delay,
        outreach_id=best["outreach_id"],
    )
    if not job_id:
        return
    logger.info(
        "InMail first touch: scheduled for %s (campaign %s) in %d min",
        best.get("name", "unknown"), campaign_id, delay // 60,
    )


async def _plan_inmail_fallback(
    campaign_id: str, now: int, *, config: dict[str, Any] | None = None,
) -> None:
    """Schedule one InMail for the best invitation that sat quiet long enough.

    Escalation touch: invitation → quiet days (default 14, per-campaign
    `inmail_fallback_days`) → one InMail → withdrawal at day 21 (unchanged).
    Free tier escalates only Open Profile members — their InMails cost zero
    credits. Credit gate, CAS claim and daily caps all live in the tool.
    """
    from ..constants import (
        INMAIL_DELAY_MAX,
        INMAIL_DELAY_MIN,
        INMAIL_FALLBACK_AFTER_DAYS,
        JOB_INMAIL,
    )

    if config is None:
        config = await run_db(_get_campaign_config, campaign_id)

    # Guard: per-campaign toggle (default ON — first-touch and fallback).
    # Steady states log at DEBUG only: a skip row per campaign per 60s tick is
    # the actions_log firehose that already grew skip_* to 86% of the table.
    if not flag_enabled(config, "inmail_fallback", default=True):
        logger.debug("InMail fallback disabled for campaign %s", campaign_id)
        return

    # Guard: dedup — don't pile up InMail jobs. Checked before the caps read
    # so the common already-planned tick costs no DB round trip.
    pending_inmail_jobs = await run_db(get_pending_job_count, campaign_id, JOB_INMAIL)
    if pending_inmail_jobs >= 2:
        logger.debug(
            "Already %d pending InMail jobs for campaign %s",
            pending_inmail_jobs, campaign_id,
        )
        return

    # Guard: capability — SN sends to anyone (credit InMail); free tier only
    # to Open Profile members. Read through tier caps, never the raw setting.
    from ..tier import get_caps
    caps = await get_caps()
    open_profile_only = not caps.can_send_credit_inmail

    min_age_days = config.get("inmail_fallback_days")
    if not isinstance(min_age_days, int) or isinstance(min_age_days, bool) or min_age_days < 1:
        min_age_days = INMAIL_FALLBACK_AFTER_DAYS

    from ..db.queries import get_inmail_fallback_candidates
    candidates = await run_db(
        get_inmail_fallback_candidates, campaign_id, min_age_days, 5, open_profile_only,
    )
    if not candidates:
        logger.debug("InMail fallback: no quiet invites in campaign %s", campaign_id)
        return

    if await _daily_cap_blocks("inmail", campaign_id):
        return

    # Schedule one InMail (open-profile first, then highest fit_score)
    best = candidates[0]
    delay = random.randint(INMAIL_DELAY_MIN, INMAIL_DELAY_MAX)
    scheduled_at = now + delay

    await run_db(
        _create_gated_job,
        campaign_id=campaign_id,
        job_type=JOB_INMAIL,
        scheduled_at=scheduled_at,
        outreach_id=best["outreach_id"],
    )
    logger.info(
        "InMail fallback: scheduled InMail for %s (campaign %s) in %d min",
        best.get("name", "unknown"), campaign_id, delay // 60,
    )


async def _plan_followups(campaign_id: str, now: int, tier: str) -> None:
    """Schedule follow-up jobs for connected outreaches that are due."""
    # Check per-campaign toggle
    config = await run_db(_get_campaign_config, campaign_id)
    if await _daily_cap_blocks("followup", campaign_id):
        return
    if not flag_enabled(config, "enable_followups"):
        logger.debug("Follow-ups disabled for campaign %s", campaign_id)
        await _log_skip(campaign_id, "followup", "feature_disabled")
        return

    # The count the plan promises and the hosted scheduler keeps (#1414): the
    # campaign's max_followups under the tier ceiling. This used to take any
    # stored 1-5 as is, so a Free campaign storing 4 (every new campaign)
    # planned 4 against the published "up to 2 follow-ups" (facts.FREE_PLAN_LINE).
    from ..services.campaign_plan import effective_max_followups

    max_followups = effective_max_followups(config, tier)
    if max_followups <= 0:
        return

    # Check for existing pending followup jobs (dedup)
    pending_followups = await run_db(get_pending_job_count, campaign_id, JOB_FOLLOWUP)
    if pending_followups >= 3:
        logger.debug("Already %d pending followup jobs for campaign %s", pending_followups, campaign_id)
        await _log_skip(campaign_id, "followup", "pending_jobs_full", {"pending": pending_followups})
        return

    # Find connected outreaches ready for follow-up
    candidates = await run_db(get_followup_candidates, campaign_id, max_followups)
    if not candidates:
        await _log_skip(campaign_id, "followup", "no_candidates")
        return

    scheduled = 0
    for candidate in candidates:
        outreach_id = candidate["outreach_id"]

        # Skip prospects that haven't been messaged yet (followup_count=0).
        # Their initial DM is handled by _plan_dms(), not follow-ups.
        if candidate.get("followup_count", 0) == 0:
            continue

        # Skip if this outreach already has a pending followup job
        if await run_db(get_pending_outreach_job, outreach_id, JOB_FOLLOWUP):
            continue

        # Skip if communication strategist owns this prospect's schedule today
        if await run_db(_skip_if_strategist_owned, outreach_id, JOB_FOLLOWUP):
            continue

        # Check if follow-up is due based on schedule (per-campaign or global)
        custom_schedule = config.get("followup_delay_days")
        if custom_schedule and not isinstance(custom_schedule, list):
            custom_schedule = None
        if not _is_followup_due(candidate, tier, custom_schedule=custom_schedule):
            continue

        # Schedule with randomized delay + stagger
        delay = random.randint(FOLLOWUP_DELAY_MIN, FOLLOWUP_DELAY_MAX)
        scheduled_at = now + delay + (scheduled * random.randint(300, 600))

        await run_db(
            _create_gated_job,
            campaign_id=campaign_id,
            job_type=JOB_FOLLOWUP,
            scheduled_at=scheduled_at,
            outreach_id=outreach_id,
        )
        logger.debug(
            "Scheduled follow-up #%d for outreach %s in %d min",
            candidate.get("followup_count", 0) + 1,
            outreach_id,
            (scheduled_at - now) // 60,
        )
        scheduled += 1
        if scheduled >= 2:
            break


_MEETING_INTENT_SENTIMENTS = frozenset({"positive", "calendar"})


async def plan_auto_replies(campaign_id: str) -> None:
    """Schedule auto-reply jobs for outreaches that received prospect messages.

    Called every 5 min by the scheduler engine. Creates JOB_AUTO_REPLY jobs
    with randomized delays (5-15 min) after the prospect's message was received.

    enable_auto_replies gates questions and small-talk. Meeting-intent replies
    (positive / calendar — typically a booking link) still get a job: marking
    them hot_lead and then going silent is how a booked demo sits unanswered.

    For autopilot campaigns: auto-sends replies after validation.
    For copilot campaigns: queues replies for user review.
    """
    from ..constants import (
        AUTO_REPLY_DELAY_MAX,
        AUTO_REPLY_DELAY_MIN,
        JOB_AUTO_REPLY,
    )
    from ..db.queries import (
        get_auto_reply_candidates,
    )

    config = await run_db(_get_campaign_config, campaign_id)
    auto_replies_on = flag_enabled(config, "enable_auto_replies")

    # Check for existing pending auto-reply jobs (dedup)
    pending = await run_db(get_pending_job_count, campaign_id, JOB_AUTO_REPLY)
    if pending >= 5:
        logger.debug("Already %d pending auto-reply jobs for campaign %s", pending, campaign_id)
        await _log_skip(campaign_id, "auto_reply", "pending_jobs_full", {"pending": pending})
        return

    # The gate refuses every reply here while the coordinator holds the
    # campaign; asked once up front so the skip is logged with its reason.
    from ..services.coordinator import coordinator_blocks_send

    if await run_db(coordinator_blocks_send, campaign_id):
        await _log_skip(campaign_id, "auto_reply", "coordinator_hold")
        return

    # Find candidates with minimum age filter (ensures delay has elapsed)
    candidates = await run_db(get_auto_reply_candidates, campaign_id, AUTO_REPLY_DELAY_MIN)
    if not auto_replies_on:
        candidates = [
            c for c in candidates
            if (c.get("last_sentiment") or "") in _MEETING_INTENT_SENTIMENTS
        ]
        if not candidates:
            logger.debug("Auto-replies disabled for campaign %s", campaign_id)
            await _log_skip(campaign_id, "auto_reply", "feature_disabled")
            return
    elif not candidates:
        await _log_skip(campaign_id, "auto_reply", "no_candidates")
        return

    now = int(time.time())
    scheduled = 0

    for candidate in candidates:
        outreach_id = candidate["outreach_id"]

        # Skip if this outreach already has a pending auto-reply job
        if await run_db(get_pending_outreach_job, outreach_id, JOB_AUTO_REPLY):
            continue

        # NOTE: Do NOT check _has_daily_plan here. The strategist creates
        # "skip_today" plans for replied/messaged outreaches, which would
        # block auto-replies entirely. Auto-replies are reactive (responding
        # to prospect messages) and must always take priority.

        # Calculate delay: random between now and remaining max delay
        last_msg_ts = candidate.get("last_message_ts", now)
        elapsed = now - last_msg_ts
        remaining_max = max(0, AUTO_REPLY_DELAY_MAX - elapsed)
        delay = random.randint(0, remaining_max) + (scheduled * random.randint(120, 300))
        scheduled_at = now + delay

        job_id = await run_db(
            _create_gated_job,
            campaign_id=campaign_id,
            job_type=JOB_AUTO_REPLY,
            scheduled_at=scheduled_at,
            outreach_id=outreach_id,
        )
        if not job_id:
            # The gate refused it, and none of its refusals of an auto_reply
            # (mode, cloud ownership, the hold) depends on the row: the next
            # candidate would be refused too, and each refusal writes a log
            # row. Stop, as the DM and invite planners do.
            break
        logger.info(
            "Scheduled auto-reply for outreach %s in %d min (sentiment=%s)",
            outreach_id[:8],
            delay // 60,
            candidate.get("last_sentiment", "unknown"),
        )
        scheduled += 1
        if scheduled >= 3:  # Max 3 per planning cycle (pacing)
            break


async def plan_inbound_auto_replies() -> None:
    """Schedule auto-reply jobs for inbound outreaches (no campaign).

    Handles organic LinkedIn messages that aren't part of any campaign.
    Uses the same delay and dedup logic as campaign auto-replies.
    """
    from ..constants import (
        AUTO_REPLY_DELAY_MAX,
        AUTO_REPLY_DELAY_MIN,
        JOB_AUTO_REPLY,
    )
    from ..db.queries import (
        get_inbound_auto_reply_candidates,
    )

    # Check for existing pending inbound auto-reply jobs
    pending = await run_db(get_pending_job_count, "", JOB_AUTO_REPLY)
    if pending >= 5:
        logger.debug("Already %d pending inbound auto-reply jobs", pending)
        return

    candidates = await run_db(get_inbound_auto_reply_candidates, AUTO_REPLY_DELAY_MIN)
    if not candidates:
        return

    now = int(time.time())
    scheduled = 0

    for candidate in candidates:
        outreach_id = candidate["outreach_id"]

        # Skip if this outreach already has a pending auto-reply job
        if await run_db(get_pending_outreach_job, outreach_id, JOB_AUTO_REPLY):
            continue

        # Calculate delay: random between now and remaining max delay
        last_msg_ts = candidate.get("last_message_ts", now)
        elapsed = now - last_msg_ts
        remaining_max = max(0, AUTO_REPLY_DELAY_MAX - elapsed)
        delay = random.randint(0, remaining_max) + (scheduled * random.randint(120, 300))
        scheduled_at = now + delay

        await run_db(
            _create_gated_job,
            campaign_id="",
            job_type=JOB_AUTO_REPLY,
            scheduled_at=scheduled_at,
            outreach_id=outreach_id,
        )
        logger.info(
            "Scheduled inbound auto-reply for outreach %s in %d min (sentiment=%s)",
            outreach_id[:8],
            delay // 60,
            candidate.get("last_sentiment", "unknown"),
        )
        scheduled += 1
        if scheduled >= 3:  # Max 3 per planning cycle (pacing)
            break


async def plan_endorsements(campaign_id: str) -> None:
    """Schedule endorsement jobs for prospects that haven't been endorsed yet.

    Called every 15 min. Endorses prospects' skills as a high-visibility
    warm-up touch (triggers "X endorsed your skills" notification).
    Sequence: Follow → Endorse → Engage → Invite → Follow-up DM.
    Skipped if enable_endorsements is off in campaign config or globally disabled.
    """
    from ..constants import ENDORSEMENT_ENABLED
    if not ENDORSEMENT_ENABLED:
        await _log_skip(campaign_id, "endorse", "globally_disabled")
        return

    # Check per-campaign toggle
    config = await run_db(_get_campaign_config, campaign_id)
    if not flag_enabled(config, "enable_endorsements"):
        logger.debug("Endorsements disabled for campaign %s", campaign_id)
        await _log_skip(campaign_id, "endorse", "feature_disabled")
        return
    if not flag_enabled(config, "enable_invitations"):
        await _log_skip(campaign_id, "endorse", "dm_only_campaign")
        return

    now = int(time.time())

    # Dedup: check for existing pending endorse jobs
    pending_count = await run_db(get_pending_job_count, campaign_id, JOB_ENDORSE)
    if pending_count >= 3:
        logger.debug("Already %d pending endorse jobs for campaign %s", pending_count, campaign_id)
        await _log_skip(campaign_id, "endorse", "pending_jobs_full", {"pending": pending_count})
        return

    # Find candidates: pending or followed outreaches without an endorsement engagement
    # Respects both permanent skip_engagement and time-limited JSON cooldowns
    def _query_endorse_candidates(cid: str) -> list:
        from ..db.schema import get_db
        _now = int(time.time())
        db = get_db()
        rows = db.execute(
            """SELECT o.id as outreach_id
               FROM outreaches o
               JOIN contacts c ON o.contact_id = c.id
               WHERE o.campaign_id = ?
                 AND o.status IN ('pending', 'invited')
                 AND COALESCE(o.next_action, '') != 'skip_engagement'
                 AND (
                   o.next_action IS NULL
                   OR o.next_action = ''
                   OR json_valid(o.next_action) = 0
                   OR json_extract(o.next_action, '$.skip_engagement_until') IS NULL
                   OR json_extract(o.next_action, '$.skip_engagement_until') < ?
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM engagements e
                     WHERE e.outreach_id = o.id AND e.action_type = 'endorse'
                 )
               ORDER BY c.fit_score DESC""",
            (cid, _now),
        ).fetchall()
        db.close()
        return rows

    candidates = await run_db(_query_endorse_candidates, campaign_id)

    if not candidates:
        await _log_skip(campaign_id, "endorse", "no_candidates")
        return

    scheduled = 0
    strategist_skips = 0
    for cand in candidates:
        outreach_id = cand["outreach_id"]

        if await run_db(get_pending_outreach_job, outreach_id, JOB_ENDORSE):
            continue

        # Skip if communication strategist owns this prospect's schedule today
        if await run_db(_skip_if_strategist_owned, outreach_id, JOB_ENDORSE):
            strategist_skips += 1
            continue

        delay = random.randint(ENDORSE_DELAY_MIN, ENDORSE_DELAY_MAX)
        scheduled_at = now + delay + (scheduled * random.randint(300, 600))

        await run_db(
            _create_gated_job,
            campaign_id=campaign_id,
            job_type=JOB_ENDORSE,
            scheduled_at=scheduled_at,
            outreach_id=outreach_id,
        )
        logger.debug(
            "Scheduled endorsement for outreach %s in %d min",
            outreach_id, (scheduled_at - now) // 60,
        )
        scheduled += 1
        if scheduled >= 3:
            break

    if scheduled == 0:
        await _log_skip(
            campaign_id, "endorse", "no_schedulable",
            {"strategist": strategist_skips},
        )


async def plan_stale_invite_withdrawals() -> None:
    """Schedule a single job to check and withdraw stale sent invitations.

    Called every hour. Finds sent invitations older than STALE_INVITE_DAYS
    and schedules withdrawal to free up invite quota.
    """
    now = int(time.time())

    # Only schedule if no withdraw job is already pending
    pending_count = await run_db(get_pending_job_count, None, JOB_WITHDRAW_INVITE)
    if pending_count > 0:
        logger.debug("Already %d pending withdraw jobs", pending_count)
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_WITHDRAW_INVITE,
        scheduled_at=now,
    )
    logger.debug("Scheduled stale invite withdrawal check")


async def plan_recover_stuck_outreaches() -> None:
    """Schedule one job that parks proven-unsendable rows and retries the rest.

    Called every hour. Does not send. Repair has no other scheduled caller;
    retry_failed exists as a tool and resets every error, including people
    we cannot DM.
    """
    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_RECOVER_STUCK_OUTREACHES)
    if pending_count > 0:
        logger.debug("Already %d pending recover-stuck jobs", pending_count)
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_RECOVER_STUCK_OUTREACHES,
        scheduled_at=now,
    )
    logger.debug("Scheduled stuck-outreach recovery")


async def plan_sales_nav_redetect() -> None:
    """Schedule a single Sales Navigator tier re-detection job.

    Called daily by the scheduler engine. The stored tier flags are
    detection results, not licence feeds; this keeps them honest against
    purchases and lapses after setup — including the auto-downgrade on a
    confirmed lapse, which only the executor's probe can confirm.
    """
    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_REDETECT_SALES_NAV)
    if pending_count > 0:
        logger.debug("Already %d pending redetect_sales_nav jobs", pending_count)
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_REDETECT_SALES_NAV,
        scheduled_at=now,
    )
    logger.debug("Scheduled Sales Navigator tier re-detection")


async def plan_process_inbound() -> None:
    """Schedule unified classify-first inbound pipeline.

    Called every 15 min by the scheduler engine. Creates one JOB_PROCESS_INBOUND
    job that detects, classifies, and acts on all inbound signals in a single pass.

    Replaces the old plan_inbound_accepts() + plan_qualify_inbound().
    """
    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_PROCESS_INBOUND)
    if pending_count > 0:
        logger.debug("Already %d pending process_inbound jobs", pending_count)
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_PROCESS_INBOUND,
        scheduled_at=now,
    )
    logger.debug("Scheduled unified inbound pipeline")


# DEPRECATED: kept for backward compatibility with in-flight jobs
async def plan_inbound_accepts() -> None:
    """DEPRECATED: Use plan_process_inbound() instead."""
    await plan_process_inbound()


async def plan_qualify_inbound() -> None:
    """DEPRECATED: Use plan_process_inbound() instead."""
    await plan_process_inbound()


async def plan_check_post_comments() -> None:
    """Schedule a job to check comments on recently published posts.

    Called every 30 min by the scheduler engine.
    """
    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_CHECK_POST_COMMENTS)
    if pending_count > 0:
        logger.debug("Already %d pending check_post_comments jobs", pending_count)
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_CHECK_POST_COMMENTS,
        scheduled_at=now,
    )
    logger.debug("Scheduled post comment check")


# ──────────────────────────────────────────────
# Signal collection planners (v1.0)
# ──────────────────────────────────────────────


async def plan_keyword_signal_collection() -> None:
    """Schedule a job to collect keyword signals from LinkedIn post searches.

    Called every 30 min by the scheduler engine. Creates one
    JOB_COLLECT_KEYWORD_SIGNALS job that iterates all active watchlists.
    """
    from ..constants import JOB_COLLECT_KEYWORD_SIGNALS

    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_COLLECT_KEYWORD_SIGNALS)
    if pending_count > 0:
        logger.debug("Already %d pending keyword signal collection jobs", pending_count)
        return

    # Only schedule if there are active watchlists
    from ..db.signal_queries import list_watchlists
    watchlists = await run_db(list_watchlists, is_active=True)
    if not watchlists:
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_COLLECT_KEYWORD_SIGNALS,
        scheduled_at=now,
    )
    logger.debug("Scheduled keyword signal collection (%d active watchlists)", len(watchlists))


async def plan_prospect_post_scan() -> None:
    """Schedule a job to scan campaign contacts' recent posts for signals.

    Called every 1 hour by the scheduler engine. Creates one
    JOB_SCAN_PROSPECT_POSTS job that iterates active campaign contacts.
    """
    from ..constants import JOB_SCAN_PROSPECT_POSTS

    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_SCAN_PROSPECT_POSTS)
    if pending_count > 0:
        logger.debug("Already %d pending prospect post scan jobs", pending_count)
        return

    # Only schedule if there are active campaigns with contacts
    active = await run_db(list_campaigns, status="active")
    if not active:
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_SCAN_PROSPECT_POSTS,
        scheduled_at=now,
    )
    logger.debug("Scheduled prospect post scan (%d active campaigns)", len(active))


async def plan_signal_classification() -> None:
    """Schedule a job to classify pending signals with LLM.

    Called every 15 min by the scheduler engine. Creates one
    JOB_CLASSIFY_SIGNALS job that processes signals with status='new'.
    """
    from ..constants import JOB_CLASSIFY_SIGNALS

    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_CLASSIFY_SIGNALS)
    if pending_count > 0:
        logger.debug("Already %d pending signal classification jobs", pending_count)
        return

    # Only schedule if there are unclassified signals
    from ..db.signal_queries import list_signals
    from ..constants import SIGNAL_STATUS_NEW

    unclassified = await run_db(list_signals, status=SIGNAL_STATUS_NEW, limit=1)
    if not unclassified:
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_CLASSIFY_SIGNALS,
        scheduled_at=now,
    )
    logger.debug("Scheduled signal classification")


async def plan_profile_view_collection() -> None:
    """Schedule a job to collect LinkedIn profile viewers.

    Called every 1 hour by the scheduler engine. Creates one
    JOB_COLLECT_PROFILE_VIEWS job. Requires LinkedIn Premium.
    """
    from ..constants import JOB_COLLECT_PROFILE_VIEWS

    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_COLLECT_PROFILE_VIEWS)
    if pending_count > 0:
        logger.debug("Already %d pending profile view collection jobs", pending_count)
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_COLLECT_PROFILE_VIEWS,
        scheduled_at=now,
    )
    logger.debug("Scheduled profile view collection")


async def plan_job_change_detection() -> None:
    """Schedule a job to detect job title/company changes for campaign contacts.

    Called every 4 hours by the scheduler engine. Creates one
    JOB_DETECT_JOB_CHANGES job that scans contacts not recently scanned.
    """
    from ..constants import JOB_DETECT_JOB_CHANGES

    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_DETECT_JOB_CHANGES)
    if pending_count > 0:
        logger.debug("Already %d pending job change detection jobs", pending_count)
        return

    # Only schedule if there are active campaigns with contacts
    active = await run_db(list_campaigns, status="active")
    if not active:
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_DETECT_JOB_CHANGES,
        scheduled_at=now,
    )
    logger.debug("Scheduled job change detection (%d active campaigns)", len(active))


async def plan_competitor_signal_collection() -> None:
    """Schedule a job to collect competitor mention signals from LinkedIn posts.

    Called every 30 min by the scheduler engine. Creates one
    JOB_COLLECT_COMPETITOR_SIGNALS job that searches for competitor names.
    """
    from ..constants import JOB_COLLECT_COMPETITOR_SIGNALS

    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_COLLECT_COMPETITOR_SIGNALS)
    if pending_count > 0:
        logger.debug("Already %d pending competitor signal collection jobs", pending_count)
        return

    # Only schedule if there are active competitor watchlists
    from ..db.signal_queries import list_watchlists

    comp_watchlists = await run_db(list_watchlists, is_active=True, watch_type="competitor")
    if not comp_watchlists:
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_COLLECT_COMPETITOR_SIGNALS,
        scheduled_at=now,
    )
    logger.debug(
        "Scheduled competitor signal collection (%d competitor watchlists)",
        len(comp_watchlists),
    )


async def plan_signal_activation() -> None:
    """Schedule signal activation — convert classified signals to outreach.

    Called every 15 min by the scheduler engine. Creates one
    JOB_ACTIVATE_SIGNALS job if classified signals exist.
    """
    from ..constants import JOB_ACTIVATE_SIGNALS, SIGNAL_STATUS_CLASSIFIED

    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_ACTIVATE_SIGNALS)
    if pending_count > 0:
        logger.debug("Already %d pending signal activation jobs", pending_count)
        return

    # Only schedule if there are classified signals to process
    from ..db.signal_queries import list_signals

    classified = await run_db(list_signals, status=SIGNAL_STATUS_CLASSIFIED, limit=1)
    if not classified:
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_ACTIVATE_SIGNALS,
        scheduled_at=now,
    )
    logger.debug("Scheduled signal activation")


async def plan_hiring_signal_collection() -> None:
    """Schedule hiring surge detection from LinkedIn job searches.

    Called every 4 hours by the scheduler engine. Creates one
    JOB_COLLECT_HIRING_SIGNALS job if no pending job exists.
    """
    from ..constants import JOB_COLLECT_HIRING_SIGNALS

    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_COLLECT_HIRING_SIGNALS)
    if pending_count > 0:
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_COLLECT_HIRING_SIGNALS,
        scheduled_at=now,
    )
    logger.debug("Scheduled hiring signal collection")


async def plan_news_signal_collection() -> None:
    """Schedule news/funding signal collection from SERPER API.

    Called every 4 hours by the scheduler engine. Creates one
    JOB_COLLECT_NEWS_SIGNALS job if no pending job exists.
    """
    from ..constants import JOB_COLLECT_NEWS_SIGNALS

    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_COLLECT_NEWS_SIGNALS)
    if pending_count > 0:
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_COLLECT_NEWS_SIGNALS,
        scheduled_at=now,
    )
    logger.debug("Scheduled news signal collection")


async def plan_watchlist_web_collection() -> None:
    """Schedule off-LinkedIn watchlist web collection from SERPER search.

    Called every 4 hours by the scheduler engine. Creates one
    JOB_COLLECT_WATCHLIST_WEB_SIGNALS job if no pending job exists.
    """
    from ..constants import JOB_COLLECT_WATCHLIST_WEB_SIGNALS

    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_COLLECT_WATCHLIST_WEB_SIGNALS)
    if pending_count > 0:
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_COLLECT_WATCHLIST_WEB_SIGNALS,
        scheduled_at=now,
    )
    logger.debug("Scheduled watchlist web signal collection")


async def plan_company_page_collection() -> None:
    """Schedule company page engagement signal collection.

    Called every 1 hour by the scheduler engine. Creates one
    JOB_COLLECT_COMPANY_PAGE job if no pending job exists.
    """
    from ..constants import JOB_COLLECT_COMPANY_PAGE

    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_COLLECT_COMPANY_PAGE)
    if pending_count > 0:
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_COLLECT_COMPANY_PAGE,
        scheduled_at=now,
    )
    logger.debug("Scheduled company page engagement collection")


async def plan_company_follower_collection() -> None:
    """Schedule company follower signal collection.

    Called every 4 hours by the scheduler engine. Creates one
    JOB_COLLECT_COMPANY_FOLLOWERS job if no pending job exists.
    """
    from ..constants import JOB_COLLECT_COMPANY_FOLLOWERS

    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_COLLECT_COMPANY_FOLLOWERS)
    if pending_count > 0:
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_COLLECT_COMPANY_FOLLOWERS,
        scheduled_at=now,
    )
    logger.debug("Scheduled company follower collection")


async def plan_post_intent_classification() -> None:
    """Schedule post content intent classification (Phase 5).

    Called every 30 minutes. Classifies prospect posts for granular
    buyer intent signals (pain_point, tech_evaluation, budget, seeking_recs).
    """
    from ..constants import JOB_CLASSIFY_POST_INTENT

    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_CLASSIFY_POST_INTENT)
    if pending_count > 0:
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_CLASSIFY_POST_INTENT,
        scheduled_at=now,
    )
    logger.debug("Scheduled post intent classification")


async def plan_comment_mining() -> None:
    """Schedule comment mining from competitor/industry posts (Phase 5).

    Called every 2 hours. Mines comments from high-engagement posts
    to discover new leads matching campaign ICPs.
    """
    from ..constants import JOB_MINE_COMMENTS

    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_MINE_COMMENTS)
    if pending_count > 0:
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_MINE_COMMENTS,
        scheduled_at=now,
    )
    logger.debug("Scheduled comment mining")


async def plan_compound_intent_detection() -> None:
    """Schedule compound intent detection — stack weak signals into strong events.

    Called every 30 minutes by the scheduler engine. Creates one
    JOB_DETECT_COMPOUND_INTENT job if no pending job exists.
    """
    from ..constants import JOB_DETECT_COMPOUND_INTENT

    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_DETECT_COMPOUND_INTENT)
    if pending_count > 0:
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_DETECT_COMPOUND_INTENT,
        scheduled_at=now,
    )
    logger.debug("Scheduled compound intent detection")


async def plan_decay_cycle() -> None:
    """Schedule signal decay cycle — expire old signals and recompute scores.

    Called every 6 hours by the scheduler engine. Creates one
    JOB_SIGNAL_DECAY_CYCLE job if no pending job exists.
    """
    from ..constants import JOB_SIGNAL_DECAY_CYCLE

    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_SIGNAL_DECAY_CYCLE)
    if pending_count > 0:
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_SIGNAL_DECAY_CYCLE,
        scheduled_at=now,
    )
    logger.debug("Scheduled signal decay cycle")


async def plan_signal_rematch() -> None:
    """Schedule periodic re-matching of homeless signals to campaigns.

    Called every 1 hour. Re-evaluates signals that previously had no
    matching campaign — they may now match a newly created campaign.
    """
    from ..constants import JOB_REMATCH_SIGNALS

    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_REMATCH_SIGNALS)
    if pending_count > 0:
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_REMATCH_SIGNALS,
        scheduled_at=now,
    )
    logger.debug("Scheduled signal rematch cycle")


async def plan_orphan_signal_backfill() -> None:
    """Schedule periodic backfill of orphan signals to known contacts.

    Called every 2 hours. Links signals (prospect_id IS NULL) whose
    linkedin_id now matches a contact in the contacts table.
    """
    from ..constants import JOB_BACKFILL_ORPHAN_SIGNALS

    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_BACKFILL_ORPHAN_SIGNALS)
    if pending_count > 0:
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_BACKFILL_ORPHAN_SIGNALS,
        scheduled_at=now,
    )
    logger.debug("Scheduled orphan signal backfill")


# ──────────────────────────────────────────────
# Brand strategy planners (account-level)
# ──────────────────────────────────────────────


def _today_weekday() -> str:
    import datetime

    return datetime.date.today().strftime("%A")


async def plan_brand_post() -> None:
    """Schedule a brand post job if the plan has a pending post action.

    Called every hour. Checks:
    1. Brand plan exists with pending post actions
    2. No pending brand_post job already (dedup)
    3. Daily brand post limit not reached
    4. Weekly published-post target not reached
    5. Today is a content_calendar day (if the calendar lists any)
    """
    from ..constants import (
        BRAND_POST_DELAY_MAX,
        BRAND_POST_DELAY_MIN,
        DAILY_CAP_BRAND_POSTS,
        JOB_BRAND_POST,
    )
    from ..db.queries import (
        get_daily_brand_post_count,
        get_weekly_brand_post_published_count,
    )
    from ..services.brand_service import (
        calendar_days,
        get_next_pending_action_by_type,
        load_brand_plan,
        weekly_post_target,
    )

    plan = await run_db(load_brand_plan)
    if not plan:
        return

    # Check for pending post actions
    next_post = get_next_pending_action_by_type(plan, "post")
    if not next_post:
        return

    # Dedup: no pending brand_post jobs
    pending = await run_db(get_pending_job_count, None, JOB_BRAND_POST)
    if pending > 0:
        logger.debug("Already %d pending brand_post jobs", pending)
        return

    posted_today = await run_db(get_daily_brand_post_count)
    if posted_today >= DAILY_CAP_BRAND_POSTS:
        logger.debug("Daily brand post cap reached (%d)", posted_today)
        return

    posted_this_week = await run_db(get_weekly_brand_post_published_count)
    week_target = weekly_post_target(plan)
    if posted_this_week >= week_target:
        logger.debug(
            "Weekly brand post target reached (%d/%d)", posted_this_week, week_target,
        )
        return

    days = calendar_days(plan)
    today = _today_weekday()
    if days and today.lower() not in {d.lower() for d in days}:
        logger.debug("Skipping brand post — %s is not a calendar day %s", today, days)
        return

    now = int(time.time())
    delay = random.randint(BRAND_POST_DELAY_MIN, BRAND_POST_DELAY_MAX)
    scheduled_at = now + delay

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_BRAND_POST,
        scheduled_at=scheduled_at,
    )
    logger.debug("Scheduled brand post in %d min", delay // 60)


async def plan_brand_engagement() -> None:
    """Schedule a brand engagement job if the plan has a pending engagement action.

    Called every 4 hours. Checks:
    1. Brand plan exists with pending engagement actions
    2. No pending brand_engage job already (dedup)
    3. Combined daily engagement limit not exceeded
    """
    from ..constants import (
        BRAND_ENGAGE_DELAY_MAX,
        BRAND_ENGAGE_DELAY_MIN,
        DAILY_CAP_BRAND_ENGAGE_JOBS,
        JOB_BRAND_ENGAGE,
    )
    from ..db.queries import get_daily_brand_engage_count
    from ..services.brand_service import (
        get_next_pending_action_by_type,
        load_brand_plan,
    )

    plan = await run_db(load_brand_plan)
    if not plan:
        return

    next_engage = get_next_pending_action_by_type(plan, "engagement")
    if not next_engage:
        return

    # Dedup
    pending = await run_db(get_pending_job_count, None, JOB_BRAND_ENGAGE)
    if pending > 0:
        logger.debug("Already %d pending brand_engage jobs", pending)
        return

    engaged_today = await run_db(get_daily_brand_engage_count)
    if engaged_today >= DAILY_CAP_BRAND_ENGAGE_JOBS:
        logger.debug("Daily brand engage job cap reached (%d)", engaged_today)
        return

    now = int(time.time())
    delay = random.randint(BRAND_ENGAGE_DELAY_MIN, BRAND_ENGAGE_DELAY_MAX)
    scheduled_at = now + delay

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_BRAND_ENGAGE,
        scheduled_at=scheduled_at,
    )
    logger.debug("Scheduled brand engagement in %d min", delay // 60)


async def plan_brand_profile() -> None:
    """Schedule a brand profile job if the plan has a pending profile action.

    Covers profile_optimize and photo_enhance. Cloud now plans brand_profile
    for hosted accounts; local stands down via CLOUD_SENT_ACCOUNT_JOB_TYPES.
    """
    from ..constants import (
        BRAND_PROFILE_DELAY_MAX,
        BRAND_PROFILE_DELAY_MIN,
        DAILY_CAP_BRAND_PROFILE_JOBS,
        JOB_BRAND_PROFILE,
    )
    from ..db.queries import get_daily_brand_profile_count, has_running_headline_test
    from ..services.brand_service import (
        get_next_pending_profile_action,
        load_brand_plan,
    )

    plan = await run_db(load_brand_plan)
    if not plan:
        return

    skip_optimize = await run_db(has_running_headline_test)
    if not get_next_pending_profile_action(plan, skip_optimize=skip_optimize):
        return

    pending = await run_db(get_pending_job_count, None, JOB_BRAND_PROFILE)
    if pending > 0:
        logger.debug("Already %d pending brand_profile jobs", pending)
        return

    done_today = await run_db(get_daily_brand_profile_count)
    if done_today >= DAILY_CAP_BRAND_PROFILE_JOBS:
        logger.debug("Daily brand profile cap reached (%d)", done_today)
        return

    now = int(time.time())
    delay = random.randint(BRAND_PROFILE_DELAY_MIN, BRAND_PROFILE_DELAY_MAX)
    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_BRAND_PROFILE,
        scheduled_at=now + delay,
    )
    logger.debug("Scheduled brand profile in %d min", delay // 60)


async def plan_brand_lifecycle() -> None:
    """Manage brand strategy lifecycle: auto-analyze, auto-plan, re-analyze.

    Called every hour. Handles:
    1. No analysis → schedule brand_analyze (first-time auto-analyze)
    2. Analysis exists but no plan → schedule brand_analyze (will auto-plan)
    3. Plan complete + age >= 28 days → schedule brand_analyze (re-analyze cycle)
    """
    from ..constants import BRAND_REANALYZE_DAYS, JOB_BRAND_ANALYZE
    from ..db.queries import get_setting
    from ..services.brand_service import (
        get_brand_age_days,
        is_plan_complete,
        load_brand_analysis,
        load_brand_plan,
    )

    # Dedup
    pending = await run_db(get_pending_job_count, None, JOB_BRAND_ANALYZE)
    if pending > 0:
        return

    analysis = await run_db(load_brand_analysis)
    plan = await run_db(load_brand_plan)
    now = int(time.time())

    should_analyze = False

    # Check if campaign creation flagged a re-analysis (ICP changed)
    reanalyze_needed = await run_db(get_setting, "brand_reanalyze_needed", False)
    if reanalyze_needed:
        should_analyze = True
        logger.debug("Brand: ICP-driven re-analysis flagged by campaign creation")
    elif not analysis:
        # First-time: auto-analyze
        should_analyze = True
        logger.debug("Brand: no analysis found, scheduling auto-analyze")
    elif not plan:
        # Analysis exists but no plan — will auto-plan after analyzing
        should_analyze = True
        logger.debug("Brand: analysis exists but no plan, scheduling auto-plan")
    elif plan and is_plan_complete(plan):
        # Plan completed — check if 28 days have passed for re-analyze
        age_days = await run_db(get_brand_age_days)
        if age_days >= BRAND_REANALYZE_DAYS:
            should_analyze = True
            logger.debug("Brand: plan complete + %d days old, scheduling re-analyze", age_days)

    if should_analyze:
        await run_db(
            _create_gated_job,
            campaign_id=None,
            job_type=JOB_BRAND_ANALYZE,
            scheduled_at=now + random.randint(300, 900),  # 5-15 min delay
        )


def _last_touch_at(candidate: dict[str, Any]) -> int:
    """When we last said something to this person, in epoch seconds.

    The cadence counts from our previous message, so each step is spaced from
    the one before it. Anchored to ``accepted_at`` instead, every step is
    measured from the acceptance and the whole drip compresses toward it —
    and ``accepted_at`` is a detection stamp anyway: a sweep writes it in
    batches (47 rows of one hosted campaign share a single second).

    ``outreach_updated_at`` is deliberately not in the chain. It is a
    row-mutation clock — ``cloud_sync._cloud_outreach_changes`` says so in its
    own docstring — and a cooldown keyed on it can never elapse, because the
    planner stamps the row on every pass and so resets the clock that decides
    the follow-up is due. Hosted, 16 Sep 2026: 67 people accepted and not one
    follow-up went out. ``created_at`` is NOT NULL, so every row keeps a clock
    without falling back to the mtime.

    See the query side in db.queries._LAST_OUTBOUND_SQL.
    """
    for key in ("last_sdr_message_at", "accepted_at", "invited_at", "created_at"):
        value = candidate.get(key)
        if value:
            return int(value)
    return 0


def _is_followup_due(
    candidate: dict[str, Any],
    tier: str,
    custom_schedule: list[int] | None = None,
) -> bool:
    """Check if a follow-up is due based on the schedule.

    Uses custom_schedule if provided (from per-campaign config),
    otherwise PRO_FOLLOWUP_SCHEDULE_DAYS for Pro tier: [1, 3, 7, 14].
    Free tier: simpler check (just needs to have been connected for >= 1 day).

    The clock is ``_last_touch_at`` — our last message — so each step is
    spaced from the one before it, never from the row's mtime.

    Args:
        candidate: Outreach candidate from get_followup_candidates()
        tier: User's tier ('free' or 'pro')
        custom_schedule: Optional per-campaign follow-up schedule in days.

    Returns:
        True if the follow-up is due now.
    """
    followup_count = candidate.get("followup_count", 0)
    outreach_updated = _last_touch_at(candidate)

    if not outreach_updated:
        return False

    now = int(time.time())
    days_since = (now - outreach_updated) // 86400

    # Every tier is spaced by the same schedule unless the campaign overrides it.
    # Free tier used to fall through to the `days_since >= 1` branch below on
    # every follow-up, which made a prospect eligible again every single day.
    schedule = custom_schedule or PRO_FOLLOWUP_SCHEDULE_DAYS

    if schedule and followup_count > 0:
        schedule_idx = min(followup_count, len(schedule) - 1)
        required_days = schedule[schedule_idx]
        return days_since >= required_days
    else:
        # First follow-up (any tier) or free tier without custom schedule
        return days_since >= 1


# ──────────────────────────────────────────────
# Communication Strategist: plan from daily strategy
# ──────────────────────────────────────────────

async def plan_from_daily_strategy(campaign_id: str) -> None:
    """Execute today's daily strategy plans for a campaign.

    Called every 15 min by the scheduler engine. Reads prospect_daily_plans
    for today, finds unexecuted actions whose timing is right, and creates
    scheduler_jobs for them.

    This is the "output side" of the Communication Strategist — it converts
    AI-generated plans into concrete scheduler jobs.
    """
    from ..services.communication_strategist import execute_planned_actions

    jobs_created = await execute_planned_actions(campaign_id)
    if jobs_created > 0:
        logger.info(
            "Strategy plans: scheduled %d jobs for campaign %s",
            jobs_created,
            campaign_id,
        )


# ──────────────────────────────────────────────
# Campaign auto-refill: top up prospects via LinkedIn search
# ──────────────────────────────────────────────

async def plan_campaign_refill() -> None:
    """Schedule campaign refill — search for more prospects when campaigns run low.

    Called every hour by the scheduler engine. Creates one
    JOB_CAMPAIGN_REFILL job if none pending.
    """
    from ..constants import JOB_CAMPAIGN_REFILL

    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_CAMPAIGN_REFILL)
    if pending_count > 0:
        logger.debug("Already %d pending campaign refill jobs", pending_count)
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_CAMPAIGN_REFILL,
        scheduled_at=now,
    )
    logger.debug("Scheduled campaign refill")


async def plan_backfill_profiles() -> None:
    """Schedule profile backfill if none pending.

    Called every 2 hours by the scheduler engine. Creates one
    JOB_BACKFILL_PROFILES job if none pending.
    """
    from ..constants import JOB_BACKFILL_PROFILES

    now = int(time.time())

    pending_count = await run_db(get_pending_job_count, None, JOB_BACKFILL_PROFILES)
    if pending_count > 0:
        logger.debug("Already %d pending profile backfill jobs", pending_count)
        return

    await run_db(
        _create_gated_job,
        campaign_id=None,
        job_type=JOB_BACKFILL_PROFILES,
        scheduled_at=now,
    )
    logger.debug("Scheduled profile backfill")
