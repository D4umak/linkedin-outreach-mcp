"""Cloud sync service — push/pull state between MCP client and backend scheduler.

MCP client pushes campaign/outreach state to the backend for 24/7 scheduling.
Backend executes outreach jobs (invites, follow-ups, engagements, reply checks)
via Cloud Scheduler every 5 minutes, even when the user's laptop is closed.

MCP client pulls back changes (outreach status updates, new messages) periodically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from typing import Any

import httpx

from .. import config, constants
from ..db import queries, signal_queries
from ..db.async_bridge import run_db

logger = logging.getLogger(__name__)

_SYNC_CHUNK_RETRIES = 3
_SYNC_CHUNK_WARN_INTERVAL = 3600.0
_sync_chunk_warned_at = 0.0

# Bounds the drain loop when a full page keeps coming back.
_SIGNAL_SYNC_MAX_PAGES = 20
_DIRECTORY_CONTRIBUTE_CHUNK = 200
_DIRECTORY_PULL_LIMIT = 500

# Live funnel ranks. Cloud must not rewind these to pending/skipped because
# the backend row is often stale relative to a local invite that just sent.
_CLOUD_STATUS_RANK = {
    "pending": 0,
    "review_pending": 0,
    "sending": 1,
    "sending_followup": 1,
    "invited": 2,
    # Same rank as invited: the invite went out, then we ended it. A stale
    # cloud 'invited' must not rewind that. Real forward progress (they
    # accepted) still outranks it. Missing from this table, .get(..., 0)
    # scored it as pending, and every pull after a local withdrawal wrote
    # the seven 151-day rows back to invited (22 Aug 2026, 21:50).
    "withdrawn": 2,
    "connected": 3,
    "messaged": 4,
    "replied": 5,
    "hot_lead": 6,
    "reverse_pitch": 6,
    "closed_happy": 7,
    "closed_unhappy": 7,
    "exhausted": 7,
    "error": -1,
    "skipped": -1,
}
# Terminal both ways: a cloud row in one of these is a decision about a
# person, not funnel churn, so it applies over any local state and no local
# state is overwritten while it stands. The outcome *words* are here because
# a backend older than _OUTCOME_STATUS (6 Sep 2026) still stores them, and an
# unranked status is logged-and-ignored — which silently dropped every manual
# close made in the dashboard.
_CLOUD_FORCE_STATUS = frozenset({
    "opted_out", "unsubscribed", "bounced",
    "opt_out", "won", "lost",
    "closed_happy", "closed_unhappy",
})

# The half of _CLOUD_FORCE_STATUS that is a compliance line rather than a
# funnel outcome. Never overwritten locally, whatever the cloud's version:
# nothing about cloud-only sending makes an opt-out reversible by a clock.
_CLOUD_SUPPRESSION_STATUS = frozenset({
    "opted_out", "unsubscribed", "bounced", "opt_out",
})

# A stop made in the dashboard. Unlike 'error' or a stale 'pending', this is
# news the mirror has no other way to learn, so it applies to a live row
# instead of being read as backward churn. The reverse direction stays
# guarded: a LOCAL park is still not undone by a backward cloud status
# (test_skipped_survives_cloud_pull), and the cloud makes the same
# distinction in _push_reasserts_a_stopped_row.
_CLOUD_STOP_STATUS = frozenset({"skipped"})

# Warn once per unknown status rather than on every pull.
_unranked_cloud_statuses_seen: set[str] = set()


def should_apply_cloud_status(local_status: str, cloud_status: str) -> bool:
    """True when a backend status update is allowed to overwrite local state."""
    if not cloud_status or cloud_status == local_status:
        return False
    # Suppression is terminal. These carry no rank, so without this they scored
    # 0 as a *local* status and any forward cloud row overwrote them — which
    # puts someone who opted out back into the planner's queue.
    if local_status in _CLOUD_FORCE_STATUS:
        return False
    if cloud_status in _CLOUD_FORCE_STATUS:
        return True
    # A stop reaches a live row. It must not, however, resurrect a row this
    # client has already parked or closed: those are handled above and by the
    # rank rules below, and a cloud 'skipped' arriving at a local 'skipped'
    # is the equality case already returned False at the top.
    if cloud_status in _CLOUD_STOP_STATUS and local_status not in _CLOUD_STOP_STATUS:
        return True
    # The backend has statuses this client does not: 'expired' is one, and the
    # only place it appears here is _format_diagnostics_backend, which counts
    # it. Ranking by .get(..., 0) silently made every such status a rank-0
    # *forward* step, so it beat anything ranked below zero and rewrote local
    # 'error' and 'skipped' rows — 4,216 of the 4,990 in the live DB. A rank
    # the table never assigned is not a comparison, it is a default that wins.
    # Reject it and say so, so the vocabulary gap is visible instead of acted on.
    if cloud_status not in _CLOUD_STATUS_RANK:
        if cloud_status not in _unranked_cloud_statuses_seen:
            _unranked_cloud_statuses_seen.add(cloud_status)
            logger.warning(
                "Backend sent outreach status %r, which this client does not rank — "
                "ignoring it. Add it to _CLOUD_STATUS_RANK to apply it.",
                cloud_status,
            )
        return False
    local_rank = _CLOUD_STATUS_RANK.get(local_status or "", 0)
    cloud_rank = _CLOUD_STATUS_RANK[cloud_status]
    # 'skipped' is terminal locally, in the same way suppression is above.
    # provider_id_repair parks unsendable contacts there and nothing picks a
    # parked row up again — that is the whole mechanism. But 'skipped' and
    # 'error' both score -1, so a cloud 'error' passed `local_rank <= 0` and a
    # cloud 'pending' passed `cloud_rank > local_rank`, and either won. Of the
    # ~12 rows parked on 21 Aug 2026, none were still parked a day later; one
    # was undone four minutes after it was set. Real forward progress still
    # gets through: if the backend has seen this person connect or reply, that
    # outranks a decision about whether we can send to them.
    if local_status == "skipped" and cloud_rank <= 0:
        return False
    if cloud_rank < 0:
        return local_rank <= 0
    return cloud_rank > local_rank


_TIMEOUT = httpx.Timeout(30.0, connect=15.0, read=30.0, write=30.0)
# Full-history backfills upsert thousands of rows on the backend's small DB —
# allow minutes (Cloud Run's own request timeout is 300s).
_BACKFILL_TIMEOUT = httpx.Timeout(300.0, connect=15.0, read=300.0, write=120.0)
# One 15k-row request exceeded Cloud Run's 300s limit outright (504). Backfill
# therefore ships ordered slices small enough to ingest well inside the limit.
#
# 1000 was still too big to be reliable. The service runs --max-instances=1 at
# 512Mi and the local daemon is calling it at the same time, so a 1000-row
# chunk drew a 502 from the Cloud Run frontend at chunk 9 of 30 — no app-side
# error, because the container never got the request. At 250 the same backfill
# completed: 17 campaigns, 5,821 contacts, 4,957 outreaches. More requests,
# each one small enough to be served.
_BACKFILL_CHUNK_ROWS = 250

# The hosted dashboard renders whatever this client last pushed, with no notion
# of how old that is. Observe withholds live campaigns outright (see
# sync_to_cloud), a rejected push carries nothing, and either way heylead.dev
# goes on presenting frozen figures as current — on 18 Aug 2026 that was three
# campaigns reading 4/3/2 connected against 0/0/0 locally, for about an hour,
# with nothing anywhere saying so. Recording each push is what lets
# show_status() and scheduler(action='status') report staleness instead of
# leaving it to be inferred.
#
# Shape: {"campaigns": {campaign_id: epoch}, "error": {"ts": epoch, "message": str}}
_PUSH_LOG_SETTING = "cloud_push_log"

# Two periodic pushes (server.CLOUD_PUSH_INTERVAL_SECONDS is 900). One missed
# push is a slow tick and says nothing; two is something not working.
PUSH_STALE_AFTER_SECONDS = 1800


class BackendAuthError(Exception):
    """Raised when the backend returns 401/403 — JWT expired or invalid."""

    pass


def _headers() -> dict[str, str]:
    """Build auth headers for backend API calls."""
    _, jwt = config.get_backend_config()
    headers = {
        "Authorization": f"Bearer {jwt}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    org_id = config.get_active_org_id()
    if org_id:
        headers["X-Org-Id"] = org_id
    return headers


def _base_url() -> str:
    """Get backend base URL."""
    url, _ = config.get_backend_config()
    return url.rstrip("/")


# ── Push freshness: what reached the dashboard, and when ──


def get_push_log() -> dict[str, Any]:
    """Read the record of what last reached the cloud, and when.

    Returns ``{"campaigns": {campaign_id: epoch}, "error": {...} | None}``.
    Always that shape, whatever is in the settings row: this feeds two status
    renderers, and a malformed row must degrade to "nothing known" rather than
    break the dashboard it exists to describe.
    """
    stored = queries.get_setting(_PUSH_LOG_SETTING, {})
    if not isinstance(stored, dict):
        return {"campaigns": {}, "error": None}
    campaigns = stored.get("campaigns")
    error = stored.get("error")
    ts = stored.get("ts")
    handover = stored.get("handover")
    return {
        "campaigns": dict(campaigns) if isinstance(campaigns, dict) else {},
        "error": error if isinstance(error, dict) else None,
        "ts": int(ts) if isinstance(ts, (int, float)) else 0,
        # First successful push per campaign. Periodic sync must not reset
        # the silence clock by restamping campaigns[id] every 15 minutes.
        "handover": dict(handover) if isinstance(handover, dict) else {},
    }


def _record_push(campaign_ids: list[str], error: str = "") -> None:
    """Stamp the outcome of one completed push.

    Campaigns absent from ``campaign_ids`` keep whatever stamp they had — that
    is the whole point. A campaign withheld in observe holds its old timestamp,
    which is the truth about the dashboard: nothing newer has been sent for it.

    A failed push records the failure and stamps no campaign, including on a
    partial failure. Chunk 1 carries the campaign rows and chunk 5 their
    messages, so a mid-sequence failure leaves the dashboard holding half an
    update; claiming the campaigns are current would be worse than saying
    nothing landed.
    """
    log = get_push_log()
    now = int(time.time())
    if error:
        log["error"] = {"ts": now, "message": error[:300]}
    else:
        handover = log.setdefault("handover", {})
        for cid in campaign_ids:
            log["campaigns"][cid] = now
            if cid not in handover:
                handover[cid] = now
        # Stamped even when the push carried no campaigns: account-level work
        # (brand posts) has no campaign to look up, and "has the backend ever
        # received a sync from us" is the question it needs answered.
        log["ts"] = now
        log["error"] = None
    queries.save_setting(_PUSH_LOG_SETTING, log)


# The other half of the sync, and until now the unreported one. A push that
# fails is recorded and read back out by dashboard_freshness_lines; a pull that
# fails was caught by a bare `except Exception` in the loop and logged at debug,
# so a cloud that kept sending while this machine stopped hearing about it
# looked exactly like a quiet afternoon. It also starves the stand-down gate
# above, whose state only the pull refreshes.
#
# Shape: {"ts": epoch_of_last_success, "error": {"ts": epoch, "message": str},
#         "skipped": {"ts": epoch, "rows": int, "campaigns": int}}
#
# "skipped" is what the pull fetched and then threw away — rows whose campaign
# is not in this DB (see _adopt_cloud_outreach). It is deliberately not an
# error: the pull worked. It says the local picture is partial, which is a
# different claim and has a different fix.
_PULL_LOG_SETTING = "cloud_pull_log"

# Pulls run every 5 minutes (server.SYNC_INTERVAL). Three missed rounds is not
# a slow tick.
PULL_STALE_AFTER_SECONDS = 1800


def get_pull_log() -> dict[str, Any]:
    """Read when results last came down from the backend, and what went wrong.

    Always ``{"ts": epoch, "error": {...} | None, "skipped": {...} | None}``,
    whatever is in the row — this feeds a status renderer, which must degrade
    to "nothing known" rather than break.
    """
    stored = queries.get_setting(_PULL_LOG_SETTING, {})
    if not isinstance(stored, dict):
        return {"ts": 0, "error": None, "skipped": None}
    ts = stored.get("ts")
    error = stored.get("error")
    skipped = stored.get("skipped")
    return {
        "ts": int(ts) if isinstance(ts, (int, float)) else 0,
        "error": error if isinstance(error, dict) else None,
        "skipped": skipped if isinstance(skipped, dict) else None,
    }


def _pull_error_text(exc: Exception) -> str:
    """Describe a failed pull in a way that survives an empty exception.

    httpx raises timeouts and read errors with no args, so ``f"Pull failed:
    {e}"`` rendered as "Pull failed: " — a line that reports a failure and
    withholds the only fact anyone could act on. The class name is the
    difference between "the backend is slow" and "DNS is down".
    """
    detail = str(exc).strip() or type(exc).__name__
    return f"Pull failed: {detail}"


def _record_pull(error: str = "", skipped: dict[str, int] | None = None) -> None:
    """Stamp the outcome of one pull.

    A success clears the standing error and moves the timestamp; a failure
    records the message and leaves the timestamp where it was, because the
    timestamp answers "how old is what we know" and a failed pull taught us
    nothing new.

    ``skipped`` is the count of rows the pull discarded, and how many campaigns
    they spanned. A success with nothing dropped clears it, so the line never
    outlives the condition — the same rule the error already follows. A failure
    leaves it alone: a pull that never landed did not discard anything, and
    wiping the last known count would erase a standing problem.
    """
    log = get_pull_log()
    now = int(time.time())
    if error:
        log["error"] = {"ts": now, "message": error[:300]}
    else:
        log["ts"] = now
        log["error"] = None
        if skipped and skipped.get("rows"):
            log["skipped"] = {
                "ts": now,
                "rows": int(skipped.get("rows") or 0),
                "campaigns": int(skipped.get("campaigns") or 0),
            }
        else:
            log["skipped"] = None
    queries.save_setting(_PULL_LOG_SETTING, log)


# Which scheduler is sending, as far as this machine knows.
#
# Hosted default is sending_host=cloud: this machine stands down for every
# type the backend sends. Local sending is send_from host=local, not a
# health-check failover. This setting still records what the backend last
# said, for status and the dashboard.
#
# A local read, deliberately. The gate is consulted once per job creation, so
# asking the backend each time is out of the question; instead every answer the
# backend gives about its scheduler refreshes this row.
_CLOUD_STATE_SETTING = "cloud_scheduler_state"

# An "enabled" older than this is no longer evidence that the cloud is still
# sending, so sending falls back to the local scheduler. Long enough to survive
# a laptop that slept between ticks, short enough that a cloud switched off
# yesterday cannot silence today's outreach.
CLOUD_STATE_TTL_SECONDS = 3600

# A healthy pull that writes nothing is not proof the host is sending.
# After this quiet stretch, local takes the campaign back if anyone is waiting.
CLOUD_OUTBOUND_QUIET_SECONDS = 3 * 3600

# A restamp of the push log is not a send. If nobody has been contacted and
# people are waiting, local takes the campaign back after this shorter window
# — the 3-hour clock above is for a host that *was* producing and then went
# quiet, not for a handover that never started.
CLOUD_ZERO_SEND_SECONDS = 15 * 60

# What the backend sends, mirrored from its own gate rather than from the
# sentence in toggle_cloud_scheduler, which lists four families and undersells
# it. Read off heylead-api send_gate.CAMPAIGN_SEND_JOB_TYPES, each one
# confirmed against a planner that queues it and an executor that runs it:
#
#     invite  followup  send_dm  engage  follow  endorse
#     email_fallback  discover  campaign_refill
#
# Two names differ between the services and mean the same action: the backend's
# "email_fallback" is this client's JOB_EMAIL_INVITE, and its "discover" is
# covered here by campaign_refill, the only enrolment job this client schedules.
# Both are gated under the local name, because the local name is what the
# planner and executor are asked about.
#
# Phase 1–3 types now have cloud executors. Account-wide jobs are listed in
# CLOUD_SENT_ACCOUNT_JOB_TYPES so an empty campaign_id still stands down.
CLOUD_SENT_JOB_TYPES = frozenset({
    constants.JOB_INVITE,
    constants.JOB_FOLLOWUP,
    constants.JOB_SEND_DM,
    constants.JOB_ENGAGE,
    constants.JOB_FOLLOW,
    constants.JOB_ENDORSE,
    constants.JOB_EMAIL_INVITE,
    constants.JOB_INMAIL,
    constants.JOB_AUTO_REPLY,
    constants.JOB_PROFILE_VIEW,
    constants.JOB_CAMPAIGN_REFILL,
})

CLOUD_SENT_ACCOUNT_JOB_TYPES = frozenset({
    constants.JOB_BRAND_POST,
    constants.JOB_BRAND_ENGAGE,
    constants.JOB_BRAND_PROFILE,
    constants.JOB_BRAND_ANALYZE,
    constants.JOB_CHECK_REPLIES,
    constants.JOB_PROCESS_INBOUND,
    constants.JOB_CHECK_POST_COMMENTS,
    constants.JOB_WITHDRAW_INVITE,
    constants.JOB_PARTNER_REMINDER,
    constants.JOB_RECOVER_STUCK_OUTREACHES,
    constants.JOB_VERIFY_ACTIONS,
    constants.JOB_AUTO_REPLY,
    constants.JOB_CAMPAIGN_REFILL,
    constants.JOB_DAILY_STRATEGY,
    constants.JOB_EXECUTE_STRATEGY_PLANS,
    constants.JOB_STRATEGY_CYCLE,
    constants.JOB_ACCEPT_INBOUND,
    constants.JOB_QUALIFY_INBOUND,
    constants.JOB_COLLECT_KEYWORD_SIGNALS,
    constants.JOB_SCAN_PROSPECT_POSTS,
    constants.JOB_COLLECT_PROFILE_VIEWS,
    constants.JOB_DETECT_JOB_CHANGES,
    constants.JOB_COLLECT_HIRING_SIGNALS,
    constants.JOB_COLLECT_NEWS_SIGNALS,
    constants.JOB_COLLECT_COMPETITOR_SIGNALS,
    constants.JOB_CLASSIFY_SIGNALS,
    constants.JOB_ACTIVATE_SIGNALS,
    constants.JOB_DETECT_COMPOUND_INTENT,
    constants.JOB_SIGNAL_DECAY_CYCLE,
    constants.JOB_REMATCH_SIGNALS,
    constants.JOB_BACKFILL_ORPHAN_SIGNALS,
    constants.JOB_COLLECT_WATCHLIST_WEB_SIGNALS,
    constants.JOB_COLLECT_COMPANY_PAGE,
    constants.JOB_COLLECT_COMPANY_FOLLOWERS,
    constants.JOB_COLLECT_POSTS_DISTRIBUTED,
    constants.JOB_RESEARCH_CONTACTS,
    constants.JOB_BACKFILL_POST_ANALYSIS,
    constants.JOB_DETECT_VIRAL_POSTS,
    constants.JOB_SCAN_NETWORK_POSTS,
    constants.JOB_CLASSIFY_POST_INTENT,
    constants.JOB_MINE_COMMENTS,
    constants.JOB_TUNE_WATCHLISTS,
    constants.JOB_OPTIMIZE_SIGNALS,
    constants.JOB_BACKFILL_PROFILES,
    constants.JOB_DAILY_DIGEST,
    constants.JOB_REDETECT_SALES_NAV,
    constants.JOB_SYNC_CONNECTIONS,
})


def get_cloud_scheduler_state() -> dict[str, Any]:
    """What the backend last said about its scheduler, and when it said it.

    Returns ``enabled``, ``ts``, and — when a newer host sent them —
    ``sending_allowed``, ``scheduler_mode``, and per-campaign heartbeat rows.
    A malformed row degrades to "nothing known", which reads as not-enabled
    and hands sending back to the local scheduler — the safe direction.
    """
    stored = queries.get_setting(_CLOUD_STATE_SETTING, {})
    if not isinstance(stored, dict):
        return {"enabled": False, "ts": 0, "campaigns": {}}
    ts = stored.get("ts")
    campaigns = stored.get("campaigns")
    return {
        "enabled": bool(stored.get("enabled", False)),
        "ts": int(ts) if isinstance(ts, (int, float)) else 0,
        "sending_allowed": stored.get("sending_allowed"),
        "scheduler_mode": stored.get("scheduler_mode") or "",
        "campaigns": campaigns if isinstance(campaigns, dict) else {},
    }


def record_cloud_scheduler_state(
    enabled: bool, heartbeat: dict[str, Any] | None = None,
) -> None:
    """Stamp the backend's answer about its scheduler.

    Called on every status read and every toggle. Not called when the backend
    could not be reached: an unanswered question must age out through the TTL
    rather than be recorded as a "no". A toggle that only knows ``enabled``
    keeps the last heartbeat so a status-less refresh does not wipe it.
    """
    stored = queries.get_setting(_CLOUD_STATE_SETTING, {})
    if not isinstance(stored, dict):
        stored = {}
    row: dict[str, Any] = {
        "enabled": bool(enabled),
        "ts": int(time.time()),
        "sending_allowed": stored.get("sending_allowed"),
        "scheduler_mode": stored.get("scheduler_mode") or "",
        "campaigns": stored.get("campaigns") if isinstance(stored.get("campaigns"), dict) else {},
    }
    if heartbeat is not None:
        if "sending_allowed" in heartbeat:
            row["sending_allowed"] = heartbeat.get("sending_allowed")
        if "scheduler_mode" in heartbeat:
            row["scheduler_mode"] = heartbeat.get("scheduler_mode") or ""
        camps: dict[str, Any] = {}
        for raw in heartbeat.get("campaigns") or []:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            entry: dict[str, Any] = {}
            if "would_send" in raw:
                entry["would_send"] = bool(raw["would_send"])
            if "last_outbound_at" in raw:
                try:
                    entry["last_outbound_at"] = int(raw["last_outbound_at"] or 0)
                except (TypeError, ValueError):
                    entry["last_outbound_at"] = 0
            if "will_send" in raw:
                will = raw["will_send"]
                entry["will_send"] = (
                    [str(x) for x in will] if isinstance(will, list) else []
                )
            if isinstance(raw.get("waiting"), dict):
                entry["waiting"] = raw["waiting"]
            camps[str(raw["id"])] = entry
        row["campaigns"] = camps
    queries.save_setting(_CLOUD_STATE_SETTING, row)


def _host_campaign(campaign_id: str) -> dict[str, Any] | None:
    """Per-campaign heartbeat from the last successful status read, or None."""
    if not campaign_id:
        return None
    row = get_cloud_scheduler_state()["campaigns"].get(campaign_id)
    return row if isinstance(row, dict) else None


def _campaign_min_fit(campaign_id: str) -> float:
    campaign = queries.get_campaign(campaign_id)
    try:
        cfg = json.loads((campaign or {}).get("config_json") or "{}")
        return float(cfg.get("min_fit_score", constants.MIN_FIT_SCORE_THRESHOLD))
    except (TypeError, ValueError, json.JSONDecodeError):
        return constants.MIN_FIT_SCORE_THRESHOLD


def _campaign_has_sendable_pending(campaign_id: str) -> bool:
    """A never-contacted person this campaign would still invite."""
    from ..db.schema import get_db

    min_fit = _campaign_min_fit(campaign_id)
    db = get_db()
    row = db.execute(
        f"""SELECT 1 FROM outreaches o
           JOIN contacts c ON c.id = o.contact_id
           WHERE o.campaign_id = ? AND o.status = 'pending'
             AND {queries.FIT_SENDABLE_SQL}
           LIMIT 1""",
        (campaign_id, min_fit),
    ).fetchone()
    db.close()
    return row is not None


def _campaign_has_unmessaged_connected(campaign_id: str) -> bool:
    """Accepted the invite, never got the opening DM — the host's first-DM hole."""
    from ..db.schema import get_db

    db = get_db()
    row = db.execute(
        f"""SELECT 1 FROM outreaches o
           JOIN contacts c ON c.id = o.contact_id
           WHERE o.campaign_id = ? AND o.status = 'connected'
             AND COALESCE(o.followup_count, 0) = 0
             AND NOT {queries.SDR_REAL_DM_SQL}
           LIMIT 1""",
        (campaign_id,),
    ).fetchone()
    db.close()
    return row is not None


