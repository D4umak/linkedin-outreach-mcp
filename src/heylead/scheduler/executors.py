"""Job executors — bridge between scheduler jobs and existing tool internals.

Each executor wraps an existing tool's `run_*()` function, passing the appropriate
parameters from the job record. This ensures all existing logic (validation, rate
limits, message generation, etc.) is reused — the scheduler just decides WHEN to run.

Executors return a `JobResult` with an explicit outcome so the engine can track
real success vs skips vs permanent failures — not just "didn't crash".
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class JobResult:
    """Structured return from job executors so the engine knows what actually happened."""

    outcome: str  # "success", "skipped", "deferred", "permanent_failure"
    message: str  # Human-readable result
    retry_after_seconds: int = 3600  # used when outcome is deferred

    def __str__(self) -> str:
        return self.message

from ..db.async_bridge import run_db
from ..db import aio as db
from ..constants import (
    JOB_ACCEPT_INBOUND,
    JOB_ACTIVATE_SIGNALS,
    JOB_PROCESS_INBOUND,
    JOB_AUTO_REPLY,
    JOB_BACKFILL_POST_ANALYSIS,
    JOB_BRAND_ANALYZE,
    JOB_BRAND_ENGAGE,
    JOB_BRAND_POST,
    JOB_BRAND_PROFILE,
    JOB_CHECK_POST_COMMENTS,
    JOB_CHECK_REPLIES,
    JOB_CLASSIFY_POST_INTENT,
    JOB_CLASSIFY_SIGNALS,
    JOB_COLLECT_COMPETITOR_SIGNALS,
    JOB_COLLECT_COMPANY_FOLLOWERS,
    JOB_COLLECT_COMPANY_PAGE,
    JOB_COLLECT_HIRING_SIGNALS,
    JOB_COLLECT_KEYWORD_SIGNALS,
    JOB_COLLECT_NEWS_SIGNALS,
    JOB_COLLECT_WATCHLIST_WEB_SIGNALS,
    JOB_COLLECT_POSTS_DISTRIBUTED,
    JOB_COLLECT_PROFILE_VIEWS,
    JOB_DETECT_COMPOUND_INTENT,
    JOB_DETECT_JOB_CHANGES,
    JOB_DETECT_VIRAL_POSTS,
    JOB_MINE_COMMENTS,
    JOB_RESEARCH_CONTACTS,
    JOB_SIGNAL_DECAY_CYCLE,
    JOB_REMATCH_SIGNALS,
    JOB_BACKFILL_ORPHAN_SIGNALS,
    JOB_TUNE_WATCHLISTS,
    JOB_OPTIMIZE_SIGNALS,
    JOB_EMAIL_INVITE,
    JOB_INMAIL,
    JOB_ENDORSE,
    JOB_ENGAGE,
    JOB_FOLLOW,
    JOB_FOLLOWUP,
    JOB_INVITE,
    JOB_SEND_DM,
    JOB_PROFILE_VIEW,
    JOB_DAILY_DIGEST,
    JOB_DAILY_STRATEGY,
    JOB_EXECUTE_STRATEGY_PLANS,
    JOB_PARTNER_REMINDER,
    JOB_BACKFILL_PROFILES,
    JOB_CAMPAIGN_REFILL,
    JOB_SYNC_CONNECTIONS,
    JOB_VERIFY_ACTIONS,
    JOB_QUALIFY_INBOUND,
    JOB_REDETECT_SALES_NAV,
    JOB_SCAN_NETWORK_POSTS,
    JOB_SCAN_PROSPECT_POSTS,
    JOB_RECOVER_STUCK_OUTREACHES,
    JOB_WITHDRAW_INVITE,
    STALE_INVITE_DAYS,
)

logger = logging.getLogger(__name__)


async def _check_outreach_still_valid(job: dict[str, Any], allowed_statuses: tuple[str, ...]) -> str | None:
    """Re-validate outreach status before executing a scheduled job.

    Returns an error message if the outreach should be skipped, or None if OK.
    This prevents sending messages to prospects whose status changed between
    job creation and job execution (e.g., prospect replied, opted out, etc.).
    """
    outreach_id = job.get("outreach_id", "")
    if not outreach_id:
        return None  # Pool-based jobs pick their own candidate

    outreach = await db.get_outreach(outreach_id)
    if not outreach:
        return f"Outreach {outreach_id[:8]} no longer exists — skipping"

    status = outreach.get("status", "")
    if status not in allowed_statuses:
        logger.info(
            "Pre-exec check: outreach %s status is '%s' (expected %s) — skipping job",
            outreach_id[:8], status, allowed_statuses,
        )
        return (
            f"Outreach status changed to '{status}' since job was scheduled — "
            f"skipping (expected one of {allowed_statuses})"
        )

    return None


async def _check_recent_messages(outreach_id: str, max_messages_per_day: int = 2) -> str | None:
    """Prevent rapid-fire messaging to the same prospect.

    Only counts SDR messages sent AFTER the last prospect reply — each new
    prospect message resets the counter so we always send at least one reply.
    Also enforces a hard 24h window to prevent runaway loops.

    Returns an error message if too many messages were sent, or None if OK.
    """
    if not outreach_id:
        return None

    messages = await db.get_messages_for_outreach(outreach_id)
    if not messages:
        return None

    now = int(time.time())
    today_start = now - 86400  # Last 24 hours

    # Find the timestamp of the latest prospect message
    last_prospect_ts = 0
    for m in messages:
        ts = m.get("created_at") or m.get("timestamp") or 0
        if m.get("role") == "prospect" and ts > last_prospect_ts:
            last_prospect_ts = ts

    # Count SDR messages sent AFTER the last prospect reply (within 24h)
    # This ensures each new prospect message resets the counter.
    # Invitation notes are not chat DMs — they must not start the gap.
    cutoff = max(today_start, last_prospect_ts)
    last_sdr_ts = 0
    sdr_messages_since_reply = 0
    for m in messages:
        if m.get("role") != "sdr":
            continue
        if (m.get("format") or "") == "invite_note":
            continue
        ts = m.get("created_at") or m.get("timestamp") or 0
        if ts > last_prospect_ts and ts > last_sdr_ts:
            last_sdr_ts = ts
        if ts > cutoff:
            sdr_messages_since_reply += 1

    # Same 24h gap generate_send / send_followup enforce. Two messages per
    # rolling day used to let a 9h-old SDR through, the tool then said
    # "too soon", and the executor recorded success so orphan rescue looped.
    if last_sdr_ts and last_sdr_ts > last_prospect_ts and (now - last_sdr_ts) < 86400:
        hours_ago = (now - last_sdr_ts) // 3600
        return (
            f"Too soon to message this prospect again "
            f"(last message {hours_ago}h ago, minimum gap 24h)"
        )

    if sdr_messages_since_reply >= max_messages_per_day:
        logger.info(
            "Rate limit: %d messages already sent to outreach %s since last reply (limit=%d)",
            sdr_messages_since_reply, outreach_id[:8], max_messages_per_day,
        )
        return (
            f"Already sent {sdr_messages_since_reply} messages to this prospect "
            f"since their last reply (limit: {max_messages_per_day})"
        )

    return None


async def _check_first_degree_excluded(
    campaign_id: str, outreach_id: str,
) -> str | None:
    """Skip message when this campaign must not reach a pre-existing connection.

    9 Sep 2026. Runs before `_check_is_first_degree`, which asks the
    opposite question — that guard exists so a DM is not sent to a
    non-connection, and it happily passes exactly the people this one refuses.
    Returns None (proceed) on any error: a campaign silently sending nothing is
    worse than one redundant DM, and the pre-send guards catch it again.
    """
    if not outreach_id:
        return None
    try:
        from ..db.queries import get_campaign
        from ..services.connection_sync import (
            is_excluded_connection_outreach,
            skip_excluded_connection,
        )

        campaign = await run_db(get_campaign, campaign_id) if campaign_id else None
        if not campaign:
            return None
        outreach = await db.get_outreach_with_contact(outreach_id)
        if not outreach:
            return None
        if not await is_excluded_connection_outreach(campaign, outreach):
            return None
        return await skip_excluded_connection(
            outreach_id, campaign_id,
            outreach.get("name", ""), where="scheduler",
        )
    except Exception:
        logger.debug("first-degree exclusion check failed", exc_info=True)
        return None


async def _check_is_first_degree(outreach_id: str) -> str | None:
    """Verify prospect is a 1st-degree connection before attempting a DM.

    Uses ``verify_dm_eligible``: acceptance evidence and a live LinkedIn
    check beat a stale local cache. Defer is not an error.

    Returns an error message to skip the job, or None if the prospect is connected.
    """
    if not outreach_id:
        return "No outreach_id — cannot verify 1st-degree connection"

    from ..linkedin.unipile import get_account_id
    from ..services.dm_connection_guard import verify_dm_eligible

    outreach = await db.get_outreach_with_contact(outreach_id)
    if not outreach:
        return "Outreach not found — cannot verify 1st-degree connection"

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No account_id — cannot verify 1st-degree connection"

    import json
    profile_json = {}
    try:
        profile_json = json.loads(outreach.get("profile_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        pass

    client = None
    try:
        from ..linkedin import get_linkedin_client
        client = get_linkedin_client()
        guard = await verify_dm_eligible(
            account_id,
            outreach,
            outreach,
            client=client,
            profile=profile_json if isinstance(profile_json, dict) else {},
        )
    finally:
        close = getattr(client, "close", None) if client is not None else None
        if close:
            result = close()
            if hasattr(result, "__await__"):
                await result

    if guard.allowed:
        return None
    if guard.outcome == "error":
        await db.update_outreach(
            outreach_id, status="error", last_attempt_error=guard.message,
        )
        logger.warning(
            "DM pre-check: %s (outreach=%s) — marked as error",
            outreach.get("name", "Unknown"), outreach_id[:8],
        )
    return guard.message


# HTTP status codes indicating permanent engagement failure
_PERMANENT_FAIL_CODES = {400, 422, 403, 404}
# Pattern to extract HTTP status codes from error strings
_STATUS_CODE_RE = re.compile(r"\b(4\d{2})\b")
# Stricter pattern: status code preceded by HTTP-context words
_HTTP_STATUS_RE = re.compile(
    r"(?:http|status|returned|unipile|linkedin|code)[^\d]{0,10}(4\d{2})\b", re.IGNORECASE,
)

# Cooldown durations for engagement failures (seconds)
_COOLDOWN_PERMANENT_FAIL = 24 * 3600  # 24h — retry next day
_COOLDOWN_ENDORSE_FAIL = 7 * 24 * 3600  # 7d — skills unlikely to change fast


def _set_engagement_cooldown(outreach_id: str, seconds: int) -> None:
    """Set a time-limited engagement cooldown instead of permanent skip.

    Uses JSON format: {"skip_engagement_until": <unix_timestamp>}
    Already supported by the engagement candidates query.
    """
    import json as _json
    import time as _time

    cooldown_until = int(_time.time()) + seconds
    from ..db.queries import update_outreach

    update_outreach(
        outreach_id,
        next_action=_json.dumps({"skip_engagement_until": cooldown_until}),
    )


def _classify_422(error_msg: str) -> str:
    """Classify a 422 error into a sub-type.

    Sub-types:
        cannot_resend_yet        — already invited, treat as success
        already_connected        — already a connection
        temporary_provider_limit — LinkedIn throttle, retry later
        invitation_pending       — invitation already pending
        unknown_422              — unrecognised 422
    """
    lower = error_msg.lower()
    if "resend" in lower or "reinvite" in lower:
        return "cannot_resend_yet"
    if "already_connected" in lower or "already connected" in lower:
        return "already_connected"
    if "provider_limit" in lower or "temporary" in lower:
        return "temporary_provider_limit"
    if "pending" in lower:
        return "invitation_pending"
    return "unknown_422"


def categorize_error(error_msg: str) -> str:
    """Classify an error message into a category for filtering and analysis.

    Uses ``_extract_status_code()`` for reliable HTTP status detection instead
    of raw substring matching (avoids false positives like "processed 403 items").

    Categories:
        rate_limit           — 429 / rate limit responses from LinkedIn/Unipile
        voyager_unavailable  — 400 "malformed request" from Voyager raw routes
        permanent_failure    — 4xx errors that won't resolve on retry
        proxy_error          — 502/503/504 from Unipile proxy (transient)
        network_error        — timeouts, connection failures, DNS errors, 5xx
        tick_budget          — asyncio.wait_for cancelled the job for the tick
        llm_error            — LLM service failures (Gemini, Claude, OpenAI)
        auth_error           — authentication / account disconnected
        unknown              — anything else
    """
    lower = error_msg.lower()
    # Use stricter pattern to avoid false matches on non-HTTP numbers
    _match = _HTTP_STATUS_RE.search(error_msg)
    status_code = int(_match.group(1)) if _match else 0

    # --- status-code based classification (reliable) ---
    if status_code == 429 or "rate limit" in lower or "too many requests" in lower:
        return "rate_limit"
    # 422 temporary_provider_limit is a transient rate limit, not permanent
    if status_code == 422 and "temporary_provider_limit" in lower:
        return "rate_limit"
    if status_code == 400:
        if "malformed request" in lower or "rejected by provider" in lower:
            return "voyager_unavailable"
        return "permanent_failure"
    if status_code == 401 or "unauthorized" in lower or "disconnected" in lower or "no linkedin account" in lower:
        return "auth_error"
    if status_code in (403, 404, 422):
        return "permanent_failure"

    # --- 5xx server errors (transient, always retry) ---
    if status_code in (502, 503, 504) or "proxy_error" in lower or ("proxy" in lower and "not working" in lower):
        return "proxy_error"
    if status_code >= 500:
        return "network_error"

    # --- keyword-only fallbacks (no status code extracted) ---
    if any(w in lower for w in (
        "premium required", "subscription_required", "permission denied",
        "not a connection", "locked or invalid", "unreachable",
        "cannot be reached", "invalid_recipient", "does not allow incoming",
        "not a 1st-degree", "profile is locked",
    )):
        return "permanent_failure"
    # Bare wait_for cancel: str(TimeoutError()) is "" so the engine substitutes
    # the class name. That is a tick-budget defer, not a LinkedIn 5xx.
    if error_msg.strip() in ("TimeoutError", "asyncio.TimeoutError"):
        return "tick_budget"
    if any(w in lower for w in ("timeout", "timed out", "connection", "dns", "reset by peer")):
        return "network_error"
    if any(w in lower for w in ("gemini", "claude", "openai", "llm", "model")):
        return "llm_error"
    return "unknown"


_PROSPECT_JOB_TYPES = frozenset({
    JOB_INVITE, JOB_SEND_DM, JOB_EMAIL_INVITE, JOB_INMAIL, JOB_FOLLOWUP,
    JOB_AUTO_REPLY, JOB_ENGAGE, JOB_FOLLOW, JOB_PROFILE_VIEW, JOB_ENDORSE,
})

# Jobs that hit LinkedIn via Unipile. When the Unipile session is in a broken
# state (CREDENTIALS, CHECKPOINT, etc.) these jobs would either fail or serve
# stale data, so we short-circuit them until the user reconnects. LLM/DB-only
# jobs (classify, rematch, backfill_*) keep running so post-reconnect catch-up
# is already staged.
_LINKEDIN_JOB_TYPES = frozenset({
    JOB_INVITE, JOB_SEND_DM, JOB_EMAIL_INVITE, JOB_INMAIL, JOB_FOLLOWUP,
    JOB_AUTO_REPLY, JOB_ENGAGE, JOB_FOLLOW, JOB_PROFILE_VIEW, JOB_ENDORSE,
    JOB_CHECK_REPLIES, JOB_CHECK_POST_COMMENTS,
    JOB_WITHDRAW_INVITE, JOB_RECOVER_STUCK_OUTREACHES,
    JOB_BACKFILL_PROFILES, JOB_RESEARCH_CONTACTS,
    JOB_SYNC_CONNECTIONS, JOB_VERIFY_ACTIONS, JOB_REDETECT_SALES_NAV,
    JOB_COLLECT_KEYWORD_SIGNALS, JOB_SCAN_PROSPECT_POSTS,
    JOB_COLLECT_PROFILE_VIEWS, JOB_DETECT_JOB_CHANGES,
    JOB_COLLECT_COMPETITOR_SIGNALS, JOB_COLLECT_HIRING_SIGNALS,
    JOB_COLLECT_NEWS_SIGNALS, JOB_COLLECT_COMPANY_PAGE,
    JOB_COLLECT_COMPANY_FOLLOWERS, JOB_COLLECT_POSTS_DISTRIBUTED,
    JOB_MINE_COMMENTS,
    JOB_BRAND_POST, JOB_BRAND_ENGAGE, JOB_BRAND_PROFILE,
})

# Cache the last verify_account result so we don't hit the API on every job.
_ACCOUNT_STATUS_CACHE_TTL_SECONDS = 60
_account_status_cache: dict[str, Any] = {"checked_at": 0.0, "ok": True, "msg": ""}


async def _ensure_account_connected() -> tuple[bool, str]:
    """Return (is_connected, message) for the active LinkedIn account.

    Cached for 60s so hot-tick scheduler loops don't DDoS Unipile. Returns
    (True, "") if no account is configured — in that case other guards kick in.
    """
    now = time.time()
    if now - _account_status_cache["checked_at"] < _ACCOUNT_STATUS_CACHE_TTL_SECONDS:
        return bool(_account_status_cache["ok"]), str(_account_status_cache["msg"])

    try:
        from ..linkedin import get_account_id, get_linkedin_client
        account_id = await run_db(get_account_id)
        if not account_id:
            _account_status_cache.update({"checked_at": now, "ok": True, "msg": ""})
            return True, ""
        client = get_linkedin_client()
        try:
            ok, msg = await client.verify_account(account_id)
        finally:
            await client.close()
        _account_status_cache.update({"checked_at": now, "ok": ok, "msg": msg})
        return ok, msg
    except Exception as e:
        # If the verify call itself errors, don't block jobs — let them fail
        # individually with better signal. But log so we can track.
        logger.warning("_ensure_account_connected: probe failed: %s", e)
        _account_status_cache.update({"checked_at": now, "ok": True, "msg": ""})
        return True, ""


async def _check_exclusion(job: dict[str, Any]) -> str | None:
    """Skip jobs targeting contacts excluded from automation."""
    job_type = job.get("job_type", "")
    if job_type not in _PROSPECT_JOB_TYPES:
        return None
    outreach_id = job.get("outreach_id", "")
    if not outreach_id:
        return None
    outreach = await db.get_outreach(outreach_id)
    if not outreach:
        return None
    contact_id = outreach.get("contact_id", "")
    if not contact_id:
        return None
    from ..db.global_contact_queries import is_excluded_by_contact_id
    excluded = await run_db(is_excluded_by_contact_id, contact_id)
    if excluded:
        from ..ops_log import record_skip_excluded
        await record_skip_excluded(
            outreach_id=outreach_id,
            campaign_id=job.get("campaign_id") or "",
            contact_id=contact_id,
        )
        logger.info("Exclusion gate: contact %s is excluded from automation — skipping job %s",
                     contact_id[:8], job.get("id", "")[:8])
        return "Contact excluded from automation (do-not-automate tag or do_not_contact lifecycle)"
    return None


async def execute_job(job: dict[str, Any]) -> str:
    """Route a scheduler job to the appropriate executor.

    Args:
        job: Full job record from scheduler_jobs table.

    Returns:
        String result from the tool execution.

    Raises:
        ValueError: Unknown job type.
        Exception: Any error from the tool execution.
    """
    started = time.monotonic()

    def _emit_job(result: Any) -> Any:
        from ..ops_log import classify_result, log_event

        outcome, skip = classify_result(result)
        extra: dict[str, Any] = {}
        if outcome in ("error", "permanent_failure"):
            extra["error_category"] = categorize_error(str(result) if result else "")
        log_event(
            "job_executed",
            job_id=job.get("id") or "",
            job_type=job.get("job_type") or "",
            outreach_id=job.get("outreach_id") or "",
            campaign_id=job.get("campaign_id") or "",
            outcome=outcome,
            skip_reason=skip,
            duration_ms=int((time.monotonic() - started) * 1000),
            **extra,
        )
        return result

    # ── Exclusion gate: skip jobs targeting excluded contacts ──
    exclusion_msg = await _check_exclusion(job)
    if exclusion_msg:
        return _emit_job(exclusion_msg)

    job_type = job["job_type"]

    # ── Ownership gate: the cloud scheduler is sending for this campaign ──
    # The planner stops queueing these once the backend holds a campaign, but
    # jobs planned before that — the user turning cloud sending on by hand,
    # mid-cycle — are already in the queue, and executing them sends what the
    # backend is also about to send. Checked before the connection gate: a job
    # that is not ours to run does not need an account probe.
    from ..services.cloud_sync import (
        CLOUD_SENT_ACCOUNT_JOB_TYPES,
        cloud_owns_account_sending,
        cloud_sends_this_job,
    )

    campaign_id = job.get("campaign_id") or ""
    if await run_db(
        cloud_sends_this_job,
        job_type,
        campaign_id,
        job.get("outreach_id") or "",
    ):
        from ..services.cloud_sync import log_ownership_decision

        await run_db(
            log_ownership_decision,
            campaign_id=campaign_id,
            job_type=job_type,
            outreach_id=job.get("outreach_id") or "",
            owner="cloud",
            source="executor",
        )
        logger.info(
            "Ownership gate: cloud scheduler owns campaign %s — dropping local %s job %s",
            campaign_id[:8], job_type, job.get("id", "")[:8],
        )
        return _emit_job(JobResult(
            outcome="skipped",
            message="The cloud scheduler is sending for this campaign — "
                    "dropped to avoid messaging the prospect twice.",
        ))

    if job_type in CLOUD_SENT_ACCOUNT_JOB_TYPES:
        if await run_db(cloud_owns_account_sending):
            logger.info(
                "Ownership gate: cloud scheduler publishes for this account — "
                "dropping local %s job %s",
                job_type, job.get("id", "")[:8],
            )
            return _emit_job(JobResult(
                outcome="skipped",
                message="The cloud scheduler is publishing for this account — "
                        "dropped to avoid posting the same thing twice.",
            ))

    # ── Connection gate: skip LinkedIn-dependent jobs when the Unipile
    # session is broken (CREDENTIALS, CHECKPOINT, etc.). The dashboard
    # shows a reconnect link; the scheduler resumes on the next tick
    # after the user completes OAuth.
    if job_type in _LINKEDIN_JOB_TYPES:
        ok, msg = await _ensure_account_connected()
        if not ok:
            return _emit_job(JobResult(
                outcome="deferred",
                message=f"LinkedIn disconnected — deferred ({msg}).",
            ))

    executors = {
        JOB_INVITE: _execute_invite,
        JOB_SEND_DM: _execute_send_dm,
        JOB_EMAIL_INVITE: _execute_email_invite,
        JOB_INMAIL: _execute_inmail,
        JOB_FOLLOWUP: _execute_followup,
        JOB_AUTO_REPLY: _execute_auto_reply,
        JOB_ENGAGE: _execute_engagement,
        JOB_FOLLOW: _execute_follow,
        JOB_PROFILE_VIEW: _execute_profile_view,
        JOB_ENDORSE: _execute_endorse,
        JOB_CHECK_REPLIES: _execute_check_replies,
        JOB_ACCEPT_INBOUND: _execute_accept_inbound,   # DEPRECATED: kept for in-flight jobs
        JOB_PROCESS_INBOUND: _execute_process_inbound, # Unified classify-first pipeline
        JOB_WITHDRAW_INVITE: _execute_withdraw_stale_invites,
        JOB_RECOVER_STUCK_OUTREACHES: _execute_recover_stuck_outreaches,
        JOB_REDETECT_SALES_NAV: _execute_redetect_sales_nav,
        JOB_QUALIFY_INBOUND: _execute_qualify_inbound, # DEPRECATED: kept for in-flight jobs
        JOB_CHECK_POST_COMMENTS: _execute_check_post_comments,
        JOB_BRAND_POST: _execute_brand_post,
        JOB_BRAND_ENGAGE: _execute_brand_engage,
        JOB_BRAND_ANALYZE: _execute_brand_analyze,
        JOB_BRAND_PROFILE: _execute_brand_profile,
        JOB_COLLECT_KEYWORD_SIGNALS: _execute_collect_keyword_signals,
        JOB_SCAN_PROSPECT_POSTS: _execute_scan_prospect_posts,
        JOB_SCAN_NETWORK_POSTS: _execute_scan_network_posts,
        JOB_CLASSIFY_SIGNALS: _execute_classify_signals,
        JOB_COLLECT_PROFILE_VIEWS: _execute_collect_profile_views,
        JOB_DETECT_JOB_CHANGES: _execute_detect_job_changes,
        JOB_COLLECT_COMPETITOR_SIGNALS: _execute_collect_competitor_signals,
        JOB_COLLECT_HIRING_SIGNALS: _execute_collect_hiring_signals,
        JOB_COLLECT_NEWS_SIGNALS: _execute_collect_news_signals,
        JOB_COLLECT_WATCHLIST_WEB_SIGNALS: _execute_collect_watchlist_web_signals,
        JOB_COLLECT_COMPANY_PAGE: _execute_collect_company_page,
        JOB_COLLECT_COMPANY_FOLLOWERS: _execute_collect_company_followers,
        JOB_ACTIVATE_SIGNALS: _execute_activate_signals,
        JOB_DETECT_COMPOUND_INTENT: _execute_detect_compound_intent,
        JOB_SIGNAL_DECAY_CYCLE: _execute_decay_cycle,
        JOB_REMATCH_SIGNALS: _execute_rematch_signals,
        JOB_BACKFILL_ORPHAN_SIGNALS: _execute_backfill_orphan_signals,
        JOB_PARTNER_REMINDER: _execute_partner_reminder,
        JOB_DAILY_DIGEST: _execute_daily_digest,
        JOB_DAILY_STRATEGY: _execute_daily_strategy,
        JOB_EXECUTE_STRATEGY_PLANS: _execute_strategy_plans,
        JOB_VERIFY_ACTIONS: _execute_verify_actions,
        JOB_SYNC_CONNECTIONS: _execute_sync_connections,
        JOB_COLLECT_POSTS_DISTRIBUTED: _execute_collect_posts_distributed,
        JOB_RESEARCH_CONTACTS: _execute_research_contacts,
        JOB_BACKFILL_POST_ANALYSIS: _execute_backfill_post_analysis,
        JOB_DETECT_VIRAL_POSTS: _execute_detect_viral_posts,
        JOB_TUNE_WATCHLISTS: _execute_tune_watchlists,
        JOB_OPTIMIZE_SIGNALS: _execute_optimize_signals,
        JOB_CLASSIFY_POST_INTENT: _execute_classify_post_intent,
        JOB_MINE_COMMENTS: _execute_mine_comments,
        JOB_CAMPAIGN_REFILL: _execute_campaign_refill,
        JOB_BACKFILL_PROFILES: _execute_backfill_profiles,
    }

    executor = executors.get(job_type)
    if not executor:
        raise ValueError(f"Unknown job type: {job_type}")

    try:
        return _emit_job(await executor(job))
    except Exception as exc:
        from ..ops_log import log_event

        log_event(
            "job_executed",
            job_id=job.get("id") or "",
            job_type=job_type,
            outreach_id=job.get("outreach_id") or "",
            campaign_id=job.get("campaign_id") or "",
            outcome="error",
            error_type=type(exc).__name__,
        )
        raise


async def _maybe_toggle_otw(campaign_id: str) -> None:
    """Toggle Open-to-Work badge based on campaign config before invite batch."""
    import json

    from ..linkedin import get_account_id, get_linkedin_client

    campaign = await db.get_campaign(campaign_id)
    if not campaign:
        return
    try:
        config = json.loads(campaign.get("config_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return

    otw_mode = config.get("open_to_work_mode", "off")
    if otw_mode == "off":
        return

    account_id = await run_db(get_account_id)
    profile = await db.get_setting("profile", {})
    provider_id = profile.get("provider_id", "")
    if not account_id or not provider_id:
        return

    from ..tools.profile_editor import apply_profile_change

    if otw_mode == "off_for_outbound":
        # Disable OTW when doing outbound invites
        try:
            client = get_linkedin_client()
            await apply_profile_change(
                client, account_id, provider_id, "open_to_work",
                json.dumps({"enabled": "false"}), source="scheduler_otw",
            )
            await client.close()
            logger.info("OTW disabled for outbound campaign %s", campaign_id)
        except Exception as e:
            logger.debug("OTW toggle failed (non-fatal): %s", e)

    elif otw_mode == "on_for_inbound":
        # Enable OTW to attract inbound connections
        try:
            client = get_linkedin_client()
            await apply_profile_change(
                client, account_id, provider_id, "open_to_work",
                json.dumps({"enabled": "true"}), source="scheduler_otw",
            )
            await client.close()
            logger.info("OTW enabled for inbound campaign %s", campaign_id)
        except Exception as e:
            logger.debug("OTW toggle failed (non-fatal): %s", e)


def _is_paused_result(result: str | None) -> bool:
    """True when the send tool declined to send (cap, ceiling, nothing warmed up).

    Nothing left the machine, so the job must not count as executed: a success
    here writes the action into the strategist's daily plan and hides the
    non-send from every dashboard.
    """
    return bool(result) and result.lstrip().startswith("⏸️")


def _is_gap_deferred_result(result: str | None) -> bool:
    """True when the tool refused because the prospect is not due yet.

    generate_send uses a skip emoji + 'too soon' / 'minimum gap'. send_followup
    says 'Too early for follow-up' with no emoji. Either way nothing left the
    machine — counting that as success lets orphan rescue re-queue every tick.
    """
    if not result:
        return False
    stripped = result.lstrip()
    if stripped.startswith("⏭️"):
        return True
    lower = stripped.lower()
    return (
        "too soon to message" in lower
        or "minimum gap" in lower
        or "too early for follow-up" in lower
    )


# What a send tool says when something actually left the machine. Kept as a
# marker set rather than prose, because the alternative — inferring a send
# from the ABSENCE of a failure marker — is what recorded refusals as
# outreach that never happened (heylead#354).
SEND_SUCCESS_MARKERS = ("✅", "📧", "📨", "💬", "🎙️")


def send_succeeded(result: str | None) -> bool:
    """True only when the tool SAYS it sent.

    Fail closed. The send tools have ~100 bare string returns between them,
    mixing refusals, errors and the odd success, so "no failure marker"
    proves nothing. A success that loses its marker now reads as a job that
    did not run — visible, and re-planned — instead of as a send that never
    happened, which nothing could see.
    """
    if not result:
        return False
    return result.lstrip().startswith(SEND_SUCCESS_MARKERS)


def _job_result_from_send_text(result: str | None) -> JobResult | None:
    """Honest outcome when the send tool returned a no-op. None means a real send."""
    if _is_paused_result(result) or _is_gap_deferred_result(result):
        return JobResult("deferred", result or "")
    if result and (
        "cap reached" in result.lower() or "limit reached" in result.lower()
    ):
        return JobResult("deferred", result)
    from ..ops_log import classify_result

    outcome, _ = classify_result(result)
    if outcome in ("skipped", "deferred"):
        return JobResult(outcome, result or "")
    if not send_succeeded(result):
        # Nothing here says a message left the machine. classify_result's
        # fallthrough calls that success, which is how "No LinkedIn account
        # connected" was written into the daily plan as a follow-up sent.
        return JobResult("skipped", result or "")
    return None


async def _execute_send_dm(job: dict[str, Any]) -> str | JobResult:
    """Execute a cold DM to an existing connection (connections-only campaigns).

    Calls run_generate_and_send() with force_channel="dm" which skips the
    invitation path and sends a direct message instead.
    """
    # Daily cap check
    from ..linkedin.rate_limiter import check_daily_cap
    can, current, cap, block = await check_daily_cap("dm")
    if not can:
        await db.log_action(
            "skip_dm", result="daily_cap",
            campaign_id=job.get("campaign_id", ""),
            details={"reason": "daily_cap", "current": current, "cap": cap},
        )
        await _track_plan_execution(
            job, "send_dm", f"Daily DM cap reached ({current}/{cap})",
            outcome="daily_cap",
        )
        return JobResult("deferred", f"Daily DM cap reached ({current}/{cap})")

    from ..tools.generate_send import run_generate_and_send

    campaign_id = job["campaign_id"]
    outreach_id = job.get("outreach_id", "")
    logger.info("Scheduler: sending DM for connections-only campaign %s (outreach=%s)", campaign_id, outreach_id or "pool")

    # Pre-execution safety: re-validate status and message rate
    skip = await _check_outreach_still_valid(job, ("pending", "connected"))
    if skip:
        await _track_plan_execution(job, "send_dm", skip, outcome="skipped")
        return JobResult("skipped", skip)
    skip = await _check_recent_messages(outreach_id)
    if skip:
        await _track_plan_execution(job, "send_dm", skip, outcome="deferred")
        return JobResult("deferred", skip)

    # ── exclude_connections: the campaign must not reach anyone it was
    # already connected to before it started. Sits above the guard below,
    # which asks the opposite question and passes exactly these people.
    skip = await _check_first_degree_excluded(campaign_id, outreach_id)
    if skip:
        await _track_plan_execution(job, "send_dm", skip, outcome="skipped")
        return JobResult("skipped", skip)

    # ── Root-cause guard: verify prospect is actually a 1st-degree connection
    # before wasting an API call.  LinkedIn search (even with network_distance=1)
    # can return non-connections, and connections may have been removed since
    # the search was run.
    skip = await _check_is_first_degree(outreach_id)
    if skip:
        await _track_plan_execution(job, "send_dm", skip, outcome="skipped")
        return JobResult("skipped", skip)

    # An invite note is not an in-thread intro. LinkedIn often keeps the
    # note on the invitation and never shows it in Messaging (Michel
    # Tjoeng, 12 Sep 2026), so routing note-only rows to follow-up made
    # the first visible bubble a "Following…" continuation. The first
    # send_dm after accept is the opener. Follow-up jobs handle the next
    # touch once a real DM exists.

    result = await run_generate_and_send(
        campaign_id=campaign_id,
        force_channel="dm",
        target_outreach_id=outreach_id,
    )

    if result and ("❌" in result or "failed" in result.lower()):
        # Permanent failures (not connected, premium required, unreachable)
        # should NOT be retried — the outreach is already marked as error
        # by run_generate_and_send.  Return instead of raising to skip
        # the engine's retry/escalation loop.
        if categorize_error(result) == "permanent_failure":
            logger.warning(
                "DM permanent failure for campaign %s (outreach=%s): %s",
                campaign_id, outreach_id, result[:200],
            )
            return JobResult("permanent_failure", result)
        raise RuntimeError(f"DM send failed: {result[:200]}")

    honest = _job_result_from_send_text(result)
    if honest:
        logger.info("Scheduler: DM not sent for campaign %s: %s", campaign_id, (result or "")[:100])
        await _track_plan_execution(job, "send_dm", result or "", outcome=honest.outcome)
        return honest

    logger.info("Scheduler: DM result for campaign %s: %s", campaign_id, result[:100])
    await _track_plan_execution(job, "send_dm", result or "")
    return JobResult("success", result or "")


async def _execute_invite(job: dict[str, Any]) -> str:
    """Execute an invitation job.

    Calls run_generate_and_send() for the campaign.
    The tool internally picks the next pending prospect by fit_score.

    Error handling:
    - 422 with 'cannot_resend_yet' → already invited, don't retry
    - Other failures → raise for normal retry logic
    """
    # Daily cap check
    from ..linkedin.rate_limiter import check_daily_cap
    can, current, cap, block = await check_daily_cap("invite", reserve=False)
    if not can:
        await db.log_action(
            "skip_invite", result="daily_cap",
            campaign_id=job.get("campaign_id", ""),
            details={"reason": "daily_cap", "current": current, "cap": cap},
        )
        await _track_plan_execution(
            job, "invite", f"Daily invitation cap reached ({current}/{cap})",
            outcome="daily_cap",
        )
        return JobResult("deferred", f"Daily invitation cap reached ({current}/{cap})")

    from ..tools.generate_send import run_generate_and_send

    campaign_id = job["campaign_id"]
    logger.info("Scheduler: sending invitation for campaign %s", campaign_id)

    # Pre-execution safety: re-validate status
    skip = await _check_outreach_still_valid(job, ("pending",))
    if skip:
        await _track_plan_execution(job, "invite", skip, outcome="skipped")
        return JobResult("skipped", skip)

    # Toggle OTW badge based on campaign config
    await _maybe_toggle_otw(campaign_id)

    # Swap headline if a headline A/B test is running
    from ..services import experiment_service

    headline_variant = None
    if not experiment_service.HEADLINE_AB_ENABLED:
        logger.debug(
            "Headline A/B disabled (HEADLINE_AB_ENABLED=False) — "
            "no profile PATCH for campaign %s", campaign_id,
        )
    else:
        headline_variant = await experiment_service.swap_headline_for_batch(campaign_id)
        if headline_variant:
            logger.info("Headline swapped to variant %s for campaign %s", headline_variant, campaign_id)

    outreach_id = job.get("outreach_id", "")
    logger.info("Scheduler: sending invitation for campaign %s (outreach=%s)", campaign_id, outreach_id or "pool")

    result = await run_generate_and_send(
        campaign_id=campaign_id,
        force_channel="invite",
        target_outreach_id=outreach_id,
    )

    # Check if the result indicates an error (tools return error strings)
    if result and ("❌" in result or "failed" in result.lower()):
        # Check for 422 sub-types that should not be retried
        status_code = _extract_status_code(result)
        if status_code == 422 or "cannot_resend_yet" in result.lower():
            sub_type = _classify_422(result)
            if sub_type in ("cannot_resend_yet", "already_connected", "invitation_pending"):
                logger.warning(
                    "Invite 422 (%s) for campaign %s — completing without retry",
                    sub_type, campaign_id,
                )
                return JobResult("skipped", result)  # Don't raise — idempotent
            if sub_type == "temporary_provider_limit":
                # Auto-withdraw oldest invite to free a spot, then retry
                logger.warning(
                    "Invite 422 (temporary_provider_limit) for campaign %s — "
                    "attempting auto-withdraw to free spot",
                    campaign_id,
                )
                try:
                    from ..linkedin import get_account_id, get_linkedin_client
                    from ..linkedin.rate_limiter import withdraw_oldest_to_free_spot
                    _acct = await run_db(get_account_id)
                    if _acct:
                        _client = get_linkedin_client()
                        wd = await withdraw_oldest_to_free_spot(_client, _acct)
                        if wd.get("success"):
                            logger.info(
                                "Auto-withdrew invite for %s (%d days old) — retrying invitation",
                                wd.get("name", "?"), wd.get("days_old", 0),
                            )
                except Exception as wd_err:
                    logger.warning("Auto-withdraw in executor failed: %s", wd_err)
                raise RuntimeError(f"Rate limited (422): {result[:200]}")

        raise RuntimeError(f"Invitation failed: {result[:200]}")

    honest = _job_result_from_send_text(result)
    if honest:
        logger.info(
            "Scheduler: invitation %s for campaign %s: %s",
            honest.outcome, campaign_id, (result or "")[:100],
        )
        await _track_plan_execution(job, "invite", result or "", outcome=honest.outcome)
        return honest

    logger.info("Scheduler: invitation result for campaign %s: %s", campaign_id, result[:100])
    if headline_variant and outreach_id:
        await experiment_service.persist_invite_headline_variant(
            campaign_id, outreach_id, headline_variant,
        )
    await _track_plan_execution(job, "invite", result or "")
    return JobResult("success", result or "")


async def _execute_email_invite(job: dict[str, Any]) -> str:
    """Execute an email invitation job (overflow from LinkedIn limits).

    Calls run_generate_and_send() with force_channel="email" for a specific
    outreach_id. The tool will use the email path directly.
    """
    from ..tools.generate_send import run_generate_and_send

    campaign_id = job["campaign_id"]
    outreach_id = job.get("outreach_id")

    if not outreach_id:
        return JobResult("skipped", "Email invite job missing outreach_id")

    logger.info(
        "Scheduler: sending email invite for outreach %s (campaign %s, LinkedIn overflow)",
        outreach_id[:8], campaign_id,
    )

    result = await run_generate_and_send(
        campaign_id=campaign_id,
        force_channel="email",
        target_outreach_id=outreach_id,
    )

    honest = _job_result_from_send_text(result)
    if honest:
        logger.info(
            "Scheduler: email invite %s for campaign %s: %s",
            honest.outcome, campaign_id, (result or "")[:100],
        )
        return honest

    if result and ("❌" in result or "failed" in result.lower()):
        raise RuntimeError(f"Email invite failed: {result[:200]}")

    logger.info(
        "Scheduler: email invite result for campaign %s: %s",
        campaign_id, (result or "")[:100],
    )
    return JobResult("success", result or "")


async def _execute_inmail(job: dict[str, Any]) -> JobResult:
    """Execute an InMail fallback job (escalation for a quiet invitation).

    Calls run_send_inmail() for a specific outreach. Credit gate, CAS claim
    and daily caps all live in the tool; this maps its result text onto a
    JobResult so benign refusals are never retried as failures — a retry
    here can cost a metered credit.
    """
    from ..tools.send_inmail import run_send_inmail

    campaign_id = job["campaign_id"]
    outreach_id = job.get("outreach_id")

    if not outreach_id:
        return JobResult("skipped", "InMail job missing outreach_id")

    logger.info(
        "Scheduler: sending InMail fallback for outreach %s (campaign %s)",
        outreach_id[:8], campaign_id,
    )

    result = await run_send_inmail(campaign_id=campaign_id, outreach_id=outreach_id)
    text = result or ""
    lower = text.lower()

    if "✅" in text:
        logger.info(
            "Scheduler: InMail result for campaign %s: %s", campaign_id, text[:100],
        )
        return JobResult("success", text)
    # Idempotent no-ops: already sent (or being sent by a concurrent job), or
    # the invite was accepted and InMail no longer applies.
    if "skipping (idempotent)" in lower or "1st-degree" in lower:
        return JobResult("skipped", text)
    # Permanently unsendable rows: no retry can fix a missing provider_id,
    # a vanished outreach/campaign, or a broken setup. Raising here put one
    # malformed row on the full retry ladder forever.
    if (
        "missing provider_id" in lower
        or "not found" in lower
        or "does not belong" in lower
        or "setup required" in lower
        or "no linkedin account" in lower
    ):
        return JobResult("skipped", text)
    # Resource waits: credits reset monthly, caps daily, campaigns get resumed.
    if "daily cap" in lower or "no inmail credits" in lower or "is paused" in lower:
        return JobResult("deferred", text)
    # Generation is nondeterministic — another day, another draft. Skipping
    # ends THIS job (deferring would re-run its 4-LLM-call lap today); the
    # candidate query's 24h attempt window re-admits the prospect tomorrow.
    if "failed validation" in lower or "failed to generate" in lower:
        return JobResult("skipped", text)
    if "fit score" in lower and "below threshold" in lower:
        return JobResult("skipped", text)
    # Unreachable recipient: planner already parks via actions_log
    # (_INMAIL_PERMANENT_FAIL_SQL). Raising here retried the same row when
    # the Unipile body had no status code and categorize_error said unknown.
    if (
        "no_connection_with_recipient" in lower
        or "cannot be reached" in lower
        or "does not allow incoming" in lower
        or "not to be first degree" in lower
    ):
        return JobResult("skipped", text)
    raise RuntimeError(f"InMail failed: {text[:200]}")


async def _execute_followup(job: dict[str, Any]) -> str:
    """Execute a follow-up message job.

    Calls run_send_followup() for a specific outreach.
    If outreach_id is set, it targets that specific outreach.
    Reads campaign voice_mode to determine message format (text/voice).
    """
    # Daily cap check (shares DM cap)
    from ..linkedin.rate_limiter import check_daily_cap
    can, current, cap, block = await check_daily_cap("followup")
    if not can:
        await db.log_action(
            "skip_followup", result="daily_cap",
            campaign_id=job.get("campaign_id", ""),
            details={"reason": "daily_cap", "current": current, "cap": cap},
        )
        await _track_plan_execution(
            job, "followup", f"Daily DM/followup cap reached ({current}/{cap})",
            outcome="daily_cap",
        )
        return JobResult("deferred", f"Daily DM/followup cap reached ({current}/{cap})")

    from ..tools.send_followup import run_send_followup

    campaign_id = job["campaign_id"]
    outreach_id = job.get("outreach_id", "")
    logger.info("Scheduler: sending follow-up for outreach %s", outreach_id or "next")

    # Pre-execution safety: re-validate status and message rate
    skip = await _check_outreach_still_valid(job, ("connected", "messaged"))
    if skip:
        await _track_plan_execution(job, "followup", skip, outcome="skipped")
        return JobResult("skipped", skip)
    skip = await _check_recent_messages(outreach_id)
    if skip:
        await _track_plan_execution(job, "followup", skip, outcome="deferred")
        return JobResult("deferred", skip)

    # ── exclude_connections: same rule as the opener. A campaign that must
    # not message an existing connection must not follow up with one either.
    skip = await _check_first_degree_excluded(campaign_id, outreach_id)
    if skip:
        await _track_plan_execution(job, "followup", skip, outcome="skipped")
        return JobResult("skipped", skip)

    # ── Root-cause guard: verify prospect is actually a 1st-degree connection
    # before attempting a follow-up DM.  Avoids 422 "invalid_recipient" errors
    # when check_replies marks a prospect as "connected" prematurely.
    skip = await _check_is_first_degree(outreach_id)
    if skip:
        await _track_plan_execution(job, "followup", skip, outcome="skipped")
        return JobResult("skipped", skip)

    # Determine format: today's plan may ask for a voice memo; the campaign
    # voice_mode bounds that choice (text_only / voice_only are absolute).
    fmt = "text"
    if outreach_id:
        try:
            from ..db.strategist_queries import get_planned_followup_format
            from ..services.experiment_service import get_voice_format_for_outreach
            planned = await run_db(get_planned_followup_format, outreach_id)
            fmt = await run_db(
                get_voice_format_for_outreach, campaign_id, outreach_id, planned,
            )
        except Exception as e:
            logger.debug("Voice format lookup failed (using text): %s", e)

    result = await run_send_followup(
        campaign_id=campaign_id,
        outreach_id=outreach_id or "",
        format=fmt,
    )

    if result and ("❌" in result or "failed" in result.lower()):
        raise RuntimeError(f"Follow-up failed: {result[:200]}")

    if _is_paused_result(result) or _is_gap_deferred_result(result):
        logger.info("Scheduler: follow-up not sent (format=%s): %s", fmt, (result or "")[:100])
        await _track_plan_execution(job, "followup", result or "", outcome="deferred")
        return JobResult("deferred", result)

    if not send_succeeded(result):
        # This branch used to be the success branch. "No LinkedIn account
        # connected", "Campaign not found for this outreach" and every other
        # bare refusal in send_followup landed here and was written into the
        # daily plan as a follow-up sent (heylead#354).
        logger.info(
            "Scheduler: follow-up did not report a send (format=%s): %s",
            fmt, (result or "")[:120],
        )
        await _track_plan_execution(job, "followup", result or "", outcome="skipped")
        return JobResult("skipped", result or "")

    logger.info("Scheduler: follow-up result (format=%s): %s", fmt, result[:100])
    await _track_plan_execution(job, "followup", result or "")
    return JobResult("success", result or "")


async def _check_engagement_budget_or_defer(job: dict[str, Any], action_label: str) -> str | None:
    """Check daily cap for engagement action. If exhausted, re-queue the job.

    Returns a deferral message if over budget, or None if budget is available.
    """
    from ..db import aio as db
    from ..linkedin.rate_limiter import check_daily_cap

    requeue_delay = 900  # 15 minutes default requeue delay

    # Map action_label to cap type
    cap_type = action_label  # "engage", "follow", "profile_view", "endorse"
    can, current, limit, block = await check_daily_cap(cap_type)
    if can:
        return None

    # Re-queue with delay instead of executing now
    new_time = int(time.time()) + requeue_delay
    await db.reschedule_job(job["id"], new_time)
    await db.log_action(
        f"skip_{action_label}",
        outreach_id=job.get("outreach_id", ""),
        campaign_id=job.get("campaign_id", ""),
        result="daily_cap",
        details={
            "reason": "daily_cap",
            "job_type": action_label,
            "current": current,
            "limit": limit,
            "requeue_delay": requeue_delay,
        },
    )
    logger.info(
        "Deferred %s job %s: daily cap reached (%d/%d), re-queued in %d min",
        action_label, job["id"], current, limit, requeue_delay // 60,
    )
    return f"Deferred: engagement budget exhausted ({current}/{limit})"


async def _execute_engagement(job: dict[str, Any]) -> str:
    """Execute an engagement warm-up job.

    Calls run_engage_prospect() for a specific outreach.
    Action defaults to "auto" (comment if post has text, react otherwise).

    Error handling:
    - 422/403/404 in result → permanent failure, mark skip_engagement (no retry)
    - 429 in result → rate limited, raise to retry later
    - Other failures → raise for normal retry logic
    """
    deferred = await _check_engagement_budget_or_defer(job, "comment")
    if deferred:
        return JobResult("deferred", deferred)

    from ..tools.engage_prospect import run_engage_prospect

    campaign_id = job["campaign_id"]
    outreach_id = job.get("outreach_id", "")
    logger.info("Scheduler: engaging prospect for outreach %s", outreach_id or "next")

    result = await run_engage_prospect(
        campaign_id=campaign_id,
        outreach_id=outreach_id or "",
        action="auto",
    )

    if result and result.startswith("Free tier limit reached"):
        return JobResult("skipped", result)

    if result and ("❌" in result or "failed" in result.lower()):
        # Extract HTTP status code from error string
        status_code = _extract_status_code(result)

        if status_code in _PERMANENT_FAIL_CODES:
            # Permanent failure — post deleted, comments disabled, account issues
            # Set time-limited cooldown so prospect becomes eligible again after 24h
            if outreach_id:
                await run_db(_set_engagement_cooldown, outreach_id, _COOLDOWN_PERMANENT_FAIL)
                await db.log_action(
                    "engagement_cooldown",
                    outreach_id=outreach_id,
                    result="cooldown_24h",
                    details={"status_code": status_code, "error": result[:200]},
                )
            logger.warning(
                "Engagement permanently failed (%d) for outreach %s — skip_engagement",
                status_code, outreach_id,
            )
            # Don't raise — job completes, outreach removed from candidate pool
            return JobResult("permanent_failure", result)

        if status_code == 429:
            # Rate limited — raise to trigger retry, but will be cleaned up
            # if it keeps failing
            logger.warning("Engagement rate limited (429) for outreach %s", outreach_id)
            raise RuntimeError(f"Engagement rate limited (429): {result[:200]}")

        # Other failures — raise for normal retry logic
        raise RuntimeError(f"Engagement failed: {result[:200]}")

    logger.info("Scheduler: engagement result: %s", result[:100])
    await _track_plan_execution(job, "engage_comment", result or "")
    return JobResult("success", result or "")


def _extract_status_code(text: str) -> int:
    """Extract HTTP status code from an error string.

    Looks for 4xx codes in the text. Returns 0 if none found.
    """
    match = _STATUS_CODE_RE.search(text)
    return int(match.group(1)) if match else 0


async def _execute_profile_view(job: dict[str, Any]) -> str:
    """Execute a profile view job.

    Calls run_engage_prospect() with action="view"
    for a specific outreach. Profile views warm up prospects by triggering
    a "X viewed your profile" notification — lightest touch.

    Error handling follows the same pattern as _execute_follow.
    """
    deferred = await _check_engagement_budget_or_defer(job, "profile_view")
    if deferred:
        return JobResult("deferred", deferred)

    from ..tools.engage_prospect import run_engage_prospect

    campaign_id = job["campaign_id"]
    outreach_id = job.get("outreach_id", "")
    logger.info("Scheduler: viewing profile for outreach %s", outreach_id or "next")

    result = await run_engage_prospect(
        campaign_id=campaign_id,
        outreach_id=outreach_id or "",
        action="view",
    )

    # Detect failures — check for known error indicators, not just "failed"
    _view_error_phrases = ("failed", "error", "cannot view", "disconnected", "not found", "timed out")
    _is_view_error = not result or any(p in (result or "").lower() for p in _view_error_phrases)

    if _is_view_error:
        status_code = _extract_status_code(result or "")

        if status_code in _PERMANENT_FAIL_CODES:
            if outreach_id:
                await run_db(_set_engagement_cooldown, outreach_id, _COOLDOWN_PERMANENT_FAIL)
                await db.log_action(
                    "profile_view_cooldown",
                    outreach_id=outreach_id,
                    result="cooldown_24h",
                    details={"status_code": status_code, "error": result[:500]},
                )
            logger.warning(
                "Profile view permanent failure (%d) for outreach %s: %s",
                status_code, outreach_id, result,
            )
            return JobResult("permanent_failure", result or "")

        if status_code == 429:
            logger.warning("Profile view rate limited (429) for outreach %s", outreach_id)
            raise RuntimeError(f"Profile view rate limited (429): {result[:500]}")

        # Log full error for debugging, not truncated
        logger.warning("Profile view transient failure for outreach %s: %s", outreach_id, result)
        raise RuntimeError(f"Profile view failed: {result[:500] if result else 'empty result'}")

    logger.info("Scheduler: profile view result: %s", result[:100] if result else "empty")
    await _track_plan_execution(job, "profile_view", result or "")
    return JobResult("success", result or "")


async def _execute_follow(job: dict[str, Any]) -> str:
    """Execute a profile follow job.

    Calls run_engage_prospect() with action="follow"
    for a specific outreach. Follows warm up prospects by triggering a
    "X started following you" notification before connecting.

    Error handling:
    - 422/403/404 → permanent failure, mark skip_engagement
    - 429 → rate limited, raise to retry later
    - Other failures → raise for normal retry logic
    """
    deferred = await _check_engagement_budget_or_defer(job, "follow")
    if deferred:
        return JobResult("deferred", deferred)

    from ..tools.engage_prospect import run_engage_prospect

    campaign_id = job["campaign_id"]
    outreach_id = job.get("outreach_id", "")
    logger.info("Scheduler: following prospect for outreach %s", outreach_id or "next")

    result = await run_engage_prospect(
        campaign_id=campaign_id,
        outreach_id=outreach_id or "",
        action="follow",
    )

    if result and ("failed" in result.lower()):
        # Voyager endpoints may be temporarily unavailable — defer instead of failing
        if "voyager" in result.lower() and "unavailable" in result.lower():
            logger.info(
                "Follow deferred (Voyager unavailable) for outreach %s",
                outreach_id,
            )
            return JobResult("deferred", f"Deferred: Voyager unavailable — {result[:100]}")

        status_code = _extract_status_code(result)

        if status_code in _PERMANENT_FAIL_CODES:
            if outreach_id:
                await run_db(_set_engagement_cooldown, outreach_id, _COOLDOWN_PERMANENT_FAIL)
                await db.log_action(
                    "follow_cooldown",
                    outreach_id=outreach_id,
                    result="cooldown_24h",
                    details={"status_code": status_code, "error": result[:200]},
                )
            logger.warning(
                "Follow failed (%d) for outreach %s — cooldown 24h",
                status_code, outreach_id,
            )
            return JobResult("permanent_failure", result)

        if status_code == 429:
            logger.warning("Follow rate limited (429) for outreach %s", outreach_id)
            raise RuntimeError(f"Follow rate limited (429): {result[:200]}")

        raise RuntimeError(f"Follow failed: {result[:200]}")

    logger.info("Scheduler: follow result: %s", result[:100])
    await _track_plan_execution(job, "follow", result or "")
    return JobResult("success", result or "")


async def _execute_check_replies(job: dict[str, Any]) -> str:
    """Execute a reply check job.

    Calls run_check_replies() which checks all active campaigns.
    """
    from ..tools.check_replies import run_check_replies

    logger.info("Scheduler: checking replies")

    result = await run_check_replies()

    # Reply checks don't typically fail hard — just log
    logger.info("Scheduler: reply check result: %s", result[:100] if result else "empty")
    return result or "No new replies"


async def _execute_auto_reply(job: dict[str, Any]) -> str:
    """Execute an auto-reply job. Always sends immediately (autopilot)."""
    # Daily cap check
    from ..linkedin.rate_limiter import check_daily_cap
    can, current, cap, block = await check_daily_cap("auto_reply")
    if not can:
        await db.log_action(
            "skip_auto_reply", result="daily_cap",
            campaign_id=job.get("campaign_id", ""),
            details={"reason": "daily_cap", "current": current, "cap": cap},
        )
        return JobResult("deferred", f"Daily auto-reply cap reached ({current}/{cap})")

    from ..tools.reply_to_prospect import run_reply_to_prospect

    campaign_id = job.get("campaign_id", "")
    outreach_id = job.get("outreach_id", "")

    if not outreach_id:
        raise ValueError("Auto-reply job missing outreach_id")

    from ..services.reply_agent import is_fresh_hold, parse_operator_hold
    from ..db.queries import get_messages_for_outreach, get_outreach
    row = await run_db(get_outreach, outreach_id)
    hold = parse_operator_hold((row or {}).get("next_action"))
    if hold:
        last_ts = 0
        for message in await run_db(get_messages_for_outreach, outreach_id) or []:
            if (message.get("role") or "") != "prospect":
                continue
            try:
                last_ts = max(last_ts, int(message.get("timestamp") or 0))
            except (TypeError, ValueError):
                continue
        if is_fresh_hold(hold, last_ts):
            return JobResult("skipped", "held for operator")

    if campaign_id:
        from ..services.coordinator import coordinator_blocks_send
        if await run_db(coordinator_blocks_send, campaign_id):
            return JobResult("skipped", "coordinator hold")

    # Determine format based on campaign voice_mode
    fmt = "text"
    if campaign_id:
        try:
            from ..services.experiment_service import get_voice_format_for_outreach
            fmt = await run_db(get_voice_format_for_outreach, campaign_id, outreach_id)
        except Exception as e:
            logger.debug("Voice format lookup failed (using text): %s", e)

    # Pre-execution safety: re-validate status and message rate
    skip = await _check_outreach_still_valid(job, ("replied", "hot_lead", "connected", "messaged"))
    if skip:
        return JobResult("skipped", skip)
    # Auto-replies should send exactly 1 reply per prospect message — never more.
    # Previous limit of 3 allowed duplicate replies when multiple jobs fired concurrently.
    skip = await _check_recent_messages(outreach_id, max_messages_per_day=1)
    if skip:
        return JobResult("skipped", skip)

    logger.info(
        "Scheduler: auto-replying to outreach %s (format=%s)",
        outreach_id[:8], fmt,
    )

    result = await run_reply_to_prospect(
        outreach_id=outreach_id,
        format=fmt,
        autonomous=True,
    )
    from ..services.job_search_guard import JOB_SEARCH_HELD_MESSAGE

    # Separate intentional skips (correct behavior) from real failures.
    # Skips are things like opt-outs, sentiment blocks, dedup — the system
    # correctly decided NOT to send.  Real failures are errors that warrant retry.
    _skip_phrases = (
        "opted out", "out of office", "cap reached", "already sent",
        "already replied", "decline keywords", "consecutive sdr messages",
        "negative sentiment", "reverse pitch", "vendor pitch", "failed validation",
        "limit reached", "wrong person", "targeting mismatch",
        "held for operator", "reply agent skipped",
        "chat not found",
        JOB_SEARCH_HELD_MESSAGE.lower(),
    )
    _error_phrases = (
        "error", "could not find", "not found", "setup required",
        "no linkedin account", "no prospect", "cannot send",
    )

    result_lower = (result or "").lower()
    _is_skip = result and any(p in result_lower for p in _skip_phrases)
    _is_error = not result or (
        any(p in result_lower for p in _error_phrases) and not _is_skip
    )

    # Log the auto-reply action for observability
    if _is_skip:
        log_result = "skipped"
    elif _is_error:
        log_result = "error"
    else:
        log_result = "success"
    await db.log_action(
        action_type="auto_reply_sent",
        outreach_id=outreach_id,
        result=log_result,
        details={"format": fmt, "scheduler": True},
    )

    if _is_error:
        raise RuntimeError(f"Auto-reply failed: {result[:200] if result else 'empty result'}")

    logger.info("Scheduler: auto-reply result (format=%s): %s", fmt, result[:100] if result else "empty")
    await _track_plan_execution(job, "auto_reply", result or "")
    # auto_reply already classifies skips vs success via _is_skip above
    if _is_skip:
        return JobResult("skipped", result or "Auto-reply skipped")
    return JobResult("success", result or "Auto-reply completed")


async def _execute_process_inbound(job: dict[str, Any]) -> str:
    """Execute the unified classify-first inbound pipeline.

    Single pass: detect → classify → act (accept/ignore/respond).
    Replaces _execute_accept_inbound + _execute_qualify_inbound.
    """
    from ..services.inbound_pipeline import process_inbound_pipeline

    logger.info("Scheduler: running unified inbound pipeline")
    result = await process_inbound_pipeline()
    logger.info("Scheduler: inbound pipeline result: %s", result)
    return result


async def _execute_accept_inbound(job: dict[str, Any]) -> str:
    """DEPRECATED: Execute an inbound invitation auto-accept job.

    Qualify-then-accept-all flow:
    1. Fetch invitations
    2. Save each as inbound signal + qualify via LLM
    3. Accept ALL invitations (grow the network)
    4. Qualification results are used later by _execute_qualify_inbound
       to send discovery DMs.
    """
    from ..linkedin import UnipileError, get_account_id, get_linkedin_client

    from ..timeutil import to_epoch as _to_epoch

    logger.info("Scheduler: checking inbound invitations")

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected"

    try:
        client = get_linkedin_client()
    except UnipileError as e:
        return f"LinkedIn client error: {e}"

    try:
        invitations = await client.get_received_invitations(account_id)
    except Exception as e:
        await client.close()
        return f"Failed to fetch inbound invitations: {e}"

    if not invitations:
        await client.close()
        return "No inbound invitations"

    accepted = 0
    saved = 0
    errors = 0
    for inv in invitations[:50]:
        inv_id = inv.get("id", "")
        if not inv_id:
            continue

        # Save as inbound signal for qualification (dedup by sender_id)
        sender_id = inv.get("sender_id", "")
        if sender_id and not await db.get_inbound_signal_by_sender(sender_id, "invitation"):
            await db.save_inbound_signal(
                signal_type="invitation",
                sender_name=inv.get("sender_name", ""),
                sender_id=sender_id,
                sender_headline=inv.get("headline", ""),
                content=inv.get("message", ""),
                sent_at=_to_epoch(inv.get("timestamp")),
            )
            saved += 1

        # Accept ALL invitations (grow the network)
        try:
            result = await client.handle_invitation(
                account_id, inv_id, action="accept",
                shared_secret=inv.get("shared_secret", ""))
            if result.get("success"):
                accepted += 1
                await db.log_action(
                    "inbound_invitation_accepted",
                    result="success",
                    details={
                        "invitation_id": inv_id,
                        "sender_name": inv.get("sender_name", ""),
                        "sender_id": sender_id,
                    },
                )
                logger.info(
                    "Accepted inbound invitation from %s (%s)",
                    inv.get("sender_name", "Unknown"), inv_id,
                )
            else:
                errors += 1
                logger.warning(
                    "Failed to accept invitation %s: %s",
                    inv_id, result.get("error", "unknown"),
                )
        except Exception as e:
            errors += 1
            logger.warning("Error accepting invitation %s: %s", inv_id, e)

    await client.close()

    summary = f"Inbound invitations: {accepted} accepted, {saved} saved for qualification"
    if errors:
        summary += f", {errors} failed"
    logger.info("Scheduler: %s", summary)
    return summary


async def _execute_qualify_inbound(job: dict[str, Any]) -> str:
    """Execute inbound signal qualification and send discovery DMs.

    1. Qualify all 'new' inbound signals against ICPs
    2. Send discovery DMs to qualified connections (full autopilot)
    """
    from ..services.inbound_service import process_inbound_signals, send_discovery_dms

    logger.info("Scheduler: qualifying inbound signals")

    # Step 1: Qualify new signals
    qual_result = await process_inbound_signals()
    logger.info("Scheduler: qualification result: %s", qual_result)

    # Step 2: Send discovery DMs to qualified signals
    dm_result = await send_discovery_dms()
    logger.info("Scheduler: discovery DM result: %s", dm_result)

    return f"{qual_result} | {dm_result}"


async def _execute_check_post_comments(job: dict[str, Any]) -> str:
    """Check comments on recently published posts for inbound leads."""
    from ..services.inbound_service import check_post_comments

    logger.info("Scheduler: checking post comments for inbound leads")

    result = await check_post_comments()
    logger.info("Scheduler: post comments result: %s", result)
    return result


async def _execute_endorse(job: dict[str, Any]) -> str:
    """Execute a skill endorsement job.

    Calls run_engage_prospect() with action="endorse".
    Endorsements trigger high-visibility LinkedIn notifications.

    Error handling follows the same pattern as _execute_follow.
    """
    from ..constants import ENDORSEMENT_ENABLED
    if not ENDORSEMENT_ENABLED:
        logger.info("Endorsement globally disabled (ENDORSEMENT_ENABLED=False) — skipping")
        return JobResult("skipped", "Endorsement globally disabled")

    deferred = await _check_engagement_budget_or_defer(job, "endorse")
    if deferred:
        return JobResult("deferred", deferred)

    from ..tools.engage_prospect import run_engage_prospect

    campaign_id = job["campaign_id"]
    outreach_id = job.get("outreach_id", "")
    logger.info("Scheduler: endorsing prospect for outreach %s", outreach_id or "next")

    result = await run_engage_prospect(
        campaign_id=campaign_id,
        outreach_id=outreach_id or "",
        action="endorse",
    )

    if result and ("failed" in result.lower()):
        status_code = _extract_status_code(result)

        # Detect permanent failures: HTTP 4xx or known unrecoverable errors
        _permanent_error_phrases = ("profile or skills not found", "no linkedin identifier")
        is_permanent = (
            status_code in _PERMANENT_FAIL_CODES
            or any(phrase in result.lower() for phrase in _permanent_error_phrases)
        )

        if is_permanent:
            if outreach_id:
                await run_db(_set_engagement_cooldown, outreach_id, _COOLDOWN_ENDORSE_FAIL)
                await db.log_action(
                    "endorse_cooldown",
                    outreach_id=outreach_id,
                    result="cooldown_7d",
                    details={"status_code": status_code, "error": result[:200]},
                )
            logger.warning(
                "Endorsement failed for outreach %s — cooldown 7d: %s",
                outreach_id, result[:100],
            )
            return JobResult("permanent_failure", result)

        if status_code == 429:
            logger.warning("Endorsement rate limited (429) for outreach %s", outreach_id)
            raise RuntimeError(f"Endorsement rate limited (429): {result[:200]}")

        raise RuntimeError(f"Endorsement failed: {result[:200]}")

    logger.info("Scheduler: endorsement result: %s", result[:100])
    await _track_plan_execution(job, "endorse", result or "")
    return JobResult("success", result or "")


async def _execute_redetect_sales_nav(job: dict[str, Any]) -> Any:
    """Weekly self-heal of the stored Sales Navigator tier flags.

    Delegates to redetect_tier, which persists only confirmed verdicts — a
    failed probe writes nothing, so this job can never downgrade a paying
    account over a transient error. Read-only on LinkedIn: one metadata GET
    and a limit-1 search, which is why the job is observe-safe.
    """
    from ..linkedin import UnipileError, get_account_id, get_linkedin_client
    from ..services.search_account_resolver import redetect_tier

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected"

    try:
        client = get_linkedin_client()
    except UnipileError as e:
        return f"LinkedIn client error: {e}"

    try:
        result = await redetect_tier(client, account_id)
    finally:
        await client.close()

    if result.get("has_sales_navigator") is None:
        return JobResult(
            outcome="deferred",
            message=(
                "Tier probe failed — stored flags untouched "
                f"({'; '.join(result.get('errors') or ['unknown error'])})"
            ),
        )
    return (
        f"Tier re-detected: has_sales_navigator={result['has_sales_navigator']}, "
        f"premium_search_has_sn={result.get('premium_search_has_sn')}"
    )


async def _execute_recover_stuck_outreaches(job: dict[str, Any]) -> str:
    """Park proven-unsendable rows, then reset first-degree DM errors.

    Does not send. Repair had no scheduled caller; retry_failed resets every
    error including people we cannot DM.
    """
    from ..linkedin import UnipileError, get_account_id, get_linkedin_client
    from ..services.provider_id_repair import repair_unsendable_invite_targets
    from ..services.recover_stuck_outreaches import retry_recoverable_errors

    parked = repaired = 0
    account_id = await run_db(get_account_id)
    if account_id:
        client = None
        try:
            client = get_linkedin_client()
        except UnipileError as e:
            logger.warning("Recover stuck: no LinkedIn client (%s) — skipping repair", e)
        if client is not None:
            try:
                report = await repair_unsendable_invite_targets(
                    client, account_id, dry_run=False,
                )
                parked = len(report.get("parked") or [])
                repaired = len(report.get("repaired") or [])
            finally:
                await client.close()

    retry = await run_db(retry_recoverable_errors)
    reset = len((retry or {}).get("reset_to_connected") or [])
    return (
        f"Recovered stuck outreaches: parked={parked} "
        f"repaired={repaired} reset_to_connected={reset}"
    )


async def _execute_withdraw_stale_invites(job: dict[str, Any]) -> str:
    """Withdraw sent invitations that are older than STALE_INVITE_DAYS.

    Fetches sent invitations from LinkedIn, finds those older than the
    threshold, and withdraws them to free up invitation quota.

    Withdrawals are paced and bounded: at most WITHDRAW_BATCH_PER_RUN per run,
    never more than the remaining daily withdrawal allowance, with a short
    randomized gap between each. A long disconnection builds a big backlog, and
    draining it back-to-back is exactly the burst pattern that gets accounts
    flagged.
    """
    import asyncio
    import random
    import time

    from ..constants import (
        WITHDRAW_BATCH_PER_RUN,
        WITHDRAW_DELAY_MAX,
        WITHDRAW_DELAY_MIN,
    )
    from ..linkedin import UnipileError, get_account_id, get_linkedin_client
    from ..linkedin.rate_limiter import check_daily_cap

    logger.info("Scheduler: checking for stale invitations to withdraw")

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected"

    try:
        client = get_linkedin_client()
    except UnipileError as e:
        return f"LinkedIn client error: {e}"

    # Get sent invitations (use cache to avoid duplicate API calls)
    from ..linkedin.rate_limiter import get_cached_pending_invitations, invalidate_pending_cache
    try:
        _, invitations = await get_cached_pending_invitations(client, account_id)
    except Exception as e:
        await client.close()
        return f"Failed to fetch sent invitations: {e}"

    if not invitations:
        await client.close()
        return "No pending sent invitations"

    # Bound this run by whatever the daily cap has left, then by the batch size.
    can_withdraw, current, cap, _ = await check_daily_cap("withdraw")
    if not can_withdraw:
        await client.close()
        return f"Daily withdrawal cap reached ({current}/{cap})"
    budget = min(cap - current, WITHDRAW_BATCH_PER_RUN)

    now = int(time.time())
    stale_threshold = now - (STALE_INVITE_DAYS * 86400)
    withdrawn = 0
    errors = 0

    from ..timeutil import to_epoch

    # Collect the stale ones first, then drain them oldest-first. Unipile
    # returns the list newest-first, so spending the run's budget in list order
    # withdraws the freshest stale invitation and leaves the oldest at the tail
    # — and the tail is never reached, because each day's sends cross the
    # threshold and land ahead of it about as fast as the daily cap drains.
    candidates: list[tuple[int, int, str, int, dict]] = []
    for inv in invitations:
        # Unipile sends invitations carry `date`; integer stamps may be millis.
        inv_time = to_epoch(
            inv.get("parsed_datetime")
            or inv.get("timestamp")
            or inv.get("created_at")
            or inv.get("date")
            or 0
        ) or 0

        if not inv_time or inv_time > stale_threshold:
            continue  # Not stale yet

        inv_id = inv.get("id") or inv.get("invitation_id") or ""
        if not inv_id:
            continue

        # Old invitations only carry a fuzzy "Sent 5 months ago" string, which
        # Unipile resolves against fetch time — good to the day, and no finer:
        # a whole cohort collapses onto one stamp whose sub-second part is
        # parse jitter tracking pagination order, not age. Bucket to the day so
        # that jitter cannot reorder anything, then break ties on the
        # invitation id, which LinkedIn issues monotonically.
        raw_id = str(inv_id)
        candidates.append(
            (inv_time // 86400, int(raw_id) if raw_id.isdigit() else 0,
             inv_id, inv_time, inv)
        )

    candidates.sort(key=lambda c: (c[0], c[1]))  # oldest first

    for _day, _seq, inv_id, inv_time, inv in candidates:
        if withdrawn >= budget:
            logger.info(
                "Withdrawal budget for this run exhausted (%d) — %d/%d used today",
                budget, current + withdrawn, cap,
            )
            break

        days_old = (now - inv_time) // 86400

        # Space withdrawals apart so a backlog drains as a trickle, not a burst.
        if withdrawn > 0:
            await asyncio.sleep(random.uniform(WITHDRAW_DELAY_MIN, WITHDRAW_DELAY_MAX))

        try:
            result = await client.withdraw_invitation(account_id, inv_id)
            if result.get("success"):
                withdrawn += 1
                # The invitation is gone from LinkedIn; close the row behind it
                # or it sits at 'invited' forever, unable to escalate and still
                # counted as outstanding.
                from ..db.queries import close_withdrawn_invitation

                closed = await run_db(
                    close_withdrawn_invitation,
                    inv.get("invited_user_id") or "",
                    inv.get("invited_user_public_id") or "",
                )
                await db.log_action(
                    "stale_invite_withdrawn",
                    outreach_id=closed or "",
                    result="success",
                    details={
                        "invitation_id": inv_id,
                        "days_old": days_old,
                        "name": inv.get("invited_user") or inv.get("name") or "",
                        "outreach_closed": bool(closed),
                    },
                )
                logger.info("Withdrew stale invitation %s (%d days old)", inv_id, days_old)
            else:
                errors += 1
                logger.warning("Failed to withdraw invitation %s: %s", inv_id, result.get("error"))
        except Exception as e:
            errors += 1
            logger.warning("Error withdrawing invitation %s: %s", inv_id, e)

    await client.close()

    if withdrawn > 0:
        invalidate_pending_cache()
        # Withdrawals just logged — make the next cap check see them.
        from ..linkedin.rate_limiter import invalidate_daily_cache
        invalidate_daily_cache()

    summary = f"Stale invitations: {withdrawn} withdrawn"
    if errors:
        summary += f", {errors} failed"
    logger.info("Scheduler: %s", summary)
    return summary


# ──────────────────────────────────────────────
# Brand strategy executors (account-level)
# ──────────────────────────────────────────────


async def _execute_brand_post(job: dict[str, Any]) -> str:
    """Execute a brand post job — generate and auto-publish a LinkedIn post.

    Loads the next pending post action from the brand plan and delegates
    to the brand_strategy tool's post execution logic.
    """
    from ..linkedin import get_account_id
    from ..services.brand_service import (
        get_next_pending_action_by_type,
        load_brand_plan,
    )

    logger.info("Scheduler: executing brand post")

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected"

    plan = await run_db(load_brand_plan)
    if not plan:
        return "No brand plan found"

    action = get_next_pending_action_by_type(plan, "post")
    if not action:
        return "No pending post actions in brand plan"

    action_id = action.get("id", "")

    from ..tools.brand_strategy import (
        ALREADY_CLAIMED,
        _claim_brand_action,
        _execute_post_action,
    )

    # An MCP brand_strategy(action="execute") runs outside the leader lock and
    # selects the same pending action — without the claim both publish it.
    if not await run_db(_claim_brand_action, action_id):
        logger.info("Scheduler: brand post action %s already claimed", action_id)
        return ALREADY_CLAIMED

    try:
        result = await _execute_post_action(action, action_id, account_id)
        logger.info("Scheduler: brand post result: %s", result[:100])
        return result
    except Exception as e:
        logger.error("Scheduler: brand post failed: %s", e)
        return f"Brand post failed: {e}"


async def _execute_brand_engage(job: dict[str, Any]) -> str:
    """Execute a brand engagement job — find trending posts and engage.

    Loads the next pending engagement action from the brand plan and delegates
    to the brand_strategy tool's engagement execution logic.
    """
    from ..linkedin import get_account_id
    from ..services.brand_service import (
        get_next_pending_action_by_type,
        load_brand_plan,
    )

    logger.info("Scheduler: executing brand engagement")

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected"

    plan = await run_db(load_brand_plan)
    if not plan:
        return "No brand plan found"

    action = get_next_pending_action_by_type(plan, "engagement")
    if not action:
        return "No pending engagement actions in brand plan"

    action_id = action.get("id", "")

    from ..tools.brand_strategy import (
        ALREADY_CLAIMED,
        _claim_brand_action,
        _execute_engagement_action,
    )

    if not await run_db(_claim_brand_action, action_id):
        logger.info("Scheduler: brand engagement action %s already claimed", action_id)
        return ALREADY_CLAIMED

    try:
        result = await _execute_engagement_action(action, action_id, account_id)
        logger.info("Scheduler: brand engagement result: %s", result[:100])
        return result
    except Exception as e:
        logger.error("Scheduler: brand engagement failed: %s", e)
        return f"Brand engagement failed: {e}"


async def _execute_brand_profile(job: dict[str, Any]) -> str:
    """Execute the next pending profile_optimize or photo_enhance action."""
    from ..linkedin import get_account_id
    from ..services.brand_service import (
        get_next_pending_profile_action,
        load_brand_plan,
    )

    logger.info("Scheduler: executing brand profile")

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected"

    plan = await run_db(load_brand_plan)
    if not plan:
        return "No brand plan found"

    from ..db.queries import has_running_headline_test

    skip_optimize = await run_db(has_running_headline_test)
    action = get_next_pending_profile_action(plan, skip_optimize=skip_optimize)
    if not action:
        if skip_optimize:
            return "Skipped profile optimize — headline A/B test is running"
        return "No pending profile actions in brand plan"

    action_id = action.get("id", "")
    action_type = action.get("type", "")

    from ..tools.brand_strategy import (
        ALREADY_CLAIMED,
        _claim_brand_action,
        _execute_profile_action,
        _handle_photo_enhance,
    )

    if not await run_db(_claim_brand_action, action_id):
        logger.info("Scheduler: brand profile action %s already claimed", action_id)
        return ALREADY_CLAIMED

    try:
        if action_type == "photo_enhance":
            result = await _handle_photo_enhance(account_id, action_id=action_id)
        else:
            result = await _execute_profile_action(action, action_id)
        logger.info("Scheduler: brand profile result: %s", result[:100])
        return result
    except Exception as e:
        logger.error("Scheduler: brand profile failed: %s", e)
        return f"Brand profile failed: {e}"


async def _execute_brand_analyze(job: dict[str, Any]) -> str:
    """Execute a brand analyze job — audit profile and optionally generate plan.

    Runs a full brand analysis. If no plan exists after analysis,
    also generates a 4-week plan automatically.
    """
    from ..linkedin import get_account_id

    logger.info("Scheduler: executing brand analysis")

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected"

    from ..tools.brand_strategy import _handle_analyze, _handle_plan

    try:
        result = await _handle_analyze(account_id, "")
        logger.info("Scheduler: brand analysis complete")

        # Clear re-analysis flag if it was set (by campaign creation)
        if await db.get_setting("brand_reanalyze_needed", False):
            await db.save_setting("brand_reanalyze_needed", False)
            logger.info("Scheduler: cleared brand_reanalyze_needed flag")

        # Auto-generate plan if none exists
        from ..services.brand_service import load_brand_plan

        plan = await run_db(load_brand_plan)
        if not plan:
            plan_result = await _handle_plan(account_id, "")
            logger.info("Scheduler: brand plan auto-generated")
            return f"{result}\n\n--- Auto-generated plan ---\n{plan_result}"

        return result
    except Exception as e:
        logger.error("Scheduler: brand analysis failed: %s", e)
        return f"Brand analysis failed: {e}"


# ──────────────────────────────────────────────
# Signal collection executors (v1.0)
# ──────────────────────────────────────────────


async def _execute_collect_keyword_signals(job: dict[str, Any]) -> str:
    """Execute keyword signal collection from LinkedIn post searches."""
    from ..collectors.keyword_collector import collect_keyword_signals

    logger.info("Scheduler: collecting keyword signals from LinkedIn posts")

    result = await collect_keyword_signals()
    logger.info("Scheduler: keyword signal collection result: %s", result)
    return result


async def _execute_scan_prospect_posts(job: dict[str, Any]) -> str:
    """Execute scanning campaign contacts' recent posts for signals."""
    from ..collectors.prospect_post_collector import collect_prospect_posts

    logger.info("Scheduler: scanning prospect posts for signals")

    result = await collect_prospect_posts()
    logger.info("Scheduler: prospect post scan result: %s", result)
    return result


