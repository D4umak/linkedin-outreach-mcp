"""SQLite CRUD helpers for the Communication Strategist (daily action plans)."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .schema import get_db


def _today_str() -> str:
    """Return today's date as YYYY-MM-DD string (UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _yesterday_str() -> str:
    """Return yesterday's date as YYYY-MM-DD string (UTC)."""
    yesterday = int(time.time()) - 86400
    return datetime.fromtimestamp(yesterday, tz=timezone.utc).strftime("%Y-%m-%d")


# ──────────────────────────────────────────────
# CRUD
# ──────────────────────────────────────────────

def save_daily_plan(
    outreach_id: str,
    campaign_id: str,
    plan_date: str,
    planned_actions: list[dict],
    plan_source: str = "llm",
) -> str:
    """Create or replace a daily plan for a prospect. Returns plan ID."""
    plan_id = uuid.uuid4().hex[:12]
    now = int(time.time())
    db = get_db()
    db.execute(
        """INSERT OR REPLACE INTO prospect_daily_plans
           (id, outreach_id, campaign_id, plan_date, planned_actions,
            executed_actions, plan_source, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, '[]', ?, ?, ?)""",
        (
            plan_id, outreach_id, campaign_id, plan_date,
            json.dumps(planned_actions), plan_source, now, now,
        ),
    )
    db.commit()
    db.close()
    return plan_id


def get_daily_plan(outreach_id: str, plan_date: str = "") -> dict[str, Any] | None:
    """Get today's plan for an outreach. Returns None if no plan exists."""
    if not plan_date:
        plan_date = _today_str()
    db = get_db()
    row = db.execute(
        "SELECT * FROM prospect_daily_plans WHERE outreach_id = ? AND plan_date = ?",
        (outreach_id, plan_date),
    ).fetchone()
    db.close()
    if not row:
        return None
    result = dict(row)
    result["planned_actions"] = json.loads(result.get("planned_actions") or "[]")
    result["executed_actions"] = json.loads(result.get("executed_actions") or "[]")
    return result


def get_campaign_daily_plans(campaign_id: str, plan_date: str = "") -> list[dict]:
    """Get all daily plans for a campaign on a given date."""
    if not plan_date:
        plan_date = _today_str()
    db = get_db()
    rows = db.execute(
        """SELECT p.*, o.status as outreach_status, c.timezone as prospect_timezone
           FROM prospect_daily_plans p
           JOIN outreaches o ON p.outreach_id = o.id
           LEFT JOIN contacts c ON o.contact_id = c.id
           WHERE p.campaign_id = ? AND p.plan_date = ?""",
        (campaign_id, plan_date),
    ).fetchall()
    db.close()
    plans = []
    for r in rows:
        d = dict(r)
        d["planned_actions"] = json.loads(d.get("planned_actions") or "[]")
        d["executed_actions"] = json.loads(d.get("executed_actions") or "[]")
        plans.append(d)
    return plans


# Plan entries that the scheduler executes under a different job label.
# A planned "voice_memo" runs as a followup job; the executor only knows
# "followup", so without this mapping the voice_memo entry was never marked
# executed and execute_planned_actions kept re-queueing it every 15 min.
_PLAN_ALIASES: dict[str, tuple[str, ...]] = {
    "followup": ("followup", "voice_memo"),
}


def resolve_plan_action_type(plan: dict[str, Any] | None, action_type: str) -> str:
    """Return the planned entry that a reported action satisfies.

    "followup" resolves to "voice_memo" when the plan asked for a voice memo
    that has not been executed or skipped yet; otherwise the label is returned
    unchanged.
    """
    if not plan:
        return action_type
    candidates = _PLAN_ALIASES.get(action_type)
    if not candidates:
        return action_type
    done = {e.get("action_type") for e in plan.get("executed_actions", [])}
    planned = [a.get("action_type") for a in plan.get("planned_actions", [])]
    for cand in candidates:
        if cand in planned and cand not in done:
            return cand
    return action_type


def get_planned_followup_format(outreach_id: str, plan_date: str = "") -> str | None:
    """What format today's plan wants the next follow-up in, or None.

    "voice" when an unexecuted voice_memo is planned, "text" when a plain
    followup is, None when the plan has no opinion (no plan, or already done).
    """
    plan = get_daily_plan(outreach_id, plan_date)
    if not plan:
        return None
    done = {e.get("action_type") for e in plan.get("executed_actions", [])}
    for a in plan.get("planned_actions", []):
        t = a.get("action_type")
        if t == "voice_memo" and t not in done:
            return "voice"
        if t == "followup" and t not in done:
            return "text"
    return None