def _campaign_has_due_followup(campaign_id: str) -> bool:
    """A connected or messaged person whose next scheduled touch is due."""
    from ..config import get_tier
    from ..constants import FREE_MAX_FOLLOWUPS, PRO_MAX_FOLLOWUPS
    from ..scheduler.planner import _is_followup_due

    campaign = queries.get_campaign(campaign_id)
    try:
        cfg = json.loads((campaign or {}).get("config_json") or "{}")
        custom = cfg.get("followup_delay_days")
        if custom and not isinstance(custom, list):
            custom = None
    except (TypeError, ValueError, json.JSONDecodeError):
        custom = None

    tier = get_tier()
    max_fu = PRO_MAX_FOLLOWUPS if tier == "pro" else FREE_MAX_FOLLOWUPS
    for candidate in queries.get_followup_candidates(campaign_id, max_fu):
        if _is_followup_due(candidate, tier, custom_schedule=custom):
            return True
    return False


def any_active_campaign_needs_refill() -> bool:
    """True when an active campaign has nobody left to invite.

    Refill is then send-path work, not a collector that can lose the tick.
    """
    from ..constants import STATUS_ACTIVE

    for campaign in queries.list_campaigns(status=STATUS_ACTIVE):
        if not _campaign_has_sendable_pending(campaign["id"]):
            return True
    return False


def _campaign_has_sendable_work(campaign_id: str) -> bool:
    """Someone this campaign would still try to contact.

    Pending invites are not the only waiting work. A quiet host that has
    already invited everyone still owes opening DMs and due follow-ups;
    standing down for those is how the drip goes silent.
    """
    return (
        _campaign_has_sendable_pending(campaign_id)
        or _campaign_has_unmessaged_connected(campaign_id)
        or _campaign_has_due_followup(campaign_id)
    )


def _last_campaign_outbound_ts(campaign_id: str) -> int:
    """Latest evidence that the *host* sent an invite, InMail, DM, or follow-up.

    Failures do not count: a 422 InMail is not the host sending.
    Local failover sends do not count either — treating them as production
    reset the quiet clock and stood this machine down for another 3 hours.
    Only actions_log rows the pull wrote with source=cloud refresh the clock
    (log_cloud_executed_send, including invited_at reconcile).
    """
    from ..db.schema import get_db

    db = get_db()
    row = db.execute(
        """SELECT MAX(al.timestamp) AS ts FROM actions_log al
             LEFT JOIN outreaches o ON o.id = al.outreach_id
            WHERE (al.campaign_id = ? OR o.campaign_id = ?)
              AND al.result = 'success'
              AND al.action_type IN (
                  'invitation_sent', 'inmail_sent', 'followup_sent',
                  'dm_sent', 'email_sent'
              )
              AND json_extract(al.details_json, '$.source') = 'cloud'""",
        (campaign_id, campaign_id),
    ).fetchone()
    db.close()
    ts = row["ts"] if row else None
    return int(ts) if ts else 0


def _handover_started_ts(campaign_id: str) -> int:
    """When this campaign was first handed to the host, not the last restamp."""
    log = get_push_log()
    raw = (log.get("handover") or {}).get(campaign_id)
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
    return int(log["campaigns"].get(campaign_id) or 0)


def _cloud_still_producing_for_campaign(campaign_id: str) -> bool:
    """False when someone is waiting and the host has gone quiet."""
    if not campaign_id:
        return False
    if not _campaign_has_sendable_work(campaign_id):
        return True
    host = _host_campaign(campaign_id)
    if host is not None and "last_outbound_at" in host:
        last = int(host.get("last_outbound_at") or 0)
    else:
        last = _last_campaign_outbound_ts(campaign_id)
    if last > 0:
        return int(time.time()) - last <= CLOUD_OUTBOUND_QUIET_SECONDS
    started = _handover_started_ts(campaign_id)
    if started == 0:
        return True
    return int(time.time()) - started <= CLOUD_ZERO_SEND_SECONDS


def outbound_ownership_predicates(campaign_id: str) -> dict[str, bool]:
    """Why cloud_owns_outbound is true or false — every predicate, no short-circuit.

    Used for ownership_decision logs. A false owner that only returns False
    cannot be debugged; this map names the failed check.
    """
    backend_mode = config.is_backend_mode()
    sending_host = config.get_sending_host() if backend_mode else "local"
    campaign = queries.get_campaign(campaign_id) if campaign_id else None
    return {
        "has_campaign_id": bool(campaign_id),
        "backend_mode": backend_mode,
        "sending_host_cloud": sending_host == "cloud",
        "campaign_exists": bool(campaign),
        "autopilot_active": bool(
            campaign
            and campaign.get("mode") == "autopilot"
            and campaign.get("status") == constants.STATUS_ACTIVE
        ),
        "in_push_log": bool(campaign_id and campaign_id in get_push_log()["campaigns"]),
    }


def log_ownership_decision(
    *,
    campaign_id: str,
    job_type: str,
    owner: str,
    source: str,
    outreach_id: str = "",
) -> None:
    from ..ops_log import log_event

    log_event(
        "ownership_decision",
        campaign_id=campaign_id,
        job_type=job_type,
        outreach_id=outreach_id or None,
        owner=owner,
        source=source,
        predicates=outbound_ownership_predicates(campaign_id),
    )


def cloud_owns_outbound(campaign_id: str) -> bool:
    """True when the backend, not this machine, is sending for this campaign.

    Ownership is a user choice (``sending_host``), not a health check. Quiet
    host, stale pull, or a first-touch the laptop used to cover do not move
    sending here. Local sending starts only via ``send_from host=local``.

    Everything below has to hold:

      - hosted mode, because a direct-mode install has no backend at all;
      - ``sending_host`` is cloud (the hosted default, including unset);
      - an autopilot campaign that is active, because that is the only kind the
        backend schedules;
      - a successful push of *this* campaign, because the cloud cannot send
        from a campaign it has no copy of.

    Called once per job creation and once more per execution.
    """
    if not campaign_id or not config.is_backend_mode():
        return False
    if config.get_sending_host() != "cloud":
        return False

    # Exactly what the backend schedules, and nothing else. A copilot campaign
    # is history to it — sync_to_cloud says so — but backfill_cloud stamps every
    # campaign in the push log regardless of mode, so without this check one
    # backfill would stand the local scheduler down for campaigns the cloud was
    # never going to work, and nothing anywhere would send them.
    campaign = queries.get_campaign(campaign_id)
    if not campaign:
        return False
    if campaign.get("mode") != "autopilot" or campaign.get("status") != constants.STATUS_ACTIVE:
        return False

    if campaign_id not in get_push_log()["campaigns"]:
        return False

    # A fresh heartbeat that lists campaigns and omits this id means the host
    # is not planning it. Treating the push log as ownership stood local down
    # for a customer (10 Sep 2026) while will_send never applied.
    state = get_cloud_scheduler_state()
    ts = int(state.get("ts") or 0)
    camps = state.get("campaigns") if isinstance(state.get("campaigns"), dict) else {}
    if (
        ts
        and (int(time.time()) - ts) <= CLOUD_STATE_TTL_SECONDS
        and camps
        and campaign_id not in camps
    ):
        logger.warning(
            "Host heartbeat omits campaign %s — not treating as cloud-owned",
            campaign_id[:8],
        )
        return False
    return True


def cloud_sends_this_job(
    job_type: str,
    campaign_id: str,
    outreach_id: str = "",
) -> bool:
    """True when the backend will actually run this job, so local must stand down.

    Account-wide types (replies, inbound, brand, refill) stand down whenever
    the hosted account sends from the cloud. Campaign types still require
    cloud_owns_outbound. When the host names ``will_send``, that list wins.
    """
    if job_type in CLOUD_SENT_ACCOUNT_JOB_TYPES and cloud_owns_account_sending():
        if not campaign_id:
            return True
        if cloud_owns_outbound(campaign_id):
            host = _host_campaign(campaign_id)
            if host is not None and "will_send" in host:
                return job_type in host["will_send"]
            return True
        return True
    if not cloud_owns_outbound(campaign_id or ""):
        return False
    host = _host_campaign(campaign_id or "")
    if host is not None and "will_send" in host:
        return job_type in host["will_send"]
    return job_type in CLOUD_SENT_JOB_TYPES


def cancel_cloud_owned_pending_jobs() -> int:
    """Cancel leftover local jobs the cloud now owns. sending_host=cloud only."""
    if not config.is_backend_mode() or config.get_sending_host() != "cloud":
        return 0
    from ..db.queries import cancel_pending_jobs_of_type

    types = CLOUD_SENT_JOB_TYPES | CLOUD_SENT_ACCOUNT_JOB_TYPES
    cancelled = 0
    for job_type in types:
        cancelled += cancel_pending_jobs_of_type(job_type)
    if cancelled:
        logger.info("Cancelled %d local jobs now owned by the cloud", cancelled)
    return cancelled


def local_scheduler_engine_enabled() -> bool:
    """False when hosted cloud owns every job — MCP/daemon must not start the engine."""
    if not config.is_backend_mode():
        return True
    return config.get_sending_host() != "cloud"


def stand_down_engine_off_leftovers() -> int:
    """Cancel cloud-owned leftovers when the local engine will not start."""
    if local_scheduler_engine_enabled():
        return 0
    return cancel_cloud_owned_pending_jobs()


def install_source() -> dict[str, Any]:
    """Where this heylead package is loading from — checkout vs PyPI site-packages."""
    import heylead
    path = getattr(heylead, "__file__", "") or ""
    from_checkout = "/Developer/heylead/" in path.replace("\\", "/")
    try:
        from heylead import constants as _c
        has_standdown = bool(getattr(_c, "CLOUD_OWNS_ALL_JOBS", False))
    except Exception:
        has_standdown = False
    return {
        "path": path,
        "from_checkout": from_checkout,
        "has_standdown": has_standdown,
        "pypi_rollback_risk": bool(
            config.is_backend_mode()
            and config.get_sending_host() == "cloud"
            and not has_standdown
        ),
    }


def warn_if_pypi_refresh_rolled_back() -> None:
    """Loud log when hosted cloud is running a package that still starts the engine."""
    info = install_source()
    if not info["pypi_rollback_risk"]:
        return
    logger.error(
        "Hosted sending_host=cloud but this install has no CLOUD_OWNS_ALL_JOBS — "
        "uvx --refresh likely rolled back to PyPI. Reinstall from this checkout: "
        "uv tool install --force --reinstall %s",
        info["path"] or ".",
    )


def cloud_owns_account_sending() -> bool:
    """True when the backend, not this machine, publishes under the user's name.

    The account-level twin of cloud_owns_outbound, for work with no campaign to
    look up. ``sending_host`` is the switch; a landed push is still required
    because the backend plans brand work from the strategy this client sends
    it and cannot post from a plan it has never received.
    """
    if not config.is_backend_mode():
        return False
    if config.get_sending_host() != "cloud":
        return False

    log = get_push_log()
    return bool(log["ts"] > 0 or log["campaigns"])


def _cloud_is_sending_for_this_account() -> bool:
    """The account-level half of both ownership questions.

    A recent "enabled" from the backend, and results still arriving from it.
    See cloud_owns_outbound for why each failure hands sending back.
    """
    state = get_cloud_scheduler_state()
    if not state["enabled"]:
        return False
    if state.get("sending_allowed") is False:
        return False

    now = int(time.time())
    if now - state["ts"] > CLOUD_STATE_TTL_SECONDS:
        return False

    pull = get_pull_log()
    if pull["error"]:
        return False
    if pull["ts"] and now - pull["ts"] > PULL_STALE_AFTER_SECONDS:
        return False

    return True