async def _execute_scan_network_posts(job: dict[str, Any]) -> str:
    """Scan 1st-degree connections' posts for signals, outside any campaign."""
    from ..collectors.network_post_collector import collect_network_posts

    logger.info("Scheduler: scanning network posts for signals")

    result = await collect_network_posts()
    logger.info("Scheduler: network post scan result: %s", result)
    return result


async def _execute_classify_signals(job: dict[str, Any]) -> str:
    """Execute classification of pending signals via LLM."""
    from ..ai.signal_classifier import classify_pending_signals

    logger.info("Scheduler: classifying pending signals")

    result = await classify_pending_signals()
    logger.info("Scheduler: signal classification result: %s", result)
    return result


async def _execute_collect_profile_views(job: dict[str, Any]) -> str:
    """Execute LinkedIn profile viewer collection."""
    from ..collectors.profile_view_collector import collect_profile_views

    logger.info("Scheduler: collecting profile view signals")

    result = await collect_profile_views()
    logger.info("Scheduler: profile view collection result: %s", result)
    return result


async def _execute_detect_job_changes(job: dict[str, Any]) -> str:
    """Execute job change detection for campaign contacts."""
    from ..collectors.job_change_collector import collect_job_changes

    logger.info("Scheduler: scanning contacts for job changes")

    result = await collect_job_changes()
    logger.info("Scheduler: job change detection result: %s", result)
    return result