def update_planned_actions(
    outreach_id: str,
    planned_actions: list[dict],
    plan_date: str = "",
) -> None:
    """Replace planned_actions only. Does not touch executed_actions."""
    plan = get_daily_plan(outreach_id, plan_date)
    if not plan:
        return
    db = get_db()
    db.execute(
        "UPDATE prospect_daily_plans SET planned_actions = ?, updated_at = ? WHERE id = ?",
        (json.dumps(planned_actions), int(time.time()), plan["id"]),
    )
    db.commit()
    db.close()


def has_replan_today(outreach_id: str, plan_date: str = "") -> bool:
    """True when a strategist_replan_decision was already logged today."""
    if not plan_date:
        plan_date = _today_str()
    start = datetime.strptime(plan_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    db = get_db()
    row = db.execute(
        """SELECT 1 FROM actions_log
           WHERE action_type = 'strategist_replan_decision'
             AND outreach_id = ?
             AND timestamp >= ?
           LIMIT 1""",
        (outreach_id, int(start.timestamp())),
    ).fetchone()
    db.close()
    return row is not None


def mark_action_executed(
    outreach_id: str,
    plan_date: str,
    action_type: str,
    result: str = "",
    job_id: str = "",
) -> None:
    """Append an executed action to a plan's executed_actions."""
    plan = get_daily_plan(outreach_id, plan_date)
    if not plan:
        return
    executed = plan["executed_actions"]
    action_type = resolve_plan_action_type(plan, action_type)
    entry: dict[str, Any] = {
        "action_type": action_type,
        "executed_at": int(time.time()),
        "result": result[:200],
        "job_id": job_id,
        "plan_created_at": plan.get("created_at", 0),
    }
    if action_type in ("followup", "voice_memo"):
        entry["format"] = "voice" if action_type == "voice_memo" else "text"
    executed.append(entry)
    db = get_db()
    db.execute(
        "UPDATE prospect_daily_plans SET executed_actions = ?, updated_at = ? WHERE id = ?",
        (json.dumps(executed), int(time.time()), plan["id"]),
    )
    db.commit()
    db.close()


def mark_action_skipped(
    outreach_id: str,
    plan_date: str,
    action_type: str,
    skip_reason: str,
) -> None:
    """Append a skipped action with reason to a plan's executed_actions."""
    plan = get_daily_plan(outreach_id, plan_date)
    if not plan:
        return
    executed = plan["executed_actions"]
    executed.append({
        "action_type": resolve_plan_action_type(plan, action_type),
        "status": "skipped",
        "skip_reason": skip_reason,
        "skipped_at": int(time.time()),
    })
    db = get_db()
    db.execute(
        "UPDATE prospect_daily_plans SET executed_actions = ?, updated_at = ? WHERE id = ?",
        (json.dumps(executed), int(time.time()), plan["id"]),
    )
    db.commit()
    db.close()


# ──────────────────────────────────────────────
# Planning Queries
# ──────────────────────────────────────────────

def get_prospects_needing_plans(campaign_id: str) -> list[dict]:
    """Get active outreaches that don't have a plan for today.

    Returns outreaches in statuses: pending, invited, connected, messaged, replied
    that have no prospect_daily_plans row for today's date.
    Joins contacts for profile data and engagement counts.

    Ordered least-recently-planned first (never planned at the very top), then
    by fit score. The caller caps the list at STRATEGIST_MAX_PROSPECTS, and a
    pure fit-score order meant the same top slice was planned every day while
    the rest of a large campaign was never reached.
    """
    today = _today_str()
    db = get_db()
    rows = db.execute(
        """SELECT o.id as outreach_id, o.campaign_id, o.status, o.followup_count,
               o.invited_at, o.accepted_at, o.first_reply_at, o.memory_json,
               c.id as contact_id, c.name, c.title, c.company, c.linkedin_url, c.linkedin_id,
               c.fit_score, c.profile_json, c.analysis_json, c.timezone as prospect_timezone,
               (SELECT cn.location FROM connections cn
                  WHERE cn.provider_id = json_extract(
                      CASE WHEN json_valid(c.profile_json) THEN c.profile_json ELSE '{}' END,
                      '$.provider_id')
                  LIMIT 1) as connection_location,
               (SELECT g.location FROM global_contacts g
                  WHERE g.linkedin_id = c.linkedin_id LIMIT 1) as directory_location,
               (SELECT COUNT(*) FROM engagements e WHERE e.outreach_id = o.id) as engagement_count,
               (SELECT GROUP_CONCAT(e.action_type) FROM engagements e WHERE e.outreach_id = o.id) as engagement_types,
               (SELECT COUNT(*) FROM messages m WHERE m.outreach_id = o.id) as message_count,
               (SELECT MAX(m.timestamp) FROM messages m WHERE m.outreach_id = o.id) as last_message_at,
               EXISTS (
                   SELECT 1 FROM connections cn
                   WHERE cn.provider_id = json_extract(
                       CASE WHEN json_valid(c.profile_json) THEN c.profile_json ELSE '{}' END,
                       '$.provider_id'
                   )
               ) as is_first_degree
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           WHERE o.campaign_id = ?
             AND o.status IN ('pending', 'invited', 'connected', 'messaged', 'replied')
             AND NOT EXISTS (
                 SELECT 1 FROM prospect_daily_plans p
                 WHERE p.outreach_id = o.id AND p.plan_date = ?
             )
           ORDER BY COALESCE(
                 (SELECT MAX(p2.plan_date) FROM prospect_daily_plans p2
                  WHERE p2.outreach_id = o.id), ''
             ) ASC,
             c.fit_score DESC""",
        (campaign_id, today),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def set_contact_timezone(contact_id: str, tz_name: str) -> None:
    """Persist an inferred timezone on the contact (idempotent)."""
    db = get_db()
    db.execute(
        "UPDATE contacts SET timezone = ?, updated_at = ? WHERE id = ?",
        (tz_name, int(time.time()), contact_id),
    )
    db.commit()
    db.close()


def has_daily_plan(outreach_id: str) -> bool:
    """Check if an outreach has a daily plan for today. Fails open (False on error)."""
    try:
        today = _today_str()
        db = get_db()
        row = db.execute(
            "SELECT 1 FROM prospect_daily_plans WHERE outreach_id = ? AND plan_date = ?",
            (outreach_id, today),
        ).fetchone()
        db.close()
        return row is not None
    except Exception:
        return False


# ──────────────────────────────────────────────
# Feedback Loop
# ──────────────────────────────────────────────

def score_yesterday_plans() -> int:
    """Score every still-unscored plan from before today.

    A missed daily-strategy tick used to leave that UTC day NULL forever
    because the SELECT was exact-equality on yesterday only.

    Scoring:
      replied → +3, accepted invite → +2, profile_viewed_back → +1,
      no change → 0, declined/ignored → -1

    Attribution: a reply or acceptance only credits plans that actually
    executed an action in the STRATEGIST_ATTRIBUTION_DAYS before it.
    Previously every unscored plan dated on or before the reply scored +3,
    including skip_today days, so the LLM was taught that doing nothing
    produces replies.
    """
    from ..constants import (
        STRATEGIST_ATTRIBUTION_DAYS,
        STRATEGIST_SCORE_REPLIED,
        STRATEGIST_SCORE_ACCEPTED,
        STRATEGIST_SCORE_NO_RESPONSE,
        STRATEGIST_SCORE_DECLINED,
    )

    today = _today_str()
    db = get_db()
    plans = db.execute(
        """SELECT p.id, p.outreach_id, p.plan_date, p.planned_actions, p.executed_actions,
               o.status as current_status, o.first_reply_at, o.accepted_at
           FROM prospect_daily_plans p
           JOIN outreaches o ON p.outreach_id = o.id
           WHERE p.plan_date < ? AND p.feedback_score IS NULL""",
        (today,)
    ).fetchall()

    scored = 0
    now = int(time.time())
    window = STRATEGIST_ATTRIBUTION_DAYS * 86400

    for plan in plans:
        plan = dict(plan)
        status = plan.get("current_status", "")
        reply_at = plan.get("first_reply_at") or 0
        accept_at = plan.get("accepted_at") or 0
        try:
            plan_start = int(
                datetime.strptime(plan["plan_date"], "%Y-%m-%d")
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
        except (TypeError, ValueError):
            plan_start = now - 86400

        acted_at = _last_real_execution(plan.get("executed_actions"), plan_start)

        def _caused(outcome_at: int) -> bool:
            # The plan acted, and the outcome landed within the window after it.
            return bool(outcome_at) and acted_at is not None and acted_at <= outcome_at < acted_at + window

        if _caused(reply_at):
            score = STRATEGIST_SCORE_REPLIED
        elif _caused(accept_at):
            score = STRATEGIST_SCORE_ACCEPTED
        elif status in ("closed_unhappy", "opted_out", "skipped"):
            score = STRATEGIST_SCORE_DECLINED
        else:
            score = STRATEGIST_SCORE_NO_RESPONSE

        db.execute(
            "UPDATE prospect_daily_plans SET feedback_score = ?, updated_at = ? WHERE id = ?",
            (score, now, plan["id"]),
        )
        scored += 1

    db.commit()
    db.close()
    return scored


def _last_real_execution(executed_json: Any, plan_start: int) -> int | None:
    """Epoch of the last successfully executed (not skipped) action, or None."""
    try:
        executed = json.loads(executed_json or "[]") if isinstance(executed_json, str) else (executed_json or [])
    except (TypeError, ValueError):
        return None
    stamps = [
        int(e.get("executed_at") or plan_start)
        for e in executed
        if isinstance(e, dict) and e.get("status") != "skipped"
        and e.get("action_type") not in (None, "skip_today")
    ]
    return max(stamps) if stamps else None


def get_feedback_data(
    days: int = 7, top_n: int = 10, campaign_id: str = "",
) -> dict[str, Any]:
    """Top N best and worst plans from the last N days for LLM feedback.

    Scoped to ``campaign_id`` when given and it has scored plans; otherwise
    falls back to every campaign (``scope`` says which). Also carries the
    campaign's voice-vs-text reply rates so the planner can see whether
    voice memos are earning their place.
    """
    cutoff = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_ts = int(time.time()) - (days * 86400)
    start_date = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d")

    db = get_db()

    def _query(scoped: bool) -> tuple[list, list]:
        where = "p.plan_date >= ? AND p.plan_date < ? AND p.feedback_score IS NOT NULL"
        params: list[Any] = [start_date, cutoff]
        if scoped:
            where += " AND p.campaign_id = ?"
            params.append(campaign_id)
        base = f"""SELECT p.planned_actions, p.executed_actions, p.feedback_score,
                   c.title, c.company, o.status
               FROM prospect_daily_plans p
               JOIN outreaches o ON p.outreach_id = o.id
               JOIN contacts c ON o.contact_id = c.id
               WHERE {where}
               ORDER BY p.feedback_score {{dir}}
               LIMIT ?"""
        best = db.execute(base.format(dir="DESC"), (*params, top_n)).fetchall()
        worst = db.execute(base.format(dir="ASC"), (*params, top_n)).fetchall()
        return best, worst

    scope = "global"
    best: list = []
    worst: list = []
    if campaign_id:
        best, worst = _query(scoped=True)
        if best or worst:
            scope = "campaign"
    if scope == "global":
        best, worst = _query(scoped=False)
    db.close()

    def _to_dict(rows: list) -> list[dict]:
        out = []
        for r in rows:
            d = dict(r)
            d["planned_actions"] = json.loads(d.get("planned_actions") or "[]")
            d["executed_actions"] = json.loads(d.get("executed_actions") or "[]")
            out.append(d)
        return out

    voice_stats: dict[str, Any] = {}
    try:
        from .queries import get_voice_memo_stats
        voice_stats = get_voice_memo_stats(campaign_id) or {}
    except Exception:
        voice_stats = {}

    return {
        "best": _to_dict(best),
        "worst": _to_dict(worst),
        "scope": scope,
        "voice_stats": voice_stats,
    }

def get_plan_quality_metrics(campaign_id: str = "", days: int = 7) -> dict:
    """Compute plan quality metrics over a time window.

    Returns: planned_count, executed_count, skipped_count, adoption_rate,
    unique_action_types, plan_diversity_score, llm_vs_fallback, avg_feedback_score,
    top_skip_reasons.
    """
    import math
    from collections import Counter
    from datetime import date, timedelta

    start_date = (date.today() - timedelta(days=days)).isoformat()
    db = get_db()
    sql = """SELECT planned_actions, executed_actions, plan_source, feedback_score
             FROM prospect_daily_plans
             WHERE plan_date >= ?"""
    params: list = [start_date]
    if campaign_id:
        sql += " AND campaign_id = ?"
        params.append(campaign_id)
    rows = db.execute(sql, params).fetchall()
    db.close()

    planned_count = 0
    executed_count = 0
    skipped_count = 0
    action_type_counter: Counter = Counter()
    skip_reasons: Counter = Counter()
    llm_count = 0
    fallback_count = 0
    feedback_scores: list[float] = []

    for r in rows:
        planned = json.loads(r["planned_actions"] or "[]")
        executed = json.loads(r["executed_actions"] or "[]")
        source = r["plan_source"] or "llm"
        score = r["feedback_score"]

        if source == "llm":
            llm_count += 1
        else:
            fallback_count += 1
        if score is not None:
            feedback_scores.append(score)

        for a in planned:
            at = a.get("action_type", "unknown")
            if at != "skip_today":
                planned_count += 1
                action_type_counter[at] += 1

        for e in executed:
            if e.get("status") == "skipped":
                skipped_count += 1
                reason = e.get("skip_reason", "unknown")
                skip_reasons[reason] += 1
            else:
                executed_count += 1

    # Shannon entropy for plan diversity (normalized 0-1)
    total_actions = sum(action_type_counter.values())
    diversity = 0.0
    if total_actions > 0 and len(action_type_counter) > 1:
        entropy = 0.0
        for count in action_type_counter.values():
            p = count / total_actions
            if p > 0:
                entropy -= p * math.log2(p)
        max_entropy = math.log2(len(action_type_counter))
        diversity = round(entropy / max_entropy, 2) if max_entropy > 0 else 0.0

    adoption_rate = round(executed_count / planned_count, 3) if planned_count else 0.0

    return {
        "planned_count": planned_count,
        "executed_count": executed_count,
        "skipped_count": skipped_count,
        "adoption_rate": adoption_rate,
        "unique_action_types": len(action_type_counter),
        "plan_diversity_score": diversity,
        "llm_vs_fallback": {"llm": llm_count, "fallback": fallback_count},
        "avg_feedback_score": (
            round(sum(feedback_scores) / len(feedback_scores), 2) if feedback_scores else None
        ),
        "top_skip_reasons": skip_reasons.most_common(5),
    }


# ──────────────────────────────────────────────
# End-to-End Latency Metrics
# ──────────────────────────────────────────────

def get_e2e_latency_metrics(campaign_id: str = "", hours: int = 24) -> dict:
    """Compute end-to-end latency from plan creation to job completion.

    Joins prospect_daily_plans → scheduler_jobs via job_id in executed_actions.
    Returns avg/p50/p95 for plan_to_job, job_execution, total stages.
    """
    from datetime import date, timedelta

    since_date = (date.today() - timedelta(hours=hours)).isoformat() if hours > 24 else _today_str()
    db = get_db()
    sql = """SELECT executed_actions, created_at as plan_created_at
             FROM prospect_daily_plans WHERE plan_date >= ?"""
    params: list = [since_date]
    if campaign_id:
        sql += " AND campaign_id = ?"
        params.append(campaign_id)
    rows = db.execute(sql, params).fetchall()

    plan_to_job: list[int] = []
    job_execution: list[int] = []
    total_e2e: list[int] = []

    for r in rows:
        executed = json.loads(r["executed_actions"] or "[]")
        plan_ts = r["plan_created_at"] or 0

        for entry in executed:
            if entry.get("status") == "skipped":
                continue
            job_id = entry.get("job_id", "")
            executed_at = entry.get("executed_at", 0)
            plan_created = entry.get("plan_created_at", plan_ts)

            if not job_id:
                continue

            # Fetch job timing
            job = db.execute(
                "SELECT started_at, completed_at, duration_ms FROM scheduler_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if not job:
                continue

            started = job["started_at"] or 0
            completed = job["completed_at"] or 0
            dur_ms = job["duration_ms"] or 0

            if plan_created and started:
                plan_to_job.append((started - plan_created) * 1000)
            if dur_ms:
                job_execution.append(dur_ms)
            if plan_created and completed:
                total_e2e.append((completed - plan_created) * 1000)

    db.close()

    def _stats(values: list[int]) -> dict:
        if not values:
            return {"avg_ms": None, "p50_ms": None, "p95_ms": None, "count": 0}
        values.sort()
        n = len(values)
        return {
            "avg_ms": round(sum(values) / n),
            "p50_ms": values[n // 2],
            "p95_ms": values[int(n * 0.95)] if n >= 20 else values[-1],
            "count": n,
        }

    return {
        "plan_to_job": _stats(plan_to_job),
        "job_execution": _stats(job_execution),
        "total_e2e": _stats(total_e2e),
    }