def _ago(seconds: int) -> str:
    """Compact age: '<1m', '4m', '1h 12m', '3d 4h'.

    formatter.format_duration() floors everything under an hour to '< 1h',
    which is the wrong resolution here: the difference between a push 4 minutes
    ago and one 50 minutes ago is exactly what the reader is checking.

    Sub-minute reads '<1m', not 'just now': every caller appends " ago", and
    "last push just now ago" is not a sentence.
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "<1m"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


async def dashboard_freshness_lines() -> list[str]:
    """Lines reporting how current the hosted dashboard is.

    Empty in direct mode, where there is no hosted dashboard to be stale.
    Otherwise one quiet summary line, plus a warning block naming the campaigns
    heylead.dev is showing stale figures for — withheld by observe, or simply
    not landed. Quiet when it is telling the truth; loud only when it is not.
    """
    if not config.is_backend_mode():
        return []

    log = await run_db(get_push_log)
    stamps = log["campaigns"]
    campaigns = await run_db(queries.list_campaigns)
    now = int(time.time())
    observing = config.get_scheduler_mode() == "observe"

    # Exactly what the periodic push carries (see sync_to_cloud). A draft or a
    # completed campaign is not stale — it was never the push's to send.
    tracked = [
        c for c in campaigns
        if c.get("mode") == "autopilot" and c.get("status") in ("active", "paused")
    ]

    withheld, stale = [], []
    for camp in tracked:
        pushed_at = stamps.get(camp["id"])
        if observing and camp.get("status") == "active":
            withheld.append((camp, pushed_at))
        elif pushed_at is None:
            # Never pushed. Only stale once a push was actually due — a
            # campaign created two minutes ago is waiting, not frozen.
            created = camp.get("created_at") or 0
            if now - created > PUSH_STALE_AFTER_SECONDS:
                stale.append((camp, None))
        elif now - pushed_at > PUSH_STALE_AFTER_SECONDS:
            stale.append((camp, pushed_at))

    newest = max(stamps.values(), default=0)
    summary = (
        f"☁️ Dashboard sync: last push {_ago(now - newest)} ago"
        if newest else
        "☁️ Dashboard sync: nothing pushed yet"
    )
    lines = [summary]

    failure = log["error"]
    if failure and failure.get("ts"):
        lines.append(
            f"⚠️ Last push failed {_ago(now - int(failure['ts']))} ago "
            f"({failure.get('message', 'unknown error')}) — heylead.dev keeps "
            "showing its older figures until one succeeds."
        )

    def _named(entries: list[tuple[dict, Any]]) -> list[str]:
        out = []
        for i, (camp, pushed_at) in enumerate(entries):
            prefix = "└──" if i == len(entries) - 1 else "├──"
            when = f"last pushed {_ago(now - pushed_at)} ago" if pushed_at else "never pushed"
            out.append(f"   {prefix} {camp.get('name') or camp['id'][:8]} — {when}")
        return out

    if withheld:
        lines.append(
            f"⚠️ {len(withheld)} campaign(s) withheld while the scheduler is in "
            "observe mode — heylead.dev still shows their old figures as "
            "current. Local numbers are correct; leave observe with "
            "`scheduler(action='toggle', enabled=True)` to catch it up."
        )
        lines.extend(_named(withheld))
    if stale:
        lines.append(
            f"⚠️ {len(stale)} campaign(s) have not reached heylead.dev recently, "
            "so it is showing figures older than the ones here."
        )
        lines.extend(_named(stale))

    lines.extend(await run_db(_pull_freshness_lines))
    lines.extend(await run_db(_host_quiet_takeover_lines))

    return lines


def _host_quiet_takeover_lines() -> list[str]:
    """Name campaigns this machine took back because the host went silent."""
    state = get_cloud_scheduler_state()
    if not state["enabled"] or state.get("sending_allowed") is False:
        return []
    quiet = []
    for camp in queries.list_campaigns(status=constants.STATUS_ACTIVE):
        if camp.get("mode") != "autopilot":
            continue
        cid = camp["id"]
        if cid not in get_push_log()["campaigns"]:
            continue
        if not _campaign_has_sendable_work(cid):
            continue
        if cloud_owns_outbound(cid):
            continue
        quiet.append(camp.get("name") or cid[:8])
    if not quiet:
        return []
    return [
        "⚠️ Host went quiet on "
        + ", ".join(quiet)
        + " — this machine is not sending. "
        "Use `scheduler(action='send_from', host='local')` to move sending here."
    ]


def _pull_freshness_lines() -> list[str]:
    """Whether what the cloud did is still reaching this machine.

    Only says anything while the cloud scheduler is believed to be on. With it
    off there is nothing sending in the cloud, so nothing to hear back about,
    and a stale pull is simply a loop with no work to do.
    """
    if not get_cloud_scheduler_state()["enabled"]:
        return []

    log = get_pull_log()
    now = int(time.time())
    failure = log["error"]
    lines: list[str] = []

    if failure and failure.get("ts"):
        message = failure.get("message", "unknown error")
        lines.append(
            f"⚠️ Cloud results are not coming back: last pull failed "
            f"{_ago(now - int(failure['ts']))} ago ({message}) — the cloud "
            "scheduler is still sending, but its replies and status changes "
            "are not reaching this machine."
        )
        if "401" in message or "403" in message or "expired" in message.lower():
            lines.append(
                "   └── Your session expired. Sign in again at "
                "https://heylead.dev/auth/login-url and re-run setup_profile "
                "with the new token."
            )
    elif log["ts"] and now - log["ts"] > PULL_STALE_AFTER_SECONDS:
        # No error and no results either: the loop is not running. That is what
        # a dead daemon, a closed laptop, or a cancelled task all look like.
        lines.append(
            f"⚠️ Nothing has come back from the cloud scheduler in "
            f"{_ago(now - log['ts'])} — it is sending, but this machine has "
            "not collected the results. Local figures are behind heylead.dev."
        )

    # Said whether or not the pull is otherwise healthy, because that is the
    # trap: the pull succeeds, reports success, and still drops every row whose
    # campaign this DB has never held. Campaigns created in the cloud never
    # come down (/scheduler/changes carries no campaigns), so their rows are
    # re-fetched and re-discarded every pull, forever, and nothing said so.
    dropped = log["skipped"]
    if dropped and dropped.get("rows"):
        lines.append(
            f"⚠️ Local campaign list is incomplete: the last pull dropped "
            f"{dropped['rows']:,} row(s) belonging to {dropped['campaigns']} "
            "campaign(s) this machine has no record of. They are sending and "
            "reporting normally in the cloud — only this DB cannot see them. "
            "Ask the backend (heylead.dev), not local tools, for a full list."
        )

    return lines


# ── Push: Sync local state to backend ──


def _load_directory_contrib_rows(since: int) -> list[dict[str, Any]]:
    """Local people changed since the last contribute. Company pages are skipped."""
    db = queries.get_db()
    # since=0 means updated_at > 0 so pull inserts stamped 0 are not echoed.
    rows = db.execute(
        "SELECT * FROM global_contacts WHERE updated_at > ?",
        (since,),
    ).fetchall()
    db.close()
    people: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        lid = str(row.get("linkedin_id") or "")
        if lid.isdigit():
            continue
        people.append(row)
    return people


def _directory_provider_id(card: dict[str, Any]) -> str:
    """Same identity as upsert_global_contact: provider_id, else ACoAA linkedin_id."""
    provider_id = (card.get("provider_id") or "").strip()
    if provider_id:
        return provider_id
    raw = card.get("profile_json") or ""
    if raw:
        try:
            blob = json.loads(raw) if isinstance(raw, str) else raw
            provider_id = (blob.get("provider_id") or "").strip()
        except (json.JSONDecodeError, TypeError, AttributeError):
            provider_id = ""
    if provider_id:
        return provider_id
    linkedin_id = (card.get("linkedin_id") or "").strip()
    if linkedin_id.startswith("ACoAA"):
        return linkedin_id
    return ""


def _parse_profile_blob(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _union_directory_profile_json(existing_raw: str, incoming_raw: str) -> str:
    """Copy public keys from the shared card; keep local email/phone/etc."""
    from .directory_card import _PRIVATE_PROFILE

    existing = _parse_profile_blob(existing_raw)
    incoming = _parse_profile_blob(incoming_raw)
    for key, val in incoming.items():
        if key in _PRIVATE_PROFILE:
            continue
        if val not in (None, ""):
            existing[key] = val
    if not existing:
        return existing_raw or incoming_raw or ""
    return json.dumps(existing, separators=(",", ":"))


def _find_directory_contact(card: dict[str, Any]) -> dict[str, Any] | None:
    linkedin_id = (card.get("linkedin_id") or "").strip()
    provider_id = _directory_provider_id(card)
    db = queries.get_db()
    row = None
    if linkedin_id:
        row = db.execute(
            "SELECT * FROM global_contacts WHERE linkedin_id = ? LIMIT 1",
            (linkedin_id,),
        ).fetchone()
    if not row and provider_id:
        row = db.execute(
            """SELECT * FROM global_contacts
               WHERE linkedin_id = ?
                  OR (profile_json IS NOT NULL AND profile_json != ''
                      AND json_valid(profile_json)
                      AND json_extract(profile_json, '$.provider_id') = ?)
               LIMIT 1""",
            (provider_id, provider_id),
        ).fetchone()
    db.close()
    return dict(row) if row else None


def _refresh_directory_public_fields(card: dict[str, Any]) -> None:
    """Update public columns only. Do not restamp updated_at or replace profile_json."""
    row = _find_directory_contact(card)
    if not row:
        return
    updates: dict[str, Any] = {}
    for key in ("name", "title", "company", "linkedin_url", "location"):
        val = (card.get(key) or "").strip()
        if val:
            updates[key] = val
    merged = _union_directory_profile_json(
        row.get("profile_json") or "", card.get("profile_json") or "",
    )
    if merged and merged != (row.get("profile_json") or ""):
        updates["profile_json"] = merged
    if not updates:
        return
    db = queries.get_db()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    db.execute(
        f"UPDATE global_contacts SET {set_clause} WHERE id = ?",
        list(updates.values()) + [row["id"]],
    )
    db.commit()
    db.close()


def _apply_directory_card(card: dict[str, Any]) -> None:
    """Cache one shared card locally without marking it as a local edit."""
    if _find_directory_contact(card):
        _refresh_directory_public_fields(card)
        return

    from ..db.global_contact_queries import upsert_global_contact

    gid = upsert_global_contact(
        linkedin_id=card.get("linkedin_id") or "",
        name=card.get("name") or "",
        title=card.get("title") or "",
        company=card.get("company") or "",
        linkedin_url=card.get("linkedin_url") or "",
        location=card.get("location") or "",
        profile_json="",
        source="shared_directory",
    )
    incoming = card.get("profile_json") or ""
    if incoming:
        db = queries.get_db()
        row = db.execute(
            "SELECT profile_json FROM global_contacts WHERE id = ?", (gid,),
        ).fetchone()
        merged = _union_directory_profile_json(
            (row["profile_json"] if row else "") or "", incoming,
        )
        # 0 so contribute's updated_at > since watermark does not echo the pool.
        db.execute(
            "UPDATE global_contacts SET profile_json = ?, updated_at = 0 WHERE id = ?",
            (merged, gid),
        )
        db.commit()
        db.close()
    else:
        db = queries.get_db()
        db.execute("UPDATE global_contacts SET updated_at = 0 WHERE id = ?", (gid,))
        db.commit()
        db.close()


async def sync_directory_to_cloud() -> None:
    """Contribute public professional cards. Hosted only; 404 is a skip."""
    if not config.is_backend_mode():
        return

    from ..linkedin import get_linkedin_client
    from .directory_card import public_directory_card

    since = int(await run_db(queries.get_setting, "directory_contrib_since", 0) or 0)
    rows = await run_db(_load_directory_contrib_rows, since)
    sent_rows: list[dict[str, Any]] = []
    cards: list[dict[str, Any]] = []
    for row in rows:
        card = public_directory_card(row)
        if card:
            sent_rows.append(row)
            cards.append(card)
    if not cards:
        return

    try:
        client = get_linkedin_client()
        unsupported = False
        for i in range(0, len(cards), _DIRECTORY_CONTRIBUTE_CHUNK):
            chunk = cards[i:i + _DIRECTORY_CONTRIBUTE_CHUNK]
            result = await client.contribute_directory(chunk)
            if result.get("unsupported"):
                logger.debug("Directory contribute unsupported (API not deployed)")
                unsupported = True
                break
        if not unsupported:
            watermark = max(int(row.get("updated_at") or 0) for row in sent_rows)
            await run_db(queries.save_setting, "directory_contrib_since", watermark)
    except Exception as e:
        logger.debug("Directory contribute failed: %s", e)


async def sync_directory_from_cloud() -> None:
    """Pull the shared directory into local global_contacts. Hosted only."""
    if not config.is_backend_mode():
        return

    from ..linkedin import get_linkedin_client

    since = int(await run_db(queries.get_setting, "directory_pull_since", 0) or 0)
    after_id = str(await run_db(queries.get_setting, "directory_pull_after_id", "") or "")

    try:
        client = get_linkedin_client()
        while True:
            page = await client.pull_directory(
                since=since, after_id=after_id, limit=_DIRECTORY_PULL_LIMIT,
            )
            if page.get("unsupported"):
                logger.debug("Directory pull unsupported (API not deployed)")
                return
            cards = page.get("cards") or []
            for card in cards:
                await run_db(_apply_directory_card, card)
            since = int(page.get("next_since") or since)
            after_id = str(page.get("next_after_id") or "")
            await run_db(queries.save_setting, "directory_pull_since", since)
            await run_db(queries.save_setting, "directory_pull_after_id", after_id)
            if len(cards) < _DIRECTORY_PULL_LIMIT:
                return
    except Exception as e:
        logger.debug("Directory pull failed: %s", e)


async def _post_sync_chunk(
    client: httpx.AsyncClient,
    url: str,
    chunk: dict[str, Any],
    headers: dict[str, str],
    n: int,
    total: int,
) -> tuple[dict[str, Any] | None, str]:
    """POST one sync chunk. Retry 5xx a few times; warn at most once an hour."""
    last_status = 0
    last_err = ""
    for attempt in range(1, _SYNC_CHUNK_RETRIES + 1):
        try:
            resp = await client.post(url, json=chunk, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {}, ""
        except httpx.HTTPStatusError as e:
            last_status = e.response.status_code
            last_err = (e.response.text or "")[:200]
            if e.response.status_code < 500 or attempt >= _SYNC_CHUNK_RETRIES:
                break
            await asyncio.sleep(0.25 * attempt)
        except httpx.HTTPError as e:
            last_status = 0
            last_err = repr(e)
            if attempt >= _SYNC_CHUNK_RETRIES:
                break
            await asyncio.sleep(0.25 * attempt)
    if last_status:
        failure = f"Sync failed: HTTP {last_status} (chunk {n}/{total})"
    else:
        failure = f"Sync failed: {last_err} (chunk {n}/{total})"
    _warn_sync_chunk_hourly(n, total, last_status, last_err)
    return None, failure


def _warn_sync_chunk_hourly(n: int, total: int, status: int, detail: str) -> None:
    global _sync_chunk_warned_at
    now = time.monotonic()
    if now - _sync_chunk_warned_at < _SYNC_CHUNK_WARN_INTERVAL:
        logger.debug(
            "Cloud sync HTTP error (chunk %d/%d): %s %s",
            n, total, status or "err", detail,
        )
        return
    _sync_chunk_warned_at = now
    logger.warning(
        "Cloud sync HTTP error (chunk %d/%d): %s %s",
        n, total, status or "err", detail,
    )


async def sync_to_cloud(
    include_all: bool = False,
    campaign_id: str = "",
) -> dict[str, Any]:
    """Push all active autopilot campaign state to the backend for cloud scheduling.

    Gathers campaigns, contacts, outreaches, messages, engagements, and settings
    from the local DB and POSTs them to /api/v1/scheduler/sync.

    Args:
        include_all: When True (one-shot backfill), skip the mode/status filter
            and push EVERY campaign — completed and manual ones included — along
            with their contacts/outreaches/messages/engagements, so the hosted
            dashboard reflects full local history. Default False keeps periodic
            sync semantics (active/paused autopilot only).
        campaign_id: When set, push only that campaign (drafts included) and
            skip the directory contribute. Used so the host has the id before
            settings/resume without a full-account upload.

    Returns the backend's response dict.
    """
    from ..correlation import get_correlation_id, new_correlation_id
    if not get_correlation_id():
        new_correlation_id()

    if not config.is_backend_mode():
        return {"error": "Backend mode not configured"}

    # ── Gather settings ──
    voice_signature = await run_db(queries.get_setting, "voice_signature", {})
    profile = await run_db(queries.get_setting, "profile", {})
    tier = config.get_tier()
    cfg = config.load_config()
    working_hours = cfg.get("working_hours", {})

    # ── Gather all autopilot campaigns (including paused) ──
    # Must sync paused campaigns too so backend reflects emergency_stop state.
    # include_all (backfill) pushes every campaign regardless of mode/status.
    #
    # Statuses first: a campaign paused or archived in the cloud whose local
    # copy still says active would otherwise go out as the go-signal again
    # (10 Sep 2026, be5f78ff…). The pull does this too, but the loop pushes
    # before it pulls and pulls only while cloud sending is on, and an older
    # backend applies whatever status the push carries. Not for the targeted
    # one-campaign push: launch resumes first and must stay one call.
    if not campaign_id:
        await refresh_campaign_statuses_from_cloud()
    all_campaigns = await run_db(queries.list_campaigns)
    if campaign_id:
        # One campaign, including drafts — so the host has the id before
        # settings/resume. Do not apply the active/paused filter.
        sync_targets = [c for c in all_campaigns if c["id"] == campaign_id]
        if not sync_targets:
            return {"error": f"Campaign {campaign_id} not found"}
    elif include_all:
        sync_targets = all_campaigns
    else:
        sync_targets = [
            c for c in all_campaigns
            if c.get("mode") == "autopilot" and c.get("status") in ("active", "paused")
        ]

    # Handing the backend an ACTIVE autopilot campaign IS the instruction to
    # send from it every 5 minutes (see the module docstring). observe mode
    # locally means "never sends, invites, engages, or enrols anyone", but the
    # backend does not read scheduler_mode yet — it only just started receiving
    # it in the settings payload below — so an observe user's push was silently
    # commissioning cloud sends. Withholding the go-signal is the only
    # guarantee the client can make on its own.
    #
    # Applies to include_all too. The backfill was the last unguarded push:
    # `scheduler(action='backfill_cloud')` skipped the mode/status filter
    # wholesale and handed over live autopilot campaigns under a mode that
    # prints "no live campaign is handed to the backend". Dashboard fidelity is
    # not worth commissioning sends the user switched off; run the backfill
    # again after leaving observe and the campaigns land.
    #
    # Only the go-signal. Paused campaigns still sync, because per the comment
    # above they ARE the stop signal: sync_campaign_status is best-effort, and
    # on failure campaign_control tells the user to repair it with
    # show_status(), whose repair path is this very function. Withholding paused
    # campaigns too would leave an observe user's failed pause stuck "active" in
    # the cloud with nothing able to correct it. Active *copilot* campaigns sync
    # as well: the backend only schedules autopilot, so they are history, not a
    # go-signal — and in the periodic branch above there are none to spare.
    #
    # Deliberately not applied to "off" either: a laptop with the local
    # scheduler off is the normal case for relying on cloud scheduling, and
    # blanking sync there would break the feature outright.
    withheld_live = 0
    if config.get_scheduler_mode() == "observe":
        kept = [
            c for c in sync_targets
            if not (c.get("status") == "active" and c.get("mode") == "autopilot")
        ]
        withheld_live = len(sync_targets) - len(kept)
        sync_targets = kept
        if withheld_live:
            logger.info(
                "observe mode: withheld %d active autopilot campaign(s) from the "
                "cloud push — handing one over is the instruction to send from it",
                withheld_live,
            )

    sync_campaigns = []
    sync_contacts = []
    sync_outreaches = []
    # Strictly-before fence for the local_news clear: anything stamped at or
    # after this instant may be a fact recorded mid-push (updated_at has
    # 1-second granularity, so <= a per-row snapshot could not tell a
    # mid-push write in the same second apart from the row itself).
    _push_fence_ts = int(time.time())
    _push_snapshot: list = []  # pushed outreach ids
    sync_messages = []
    sync_engagements = []
    # Messages deleted on LinkedIn (delete_message, or a deletion noticed by
    # the inbox sync) carry deleted_at locally. The backend has accepted
    # `deleted_message_ids` since the phantom-reply repair, but nothing here
    # ever sent them, so a row deleted on this machine lived on in the hosted
    # store and on every dashboard timeline (8 Sep 2026: two follow-ups the
    # user removed from LinkedIn by hand were still "followup sent" hosted).
    deleted_message_ids: list[str] = []

    for camp in sync_targets:
        camp_id = camp["id"]

        # The JSON columns ride only when this DB actually holds them. A blank
        # one is never a fact: a NULL column on a 2026-vintage row, or the ''
        # that _insert_cloud_campaign mints for a cloud-born campaign (the
        # campaign list endpoint carries no ICP body). Sending '' overwrote
        # the backend's real ICP on 8 Sep 2026 and every hosted
        # discover/campaign_refill died with "Campaign has no valid ICP JSON".
        # Omitting the key also keeps the payload string-only (the backend
        # 422s on None, which once killed every backfill).
        row = {
            "id": camp_id,
            "name": camp.get("name") or "",
            "mode": camp.get("mode") or "autopilot",
            "status": camp.get("status") or "active",
        }
        for field in ("icp_json", "config_json", "context_json"):
            value = camp.get(field)
            if isinstance(value, str) and value.strip():
                row[field] = value
        sync_campaigns.append(row)

        # Contacts
        contacts = await run_db(queries.get_contacts_for_campaign, camp_id)
        for contact in contacts:
            sync_contacts.append({
                "id": contact["id"],
                "campaign_id": camp_id,
                "name": contact.get("name") or "",
                "title": contact.get("title") or "",
                "company": contact.get("company") or "",
                "linkedin_id": contact.get("linkedin_id") or "",
                "linkedin_url": contact.get("linkedin_url") or "",
                "profile_json": contact.get("profile_json") or "",
                "analysis_json": contact.get("analysis_json") or "",
                "fit_score": contact.get("fit_score") or 0.0,
                "source": contact.get("source") or "",
                "source_detail": contact.get("source_detail") or "",
            })

        # Outreaches
        def _get_outreaches(cid):
            db = queries.get_db()
            rows = db.execute(
                "SELECT * FROM outreaches WHERE campaign_id = ?", (cid,)
            ).fetchall()
            db.close()
            return rows

        outreach_rows = await run_db(_get_outreaches, camp_id)
        for o in outreach_rows:
            out = dict(o)
            _push_row = {
                "id": out["id"],
                "campaign_id": camp_id,
                "contact_id": out["contact_id"],
                # No funnel state. Sending has been cloud-only since phases
                # 1-2 (`sending_host` is cloud, the tick runs on
                # heylead-tick), so status, the three stamps, outcome_json,
                # next_action, followup_count and last_attempt_error are all
                # things this mirror was TOLD. Saying them back is an echo,
                # and on 14 Sep 2026 a 15-hour-old `closed_unhappy` (base 3
                # against stored 4) overwrote a correct `replied` and closed
                # a live lead for four hours.
                #
                # The row is still sent: its PRESENCE is how an outreach on a
                # locally-created campaign is announced. Its STATE is the
                # cloud's. The backend reads an absent key as "keep"
                # (heylead-api#534) — against an older backend this payload
                # would rewind every row to 'pending'.
            }
            # Versioned Outreach Sync: say which cloud version this state is
            # based on — the backend's only way to tell news from echo.
            # Never-versioned rows stay on the legacy protocol (claiming
            # base 0 against an upgraded backend reads as a permanent echo).
            _cv = int(out.get("cloud_status_version") or 0)
            if _cv > 0:
                _push_row["base_version"] = _cv
            sync_outreaches.append(_push_row)
            _push_snapshot.append(out["id"])

            # Messages for each outreach
            messages = await run_db(queries.get_messages_for_outreach, out["id"])
            for msg in messages:
                if not msg.get("id"):
                    # Legacy inbox-import rows can lack ids; they can't be
                    # upserted stably and one such row 422s the whole payload.
                    logger.debug("Skipping id-less message on outreach %s", out["id"][:8])
                    continue
                if msg.get("deleted_at"):
                    deleted_message_ids.append(msg["id"])
                    continue
                sync_messages.append({
                    "id": msg["id"],
                    "outreach_id": out["id"],
                    "role": msg.get("role") or "",
                    "text": msg.get("text") or "",
                    "sentiment": msg.get("sentiment") or "",
                    "timestamp": msg.get("timestamp") or 0,
                    # 'invite_note' is how the backend tells a note from an
                    # opening DM (_INVITE_NOTE_SQL); without it every synced
                    # note counts as the first message.
                    "format": msg.get("format") or "text",
                })

        # Engagements
        def _get_engagements(cid):
            db2 = queries.get_db()
            rows = db2.execute(
                "SELECT e.* FROM engagements e "
                "JOIN outreaches o ON e.outreach_id = o.id "
                "WHERE o.campaign_id = ?",
                (cid,),
            ).fetchall()
            db2.close()
            return rows

        eng_rows = await run_db(_get_engagements, camp_id)
        for eng in eng_rows:
            e = dict(eng)
            sync_engagements.append({
                "id": e["id"],
                "outreach_id": e["outreach_id"],
                "action_type": e.get("action_type") or "",
                "post_id": e.get("post_id") or "",
                "post_text": e.get("post_text") or "",
                "text": e.get("text") or "",
                "reaction_type": e.get("reaction_type") or "",
                "status": e.get("status") or "sent",
                # When it happened. Without it the backend stamps its own
                # arrival time and a backfill of months reads as one day.
                "created_at": e.get("created_at"),
            })

    # ── POST to backend ──
    from ..tier import as_bool as _as_bool
    has_sales_nav = _as_bool(
        await run_db(queries.get_setting, "has_sales_navigator", False)
    )
    has_premium = await run_db(queries.get_setting, "has_linkedin_premium", None)
    from ..tier import INMAIL_CAPABILITY_KEY
    inmail_capability = await run_db(queries.get_setting, INMAIL_CAPABILITY_KEY, "") or ""

    # Brand strategy data for cloud scheduler
    brand_analysis = await run_db(queries.get_setting, "brand_analysis")
    brand_strategy = await run_db(queries.get_setting, "brand_strategy")
    brand_baseline = await run_db(queries.get_setting, "brand_baseline")
    brand_actions_completed = await run_db(queries.get_setting, "brand_actions_completed")
    headline_ab = await run_db(queries.get_setting, "headline_ab")

    # Outreach rows hard-deleted here (the sendable-queue repair) leave a
    # tombstone. The backend has accepted `deleted_outreach_ids` since Aug 2026
    # and the client never sent it, so a locally deleted prospect stayed in the
    # hosted store and the very next pull re-created it locally.
    #
    # Snapshot once, here, and clear exactly these ids after the push: a
    # tombstone written AFTER this read never rode this push, and clearing it
    # would lose that deletion forever (the same TOCTOU as the local_news fence
    # below).
    #
    # Full syncs only. A targeted one-campaign push is the launch fast path and
    # must stay small. Sending every campaign's tombstones from it would in
    # fact be correct — the backend scopes the delete to the caller's own rows
    # (`c.user_id`, scheduler_store.py:3564-3579), not to the campaigns in the
    # request — but pointless: it would grow the one payload launch waits on to
    # carry deletions launch does not care about. Sending only that campaign's
    # would need a per-campaign read for the same non-gain. A tombstone carries
    # no deadline, so the deletions ride the next 15-minute full push.
    #
    # This is the same guard shape as the directory contribute at the end of
    # the push: both skip work on a targeted push purely to keep launch small.
    # (The directory's extra property is independence — it runs even after a
    # failed chunk; this clear, by contrast, is fenced on full success.)
    deleted_outreach_ids: list[str] = []
    if not campaign_id:
        deleted_outreach_ids = await run_db(queries.list_outreach_tombstone_ids)

    payload = {
        "settings": {
            "voice_signature": voice_signature,
            "profile": profile,
            "tier": tier,
            "working_hours": working_hours,
            "has_sales_navigator": has_sales_nav,
            "has_linkedin_premium": (
                None if has_premium is None else _as_bool(has_premium)
            ),
            "inmail_send_capability": inmail_capability or None,
            "brand_analysis": brand_analysis,
            "brand_strategy": brand_strategy,
            "brand_baseline": brand_baseline,
            "brand_actions_completed": brand_actions_completed,
            "headline_ab": headline_ab,
            "always_on": config.is_scheduler_always_on(),
            # The backend executes outreach every 5 minutes with the laptop
            # closed, and "always_on" says nothing about whether the user has
            # consented to sending at all. Without this field an observe-mode
            # user had no way to express that to the cloud. The backend must
            # gate its send/invite/engage/enrol jobs on it.
            "scheduler_mode": config.get_scheduler_mode(),
        },
        "campaigns": sync_campaigns,
        "contacts": sync_contacts,
        "outreaches": sync_outreaches,
        "messages": sync_messages,
        "deleted_message_ids": deleted_message_ids,
        "deleted_outreach_ids": deleted_outreach_ids,
        "engagements": sync_engagements,
    }
    # NB: this dict is a shape reference and is never POSTed. Only
    # payload["settings"] is read below; the request bodies are the chunks in
    # `payloads`. POSTing `payload` itself would collapse every chunk into one
    # request and destroy the delete-after-upsert ordering the next comment
    # depends on.

    # Every sync splits into ordered chunks (campaigns first, then contacts,
    # outreaches, messages, engagements) so no single request outlives Cloud
    # Run's timeout — a periodic push with real history (~700 rows) timed out
    # at 30s just like the 15k-row backfill 504'd. Small payloads collapse to
    # a single chunk. Dependency order matters: rows referencing other rows
    # always arrive after them. Settings ride every chunk (idempotent) so a
    # partial payload can never clobber stored settings with defaults.
    settings_dict = payload["settings"]
    empty = {"campaigns": [], "contacts": [], "outreaches": [],
             "messages": [], "engagements": [], "deleted_message_ids": [],
             "deleted_outreach_ids": []}
    payloads = [{**empty, "settings": settings_dict, "campaigns": sync_campaigns}]
    # deleted_outreach_ids goes LAST, after every outreaches chunk. Each chunk
    # is a separate request and the backend applies them in the order they
    # arrive, so a delete must never be sent before a chunk that still carries
    # the row — the later upsert would simply re-create it. That can happen:
    # the outreach enumeration above ran before the tombstone snapshot, so a
    # repair racing this push can put the same row in both lists.
    #
    # (Do not read this as "the ingest deletes last within a request".
    # scheduler_store.py applies deleted_outreach_ids at :3564 — after the
    # outreaches upsert, but BEFORE the engagements loop at :3599. No chunk
    # here carries both keys, so that ordering never bites; the guarantee this
    # code relies on is the cross-request one above, not a per-request one.)
    for key, rows in (("contacts", sync_contacts), ("outreaches", sync_outreaches),
                      ("messages", sync_messages), ("engagements", sync_engagements),
                      ("deleted_message_ids", deleted_message_ids),
                      ("deleted_outreach_ids", deleted_outreach_ids)):
        for i in range(0, len(rows), _BACKFILL_CHUNK_ROWS):
            payloads.append(
                {**empty, "settings": settings_dict, key: rows[i:i + _BACKFILL_CHUNK_ROWS]}
            )

    base = _base_url()
    totals: dict[str, Any] = {}
    failure = ""
    async with httpx.AsyncClient(timeout=_BACKFILL_TIMEOUT) as client:
        for n, chunk in enumerate(payloads, start=1):
            result, failure = await _post_sync_chunk(
                client,
                f"{base}/api/v1/scheduler/sync",
                chunk,
                _headers(),
                n,
                len(payloads),
            )
            if failure:
                await run_db(_record_push, [], failure)
                break
            for k, v in (result or {}).items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    totals[k] = totals.get(k, 0) + v
                elif isinstance(v, list):
                    existing = totals.get(k)
                    if isinstance(existing, list):
                        existing.extend(v)
                    else:
                        totals[k] = list(v)
                else:
                    totals.setdefault(k, v)

    # Versioned Outreach Sync: a fully successful push has carried every
    # marked local fact up (as news on a current base, or it was held as a
    # conflict the next pull resolves) — either way the pull may speak again.
    if not failure and sync_outreaches:
        try:
            def _clear_news():
                # Fenced on updated_at: a funnel fact recorded AFTER the
                # payload was built was never in it — clearing its flag
                # would hand the very next pull permission to erase it
                # (TOCTOU, review 5 Sep). It keeps its flag and rides the
                # next push.
                db = queries.get_db()
                for i in range(0, len(_push_snapshot), 500):
                    batch = _push_snapshot[i:i + 500]
                    marks = ",".join("?" * len(batch))
                    db.execute(
                        f"UPDATE outreaches SET local_news = 0 "
                        f"WHERE id IN ({marks}) AND local_news = 1 "
                        f"AND updated_at < ?",
                        [*batch, _push_fence_ts],
                    )
                db.commit()
                db.close()

            await run_db(_clear_news)
        except Exception as e:
            logger.debug("local_news clear after push failed: %s", e)

    # Only a fully successful push proves the hosted store heard the deletions.
    # Clearing after a rejected chunk would strand the rows in the cloud with
    # nothing left to tell it about them. Clear exactly the snapshot, never a
    # fresh read. Non-fatal: a tombstone that survives simply rides the next
    # push, and the backend's delete is idempotent.
    #
    # The tombstone row is DELETED here, not marked synced. That is deliberate,
    # and it is safe because of when the row is needed:
    #
    #   t0      a local hard delete records the tombstone; the hosted row still
    #           exists.
    #   t0..t1  pulls run every 5 minutes and would re-create the row — the
    #           tombstone is present, so the pull refuses it (Task 4).
    #   t1      this push delivers the deletion; the hosted row is gone; the
    #           tombstone is cleared here.
    #   > t1    pulls fetch nothing for that id. There is no hosted row left to
    #           resurrect, so no blocklist entry is needed.
    #
    # The tombstone therefore exists for exactly the window in which it does
    # any work, and no TTL sweep or synced_at column is required.
    #
    # The one hole in the argument above is an in-flight pull: one that fetched
    # hosted rows BEFORE t1 and applies them AFTER this clear. It is closed on
    # the pull side, and that is what makes this DELETE safe:
    #
    #   pull_changes reads the tombstone blocklist BEFORE its fetch and applies
    #   the rows against that snapshot.
    #
    # Moving that read below the fetch — or making it a per-row lookup while
    # applying — re-opens the race: the pull would see the table this clear has
    # just emptied and re-adopt exactly the row whose deletion had landed. This
    # DELETE would then be unsafe and the tombstone would need a synced_at
    # column or a TTL sweep instead. The test that fails if the read moves is
    # test_the_blocklist_is_read_before_the_rows_are_fetched, which clears a
    # tombstone from inside the in-flight GET. The pull side carries the other
    # half of this cross-reference, at the `tombstoned = ...` line in
    # pull_changes.
    if not failure and deleted_outreach_ids:
        try:
            await run_db(queries.clear_outreach_tombstones, deleted_outreach_ids)
        except Exception as e:
            # warning, not debug: a clear that keeps failing (locked DB) is
            # otherwise silent while the full id list is re-sent every 15
            # minutes and the table grows without bound.
            logger.warning(
                "outreach tombstone clear after push failed (%d ids will re-send): %s",
                len(deleted_outreach_ids), e)

    # Directory is independent of campaign sync — contribute even when a chunk failed.
    # A targeted one-campaign push must stay small so launch does not wait on it.
    if not campaign_id:
        try:
            await sync_directory_to_cloud()
        except Exception as e:
            logger.warning("Directory contribute failed (non-fatal): %s", e)

    if failure:
        return {"error": failure}

    # Every chunk landed. Stamp what went up, so a later status read can say how
    # old the dashboard's figures are rather than leaving the user to infer it.
    # A 200 that refused a campaign must not count as handover — that is how
    # A customer's campaign sat in the push log while the host never planned it.
    refused = {
        str(cid) for cid in (totals.get("refused_campaign_ids") or []) if cid
    }
    await run_db(
        _record_push,
        [c["id"] for c in sync_campaigns if c["id"] not in refused],
    )
    result = totals
    # Client-side, set after the merge so the caller can say what was held back
    # instead of quietly reporting a short count.
    result["observe_withheld"] = withheld_live
    logger.info(
        "Cloud sync complete (%d request%s): %d campaigns, %d contacts, %d outreaches",
        len(payloads), "s" if len(payloads) != 1 else "",
        result.get("campaigns", 0), result.get("contacts", 0), result.get("outreaches", 0),
    )

    # ── Push signals to backend (separate endpoint) ──
    try:
        await _sync_signals_to_cloud()
    except Exception as e:
        logger.warning("Signal sync failed (non-fatal): %s", e)

    try:
        await _sync_watchlists_to_cloud()
    except Exception as e:
        logger.warning("Watchlist sync failed (non-fatal): %s", e)

    try:
        await _sync_icps_to_cloud()
    except Exception as e:
        logger.warning("ICP sync failed (non-fatal): %s", e)

    return result


async def _sync_signals_to_cloud() -> None:
    """Push locally-collected signals to the backend via /signals/ingest/batch.

    Uses last_signal_sync_ts setting to only push new signals.
    Backend deduplicates by linkedin_id + signal_type within 24h.

    The watermark may only move over rows the backend acknowledged. A failed
    chunk stops it where it is, and a page that came back full stops it at the
    oldest row the query could not return — otherwise a single re-classified
    old row (its classified_at is the batch maximum) buries every signal that
    did not fit on the page.
    """
    last_sync = await run_db(queries.get_setting, "last_signal_sync_ts", 0)
    if not isinstance(last_sync, (int, float)):
        last_sync = 0

    page_limit = 200
    base = _base_url()
    watermark = int(last_sync)
    total_ingested = 0

    for _page in range(_SIGNAL_SYNC_MAX_PAGES):
        signals = await run_db(
            signal_queries.list_signals_for_sync, watermark, page_limit,
        )
        if not signals:
            break

        truncated = len(signals) == page_limit
        cap: int | None = None
        if truncated:
            cap = int(signals[-1].get("detected_at") or 0) - 1

        rows: list[tuple[dict[str, Any], int]] = []
        for sig in signals:
            # The watermark has to move with whatever brought the signal into
            # this batch, or a status change replays on every sync forever.
            stamp = max(
                int(sig.get(field) or 0)
                for field in ("detected_at", "classified_at", "actioned_at")
            )

            metadata: dict[str, Any] = {}
            if sig.get("metadata_json"):
                try:
                    metadata = json.loads(sig["metadata_json"])
                except (json.JSONDecodeError, TypeError):
                    pass

            rows.append(({
                "signal_type": sig.get("signal_type", "custom"),
                "prospect_name": sig.get("prospect_name") or "",
                "prospect_title": sig.get("prospect_title") or "",
                "linkedin_id": sig.get("linkedin_id") or "",
                "company": metadata.get("company", ""),
                "content": sig.get("content") or "",
                "source": sig.get("source") or "mcp",
                "campaign_id": sig.get("campaign_id") or "",
                "metadata": metadata,
                "intent": sig.get("intent") or "",
                "confidence": sig.get("confidence") or 0.0,
                # Without this the backend can only ever record that a signal
                # exists, never that it was acted on.
                "status": sig.get("status") or "",
            }, stamp))

        advanced = watermark
        all_sent = True

        # Send in batches of 50 (backend max)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for i in range(0, len(rows), 50):
                chunk = rows[i : i + 50]
                try:
                    resp = await client.post(
                        f"{base}/api/v1/signals/ingest/batch",
                        json={"signals": [payload for payload, _ in chunk]},
                        headers=_headers(),
                    )
                    resp.raise_for_status()
                    result = resp.json()
                    total_ingested += result.get("ingested", 0)
                except httpx.HTTPError as e:
                    logger.warning("Signal batch sync failed: %s", e)
                    all_sent = False
                    break
                advanced = max(advanced, max(stamp for _, stamp in chunk))

        if cap is not None:
            advanced = min(advanced, max(cap, watermark))
        if advanced <= watermark:
            break

        watermark = advanced
        await run_db(queries.save_setting, "last_signal_sync_ts", watermark)

        if not truncated or not all_sent:
            break

    if total_ingested > 0:
        logger.info("Synced %d signals to backend", total_ingested)


async def _sync_watchlists_to_cloud() -> None:
    """Copy local SQLite watchlists onto Postgres (one-shot / keep-alive)."""
    watchlists = await run_db(signal_queries.list_watchlists, is_active=None)
    if not watchlists:
        return
    payload = []
    for wl in watchlists:
        keywords = wl.get("keywords_list") or []
        payload.append({
            "id": wl.get("id") or "",
            "name": wl.get("name") or "",
            "kind": wl.get("watch_type") or "keyword",
            "query": keywords[0] if keywords else "",
            "keywords": keywords,
            "campaign_id": wl.get("campaign_id") or "",
            "enabled": bool(wl.get("is_active", 1)),
        })
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{_base_url()}/api/v1/signals/watchlists/ingest",
            headers=_headers(),
            json={"watchlists": payload},
        )
        if resp.status_code >= 400:
            logger.warning("Watchlist ingest failed: %s %s", resp.status_code, resp.text[:200])
            return
    logger.info("Synced %d watchlists to backend", len(payload))


async def _sync_icps_to_cloud() -> None:
    """Copy local ICPs onto Postgres so hosted classify can use them."""
    icps = await run_db(queries.list_icps)
    if not icps:
        return
    payload = []
    for icp in icps:
        raw = icp.get("icp_json") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        payload.append({
            "id": icp.get("id") or "",
            "name": icp.get("name") or "",
            "icp_json": raw,
        })
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{_base_url()}/api/v1/signals/icps/ingest",
            headers=_headers(),
            json={"icps": payload},
        )
        if resp.status_code >= 400:
            logger.warning("ICP ingest failed: %s %s", resp.status_code, resp.text[:200])
            return
    logger.info("Synced %d ICPs to backend", len(payload))
    try:
        chunks = await run_db(queries.list_icp_chunk_texts)
    except Exception:
        chunks = []
    if not chunks:
        return
    chunk_payload = [
        {
            "id": c.get("id") or "",
            "icp_id": c.get("icp_id") or "",
            "source_id": c.get("source_id") or "",
            "chunk_text": c.get("text") or "",
        }
        for c in chunks
        if c.get("text")
    ]
    if not chunk_payload:
        return
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{_base_url()}/api/v1/signals/icp-chunks/ingest",
            headers=_headers(),
            json={"chunks": chunk_payload},
        )
        if resp.status_code >= 400:
            logger.warning("ICP chunk ingest failed: %s %s", resp.status_code, resp.text[:200])
            return
    logger.info("Synced %d ICP chunk texts to backend", len(chunk_payload))


# ── Pull: Fetch changes from backend ──


def _same_outcome_json(local_value: Any, cloud_value: Any) -> bool:
    """Compare outcome_json by content, not by byte-for-byte formatting.

    The backend re-serializes, so key order and spacing drift while the payload
    is identical. A raw string compare would call every pull a change.
    """
    if local_value == cloud_value:
        return True
    try:
        return json.loads(local_value or "null") == json.loads(cloud_value or "null")
    except (TypeError, ValueError):
        return False


# Which cloud campaigns this client will create locally.
#
# Restricted on purpose. A local delete syncs to the backend
# (sync_campaign_delete), so a campaign the user deleted here is gone or
# archived there — refusing archived is therefore the same guard the blanket
# refusal used to provide, without also blocking campaigns that were simply
# born in the cloud and never had a local row to lose.
_ADOPTABLE_CAMPAIGN_STATUSES = frozenset({"active", "paused"})


async def _fetch_cloud_campaigns() -> dict[str, dict[str, Any]]:
    """The backend's campaigns, keyed by id.

    /scheduler/changes carries no campaigns, so this is the only way the client
    can learn that a campaign_id it has never seen is real, live work rather
    than a ghost. Failures return {} — an unreachable backend must leave the
    pull exactly as it behaved before, not abort it.
    """
    return await _list_cloud_campaigns() or {}


async def _list_cloud_campaigns() -> dict[str, dict[str, Any]] | None:
    """Like _fetch_cloud_campaigns, but None when the list could not be had.

    The status refresh has to tell "the cloud lists nothing" from "the cloud
    did not answer": only an answer counts as a refresh, so a failed fetch
    must not hold the next one off for a whole push interval.
    """
    base = _base_url()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{base}/api/v1/campaigns", headers=_headers())
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("Could not list cloud campaigns: %r", e)
        return None

    if not isinstance(payload, list):
        logger.warning("Cloud campaign list was not a list: %s", type(payload).__name__)
        return None
    out: dict[str, dict[str, Any]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("campaign_id") or "").strip()
        if cid:
            out[cid] = item
    return out


def _insert_cloud_campaign(campaign_id: str, name: str, status: str) -> None:
    """Create the local row for a cloud-born campaign, keeping the cloud id.

    The id has to survive: every outreach, message and engagement in the same
    pull references it, and the next push must upsert the backend's own row
    rather than mint a duplicate. queries.create_campaign generates a fresh
    uuid, so it cannot be used here.
    """
    now = int(time.time())
    db = queries.get_db()
    try:
        db.execute(
            """INSERT OR IGNORE INTO campaigns
                   (id, name, icp_json, status, mode, config_json,
                    context_json, created_at, updated_at)
               VALUES (?, ?, '', ?, 'autopilot', '', '', ?, ?)""",
            (campaign_id, name, status, now, now),
        )
        db.commit()
    finally:
        db.close()


async def _adopt_cloud_campaigns(
    campaign_ids: set[str],
    listed: dict[str, dict[str, Any]] | None = None,
) -> set[str]:
    """Create local rows for the cloud campaigns worth having. Returns adopted ids.

    ``listed`` is the cloud campaign list when the caller already has it, so
    one pull lists the cloud once; without it the list is fetched here.
    """
    if not campaign_ids:
        return set()

    # A delete whose backend half has not landed yet. The local rows are
    # already gone and the cloud row is still active, so status alone says
    # "adopt me" about a campaign the user just deleted — the tombstone is the
    # only thing that still knows better until retry_pending_cloud_deletes()
    # gets through.
    pending_delete = await run_db(
        queries.get_setting, _DELETE_TOMBSTONES_SETTING, {}
    ) or {}

    if listed is None:
        listed = await _fetch_cloud_campaigns()
    adopted: set[str] = set()
    for cid in campaign_ids:
        if cid in pending_delete:
            continue
        meta = listed.get(cid)
        if not meta:
            # The backend does not list it either: no evidence it belongs here.
            continue
        status = str(meta.get("status") or "").strip().lower()
        if status not in _ADOPTABLE_CAMPAIGN_STATUSES:
            continue
        name = str(meta.get("name") or "").strip() or "Untitled cloud campaign"
        try:
            await run_db(_insert_cloud_campaign, cid, name, status)
        except Exception as e:
            logger.warning("Could not adopt cloud campaign %s: %r", cid, e)
            continue
        adopted.add(cid)
        logger.info("Adopted cloud campaign %s (%s, %s)", cid, name, status)
    return adopted


# How much each campaign status lets a campaign do. The cloud moves a local
# copy down this scale, and back up only as far as undoing a stop it brought
# down itself; see should_apply_cloud_campaign_status.
_CAMPAIGN_ACTIVITY_RANK = {
    constants.STATUS_ACTIVE: 2,
    constants.STATUS_PAUSED: 1,
    constants.STATUS_DRAFT: 1,
    constants.STATUS_COMPLETED: 0,
    "archived": 0,
}

# The mark a refresh leaves in config_json when it applies a cloud stop:
# {"status": <the status it set>, "from": <the status before>, "at": <epoch>},
# plus pause_reason = _CLOUD_STOP_PAUSE_REASON. It is how the next refresh
# tells a stop that came down from the cloud (which a cloud resume may lift)
# from a local one (which must keep going out as the stop signal). Every local
# pause path (campaign_control, emergency_stop, the weekly-limit auto-pause)
# overwrites pause_reason and a local resume pops it, so a mark outlived by a
# local status change no longer matches and lifts nothing.
_CLOUD_STOP_KEY = "cloud_stop"
_CLOUD_STOP_PAUSE_REASON = "cloud_status_refresh"

# The user's intent that the cloud has not been told yet. Launch, monitor and
# resume in observe activate a campaign locally and deliberately send no
# /resume (that call is the backend's go-signal). Since heylead-api #328 only
# /resume can move a paused cloud campaign to active, so until leaving observe
# sends it the cloud is BEHIND this row, not ahead of it: the refresh must not
# read the cloud's "paused" as a dashboard pause and demote the row, which
# undid the resume before the toggle could send it. Set in config_json as
# PENDING_CLOUD_RESUME_KEY = True plus PENDING_CLOUD_RESUME_AT = epoch; cleared
# once the cloud resume lands or is refused for good.
PENDING_CLOUD_RESUME_KEY = "pending_cloud_resume"
PENDING_CLOUD_RESUME_AT = "pending_cloud_resume_at"


def should_apply_cloud_campaign_status(
    local_status: str, cloud_status: str, *, cloud_stopped: bool = False,
) -> bool:
    """True when the cloud's campaign status may overwrite the local copy's.

    Nothing used to bring a cloud status back here, so a local copy stayed
    "active" after the campaign was paused or archived from the dashboard, and
    every periodic push resent "active" as the go-signal (10 Sep 2026,
    be5f78ff…, archived in the cloud and active in this DB).

    A stop always travels down: the cloud may move a local copy to any status
    ranked lower (active -> paused/draft/archived/completed, paused/draft ->
    archived/completed). A local pause the cloud has not heard of yet (its
    /pause call failed) must keep going out as the stop signal, so a cloud
    status ranked higher changes nothing — unless ``cloud_stopped``: the local
    stop is one an earlier refresh brought down from the cloud, and the cloud
    has since resumed or unarchived the campaign. Refusing that lift is what
    undid dashboard resumes: the next push re-sent the stale "paused".
    A status either side does not rank is not a comparison and changes
    nothing.
    """
    local = str(local_status or "").strip().lower()
    cloud = str(cloud_status or "").strip().lower()
    if not local or not cloud or local == cloud:
        return False
    if local not in _CAMPAIGN_ACTIVITY_RANK or cloud not in _CAMPAIGN_ACTIVITY_RANK:
        return False
    if _CAMPAIGN_ACTIVITY_RANK[cloud] < _CAMPAIGN_ACTIVITY_RANK[local]:
        return True
    return cloud_stopped and _CAMPAIGN_ACTIVITY_RANK[cloud] > _CAMPAIGN_ACTIVITY_RANK[local]


def _campaign_config(row: dict[str, Any]) -> dict[str, Any] | None:
    """The row's config_json as a dict; None when it is not a JSON object."""
    try:
        cfg = json.loads(row.get("config_json") or "{}")
    except (TypeError, ValueError):
        return None
    return cfg if isinstance(cfg, dict) else None