async def _execute_collect_competitor_signals(job: dict[str, Any]) -> str:
    """Execute competitor mention collection from LinkedIn posts."""
    from ..collectors.competitor_collector import collect_competitor_signals

    logger.info("Scheduler: collecting competitor mention signals")

    result = await collect_competitor_signals()
    logger.info("Scheduler: competitor signal collection result: %s", result)
    return result


async def _execute_collect_hiring_signals(job: dict[str, Any]) -> str:
    """Execute hiring surge detection from LinkedIn job searches."""
    from ..collectors.hiring_collector import collect_hiring_signals

    logger.info("Scheduler: collecting hiring surge signals")

    result = await collect_hiring_signals()
    logger.info("Scheduler: hiring signal collection result: %s", result)
    return result


async def _execute_collect_news_signals(job: dict[str, Any]) -> str:
    """Execute news/funding signal collection from SERPER API."""
    from ..collectors.news_collector import collect_news_signals

    logger.info("Scheduler: collecting news/funding signals")

    result = await collect_news_signals()
    logger.info("Scheduler: news signal collection result: %s", result)
    return result


async def _execute_collect_watchlist_web_signals(job: dict[str, Any]) -> str:
    """Execute off-LinkedIn watchlist web collection from SERPER search."""
    from ..collectors.watchlist_web_collector import collect_watchlist_web_signals

    logger.info("Scheduler: collecting watchlist web signals")

    result = await collect_watchlist_web_signals()
    logger.info("Scheduler: watchlist web collection result: %s", result)
    return result