def has_pending_cloud_resume(cfg: dict[str, Any] | None) -> bool:
    """True when config_json records a resume observe kept from the cloud."""
    return bool(cfg) and cfg.get(PENDING_CLOUD_RESUME_KEY) is True


def _has_cloud_stop_mark(local_status: str, cfg: dict[str, Any] | None) -> bool:
    """True when the local status is a stop an earlier refresh brought down."""
    if not cfg:
        return False
    mark = cfg.get(_CLOUD_STOP_KEY)
    return (
        isinstance(mark, dict)
        and str(mark.get("status") or "").strip().lower()
        == str(local_status or "").strip().lower()
        and cfg.get("pause_reason") == _CLOUD_STOP_PAUSE_REASON
    )


def _apply_cloud_campaign_statuses(
    cloud_statuses: dict[str, str],
) -> list[tuple[str, str, str]]:
    """Move local campaign copies to the cloud's status where the rule allows.

    A stop it applies is marked (see _CLOUD_STOP_KEY) so a later cloud resume
    can lift it; lifting to active clears the mark. A config_json that is not
    a JSON object is left alone: the stop still applies, unmarked, so it
    stays sticky — the safe direction.

    A local active row carrying PENDING_CLOUD_RESUME_KEY is a resume the cloud
    has not been sent yet, so a cloud "paused" or "draft" is stale and changes
    nothing; a cloud "active" means the cloud caught up and only clears the
    flag. A cloud "archived" or "completed" still applies (a resume would be
    refused, or would wrongly restart it) and clears the flag. Unflagged rows
    follow the rule above unchanged, so a dashboard pause still comes down.

    Returns (campaign_id, old_status, new_status) for every row it changed.
    Campaigns this DB does not hold are skipped: adoption decides those.
    """
    if not cloud_statuses:
        return []
    local_rows = queries.batch_get_campaigns(sorted(cloud_statuses))
    changed: list[tuple[str, str, str]] = []
    now = int(time.time())
    for cid, row in local_rows.items():
        old = str(row.get("status") or "").strip().lower()
        new = str(cloud_statuses.get(cid) or "").strip().lower()
        cfg = _campaign_config(row)
        if old == constants.STATUS_ACTIVE and has_pending_cloud_resume(cfg):
            if new in (constants.STATUS_PAUSED, constants.STATUS_DRAFT):
                continue
            if new == constants.STATUS_ACTIVE:
                assert cfg is not None
                cfg.pop(PENDING_CLOUD_RESUME_KEY, None)
                cfg.pop(PENDING_CLOUD_RESUME_AT, None)
                queries.update_campaign(cid, config_json=json.dumps(cfg))
                continue
        marked = _has_cloud_stop_mark(old, cfg)
        if not should_apply_cloud_campaign_status(old, new, cloud_stopped=marked):
            continue
        lifted = _CAMPAIGN_ACTIVITY_RANK[new] > _CAMPAIGN_ACTIVITY_RANK[old]
        updates: dict[str, Any] = {"status": new}
        if cfg is not None:
            cfg.pop(PENDING_CLOUD_RESUME_KEY, None)
            cfg.pop(PENDING_CLOUD_RESUME_AT, None)
            if not lifted:
                cfg["pause_reason"] = _CLOUD_STOP_PAUSE_REASON
                cfg[_CLOUD_STOP_KEY] = {"status": new, "from": old, "at": now}
                if new == constants.STATUS_PAUSED:
                    cfg["paused_at"] = now
            elif new == constants.STATUS_ACTIVE:
                cfg.pop("pause_reason", None)
                cfg.pop("paused_at", None)
                cfg.pop(_CLOUD_STOP_KEY, None)
            else:
                # Unarchived in the cloud: still a stop the cloud owns.
                cfg[_CLOUD_STOP_KEY] = {"status": new, "from": old, "at": now}
            updates["config_json"] = json.dumps(cfg)
        queries.update_campaign(cid, **updates)
        queries.log_action(
            "campaign_status_change",
            result=new,
            details={
                "campaign_id": cid,
                "campaign_name": row.get("name") or "",
                "old_status": old,
                "new_status": new,
                "changed_by": "cloud",
                "reason": (
                    "cloud_status_refresh_lifted_cloud_stop" if lifted
                    else "cloud_status_refresh"
                ),
            },
        )
        changed.append((cid, old, new))
    return changed


# When the last successful campaign-status refresh ran (time.monotonic()),
# None before the first. GET /campaigns computes stats for every campaign and
# the pull runs every 5 minutes, so the pull fetches the list for this only
# when nothing has refreshed within the push interval; the push always
# refreshes. A failed fetch does not count.
_last_campaign_status_refresh: float | None = None
_CAMPAIGN_STATUS_REFRESH_SECONDS = 900


def campaign_status_refresh_due() -> bool:
    """True when no status refresh has run within the push interval."""
    last = _last_campaign_status_refresh
    return last is None or time.monotonic() - last >= _CAMPAIGN_STATUS_REFRESH_SECONDS


async def refresh_campaign_statuses_from_cloud(
    listed: dict[str, dict[str, Any]] | None = None,
) -> int:
    """Bring cloud stops, and resumes of stops it brought, to the local copies.

    Runs before the periodic push, so a push never resends "active" for a
    campaign the cloud has stopped, nor "paused" for one the dashboard paused
    and then resumed; and on the pull. ``listed`` is a campaign list the
    caller already fetched (the pull's adoption pass), so one pull never
    lists the cloud twice. Best-effort: a failure logs and changes nothing,
    leaving the pull and push as they were, and does not count as a refresh.
    Returns how many local campaigns changed.
    """
    global _last_campaign_status_refresh
    try:
        if listed is None:
            listed = await _list_cloud_campaigns()
            if listed is None:
                return 0
        _last_campaign_status_refresh = time.monotonic()
        statuses = {
            cid: str(meta.get("status") or "").strip().lower()
            for cid, meta in listed.items()
        }
        changed = await run_db(_apply_cloud_campaign_statuses, statuses)
    except Exception as e:
        logger.warning("Could not refresh campaign status from the cloud: %r", e)
        return 0
    for cid, old, new in changed:
        logger.info(
            "Campaign %s is %s in the cloud; local copy moved from %s", cid, new, old,
        )
    return len(changed)


async def _adopt_cloud_outreach(oid: str, update: dict[str, Any]) -> dict[str, Any] | None:
    """Create the local contact + outreach for a cloud-created outreach row.

    Hosted discovery creates outreaches server-side; until 29 Aug 2026 the
    pull skipped every id it had never seen, so the cloud's own prospects —
    and all their messages and engagements — were invisible locally, forever.
    The backend now sends campaign_id and flat contact_* fields on each
    outreach_update row (older backends don't — those rows return None here
    and stay skipped, which is the pre-adoption behavior).

    Deliberate refusals, both returning None:
    - campaign_id absent from the local DB: the row is a ghost of a locally
      deleted campaign, and a delete must not be undone by the next pull.
    - no contact identity at all: nothing to build a person from.

    Cloud ids are preserved on both rows so the same pull's messages and
    engagements FK-match, and so the next push upserts the backend's own
    rows instead of minting duplicates. save_contact still dedups: if the
    person already exists in the campaign, the outreach points at the
    existing local contact instead.
    """
    campaign_id = str(update.get("campaign_id") or "").strip()
    name = str(update.get("contact_name") or "").strip()
    linkedin_id = str(update.get("contact_linkedin_id") or "").strip()
    linkedin_url = str(update.get("contact_linkedin_url") or "").strip()
    if not campaign_id or not (linkedin_id or linkedin_url or name):
        return None
    campaign = await run_db(queries.get_campaign, campaign_id)
    if not campaign:
        return None

    contact_id = await run_db(
        queries.save_contact,
        campaign_id=campaign_id,
        name=name or linkedin_id or "Unknown",
        title=str(update.get("contact_title") or ""),
        company=str(update.get("contact_company") or ""),
        linkedin_url=linkedin_url,
        linkedin_id=linkedin_id,
        fit_score=float(update.get("contact_fit_score") or 0.0),
        # Keep whatever found this person. Stamping "cloud_sync" unconditionally
        # was fine while it only described how the row reached us, but the push
        # sends `source` back up, so it overwrote the lane that found them in
        # the cloud too — 641 rows by 8 Sep 2026, 374 in a single week, every
        # one of them answering "where did this person come from" with "a
        # sync ran". Older backends do not send the field; those rows still
        # fall back, and they are the only ones that should.
        source=str(update.get("contact_source") or "").strip() or "cloud_sync",
        source_detail=(
            str(update.get("contact_source_detail") or "").strip()
            if str(update.get("contact_source") or "").strip()
            else "adopted from cloud pull"
        ),
        contact_id=str(update.get("contact_id") or ""),
    )
    await run_db(
        queries.create_outreach,
        campaign_id,
        contact_id,
        outreach_id=oid,
    )
    logger.debug(
        "Adopted cloud outreach %s (contact %s) into campaign %s",
        oid, contact_id, campaign_id,
    )
    return await run_db(queries.get_outreach, oid)