# ──────────────────────────────────────────────
# Company page engagement collectors (Phase 2 intent expansion)
# ──────────────────────────────────────────────


async def _execute_collect_company_page(job: dict[str, Any]) -> str:
    """Execute company page engagement signal collection."""
    from ..collectors.company_page_collector import collect_company_page_signals

    logger.info("Scheduler: collecting company page engagement signals")
    result = await collect_company_page_signals()
    logger.info("Scheduler: company page collection result: %s", result)
    return result


async def _execute_collect_company_followers(job: dict[str, Any]) -> str:
    """Execute company follower signal collection."""
    from ..collectors.company_follower_collector import collect_company_followers

    logger.info("Scheduler: collecting company follower signals")
    result = await collect_company_followers()
    logger.info("Scheduler: company follower collection result: %s", result)
    return result


# ──────────────────────────────────────────────
# Signal activation executor (v1.1)
# ──────────────────────────────────────────────


async def _execute_activate_signals(job: dict[str, Any]) -> str:
    """Execute signal activation — convert classified signals into outreach.

    Routes signals by composite score:
    - Score >= 0.7 + buying intent → auto-create outreach with signal context
    - Score >= 0.5 → add to matching campaign as warm lead
    - Score >= 0.3 + existing contact → boost engagement priority
    - Below threshold → mark as below_threshold
    """
    from ..services.signal_activator import activate_pending_signals

    logger.info("Scheduler: activating classified signals")

    result = await activate_pending_signals()
    logger.info("Scheduler: signal activation result: %s", result)
    return result


async def _execute_detect_compound_intent(job: dict[str, Any]) -> str:
    """Execute compound intent detection — stack signals into high-value events.

    Scans all signal accounts for 2+ signal types within 14 days.
    Creates intent_events when compound patterns match (e.g.,
    job_change + hiring_surge → "new_leader_building").
    """
    from ..services.signal_scorer import detect_all_compound_intents

    logger.info("Scheduler: detecting compound intent events")

    result = await run_db(detect_all_compound_intents)
    logger.info("Scheduler: compound intent result: %s", result)
    return result


async def _execute_decay_cycle(job: dict[str, Any]) -> str:
    """Execute signal decay cycle — expire old signals and recompute scores.

    Runs every 6 hours:
    1. Expires signals past their TTL
    2. Expires old intent events
    3. Recomputes all signal account composite scores
    """
    from ..services.signal_service import run_decay_cycle

    logger.info("Scheduler: running signal decay cycle")

    result = await run_db(run_decay_cycle)
    logger.info("Scheduler: decay cycle result: %s", result)
    return result


async def _execute_rematch_signals(job: dict[str, Any]) -> str:
    """Re-match homeless signals to active campaigns.

    Runs every 1 hour. Picks up signals that previously had no matching
    campaign and re-evaluates them against all active campaign ICPs.
    """
    from ..services.signal_linker import match_signals_to_campaigns

    logger.info("Scheduler: running signal rematch cycle")

    result = await run_db(match_signals_to_campaigns)
    logger.info("Scheduler: signal rematch result: %s", result)
    return result