def _is_phantom_invite_reset(local: dict[str, Any], update: dict[str, Any]) -> bool:
    """True when the cloud voided an invite this row still believes in.

    ``should_apply_cloud_status`` is monotonic, so a cloud 'pending' can never
    overwrite a local 'invited' — which is right for the stale row that caused
    the 19 Aug 2026 regression, and wrong for the one case where the cloud
    knows better than we do: reconcile-invites proved the invitation never
    reached LinkedIn (silently discarded past the weekly cap) and rewound the
    row so it can be sent again. Both arrive as status='pending'; only the
    reset carries ``invite_reset_at``.

    The marker must not be older than the invite it voids. The client sends
    invites too and pushes them up minutes later, so a changes feed built
    before that push still carries the earlier reset — honouring it blindly
    would delete a live invitation.

    The rewind can also arrive one rank higher (5 Sep 2026): a cloud send_dm
    refused as not-first-degree voids a 'connected' row over the same marker —
    the connection was claimed on distance checks that ran through a dying
    session, and LinkedIn's 422 disproved it. Locally that row says
    'connected'; the marker must be at least as new as every stamp it voids
    (an acceptance detected after the reset is news the reset cannot claim).
    """
    if (update.get("status") or "") != "pending":
        return False
    # 'messaged' can be part of the same phantom chain (a DM "sent" through a
    # dead session); without it here the mirror wedges — cloud pending, local
    # messaged, every push held, forever. 'replied' and beyond are never
    # rewound: a reply proves a real conversation exists.
    if (local.get("status") or "") not in ("invited", "connected", "messaged"):
        return False
    reset_at = update.get("invite_reset_at") or 0
    if not reset_at:
        return False
    return reset_at >= max(
        local.get("invited_at") or 0, local.get("accepted_at") or 0,
    )


def _is_phantom_dm_reset(local: dict[str, Any], update: dict[str, Any]) -> bool:
    """True when the cloud voided a DM this row still believes was sent.

    5 Sep 2026, message edition of the invite reset: a dead LinkedIn session
    (Unipile source CREDENTIALS) let the cloud phantom-mark rows 'messaged'
    for DMs LinkedIn never received. The backend repair rewinds them to
    connected/pending with ``message_reset_at`` stamped; the monotonic pull
    guard would refuse that downgrade, and the local mirror would then push
    'messaged' back — so the marker, and only the marker, lets the rewind
    land. Rows past 'messaged' (replied and beyond) are never rewound: a
    reply proves a real conversation exists.
    """
    if (update.get("status") or "") not in ("connected", "pending"):
        return False
    if (local.get("status") or "") != "messaged":
        return False
    return bool(update.get("message_reset_at"))


def _promote_invited_from_stamp(
    changes: dict[str, Any],
    local: dict[str, Any],
    update: dict[str, Any],
) -> None:
    """If the invite already went out, do not leave the mirror pending/sending.

    Pull used to write ``invited_at`` (and invitation_sent) while status stayed
    ``pending``. A sending row with the same stamp never reached ``invited``.
    """
    if _is_phantom_invite_reset(local, update):
        return
    if "invited_at" in changes:
        invited = changes["invited_at"]
    else:
        invited = update.get("invited_at") or local.get("invited_at")
    if not invited:
        return
    resulting = changes.get("status", local.get("status") or "")
    if resulting in ("pending", "sending"):
        changes["status"] = "invited"