async def _execute_backfill_orphan_signals(job: dict[str, Any]) -> str:
    """Backfill orphan signals to now-known contacts.

    Runs every 2 hours. Links signals (prospect_id IS NULL) whose
    linkedin_id now matches a contact in the contacts table.
    """
    from ..services.signal_linker import backfill_orphan_signals

    logger.info("Scheduler: running orphan signal backfill")

    result = await run_db(backfill_orphan_signals)
    logger.info("Scheduler: orphan backfill result: %s", result)
    return result


async def _execute_partner_reminder(job: dict[str, Any]) -> str:
    """Check for due partner follow-ups and send email reminders to self.

    Runs every hour. For each overdue partner follow-up:
    1. Builds reminder email with partner context
    2. Sends to user's own email via Unipile
    3. Advances the follow-up schedule (escalating cadence)
    """
    from ..tools.partner_followup import build_partner_reminder_email

    logger.info("Scheduler: checking partner follow-up reminders")

    due = await db.get_due_partner_reminders()
    if not due:
        return "No partner follow-ups due"

    user_email = await db.get_setting("user_email", "")
    if not user_email:
        logger.warning(
            "Partner reminder: %d follow-ups due but no user_email configured",
            len(due),
        )
        return f"{len(due)} partner follow-ups due but no email account configured"

    from ..linkedin import UnipileError, get_linkedin_client
    from ..services.unipile_email import resolve_email_account_id

    sent = 0
    try:
        client = get_linkedin_client()
    except UnipileError as e:
        logger.warning("Partner reminder: no client: %s", e)
        return f"{len(due)} partner follow-ups due but no email account configured"
    try:
        email_account_id = await resolve_email_account_id(client)
    except Exception as e:
        # Resolution now distinguishes "could not ask" from "no mailbox" and
        # raises for the former. Unhandled, that ends an unattended job and
        # leaks the client. Reporting the real cause also matters: "no email
        # account configured" sent the operator to reconnect a mailbox that
        # was already connected.
        await client.close()
        logger.warning(
            "Partner reminder: %d follow-ups due but the mailbox could not be "
            "checked: %s", len(due), e,
        )
        return (
            f"{len(due)} partner follow-ups due but the mailbox could not be "
            f"checked: {e}"
        )
    if not email_account_id:
        await client.close()
        logger.warning(
            "Partner reminder: %d follow-ups due but no Unipile mailbox connected",
            len(due),
        )
        return f"{len(due)} partner follow-ups due but no email account configured"
    for partner in due:
        try:
            email_body = build_partner_reminder_email(partner)
            name = partner.get("name", "Unknown")
            company = partner.get("company", "")
            count = partner.get("followup_count", 0) + 1
            subject = f"🔔 Follow up with {name}" + (f" ({company})" if company else "")

            result = await client.send_email(
                account_id=email_account_id,
                to_email=user_email,
                to_name="HeyLead",
                subject=subject,
                body=email_body,
                body_html=email_body,
            )
            if result.get("success"):
                await db.advance_partner_followup(partner["id"])
                sent += 1
                logger.info("Partner reminder sent: %s (#%d)", name, count)
            else:
                logger.warning(
                    "Partner reminder email failed for %s: %s",
                    name, result.get("error", "unknown"),
                )
        except Exception as e:
            logger.error("Partner reminder error for %s: %s", partner.get("name"), e)

    await client.close()
    msg = f"Sent {sent}/{len(due)} partner follow-up reminders"
    logger.info("Scheduler: %s", msg)
    return msg


async def _execute_daily_digest(job: dict[str, Any]) -> str:
    """Compile and log the daily digest.

    The digest is stored as a scheduler event so it can be retrieved
    via scheduler(action='logs'). If email is configured, also sends
    a copy to the user.
    """
    from ..services.daily_digest import compile_daily_digest
    logger.info("Scheduler: compiling daily digest")
    digest = await compile_daily_digest()

    # Store as event for retrieval via logs tool
    await db.log_scheduler_event(
        "daily_digest",
        context={"digest": digest},
    )

    # If email is configured, send digest to user via Unipile (never Mail.app)
    user_email = await db.get_setting("user_email", "")

    if user_email:
        client = None
        try:
            from ..linkedin import get_linkedin_client
            from ..services.unipile_email import resolve_email_account_id

            client = get_linkedin_client()
            email_account_id = await resolve_email_account_id(client)
            if not email_account_id:
                logger.info("Scheduler: digest compiled but no Unipile mailbox connected")
            else:
                from datetime import date
                subject = f"HeyLead Daily Digest — {date.today().isoformat()}"
                send_result = await client.send_email(
                    account_id=email_account_id,
                    to_email=user_email,
                    to_name="HeyLead User",
                    subject=subject,
                    body=digest,
                )
                # The result decides what we claim. This used to log "emailed"
                # unconditionally, which removed the only signal an operator
                # has that the job actually works.
                if (send_result or {}).get("success"):
                    logger.info("Scheduler: daily digest emailed to %s", user_email)
                else:
                    logger.warning(
                        "Scheduler: digest NOT delivered to %s: %s",
                        user_email, (send_result or {}).get("error", "unknown"),
                    )
        except Exception as e:
            logger.warning("Scheduler: failed to email digest: %s", e)
        finally:
            # An unguarded await sits between constructing the client and
            # closing it, and mailbox resolution can now raise. Without this,
            # every occurrence leaks an httpx client inside a daemon that stays
            # up for days.
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    logger.debug("digest: client close failed", exc_info=True)

    return digest