def _cloud_outreach_changes(local: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """The fields of a cloud outreach update that actually differ from local.

    Returning {} means "do not write". This existed as an inline block whose
    timestamp branch never consulted ``local``::

        for field in ("invited_at", "accepted_at", "first_reply_at", "outcome_json"):
            if update.get(field):
                kwargs[field] = update[field]

    so a value the backend simply echoed back still triggered
    ``update_outreach``, which stamps ``updated_at = now()``. On this install
    that re-saved 1705 outreaches on every pull, ~90 pulls a day — ~153k writes
    that change nothing. Each pull overwrote the previous stamp, so only the
    newest burst was ever visible and it read like a one-off ad-hoc script
    rather than a loop; two sessions lost half an hour to that on 22 Aug 2026.

    The cost is that ``outreaches.updated_at`` stops recording when the row
    changed. ``_is_followup_due`` used to fall back to it and could then never
    come due — it now counts from the conversation
    (``planner._last_touch_at`` / ``queries._LAST_OUTBOUND_SQL``) — but
    ``show_status`` still counts daily activity with it.
    """
    changes: dict[str, Any] = {}

    # ── Versioned Outreach Sync: the clock decides for versioned rows ──
    # The cloud versions the funnel tuple (status + three stamps) as a unit;
    # a pulled clock ahead of ours means the whole tuple is newer and lands
    # whole — no rank comparison, no marker carve-outs. Equal-or-behind means
    # nothing new. Unpushed local news defers the overwrite until pushed.
    # 'replied'+ is never rewound: a reply proves a conversation, and a cloud
    # that disagrees should surface as a disagreement, not win silently.
    cloud_version = int(update.get("status_version") or 0)
    if cloud_version > 0:
        local_version = int(local.get("cloud_status_version") or 0)
        if cloud_version <= local_version:
            return {}
        if local.get("local_news"):
            # An unpushed local fact defers the overwrite — but not forever:
            # a push that keeps failing would freeze the mirror (review,
            # 5 Sep). After 6h (24 missed 15-min pushes, a bigger problem
            # with its own alarms) the cloud wins and the local detector
            # re-asserts the fact if it still holds.
            import time as _time
            age = int(_time.time()) - int(local.get("updated_at") or 0)
            if age < 6 * 3600:
                return {}
        cloud_status_v = update.get("status") or ""
        local_status_v = local.get("status") or ""
        # Carve-outs the version branch must keep (review, 5 Sep — dropping
        # them re-opened two closed holes):
        # * replied+ proves a conversation; the clock does not outrank it;
        # * opted_out/unsubscribed/bounced are terminal suppressions — a
        #   versioned advance must never re-enter someone who opted out;
        # * a status this client has no vocabulary for ('expired' rewrote
        #   4,216 rows on 22 Aug) is a newer client's news, not ours to apply.
        rewinds_replied = (
            local_status_v in ("replied", "hot_lead", "reverse_pitch",
                               "closed_happy", "closed_unhappy")
            and cloud_status_v not in ("replied", "hot_lead", "reverse_pitch",
                                       "closed_happy", "closed_unhappy")
        )
        if rewinds_replied:
            return {}
        # A strictly newer cloud version outranks a local close. The laptop
        # has made no decision about a person since sending moved to the
        # cloud in phases 1-2, so a local closed_happy/closed_unhappy is at
        # best a copy of an older cloud decision — and when it is wrong it is
        # unfixable, because the rank path refuses it too (closed_unhappy 7
        # against replied 5). 14 Sep 2026: one prospect, local closed_unhappy v3
        # against cloud replied v6, re-pushed every 15 minutes.
        #
        # The opt-out family stays terminal, whatever the clock says.
        if local_status_v in _CLOUD_SUPPRESSION_STATUS:
            return {}
        if (cloud_status_v not in _CLOUD_STATUS_RANK
                and cloud_status_v not in ("error", "skipped")):
            return {}
        changes["cloud_status_version"] = cloud_version
        if cloud_status_v != local_status_v:
            changes["status"] = cloud_status_v
        for field in ("invited_at", "accepted_at", "first_reply_at"):
            if field in update and update.get(field) != local.get(field):
                changes[field] = update.get(field)
        for field in ("headline_variant", "headline_test_id"):
            value = update.get(field)
            if value and value != local.get(field):
                changes[field] = value
        outcome = update.get("outcome_json")
        if outcome and not _same_outcome_json(local.get("outcome_json"), outcome):
            changes["outcome_json"] = outcome
        if "followup_count" in update:
            cloud_count = update["followup_count"]
            if cloud_count is not None and cloud_count > (local.get("followup_count") or 0):
                changes["followup_count"] = cloud_count
        _promote_invited_from_stamp(changes, local, update)
        # A version-only advance with an identical tuple stores just the
        # clock — a full-row rewrite per row per pull was pure churn.
        return changes

    cloud_status = update.get("status")
    if cloud_status and should_apply_cloud_status(local.get("status") or "", cloud_status):
        changes["status"] = cloud_status
        if cloud_status == "skipped":
            # A skip arriving from the cloud is a person pressing Skip in the
            # dashboard, and fit_gate.restore_fit_skipped_if_eligible exempts
            # exactly two markers — 'operator_skip' and 'do_not_contact'.
            # Without this the next rescore that lifted their fit above the
            # floor put a human-skipped prospect straight back in the queue.
            changes["last_attempt_error"] = "operator_skip"
    elif _is_phantom_invite_reset(local, update):
        # Clear the stamps with the status: the push ships invited_at (and,
        # for a voided connection, accepted_at) back up, and a row that still
        # carries them reads as invited/connected everywhere else.
        changes["status"] = "pending"
        changes["invited_at"] = None
        changes["accepted_at"] = None
    elif _is_phantom_dm_reset(local, update):
        changes["status"] = update.get("status")

    if "followup_count" in update:
        cloud_count = update["followup_count"]
        local_count = local.get("followup_count") or 0
        # Strictly greater: the cloud must not walk the sequence backwards, and
        # writing an equal count only re-stamps the row.
        if cloud_count is not None and cloud_count > local_count:
            changes["followup_count"] = cloud_count

    # Event timestamps for velocity metrics, applied only when they move.
    for field in ("invited_at", "accepted_at", "first_reply_at"):
        value = update.get(field)
        if value and value != local.get(field):
            changes[field] = value

    for field in ("headline_variant", "headline_test_id"):
        value = update.get(field)
        if value and value != local.get(field):
            changes[field] = value

    outcome = update.get("outcome_json")
    if outcome and not _same_outcome_json(local.get("outcome_json"), outcome):
        changes["outcome_json"] = outcome

    _promote_invited_from_stamp(changes, local, update)
    return changes


def _outreach_id(update: dict[str, Any]) -> str:
    """The id of a pulled outreach row, coerced once.

    pull_changes reads it in two places — the campaign set it builds before the
    loop, and the loop itself — and both test it against the tombstone
    blocklist. Reading it differently in the two would let them disagree; see
    the comment at the `referenced` set.
    """
    return str(update.get("id") or "")


async def pull_changes(since: int) -> dict[str, Any]:
    """Pull state changes made by the backend scheduler since a timestamp.

    Applies outreach status updates and saves new messages to the local DB.
    Returns the changes dict from the backend.
    """
    from ..correlation import get_correlation_id, new_correlation_id
    if not get_correlation_id():
        new_correlation_id()

    if not config.is_backend_mode():
        return {"error": "Backend mode not configured"}

    # Outreaches hard-deleted here whose deletion has not yet reached the
    # hosted store. Until a push carries them, the hosted rows are still there
    # and every pull offers them back — that is how 577 rows deleted from one
    # campaign were on the dashboard again four days later.
    #
    # Read BEFORE the fetch, and applied from this snapshot. The push DELETEs a
    # tombstone the moment the deletion is delivered (see sync_to_cloud), which
    # is safe only because no pull can re-adopt a row the cloud no longer has —
    # except an in-flight one, which fetched while the hosted row still existed.
    # Reading the blocklist after the fetch, or per row while applying, would
    # see the just-emptied table and re-create exactly that row. Ordering is
    # the whole guard; there is no synced_at column to fall back on.
    tombstoned = set(await run_db(queries.list_outreach_tombstone_ids))

    base = _base_url()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.get(
                f"{base}/api/v1/scheduler/changes",
                params={"since": since},
                headers=_headers(),
            )
            resp.raise_for_status()
            changes = resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                # Recorded before raising: the caller may or may not catch this
                # (the sync loop used to swallow it), and the whole point is
                # that a session which stopped working is never silent.
                await run_db(
                    _record_pull,
                    f"Pull failed: HTTP {e.response.status_code} — JWT expired or invalid.",
                )
                raise BackendAuthError(
                    "Backend returned %d — JWT expired or invalid."
                    % e.response.status_code
                )
            logger.error("Pull changes HTTP error: %s", e.response.status_code)
            failure = f"Pull failed: HTTP {e.response.status_code}"
            await run_db(_record_pull, failure)
            return {"error": failure}
        except httpx.HTTPError as e:
            logger.error("Pull changes failed: %r", e)
            failure = _pull_error_text(e)
            await run_db(_record_pull, failure)
            return {"error": failure}

    # Apply outreach updates to local DB
    from ..linkedin.rate_limiter import (
        backfill_unlogged_cloud_invites,
        log_cloud_executed_send,
    )

    outreach_updates = changes.get("outreach_updates", [])
    outreach_applied = 0
    outreach_adopted = 0
    just_touched: list[str] = []
    outreach_ghosts = 0
    # Rows the blocklist refused, and the children that hang off them. Counted
    # apart from outreach_ghosts / messages_skipped: those mean "the cloud
    # knows someone this DB has never held", and this means the opposite —
    # we deleted them on purpose and the cloud has not been told yet.
    outreach_deleted_here = 0
    messages_deleted_here = 0
    engagements_deleted_here = 0
    # Which campaigns the discarded rows belonged to. A count of rows alone
    # reads as noise; "25 campaigns" is what tells the reader the local list
    # is missing whole campaigns rather than a few stragglers.
    ghost_campaigns: set[str] = set()
    # Rows dropped because their campaign is absent — the count the status
    # line quotes. outreach_ghosts stays the total for the log, which wants
    # every refusal regardless of cause.
    ghost_rows_absent = 0

    # Adopt cloud-born campaigns before walking the rows, so their outreaches
    # land in this same pull instead of being discarded once more. Resolved in
    # one batch: a per-row lookup would be one HTTP round-trip per orphan, and
    # the orphans arrive in the thousands.
    # Tombstoned rows are excluded here so that a row that will be refused is
    # not the reason a cloud-born campaign gets created on this machine. Only
    # the campaign: the contact is refused a few lines down, in the loop.
    # _adopt_cloud_outreach owns the save_contact call and is never reached for
    # a tombstoned id whatever this set holds, so no contact can ride in on one.
    #
    # Both sites test the blocklist through _outreach_id so they cannot read
    # the id differently. Comparing str(u["id"]) here and the raw `oid` in the
    # loop cannot diverge for real data — outreach ids are TEXT on both sides
    # and every producer is a str — but a backend sending a non-string id would
    # make them disagree, withholding the campaign while the row itself
    # proceeded. That direction is the conservative one, so this is tidiness
    # rather than a fix; one coercion just costs nothing.
    referenced = {
        str(u.get("campaign_id") or "").strip()
        for u in outreach_updates
        if str(u.get("campaign_id") or "").strip()
        and _outreach_id(u) not in tombstoned
    }
    # Campaigns this DB genuinely does not hold, after adoption has had its
    # turn. Only these belong in the user-facing count: the line claims
    # "campaigns this machine has no record of", and a row refused for any
    # other reason — most often a second cloud id for someone already here,
    # which idx_outreaches_campaign_contact rejects by design — would name a
    # campaign that is sitting right there and make the sentence untrue.
    absent_campaigns: set[str] = set()
    # The cloud campaign list, fetched at most once per pull and shared by
    # adoption and the status refresh below.
    cloud_listed: dict[str, dict[str, Any]] | None = None
    cloud_list_failed = False
    if referenced:
        known = await run_db(queries.batch_get_campaigns, sorted(referenced))
        orphans = referenced - set(known)
        if orphans:
            cloud_listed = await _list_cloud_campaigns()
            cloud_list_failed = cloud_listed is None
        adopted_now = await _adopt_cloud_campaigns(orphans, listed=cloud_listed or {})
        absent_campaigns = orphans - adopted_now
    # Cloud stops reach the local campaign copies: a copy left "active" is
    # resent as the go-signal by the next push (10 Sep 2026). The push
    # refreshes every time; the pull reuses a list it already holds and
    # otherwise lists the cloud only when no refresh ran within the push
    # interval. A list that just failed is not asked for twice in one pull.
    # Best-effort; it raises a status only to undo a stop it brought down.
    if cloud_listed is not None or (
        not cloud_list_failed and campaign_status_refresh_due()
    ):
        await refresh_campaign_statuses_from_cloud(cloud_listed)
    for update in outreach_updates:
        oid = _outreach_id(update)
        if not oid:
            continue
        if oid in tombstoned:
            # Before the get_outreach lookup on purpose: the id is blocked, not
            # the absence of the row. Another path (a sync echo, an import) can
            # put the row back while its deletion is still in flight, and
            # applying a cloud update to that row would re-animate it too.
            outreach_deleted_here += 1
            continue
        # Guarded per item, same lesson as the messages loop below: one
        # poisoned row aborting the pull is how the 26–29 Aug wedge happened.
        try:
            local = await run_db(queries.get_outreach, oid)
            if not local:
                local = await _adopt_cloud_outreach(oid, update)
                if not local:
                    outreach_ghosts += 1
                    ghost_campaign = str(update.get("campaign_id") or "").strip()
                    if ghost_campaign and ghost_campaign in absent_campaigns:
                        ghost_campaigns.add(ghost_campaign)
                        ghost_rows_absent += 1
                    continue
                outreach_adopted += 1
            just_touched.append(oid)
            kwargs = _cloud_outreach_changes(local, update)
            if kwargs:
                await run_db(queries.update_outreach, oid,
                             from_cloud=True, **kwargs)
                outreach_applied += 1
                logger.debug("Applied cloud update to outreach %s: %s", oid, kwargs)
            # The cap reads actions_log, not invited_at. Log even when the
            # outreach write is a no-op — that is the production miss: the
            # stamp is already local and nothing ever recorded the send.
            invited_at = update.get("invited_at") or local.get("invited_at")
            if invited_at:
                await run_db(
                    log_cloud_executed_send,
                    "invitation_sent",
                    oid,
                    timestamp=int(invited_at),
                    campaign_id=local.get("campaign_id") or "",
                )
        except Exception as e:
            logger.warning("Cloud outreach update for %s failed: %s", oid, e)
    if outreach_adopted or outreach_ghosts:
        logger.info(
            "Adopted %d cloud-created outreach(es); skipped %d for campaigns "
            "not in this DB (deleted locally, or payload carries no contact)",
            outreach_adopted, outreach_ghosts,
        )

    # Cloud-owned laptops never start the local engine, so startup recover
    # never runs. Heal sending/invited_at mirrors on every successful pull.
    try:
        recovered = await run_db(queries.recover_stuck_sending_outreaches, 360)
        if recovered:
            logger.warning(
                "Pull: recovered %d stuck sending outreaches", recovered,
            )
    except Exception as e:
        logger.debug("Pull stuck-sending recover failed: %s", e)

    # Save new messages. Guarded per item: hosted discovery creates
    # outreaches server-side that this DB has never seen, and one of their
    # messages FK-failing must not abort the whole pull (it wedged every
    # pull for three days, 26–29 Aug 2026, because last_pull_timestamp is
    # only stamped at the end).
    new_messages = changes.get("new_messages", [])
    messages_applied = 0
    messages_skipped = 0
    for msg in new_messages:
        outreach_id = msg.get("outreach_id")
        if not outreach_id:
            continue
        if outreach_id in tombstoned:
            # The parent was refused above, so this would either FK-fail into
            # messages_skipped — whose log line would then blame a
            # "cloud-created" outreach we in fact deleted ourselves — or, worse,
            # attach to a row some other path re-created.
            messages_deleted_here += 1
            continue
        try:
            # Check if message already exists locally
            local_msgs = await run_db(queries.get_messages_for_outreach, outreach_id)
            existing_ids = {m["id"] for m in local_msgs}
            if msg.get("id") in existing_ids:
                continue
            already = any(
                m.get("role") == (msg.get("role") or "sdr")
                and m.get("text") == (msg.get("text") or "")
                for m in local_msgs
            )
            # Mirror the hosted row, do not re-mint it. Saving under a fresh
            # local id made the next push hand this same message back to the
            # backend as new, and the backend stored the copy as a 'dm' with
            # no chat_id — one echo per hosted message, invite notes
            # included (8 Sep 2026: 264 rows, DMs counter 128 against 5
            # real sends). The hosted id is the identity on both sides; the
            # hosted created_at is when it was sent, not when we pulled it;
            # and an invitation note keeps the format that marks it as one.
            cloud_type = (msg.get("message_type") or "").lower()
            cloud_format = str(msg.get("format") or "text")
            if cloud_type in ("invitation", "invite"):
                cloud_format = "invite_note"
            sent_at = msg.get("created_at") or msg.get("timestamp")
            await run_db(queries.save_message,
                outreach_id=outreach_id,
                role=msg.get("role", "sdr"),
                text=msg.get("text", ""),
                sentiment=msg.get("sentiment", ""),
                format=cloud_format,
                timestamp=int(sent_at) if sent_at else None,
                message_id=str(msg.get("id") or "") or None,
            )
            messages_applied += 1
            logger.debug("Saved cloud message for outreach %s", outreach_id)
            if already:
                continue
            role = msg.get("role", "sdr")
            mtype = (msg.get("message_type") or "dm").lower()
            if role != "sdr" or mtype == "invitation":
                continue
            sent_at = msg.get("timestamp") or msg.get("created_at")
            await run_db(
                log_cloud_executed_send,
                "followup_sent" if mtype == "followup" else "dm_sent",
                outreach_id,
                timestamp=int(sent_at) if sent_at else None,
                details={"source": "cloud", "cloud_id": msg.get("id") or ""},
            )
        except Exception as e:
            messages_skipped += 1
            logger.debug(
                "Message sync failed for outreach %s: %s", outreach_id, e
            )
    if messages_skipped:
        logger.warning(
            "Skipped %d cloud message(s) for outreaches this DB does not "
            "hold (cloud-created, not yet synced)",
            messages_skipped,
        )

    # Apply brand strategy updates from backend
    brand_updates = changes.get("brand_updates", {})
    if brand_updates.get("brand_strategy"):
        await run_db(queries.save_setting, "brand_strategy", brand_updates["brand_strategy"])
        logger.debug("Applied brand strategy update from cloud")
    if brand_updates.get("brand_analysis"):
        await run_db(queries.save_setting, "brand_analysis", brand_updates["brand_analysis"])
        logger.debug("Applied brand analysis update from cloud")

    # Sync new engagements from backend
    new_engagements = changes.get("new_engagements", [])
    engagements_applied = 0
    for eng in new_engagements:
        outreach_id = eng.get("outreach_id")
        if not outreach_id:
            continue
        if outreach_id in tombstoned:
            engagements_deleted_here += 1
            continue
        try:
            saved = await run_db(_save_pulled_engagement, eng)
            if saved:
                engagements_applied += 1
                logger.debug("Saved cloud engagement for outreach %s", outreach_id)
        except Exception as e:
            logger.debug("Engagement sync failed for outreach %s: %s", outreach_id, e)

    # Repair can tombstone during the GET. The snapshot taken before fetch
    # would miss that id and apply would adopt it. Re-read after apply.
    late = set(await run_db(queries.list_outreach_tombstone_ids))
    victims = [oid for oid in dict.fromkeys(just_touched) if oid in late]
    if victims:
        deleted = await run_db(queries.hard_delete_outreach_ids, victims)
        logger.info(
            "Hard-deleted %d outreach(es) tombstoned during pull", deleted,
        )

    # Sync usage counters from backend (overwrite local with authoritative backend data)
    usage = changes.get("usage", {})
    if usage:
        try:
            from ..db.queries import set_monthly_usage
            await run_db(set_monthly_usage, usage)
            logger.debug("Applied usage sync from cloud: %s", usage)
        except Exception as e:
            logger.debug("Usage sync failed: %s", e)

    # Sync rate limits from backend (authoritative for cloud-scheduled sends)
    rate_limits = changes.get("rate_limits", {})
    if rate_limits:
        try:
            await run_db(_apply_rate_limits, rate_limits)
            logger.debug("Applied rate limits sync from cloud: %s", rate_limits)
        except Exception as e:
            logger.debug("Rate limits sync failed: %s", e)

    # Invites already applied locally never reappear in outreach_updates
    # (the no-op guard). Their actions_log row still has to exist.
    try:
        filled = await run_db(backfill_unlogged_cloud_invites)
        if filled:
            logger.info("Logged %d cloud invite(s) the pull had already applied", filled)
    except Exception as e:
        logger.debug("Cloud invite actions_log backfill failed: %s", e)

    # Log received AND applied. Counting only received is what made the
    # no-op-write bug look like a 1705-row bulk script after the writes stopped.
    if (
        outreach_updates or new_messages or new_engagements
        or brand_updates or rate_limits
    ):
        logger.info(
            "Pulled %d/%d outreach updates + %d/%d messages + %d/%d engagements from cloud",
            outreach_applied,
            len(outreach_updates),
            messages_applied,
            len(new_messages),
            engagements_applied,
            len(new_engagements),
        )

    if outreach_deleted_here or messages_deleted_here or engagements_deleted_here:
        logger.info(
            "Refused %d cloud outreach(es) deleted locally and awaiting a push, "
            "with %d message(s) and %d engagement(s) of theirs",
            outreach_deleted_here, messages_deleted_here, engagements_deleted_here,
        )

    # Save last pull timestamp
    import time
    await run_db(queries.save_setting, "last_pull_timestamp", int(time.time()))
    await run_db(
        _record_pull,
        "",
        {"rows": ghost_rows_absent, "campaigns": len(ghost_campaigns)},
    )

    return changes


# Sanity window for a backend timestamp. Every daily counter in this codebase
# is a "created_at >= today_start" scan (db/queries.py), so a far-future
# created_at counts toward today AND every day after it — strictly worse than a
# now() stamp, which at least ages out overnight. A value outside this window is
# a malformed payload, not history, and is discarded rather than stored.
_EPOCH_FLOOR = 1_262_304_000  # 2010-01-01, long before this product existed
_EPOCH_SKEW = 86_400  # one day of tolerance for backend/laptop clock drift


def _plausible_epoch(value: float) -> int | None:
    """Normalise a numeric timestamp to epoch seconds, or None if implausible.

    Backends commonly ship milliseconds. 1786707486000 stored verbatim is the
    year 58588; scaled it is the correct instant. Scaling is decided by
    magnitude — a value at least a thousand times the floor cannot be seconds —
    and never by guesswork about the caller's intent.
    """
    try:
        n = int(value)
    except (OverflowError, ValueError):  # inf, nan
        return None
    while n >= _EPOCH_FLOOR * 1000:  # milliseconds, then microseconds
        n //= 1000
    ceiling = int(time.time()) + _EPOCH_SKEW
    return n if _EPOCH_FLOOR <= n <= ceiling else None


def _coerce_epoch(value: Any) -> int | None:
    """Best-effort epoch seconds from a backend timestamp, else None.

    The changes endpoint has shipped created_at as both an epoch number and an
    ISO-8601 string depending on the store behind it, so accept both, in
    seconds or milliseconds. Anything that cannot be read as a plausible past
    instant returns None and the caller keeps its own now() stamp: an unusable
    timestamp degrades one row's dating, it does not reject the row or the pull.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return _plausible_epoch(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return _plausible_epoch(float(text))
        except ValueError:
            pass
        import datetime
        try:
            dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return _plausible_epoch(dt.timestamp())
    return None


def _save_pulled_engagement(eng: dict[str, Any]) -> str | None:
    """Insert a backend engagement locally unless we already hold that row.

    Returns the local row id, or None when the row was a duplicate or the
    insert was rejected.

    Context: one pull inserted 3,054 engagement rows in a single second.
    /scheduler/changes replays engagement history rather than returning only
    genuinely-new rows, and this loop saved every item unconditionally. The one
    DB-level defence, the UNIQUE index on engagements(post_id, account_id) in
    db/schema.py, cannot catch the replay — it is a PARTIAL index, declared
    WHERE account_id IS NOT NULL AND account_id != '' AND post_id != '', and
    pulled rows carry no account_id, so they are excluded from the index
    outright and no uniqueness is enforced on them at all.

    Dedup is therefore done on the backend's own row id: the only field that
    identifies a row rather than describing it. Rows this client pushed up keep
    their local id (see sync_to_cloud), so a replay of our own history matches a
    row we already hold. Rows the cloud executed arrive with a new id and are
    stored UNDER THAT ID, so the next replay of them matches too — which is why
    this writes the row directly instead of going through save_engagement, whose
    generated uuid would make every replay look new forever.

    The content key below is a fallback for payloads that carry no id, and it
    covers post-scoped rows only. It is deliberately NOT applied to rows with an
    empty post_id — profile_view, follow and endorse all save post_id=""
    (tools/engage_prospect.py) and legitimately repeat per outreach. Collapsing
    those would hide cloud-executed visible actions from
    get_daily_engagement_counts_pending, which feeds the LinkedIn rate limiter
    (linkedin/rate_limiter.py), telling the client it has daily headroom it does
    not have. Duplicating a row over-counts and merely makes the limiter
    cautious; dropping one under-counts and makes it permissive, which is the
    direction the caps exist to prevent. So when identity is unknowable, insert.
    """
    row_id = str(eng.get("id") or "").strip()
    outreach_id = eng.get("outreach_id") or ""
    action_type = eng.get("action_type") or ""
    post_id = eng.get("post_id") or ""
    text = eng.get("text") or ""
    reaction_type = eng.get("reaction_type") or ""

    db = queries.get_db()
    if row_id:
        existing = db.execute(
            "SELECT id FROM engagements WHERE id = ?", (row_id,)
        ).fetchone()
    elif post_id:
        # No id to trust. Text and reaction_type stay in the key so that two
        # replies to different commenters on one post (action_type
        # 'reply_comment') both survive — they are two real actions.
        existing = db.execute(
            "SELECT id FROM engagements "
            "WHERE outreach_id = ? AND post_id = ? AND action_type = ? "
            "AND COALESCE(text, '') = ? AND COALESCE(reaction_type, '') = ?",
            (outreach_id, post_id, action_type, text, reaction_type),
        ).fetchone()
    else:
        existing = None
    db.close()
    if existing:
        logger.debug(
            "Skipping duplicate cloud engagement %s/%s on outreach %s",
            action_type, post_id, outreach_id,
        )
        return None

    # created_at comes from the backend when it ships one. Without it the row
    # keeps a now() stamp, exactly as save_engagement would have stamped it.
    created_at = _coerce_epoch(eng.get("created_at"))
    if created_at is None:
        created_at = int(time.time())

    engagement_id = row_id or str(uuid.uuid4())
    db = queries.get_db()
    try:
        db.execute(
            """INSERT INTO engagements
               (id, outreach_id, action_type, post_id, post_text, text,
                reaction_type, status, reasoning, campaign_id, account_id,
                created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (engagement_id, outreach_id, action_type, post_id,
             eng.get("post_text") or "", text, reaction_type,
             eng.get("status") or "sent", "",
             eng.get("campaign_id") or None, None, created_at),
        )
        db.commit()
    except sqlite3.IntegrityError:
        # Either the partial UNIQUE index or a racing insert of the same id.
        # Either way the row is already accounted for locally.
        db.close()
        return None
    db.close()
    return engagement_id


def _apply_rate_limits(rate_limits: dict[str, Any]) -> None:
    """Apply backend rate_limits data to local rate_limits table."""
    import datetime

    from ..db.schema import get_db

    today = datetime.date.today().isoformat()
    sent = rate_limits.get("sent_today", 0)
    accepted = rate_limits.get("accepted_today", 0)
    # The service reports "no limit" as null here and as 0 on the sibling
    # route; a plain .get default fires for neither, so both used to land in
    # the column. None now means "leave whatever is there" (see the COALESCE
    # below) rather than overwriting a real cap with a fake one.
    from .health_score import coerce_daily_limit

    _raw_limit = rate_limits.get("daily_limit")
    daily_limit = coerce_daily_limit(_raw_limit) if _raw_limit not in (None, 0) else None

    db = get_db()
    # Upsert today's rate limits with backend-authoritative values.
    # INSERT OR IGNORE so a racing get_rate_limit_today() does not raise
    # IntegrityError; the UPDATE then writes the backend numbers either way.
    import uuid
    db.execute(
        "INSERT OR IGNORE INTO rate_limits (id, date, sent, accepted, daily_limit) "
        "VALUES (?, ?, ?, ?, COALESCE(?, 15))",
        (str(uuid.uuid4()), today, sent, accepted, daily_limit),
    )
    db.execute(
        "UPDATE rate_limits SET sent = ?, accepted = ?, "
        "daily_limit = COALESCE(?, daily_limit) WHERE date = ?",
        (sent, accepted, daily_limit, today),
    )
    db.commit()
    db.close()


# ── Ensure Synced: Auto-pull before dashboard reads ──


async def ensure_synced() -> dict[str, Any]:
    """Pull changes from backend if enough time has passed since last pull.

    Called before show_status() and campaign_report() to keep local DB fresh.
    Uses last_pull_timestamp setting to avoid redundant pulls.
    Minimum interval: 30 seconds between pulls.
    """
    if not config.is_backend_mode():
        return {}

    import time

    last_pull = await run_db(queries.get_setting, "last_pull_timestamp", 0)
    if not isinstance(last_pull, (int, float)):
        last_pull = 0
    now = int(time.time())

    # Skip if pulled recently (< 30 seconds ago)
    if now - last_pull < 30:
        logger.debug("Skipping pull — last pull was %ds ago", now - last_pull)
        return {}

    try:
        return await pull_changes(int(last_pull))
    except BackendAuthError:
        raise
    except Exception as e:
        logger.warning("ensure_synced failed: %s", e)
        return {"error": str(e)}


# ── Campaign Status Sync ──


async def sync_campaign_status(
    campaign_id: str,
    status: str,
    *,
    caller: str = "user",
    reason: str = "",
) -> bool:
    """Push a single campaign status change to the backend.

    Called from pause/resume/emergency_stop to keep backend in sync.
    Returns True if sync succeeded, False otherwise (local change still applies).
    """
    if not config.is_backend_mode():
        # Direct mode has no cloud scheduler: the local scheduler reads the DB
        # directly, so there is nothing to sync and nothing has failed.
        return True

    ok, _detail = await sync_campaign_status_explained(
        campaign_id, status, caller=caller, reason=reason,
    )
    return ok


async def sync_campaign_status_explained(
    campaign_id: str,
    status: str,
    *,
    caller: str = "user",
    reason: str = "",
) -> tuple[bool, str]:
    """Like sync_campaign_status, plus the backend's refusal text.

    The bool stays the same contract. The string is the FastAPI detail on a
    refused call (a 409 on an archived row), or "" when there is nothing
    useful to show the user.
    """
    if not config.is_backend_mode():
        return True, ""

    action = "pause" if status == "paused" else "resume"
    outcome, detail = await _campaign_scope_request(
        "post", f"/api/v1/campaigns/{campaign_id}/{action}"
    )
    if outcome == "ok":
        logger.info(
            "Synced campaign %s status=%s to backend (caller=%s, reason=%s)",
            campaign_id, status, caller, reason,
        )
        return True, ""
    # not_found included, with an empty detail: campaign_control's
    # _sync_active_to_host pushes that one campaign and resumes again, which is
    # the fix for a row the backend has never seen. Nothing else retries a
    # failed call; the periodic push cannot resume a paused cloud campaign
    # (heylead-api #328), so the tools name the command that retries it.
    return False, detail


# Answers that mean the backend will not resume this campaign however often it
# is asked (409: archived, or a transition it refuses; 410: gone). Anything
# else, a network error or a 5xx included, may work on a later try.
_DEFINITIVE_REFUSAL_STATUSES = (409, 410)


async def resume_campaign_in_cloud_classified(
    campaign_id: str, *, caller: str = "user", reason: str = "",
) -> tuple[str, str]:
    """POST /campaigns/{id}/resume and say what kind of answer came back.

    Returns (kind, detail): kind is "ok", "refused" (a definitive refusal, see
    _DEFINITIVE_REFUSAL_STATUSES), "not_found" (no workspace holds the row) or
    "error" (worth retrying). detail is the backend's reason or the error.
    """
    if not config.is_backend_mode():
        return "ok", ""
    outcome, detail, status = await _campaign_scope_request_full(
        "post", f"/api/v1/campaigns/{campaign_id}/resume"
    )
    if outcome == "ok":
        logger.info(
            "Resumed campaign %s in the cloud (caller=%s, reason=%s)",
            campaign_id, caller, reason,
        )
        return "ok", ""
    if outcome == "not_found":
        return "not_found", ""
    if status in _DEFINITIVE_REFUSAL_STATUSES:
        return "refused", detail
    return "error", detail


async def sync_emergency_stop() -> bool:
    """Push emergency stop to the backend (pauses all active campaigns).

    Returns True if sync succeeded.
    """
    if not config.is_backend_mode():
        # Direct mode has no cloud scheduler: the local scheduler reads the DB
        # directly, so there is nothing to sync and nothing has failed.
        return True

    base = _base_url()
    url = f"{base}/api/v1/campaigns/emergency-stop"

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.post(url, headers=_headers())
            resp.raise_for_status()
            result = resp.json()
            logger.info(
                "Emergency stop synced to backend: %d campaigns paused",
                result.get("paused_count", 0),
            )
            return True
        except httpx.HTTPError as e:
            logger.warning("Backend emergency stop sync failed: %s", e)
            return False


# ── Hosted JSON: generic GET/POST for inspect and campaign writes ──


async def get_hosted_json(
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float | httpx.Timeout | None = None,
) -> dict[str, Any]:
    """GET {backend}{path} with JWT + org headers.

    Raises BackendAuthError on 401/403. Other HTTP errors raise
    httpx.HTTPStatusError so the caller can show a line.
    """
    return await request_hosted_json("GET", path, params=params, timeout=timeout)


async def post_hosted_json(
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    timeout: float | httpx.Timeout | None = None,
) -> dict[str, Any]:
    """POST {backend}{path} with JWT + org headers."""
    return await request_hosted_json("POST", path, json_body=json_body, timeout=timeout)


async def request_hosted_json(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: float | httpx.Timeout | None = None,
) -> dict[str, Any]:
    if not config.is_backend_mode():
        raise BackendAuthError("Backend mode not configured")
    url = f"{_base_url()}{path}"
    async with httpx.AsyncClient(timeout=timeout or _TIMEOUT) as client:
        resp = await client.request(
            method.upper(), url, params=params or {}, json=json_body,
            headers=_headers(),
        )
        if resp.status_code in (401, 403):
            raise BackendAuthError(
                "Backend returned %d — JWT expired or invalid." % resp.status_code
            )
        resp.raise_for_status()
        payload = resp.json()
        return payload if isinstance(payload, dict) else {"value": payload}


# ── Live Stats: Fetch dashboard data directly from backend ──


async def fetch_live_stats() -> dict[str, Any] | None:
    """Fetch live dashboard stats from the backend.

    Returns dict with campaigns, rate_limits, usage, engagements, hot_leads.
    Returns None if backend is unreachable or not in backend mode.
    """
    if not config.is_backend_mode():
        return None

    base = _base_url()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.get(
                f"{base}/api/v1/stats",
                headers=_headers(),
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                raise BackendAuthError(
                    "Backend returned %d — JWT expired or invalid."
                    % e.response.status_code
                )
            logger.warning("Live stats HTTP error: %s", e.response.status_code)
            return None
        except httpx.HTTPError as e:
            logger.warning("Live stats failed: %s", e)
            return None


# ── Toggle: Enable/disable cloud scheduler ──


async def toggle_cloud_scheduler(enabled: bool, *, sync_first: bool = True) -> str:
    """Enable or disable the cloud scheduler on the backend.

    If enabling: syncs local state first, then toggles.
    If disabling: just toggles (no sync needed).

    Returns a user-facing status message.
    """
    if not config.is_backend_mode():
        return "Cloud scheduling requires backend mode. Set up your profile first."

    base = _base_url()

    if enabled and sync_first:
        # Sync first, then enable
        sync_result = await sync_to_cloud()
        if "error" in sync_result:
            return f"Cloud sync failed: {sync_result['error']}\nCannot enable cloud scheduler."

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.post(
                f"{base}/api/v1/scheduler/toggle",
                json={"enabled": enabled},
                headers=_headers(),
            )
            resp.raise_for_status()
            result = resp.json()
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json().get("detail", "")
            except Exception:
                detail = e.response.text
            return f"Toggle failed: {detail}"
        except httpx.HTTPError as e:
            return f"Toggle failed: {e}"

    status = result.get("status", "unknown")
    await run_db(record_cloud_scheduler_state, status == "enabled")
    if status == "enabled":
        return (
            "Cloud scheduler **enabled**.\n\n"
            "The backend will now process outreach every 5 minutes, 24/7 "
            "even when your laptop is off:\n"
            "- Send invitations to pending prospects (autopilot campaigns)\n"
            "- Send follow-ups on schedule (day 1, 3, 7, 14)\n"
            "- Check for replies and classify sentiment\n"
            "- Engage with prospect posts (comments & reactions)\n\n"
            "All actions respect rate limits, working hours, and daily caps.\n"
            "Use `scheduler_status()` to monitor cloud activity."
        )
    else:
        return (
            "Cloud scheduler **disabled**.\n\n"
            "The backend will stop processing new outreach.\n"
            "Already-running jobs will complete.\n\n"
            "Use `toggle_scheduler(enabled=True, cloud=True)` to re-enable."
        )


async def confirm_host_will_send(campaign_id: str) -> tuple[bool, str]:
    """Re-fetch /scheduler/status and require this campaign in the snapshot.

    Launch used to treat "cloud scheduler already enabled" as success even
    when the heartbeat omitted the campaign (a customer, 10 Sep 2026).
    """
    if not campaign_id:
        return False, "no campaign id"
    try:
        status = await get_cloud_scheduler_status()
    except Exception as e:
        logger.warning("Could not confirm host will send for %s: %r", campaign_id[:8], e)
        return False, f"{e}"
    if status.get("error"):
        return False, str(status["error"])
    camps = status.get("campaigns")
    if not isinstance(camps, list):
        return False, "host heartbeat has no campaign list"
    row = next(
        (c for c in camps if isinstance(c, dict) and str(c.get("id") or "") == campaign_id),
        None,
    )
    if row is None:
        logger.warning(
            "Host heartbeat omits campaign %s after launch/resume",
            campaign_id[:8],
        )
        return False, "host heartbeat does not list this campaign"
    if "would_send" in row and not row["would_send"]:
        return False, "host would_send is false"
    return True, "host will send"


async def commission_cloud_sending(campaign_id: str = "") -> tuple[bool, str]:
    """Hand this account's sending to the backend, and report whether it took.

    Called when a user launches a campaign: launching is the opt-in to
    autonomous outreach, and outreach that stops when the lid closes is not
    what anyone opted into. Everything the toggle needs — the state push, the
    tier check, the LinkedIn connection — already happens inside
    toggle_cloud_scheduler; this only classifies the outcome so a caller can
    tell the user which machines are now sending.

    When ``campaign_id`` is set, a live status read must list that campaign
    with ``would_send`` (if the host sends the field). An already-enabled
    scheduler is not enough.

    Returns ``(commissioned, detail)``. Never raises: a launch must not fail
    because the backend was unreachable.
    """
    if not config.is_backend_mode():
        return False, "no hosted account — this install sends from this machine only"

    # Already on: a full toggle/sync of every campaign is what hung launch.
    # The campaign itself is pushed at create time, and a 404 resume retries
    # one targeted push. Periodic sync picks up the rest.
    state = await run_db(get_cloud_scheduler_state)
    if state.get("enabled"):
        if campaign_id:
            return await confirm_host_will_send(campaign_id)
        return True, "cloud scheduler already enabled"

    try:
        message = await toggle_cloud_scheduler(True, sync_first=False)
    except Exception as e:  # network, timeout, anything
        logger.warning("Could not commission cloud sending: %r", e)
        return False, f"{e}"

    # The two producers of this string live a hundred lines apart in this same
    # module, and the success one is unambiguous. A prefix check keeps the
    # caller from having to parse prose.
    if message.startswith("Cloud scheduler **enabled**"):
        if campaign_id:
            ok, detail = await confirm_host_will_send(campaign_id)
            return ok, detail if not ok else message
        return True, message
    return False, message


async def ensure_hosted_sending_default() -> tuple[bool, str]:
    """Commission cloud sending for existing hosted installs.

    ``sending_host`` defaults to cloud, but a campaign launched before that
    default existed may still have the backend scheduler off. Called on
    engine start, show_status, and setup_profile so those campaigns move
    over without each being re-launched.

    Observe is left alone: that mode means nobody sends, including the cloud.
    """
    if not config.is_backend_mode():
        return False, "not hosted"
    if config.get_sending_host() != "cloud":
        return False, "sending_host is local"
    if config.get_scheduler_mode() == "observe":
        return False, "observe"
    state = await run_db(get_cloud_scheduler_state)
    already_on = bool(state.get("enabled"))
    commissioned, detail = await commission_cloud_sending()
    if commissioned and not already_on:
        try:
            await sync_to_cloud()
        except Exception as e:
            logger.warning("Could not push campaigns after commissioning cloud: %r", e)
    return commissioned, detail


# ── Status: Get cloud scheduler status ──


async def get_cloud_scheduler_status() -> dict[str, Any]:
    """Fetch scheduler status from the backend.

    Returns dict with enabled, pending_jobs, recent_activity, campaigns, etc.
    """
    if not config.is_backend_mode():
        return {"error": "Backend mode not configured"}

    base = _base_url()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.get(
                f"{base}/api/v1/scheduler/status",
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            return {"error": f"Status check failed: HTTP {e.response.status_code}"}
        except httpx.HTTPError as e:
            return {"error": f"Status check failed: {e}"}

    # Every answer refreshes what the planner's stand-down gate reads. An
    # unanswered question deliberately records nothing — see cloud_owns_outbound.
    await run_db(record_cloud_scheduler_state, bool(data.get("enabled", False)), data)
    return data


# ── Archive / Delete Campaign Sync ──


def _scope_headers(org_id: str) -> dict[str, str]:
    """Auth headers addressed to one specific workspace ("" = default)."""
    headers = _headers()
    headers.pop("X-Org-Id", None)
    if org_id:
        headers["X-Org-Id"] = org_id
    return headers


def _response_reason(resp: Any) -> str:
    """The backend's own words for a failure, or "" — never raises.

    FastAPI puts the reason in {"detail": ...}; anything else is truncated
    body text.
    """
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("detail"):
            return str(body["detail"])[:300]
    except Exception:
        pass
    try:
        return str(getattr(resp, "text", "") or "")[:200]
    except Exception:
        return ""


async def _campaign_scope_request(method: str, path: str) -> tuple[str, str]:
    """_campaign_scope_request_full without the status code."""
    outcome, detail, _status = await _campaign_scope_request_full(method, path)
    return outcome, detail


async def _campaign_scope_request_full(
    method: str, path: str,
) -> tuple[str, str, int | None]:
    """Issue a per-campaign backend request, following the campaign across
    workspaces on 404.

    Campaigns stay in the workspace they were pushed to, but every request
    here is addressed by the *current* active_org_id. Switch workspaces
    between the push and the pause/archive/delete and the op 404s while the
    cloud scheduler keeps sending from the row — 28 campaigns sat on the
    hosted dashboard frozen "active" this way (24 Aug 2026). On 404 the
    account's other workspaces are tried, the default (no X-Org-Id) one first
    since pre-org pushes all landed there.

    Returns ("ok"|"not_found"|"error", reason, status). reason is the
    backend's own words on a refused call, or "". status is the HTTP status of
    the answer that decided an "error" (None when no answer came back), so a
    caller can tell a refusal (409) from a failure worth retrying.
    """
    url = f"{_base_url()}{path}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        send = getattr(client, method)
        try:
            resp = await send(url, headers=_headers())
        except httpx.HTTPError as e:
            logger.warning("Backend %s %s failed: %s", method.upper(), path, e)
            return "error", str(e), None
        if resp.status_code < 400:
            return "ok", "", resp.status_code
        if resp.status_code != 404:
            # Say why. A 409 from /resume now means the backend refused the
            # transition — "archived; unarchive it first" — and a bare status
            # code in the daemon log cost a morning of log archaeology on
            # 7 Sep 2026 (heylead-api: an archived campaign resumed by a
            # client that could not have known).
            detail = _response_reason(resp)
            logger.warning(
                "Backend %s %s failed: %s %s",
                method.upper(), path, resp.status_code, detail,
            )
            return "error", detail, resp.status_code

        try:
            orgs_resp = await client.get(
                f"{_base_url()}/api/v1/orgs", headers=_scope_headers("")
            )
            org_ids = [
                str(o.get("id") or "")
                for o in (orgs_resp.json().get("orgs") or [])
            ] if orgs_resp.status_code < 400 else None
        except (httpx.HTTPError, ValueError):
            org_ids = None
        if org_ids is None:
            # Could not enumerate workspaces: 404 in the active one proves
            # nothing about the others, so don't claim the row is gone.
            logger.warning(
                "Backend %s %s: 404 in the active workspace and the org list "
                "is unreachable — cannot locate the campaign", method.upper(), path,
            )
            return "error", "", None

        active = config.get_active_org_id()
        saw_error = False
        last_detail = ""
        last_status: int | None = None
        for scope in [""] + [i for i in org_ids if i]:
            if scope == active:
                continue
            try:
                resp = await send(url, headers=_scope_headers(scope))
            except httpx.HTTPError:
                saw_error = True
                continue
            if resp.status_code < 400:
                logger.info(
                    "Backend %s %s landed in workspace %s (active was %s)",
                    method.upper(), path, scope or "default", active or "default",
                )
                return "ok", "", resp.status_code
            if resp.status_code != 404:
                last_detail = _response_reason(resp)
                last_status = resp.status_code
                logger.warning(
                    "Backend %s %s failed in workspace %s: %s %s",
                    method.upper(), path, scope or "default", resp.status_code,
                    last_detail,
                )
                saw_error = True
        if saw_error:
            logger.warning(
                "Backend %s %s failed in every reachable workspace",
                method.upper(), path,
            )
            return "error", last_detail, last_status
        return "not_found", "", 404


async def sync_campaign_archive(campaign_id: str) -> bool:
    """Push a campaign archive to the backend.

    Returns True if sync succeeded.
    """
    if not config.is_backend_mode():
        return False

    outcome, _detail = await _campaign_scope_request(
        "post", f"/api/v1/campaigns/{campaign_id}/archive"
    )
    if outcome == "ok":
        logger.info("Synced campaign %s archive to backend", campaign_id)
        return True
    return False


# One entry per campaign whose backend delete has not landed yet:
# {campaign_id: {"first_failed_ts", "last_failed_ts", "attempts"}}. The local
# rows are already gone when the delete sync runs, so a failure here is the
# LAST chance to learn the campaign id — without this record an unreachable
# backend meant a cloud row sending forever with nothing left to point at it.
_DELETE_TOMBSTONES_SETTING = "cloud_delete_pending"


def _record_delete_tombstone(campaign_id: str) -> None:
    stored = queries.get_setting(_DELETE_TOMBSTONES_SETTING, {}) or {}
    now = int(time.time())
    row = stored.get(campaign_id) or {"first_failed_ts": now, "attempts": 0}
    row["attempts"] = int(row.get("attempts", 0)) + 1
    row["last_failed_ts"] = now
    stored[campaign_id] = row
    queries.save_setting(_DELETE_TOMBSTONES_SETTING, stored)


def _clear_delete_tombstone(campaign_id: str) -> None:
    stored = queries.get_setting(_DELETE_TOMBSTONES_SETTING, {}) or {}
    if campaign_id in stored:
        stored.pop(campaign_id)
        queries.save_setting(_DELETE_TOMBSTONES_SETTING, stored)


async def retry_pending_cloud_deletes() -> int:
    """Re-issue backend deletes that failed earlier. Returns how many landed.

    Called from the periodic sync tick; a no-op when nothing is pending.
    Success and "gone from every workspace" both clear the record; anything
    else re-records and waits for the next tick.
    """
    if not config.is_backend_mode():
        return 0
    stored = await run_db(queries.get_setting, _DELETE_TOMBSTONES_SETTING, {}) or {}
    cleared = 0
    for campaign_id in list(stored):
        if await sync_campaign_delete(campaign_id):
            cleared += 1
    return cleared


async def sync_campaign_delete(campaign_id: str) -> bool:
    """Push a campaign deletion to the backend.

    Returns True if the backend no longer holds the campaign — deleted now,
    or already absent from every workspace (a draft that was never pushed).
    A failure records a tombstone that retry_pending_cloud_deletes() replays.
    """
    if not config.is_backend_mode():
        return False

    outcome, _detail = await _campaign_scope_request(
        "delete", f"/api/v1/campaigns/{campaign_id}"
    )
    if outcome in ("ok", "not_found"):
        if outcome == "ok":
            logger.info("Synced campaign %s deletion to backend", campaign_id)
        await run_db(_clear_delete_tombstone, campaign_id)
        return True
    await run_db(_record_delete_tombstone, campaign_id)
    return False


# ── Prospect / Outreach Sync ──


async def sync_outreach_skip(
    outreach_id: str,
    reason: str = "",
    reason_code: str = "",
    reason_note: str = "",
) -> bool:
    """Push an outreach skip to the backend.

    The structured pair is what the backend's learning loop reads; ``reason``
    stays for the human-readable note. Until 9 Sep 2026 the backend accepted
    ``reason`` and never read it, and skip_prospect called this without one
    at all, so a skip carried no information anywhere.

    Returns True if sync succeeded.
    """
    if not config.is_backend_mode():
        return False

    base = _base_url()
    url = f"{base}/api/v1/prospects/{outreach_id}/skip"
    payload = {
        "reason": reason,
        "reason_code": reason_code,
        "reason_note": reason_note,
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.post(
                url, json=payload, headers=_headers(),
            )
            resp.raise_for_status()
            logger.info("Synced outreach %s skip to backend", outreach_id)
            return True
        except httpx.HTTPError as e:
            logger.warning("Backend skip sync failed for %s: %s", outreach_id, e)
            return False


async def sync_outreach_close(
    outreach_id: str,
    outcome: str,
    reason: str = "",
    meeting_link: str = "",
    reason_code: str = "",
    reason_note: str = "",
) -> bool:
    """Push an outreach close to the backend.

    Returns True if sync succeeded.
    """
    if not config.is_backend_mode():
        return False

    base = _base_url()
    url = f"{base}/api/v1/prospects/{outreach_id}/close"
    payload: dict[str, str] = {"outcome": outcome}
    if reason:
        payload["reason"] = reason
    if meeting_link:
        payload["meeting_link"] = meeting_link
    # An opt_out close is the dashboard's Stop. The code is what the backend
    # can learn from; the sentence is what a human reads.
    if reason_code:
        payload["reason_code"] = reason_code
    if reason_note:
        payload["reason_note"] = reason_note

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.post(url, json=payload, headers=_headers())
            resp.raise_for_status()
            logger.info("Synced outreach %s close (outcome=%s) to backend", outreach_id, outcome)
            return True
        except httpx.HTTPError as e:
            logger.warning("Backend close sync failed for %s: %s", outreach_id, e)
            return False


# ── Campaign Settings Sync ──


async def sync_campaign_settings(campaign_id: str, settings: dict) -> bool:
    """Push campaign settings changes to the backend.

    Returns True if sync succeeded.
    """
    if not config.is_backend_mode():
        return False

    base = _base_url()
    url = f"{base}/api/v1/campaigns/{campaign_id}/settings"

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.patch(url, json=settings, headers=_headers())
            resp.raise_for_status()
            logger.info("Synced campaign %s settings to backend: %s", campaign_id, list(settings.keys()))
            return True
        except httpx.HTTPError as e:
            logger.warning("Backend settings sync failed for %s: %s", campaign_id, e)
            return False