# ──────────────────────────────────────────────
# Communication Strategist Executors
# ──────────────────────────────────────────────

async def _execute_daily_strategy(job: dict[str, Any]) -> str:
    """Execute the daily strategy planning cycle."""
    from ..services.communication_strategist import run_daily_strategy

    logger.info("Scheduler: running daily strategy planning")
    result = await run_daily_strategy()
    plans = result.get("plans_created", 0)
    evaluated = result.get("plans_evaluated", 0)
    return f"Daily strategy: {plans} plans created, {evaluated} evaluated"


async def _execute_strategy_plans(job: dict[str, Any]) -> str:
    """Execute planned actions from daily strategy for a campaign."""
    campaign_id = job.get("campaign_id")
    if not campaign_id:
        return "No campaign_id for strategy plan execution"
    from ..services.communication_strategist import execute_planned_actions

    jobs_created = await execute_planned_actions(campaign_id)
    return f"Strategy plans: {jobs_created} jobs created"


async def _execute_verify_actions(job: dict[str, Any]) -> str:
    """Verify that recent outreach actions actually landed on LinkedIn."""
    from ..services.action_verifier import verify_recent_actions

    stats = await verify_recent_actions()
    return (
        f"Verification: checked={stats['checked']} "
        f"confirmed={stats['confirmed']} "
        f"unconfirmed={stats['unconfirmed']} "
        f"errors={stats['errors']}"
    )


async def _execute_sync_connections(job: dict[str, Any]) -> str:
    """Sync 1st-degree LinkedIn connections to the local DB.

    Fetches relations from Unipile and upserts into the local connections table,
    keeping the cache the dedup and 1st-degree pre-send checks read.

    Queued every 4 hours. A run that completes a clean relations walk stamps the
    full-sync marker, and the runs inside the next FULL_SYNC_STALE_SECONDS then
    return the cached count without calling the API. Nothing else stamps it: an
    inbox-fallback run, a response the prune guard reads as truncated, and a walk
    abandoned at the budget all leave the marker where it was, so those accounts
    pay for a fetch attempt on every four-hourly run. That is the intended
    trade — the marker says "the whole network was fetched", and only a complete
    walk can say it — and it is what the budget below is for: the attempt is
    capped, so a run that cannot finish cannot take the scheduler tick with it.
    """
    from ..linkedin import get_linkedin_client
    from ..services.connection_sync import (
        SCHEDULED_SYNC_BUDGET_SECONDS,
        get_connection_count,
        sync_connections,
    )

    client = get_linkedin_client()
    try:
        account_id, _ = await client.find_linkedin_account()
        if not account_id:
            return "No LinkedIn account connected — skipping connection sync"

        synced = await sync_connections(
            client, account_id, time_budget=SCHEDULED_SYNC_BUDGET_SECONDS,
        )
        total = await run_db(get_connection_count, account_id)
        return f"Connection sync: {synced} synced, {total} total in local DB"
    except Exception as e:
        logger.warning("Connection sync failed: %s", e)
        return f"Connection sync failed: {e}"
    finally:
        await client.close()


# ──────────────────────────────────────────────
# Post Intelligence v2 Executors
# ──────────────────────────────────────────────


async def _execute_collect_posts_distributed(job: dict[str, Any]) -> str:
    """Execute distributed multi-account post collection."""
    from ..collectors.distributed_post_collector import collect_posts_distributed

    logger.info("Scheduler: running distributed post collection")
    result = await collect_posts_distributed()
    logger.info("Scheduler: distributed collection result: %s", result)
    return result


async def _execute_research_contacts(job: dict[str, Any]) -> str:
    """Execute contact research pipeline."""
    from ..services.post_research_service import research_pending_contacts

    logger.info("Scheduler: researching pending contacts")
    result = await research_pending_contacts()
    logger.info("Scheduler: research result: %s", result)
    return result


async def _execute_backfill_post_analysis(job: dict[str, Any]) -> str:
    """Execute backfill of unanalyzed posts."""
    from ..services.post_research_service import backfill_post_analysis

    logger.info("Scheduler: backfilling unanalyzed posts")
    result = await backfill_post_analysis()
    logger.info("Scheduler: backfill result: %s", result)
    return result


async def _execute_detect_viral_posts(job: dict[str, Any]) -> str:
    """Execute viral post detection from metric snapshots."""
    from ..services.post_research_service import detect_viral_posts

    logger.info("Scheduler: detecting viral posts")
    result = await detect_viral_posts()
    logger.info("Scheduler: viral detection result: %s", result)
    return result


async def _execute_tune_watchlists(job: dict[str, Any]) -> str:
    """Execute keyword watchlist auto-tuning."""
    from ..services.post_research_service import auto_tune_watchlists

    logger.info("Scheduler: auto-tuning keyword watchlists")
    result = await auto_tune_watchlists()
    logger.info("Scheduler: watchlist tuning result: %s", result)
    return result


async def _execute_optimize_signals(job: dict[str, Any]) -> str:
    """Execute daily signal self-optimization (weights, keywords, warmup, thresholds)."""
    from ..services.signal_optimizer import run_signal_optimization

    logger.info("Scheduler: running signal optimization")
    result = await run_signal_optimization()
    parts = []
    if result["weights_adjusted"]:
        parts.append(f"{result['weights_adjusted']} weights")
    if result["keywords_added"]:
        parts.append(f"{result['keywords_added']} keywords")
    if result["warmup_changes"]:
        parts.append(f"{result['warmup_changes']} warmup")
    if result["threshold_changes"]:
        parts.append(f"{result['threshold_changes']} thresholds")
    summary = ", ".join(parts) if parts else "no changes"
    logger.info("Scheduler: signal optimization done: %s", summary)
    return f"Signal optimization: {summary}"


# ──────────────────────────────────────────────
# Post Intelligence Phase 5 executors
# ──────────────────────────────────────────────


async def _execute_classify_post_intent(job: dict[str, Any]) -> str:
    """Execute post content intent classification (Phase 5)."""
    from ..collectors.post_intent_collector import classify_post_intents

    logger.info("Scheduler: classifying post intents")
    result = await classify_post_intents()
    logger.info("Scheduler: post intent result: %s", result)
    return result


async def _execute_mine_comments(job: dict[str, Any]) -> str:
    """Execute comment mining from competitor/industry posts (Phase 5)."""
    from ..collectors.comment_mining_collector import mine_post_comments

    logger.info("Scheduler: mining post comments")
    result = await mine_post_comments()
    logger.info("Scheduler: comment mining result: %s", result)
    return result


# ──────────────────────────────────────────────
# Campaign auto-refill executor
# ──────────────────────────────────────────────


async def _execute_campaign_refill(job: dict[str, Any]) -> str:
    """Execute campaign refill — search LinkedIn for more prospects when running low."""
    import asyncio

    from ..services.campaign_refill_service import REFILL_BUDGET_SECONDS, refill_campaigns

    remaining = float(job.get("_job_timeout") or REFILL_BUDGET_SECONDS)
    budget = min(REFILL_BUDGET_SECONDS, max(1.0, remaining - 4.0))
    logger.info("Scheduler: checking campaigns for refill (budget=%.1fs)", budget)
    try:
        result = await refill_campaigns(budget_seconds=budget)
    except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError) as exc:
        logger.warning("Campaign refill deferred after timeout: %s", type(exc).__name__)
        return JobResult("deferred", f"refill deferred: {type(exc).__name__}")
    logger.info("Scheduler: campaign refill result: %s", result)
    return result


async def _execute_backfill_profiles(job: dict[str, Any]) -> str:
    """Backfill empty profile_json for contacts across all campaigns."""
    from ..services.campaign_refill_service import backfill_empty_profiles

    logger.info("Scheduler: backfilling empty profiles")
    result = await backfill_empty_profiles()
    logger.info("Scheduler: profile backfill result: %s", result)
    return result


# ──────────────────────────────────────────────
# Plan Execution Tracking
# ──────────────────────────────────────────────

def _plan_skip_reason(result: str, outcome: str) -> str:
    lower = (result or "").lower()
    if "cap reached" in lower or outcome == "daily_cap":
        return "daily_cap"
    if "too soon" in lower or "minimum gap" in lower:
        return "message_gap"
    return outcome or "skipped"


async def _track_plan_execution(
    job: dict[str, Any], action_type: str, result: str = "", outcome: str = "success",
) -> None:
    """Record the daily-plan outcome so the strategist does not re-queue.

    Skipped / deferred / daily_cap mark the action skipped. Non-fatal.
    """
    outreach_id = job.get("outreach_id")
    if not outreach_id:
        return
    try:
        from ..db import aio as db
        from ..db.strategist_queries import _today_str

        today = _today_str()
        if outcome in ("skipped", "deferred", "daily_cap"):
            await db.mark_action_skipped(
                outreach_id, today, action_type, _plan_skip_reason(result, outcome),
            )
            return
        await db.mark_action_executed(
            outreach_id,
            today,
            action_type,
            result[:200] if result else "",
            job.get("id", ""),
        )
    except Exception:
        pass  # Non-fatal
