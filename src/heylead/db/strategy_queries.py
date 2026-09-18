"""SQLite CRUD helpers for the autonomous strategy engine."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .schema import get_db


# ──────────────────────────────────────────────
# Strategy Patterns
# ──────────────────────────────────────────────

def save_strategy_pattern(
    pattern_type: str,
    pattern_key: str,
    evidence_json: str,
    *,
    description: str | None = None,
    confidence: float = 0.0,
    estimated_revenue_impact: float = 0.0,
    acceptance_rate: float | None = None,
    reply_rate: float | None = None,
    conversion_rate: float | None = None,
    sample_size: int = 0,
) -> str:
    """Save a new strategy pattern. Returns the pattern ID."""
    pattern_id = uuid.uuid4().hex[:12]
    now = int(time.time())
    db = get_db()
    db.execute(
        """INSERT INTO strategy_patterns
           (id, pattern_type, pattern_key, description, evidence_json,
            confidence, estimated_revenue_impact, acceptance_rate, reply_rate,
            conversion_rate, sample_size, status, discovered_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
        (
            pattern_id, pattern_type, pattern_key, description, evidence_json,
            confidence, estimated_revenue_impact, acceptance_rate, reply_rate,
            conversion_rate, sample_size, now, now,
        ),
    )
    db.commit()
    db.close()
    return pattern_id


def get_strategy_pattern(pattern_id: str) -> dict[str, Any] | None:
    """Get a single strategy pattern by ID."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM strategy_patterns WHERE id = ?", (pattern_id,)
    ).fetchone()
    db.close()
    return dict(row) if row else None


def list_strategy_patterns(
    pattern_type: str = "",
    status: str = "active",
    min_confidence: float = 0.0,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List strategy patterns with optional filters."""
    db = get_db()
    sql = "SELECT * FROM strategy_patterns WHERE 1=1"
    params: list[Any] = []
    if pattern_type:
        sql += " AND pattern_type = ?"
        params.append(pattern_type)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if min_confidence > 0:
        sql += " AND confidence >= ?"
        params.append(min_confidence)
    sql += " ORDER BY estimated_revenue_impact DESC LIMIT ?"
    params.append(limit)
    rows = db.execute(sql, params).fetchall()
    db.close()
    return [dict(r) for r in rows]


def update_strategy_pattern(pattern_id: str, **kwargs: Any) -> None:
    """Update fields on a strategy pattern."""
    if not kwargs:
        return
    db = get_db()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [pattern_id]
    db.execute(f"UPDATE strategy_patterns SET {sets} WHERE id = ?", vals)
    db.commit()
    db.close()


def upsert_strategy_pattern(
    pattern_type: str,
    pattern_key: str,
    evidence_json: str,
    **kwargs: Any,
) -> str:
    """Insert or update a pattern by type + key. Returns pattern ID."""
    db = get_db()
    row = db.execute(
        "SELECT id FROM strategy_patterns WHERE pattern_type = ? AND pattern_key = ? AND status = 'active'",
        (pattern_type, pattern_key),
    ).fetchone()
    db.close()

    if row:
        update_strategy_pattern(
            row["id"],
            evidence_json=evidence_json,
            last_validated=int(time.time()),
            **kwargs,
        )
        return row["id"]
    return save_strategy_pattern(pattern_type, pattern_key, evidence_json, **kwargs)


# ──────────────────────────────────────────────
# Strategy Actions
# ──────────────────────────────────────────────

def save_strategy_action(
    action_type: str,
    details_json: str,
    *,
    campaign_id: str | None = None,
    pattern_id: str | None = None,
) -> str:
    """Save a new strategy action. Returns the action ID."""
    action_id = uuid.uuid4().hex[:12]
    now = int(time.time())
    db = get_db()
    db.execute(
        """INSERT INTO strategy_actions
           (id, action_type, campaign_id, pattern_id, details_json, status, created_at)
           VALUES (?, ?, ?, ?, ?, 'applied', ?)""",
        (action_id, action_type, campaign_id, pattern_id, details_json, now),
    )
    db.commit()
    db.close()
    return action_id


def list_strategy_actions(
    campaign_id: str = "",
    action_type: str = "",
    status: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List strategy actions with optional filters."""
    db = get_db()
    sql = "SELECT * FROM strategy_actions WHERE 1=1"
    params: list[Any] = []
    if campaign_id:
        sql += " AND campaign_id = ?"
        params.append(campaign_id)
    if action_type:
        sql += " AND action_type = ?"
        params.append(action_type)
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = db.execute(sql, params).fetchall()
    db.close()
    return [dict(r) for r in rows]


def update_strategy_action(action_id: str, **kwargs: Any) -> None:
    """Update fields on a strategy action."""
    if not kwargs:
        return
    db = get_db()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [action_id]
    db.execute(f"UPDATE strategy_actions SET {sets} WHERE id = ?", vals)
    db.commit()
    db.close()


def get_unmeasured_actions(max_age_hours: int = 48) -> list[dict[str, Any]]:
    """Get applied actions that haven't been measured yet (for feedback loop)."""
    db = get_db()
    cutoff = int(time.time()) - (max_age_hours * 3600)
    rows = db.execute(
        """SELECT * FROM strategy_actions
           WHERE status = 'applied' AND created_at < ?
           ORDER BY created_at ASC""",
        (cutoff,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────
# Cross-Campaign Analytical Queries
# ──────────────────────────────────────────────

def get_segment_performance() -> list[dict[str, Any]]:
    """Aggregate funnel performance by industry + seniority across all campaigns.

    Groups contacts by their company industry and title seniority,
    then calculates acceptance, reply, and conversion rates per segment.
    Also computes average estimated_revenue per segment.
    """
    db = get_db()
    rows = db.execute("""
        SELECT
            c.company AS segment_company,
            c.title AS segment_title,
            COUNT(DISTINCT o.id) AS total,
            SUM(CASE WHEN o.status IN ('invited','withdrawn','connected','messaged','replied',
                'hot_lead','closed_happy','closed_unhappy','opted_out') THEN 1 ELSE 0 END) AS invited,
            SUM(CASE WHEN o.status IN ('connected','messaged','replied',
                'hot_lead','closed_happy','closed_unhappy','opted_out') THEN 1 ELSE 0 END) AS connected,
            SUM(CASE WHEN o.status IN ('replied','hot_lead',
                'closed_happy','closed_unhappy') THEN 1 ELSE 0 END) AS replied,
            SUM(CASE WHEN o.status = 'hot_lead' THEN 1 ELSE 0 END) AS hot_leads,
            SUM(CASE WHEN o.status = 'closed_happy' THEN 1 ELSE 0 END) AS won,
            SUM(CASE WHEN o.status = 'closed_unhappy' THEN 1 ELSE 0 END) AS lost,
            AVG(c.estimated_revenue) AS avg_revenue,
            SUM(c.estimated_revenue) AS total_revenue,
            AVG(c.fit_score) AS avg_fit_score
        FROM outreaches o
        JOIN contacts c ON o.contact_id = c.id
        JOIN campaigns camp ON o.campaign_id = camp.id
        WHERE camp.status IN ('active', 'paused', 'completed')
        GROUP BY c.company, c.title
        HAVING total >= 5
        ORDER BY won DESC, replied DESC
        LIMIT 100
    """).fetchall()
    db.close()

    results = []
    for r in rows:
        d = dict(r)
        invited = d["invited"] or 1
        connected = d["connected"] or 0
        d["acceptance_rate"] = round(connected / invited * 100, 1) if invited > 0 else 0
        d["reply_rate"] = round(d["replied"] / connected * 100, 1) if connected > 0 else 0
        d["conversion_rate"] = round(
            d["won"] / (d["won"] + d["lost"]) * 100, 1
        ) if (d["won"] + d["lost"]) > 0 else 0
        results.append(d)
    return results


def get_campaign_segment_performance(campaign_id: str) -> list[dict[str, Any]]:
    """Per-campaign segment performance for optimization.

    Groups by company + title to identify underperforming segments.
    """
    db = get_db()
    rows = db.execute("""
        SELECT
            c.company AS segment_company,
            c.title AS segment_title,
            COUNT(DISTINCT o.id) AS total,
            SUM(CASE WHEN o.status IN ('invited','withdrawn','connected','messaged','replied',
                'hot_lead','closed_happy','closed_unhappy','opted_out') THEN 1 ELSE 0 END) AS invited,
            SUM(CASE WHEN o.status IN ('connected','messaged','replied',
                'hot_lead','closed_happy','closed_unhappy','opted_out') THEN 1 ELSE 0 END) AS connected,
            SUM(CASE WHEN o.status IN ('replied','hot_lead',
                'closed_happy','closed_unhappy') THEN 1 ELSE 0 END) AS replied,
            SUM(CASE WHEN o.status = 'closed_happy' THEN 1 ELSE 0 END) AS won,
            AVG(c.estimated_revenue) AS avg_revenue,
            AVG(c.fit_score) AS avg_fit_score
        FROM outreaches o
        JOIN contacts c ON o.contact_id = c.id
        WHERE o.campaign_id = ?
        GROUP BY c.company, c.title
        HAVING total >= 3
        ORDER BY invited DESC
    """, (campaign_id,)).fetchall()
    db.close()

    results = []
    for r in rows:
        d = dict(r)
        invited = d["invited"] or 1
        connected = d["connected"] or 0
        d["acceptance_rate"] = round(connected / invited * 100, 1) if invited > 0 else 0
        d["reply_rate"] = round(d["replied"] / connected * 100, 1) if connected > 0 else 0
        results.append(d)
    return results


def get_timing_heatmap() -> list[dict[str, Any]]:
    """Aggregate invited_at by day-of-week and hour to find optimal send times.

    Returns rows with dow (0=Sun), hour, count, accepted, acceptance_rate.
    """
    db = get_db()
    rows = db.execute("""
        SELECT
            CAST(strftime('%w', datetime(o.invited_at, 'unixepoch')) AS INTEGER) AS dow,
            CAST(strftime('%H', datetime(o.invited_at, 'unixepoch')) AS INTEGER) AS hour,
            COUNT(*) AS invites,
            SUM(CASE WHEN o.accepted_at IS NOT NULL THEN 1 ELSE 0 END) AS accepted
        FROM outreaches o
        WHERE o.invited_at IS NOT NULL
        GROUP BY dow, hour
        HAVING invites >= 3
        ORDER BY dow, hour
    """).fetchall()
    db.close()

    results = []
    for r in rows:
        d = dict(r)
        d["acceptance_rate"] = round(d["accepted"] / d["invites"] * 100, 1) if d["invites"] > 0 else 0
        results.append(d)
    return results


def get_engagement_strategy_correlation() -> list[dict[str, Any]]:
    """Correlate engagement patterns with conversion outcomes.

    Analyzes whether comment-first vs react-first vs no-engagement
    correlates with higher acceptance and conversion.
    """
    db = get_db()
    rows = db.execute("""
        SELECT
            CASE
                WHEN first_eng.action_type = 'comment' THEN 'comment_first'
                WHEN first_eng.action_type = 'react' THEN 'react_first'
                WHEN first_eng.action_type = 'follow' THEN 'follow_first'
                WHEN first_eng.action_type = 'endorse' THEN 'endorse_first'
                ELSE 'no_engagement'
            END AS strategy,
            COUNT(DISTINCT o.id) AS total,
            SUM(CASE WHEN o.status IN ('connected','messaged','replied',
                'hot_lead','closed_happy','closed_unhappy','opted_out') THEN 1 ELSE 0 END) AS connected,
            SUM(CASE WHEN o.status IN ('replied','hot_lead',
                'closed_happy','closed_unhappy') THEN 1 ELSE 0 END) AS replied,
            SUM(CASE WHEN o.status = 'closed_happy' THEN 1 ELSE 0 END) AS won,
            AVG(c.estimated_revenue) AS avg_revenue
        FROM outreaches o
        JOIN contacts c ON o.contact_id = c.id
        LEFT JOIN (
            SELECT outreach_id, action_type,
                   ROW_NUMBER() OVER (PARTITION BY outreach_id ORDER BY created_at ASC) AS rn
            FROM engagements
        ) first_eng ON first_eng.outreach_id = o.id AND first_eng.rn = 1
        WHERE o.status != 'pending'
        GROUP BY strategy
        ORDER BY won DESC
    """).fetchall()
    db.close()

    results = []
    for r in rows:
        d = dict(r)
        total = d["total"] or 1
        connected = d["connected"] or 0
        d["acceptance_rate"] = round(connected / total * 100, 1) if total > 0 else 0
        d["reply_rate"] = round(d["replied"] / connected * 100, 1) if connected > 0 else 0
        d["conversion_rate"] = round(
            d["won"] / (d["won"] + total - d["won"]) * 100, 1
        ) if total > 0 else 0
        results.append(d)
    return results


def get_revenue_weighted_funnel() -> dict[str, Any]:
    """Full funnel weighted by estimated_revenue instead of raw counts.

    Returns total pipeline value at each stage.
    """
    db = get_db()
    row = db.execute("""
        SELECT
            SUM(c.estimated_revenue) AS total_pipeline,
            SUM(CASE WHEN o.status IN ('invited','withdrawn','connected','messaged','replied',
                'hot_lead','closed_happy','closed_unhappy','opted_out')
                THEN c.estimated_revenue ELSE 0 END) AS invited_value,
            SUM(CASE WHEN o.status IN ('connected','messaged','replied',
                'hot_lead','closed_happy','closed_unhappy','opted_out')
                THEN c.estimated_revenue ELSE 0 END) AS connected_value,
            SUM(CASE WHEN o.status IN ('replied','hot_lead','closed_happy','closed_unhappy','reverse_pitch','opted_out')
                THEN c.estimated_revenue ELSE 0 END) AS replied_value,
            SUM(CASE WHEN o.status IN ('hot_lead','closed_happy')
                THEN c.estimated_revenue ELSE 0 END) AS hot_value,
            SUM(CASE WHEN o.status = 'closed_happy'
                THEN c.estimated_revenue ELSE 0 END) AS won_value,
            COUNT(DISTINCT CASE WHEN o.status != 'skipped' THEN o.id END) AS total_prospects,
            AVG(c.estimated_revenue) AS avg_revenue
        FROM outreaches o
        JOIN contacts c ON o.contact_id = c.id
        JOIN campaigns camp ON o.campaign_id = camp.id
        WHERE camp.status IN ('active', 'paused', 'completed')
    """).fetchone()
    db.close()
    return dict(row) if row else {}


def get_fit_score_outcome_correlation() -> list[dict[str, Any]]:
    """Correlate fit_score ranges with actual outcomes.

    Groups by fit_score buckets (0-0.2, 0.2-0.4, etc.) and shows conversion rates.
    """
    db = get_db()
    rows = db.execute("""
        SELECT
            CASE
                WHEN c.fit_score < 0.2 THEN '0.0-0.2'
                WHEN c.fit_score < 0.4 THEN '0.2-0.4'
                WHEN c.fit_score < 0.6 THEN '0.4-0.6'
                WHEN c.fit_score < 0.8 THEN '0.6-0.8'
                ELSE '0.8-1.0'
            END AS fit_bucket,
            COUNT(DISTINCT o.id) AS total,
            SUM(CASE WHEN o.status IN ('connected','messaged','replied',
                'hot_lead','closed_happy','closed_unhappy','opted_out') THEN 1 ELSE 0 END) AS connected,
            SUM(CASE WHEN o.status IN ('replied','hot_lead',
                'closed_happy','closed_unhappy') THEN 1 ELSE 0 END) AS replied,
            SUM(CASE WHEN o.status = 'closed_happy' THEN 1 ELSE 0 END) AS won,
            SUM(CASE WHEN o.status = 'closed_unhappy' THEN 1 ELSE 0 END) AS lost,
            AVG(c.estimated_revenue) AS avg_revenue
        FROM outreaches o
        JOIN contacts c ON o.contact_id = c.id
        WHERE o.status != 'pending'
        GROUP BY fit_bucket
        ORDER BY fit_bucket
    """).fetchall()
    db.close()

    results = []
    for r in rows:
        d = dict(r)
        d["acceptance_rate"] = round(
            d["connected"] / d["total"] * 100, 1
        ) if d["total"] > 0 else 0
        d["reply_rate"] = round(
            d["replied"] / d["connected"] * 100, 1
        ) if d["connected"] > 0 else 0
        d["conversion_rate"] = round(
            d["won"] / (d["won"] + d["lost"]) * 100, 1
        ) if (d["won"] + d["lost"]) > 0 else 0
        results.append(d)
    return results


def get_won_deal_profiles(limit: int = 20) -> list[dict[str, Any]]:
    """Get profiles of all won deals across campaigns for pattern detection."""
    db = get_db()
    rows = db.execute("""
        SELECT
            c.name, c.title, c.company, c.fit_score, c.estimated_revenue,
            c.analysis_json, c.profile_json,
            o.variant, o.channel, o.followup_count,
            o.invited_at, o.accepted_at, o.first_reply_at,
            camp.name AS campaign_name, camp.icp_json
        FROM outreaches o
        JOIN contacts c ON o.contact_id = c.id
        JOIN campaigns camp ON o.campaign_id = camp.id
        WHERE o.status = 'closed_happy'
        ORDER BY o.updated_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_lost_deal_profiles(limit: int = 20) -> list[dict[str, Any]]:
    """Get profiles of all lost deals across campaigns for pattern detection."""
    db = get_db()
    rows = db.execute("""
        SELECT
            c.name, c.title, c.company, c.fit_score, c.estimated_revenue,
            c.analysis_json, c.profile_json,
            o.variant, o.channel, o.followup_count, o.outcome_json,
            camp.name AS campaign_name, camp.icp_json
        FROM outreaches o
        JOIN contacts c ON o.contact_id = c.id
        JOIN campaigns camp ON o.campaign_id = camp.id
        WHERE o.status = 'closed_unhappy'
        ORDER BY o.updated_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_active_campaign_icps() -> list[dict[str, Any]]:
    """Get ICP data for all active campaigns (for uncovered segment detection)."""
    db = get_db()
    rows = db.execute("""
        SELECT id, name, icp_json, status, spawned_by
        FROM campaigns
        WHERE status IN ('active', 'paused')
        ORDER BY created_at DESC
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_contacts_without_revenue(limit: int = 500) -> list[dict[str, Any]]:
    """Get contacts that need revenue estimation."""
    db = get_db()
    rows = db.execute("""
        SELECT c.id, c.title, c.company, c.profile_json, c.analysis_json,
               camp.icp_json
        FROM contacts c
        LEFT JOIN campaigns camp ON c.campaign_id = camp.id
        WHERE c.estimated_revenue = 0 OR c.estimated_revenue IS NULL
        LIMIT ?
    """, (limit,)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def update_contact_revenue(contact_id: str, estimated_revenue: float) -> None:
    """Update the estimated_revenue for a contact."""
    db = get_db()
    db.execute(
        "UPDATE contacts SET estimated_revenue = ? WHERE id = ?",
        (estimated_revenue, contact_id),
    )
    db.commit()
    db.close()


def get_pending_outreaches_for_reorder(campaign_id: str) -> list[dict[str, Any]]:
    """Get pending outreaches that can be reordered by revenue score."""
    db = get_db()
    rows = db.execute("""
        SELECT o.id, o.contact_id, c.fit_score, c.estimated_revenue,
               c.title, c.company, c.source
        FROM outreaches o
        JOIN contacts c ON o.contact_id = c.id
        WHERE o.campaign_id = ? AND o.status = 'pending'
        ORDER BY c.estimated_revenue DESC
    """, (campaign_id,)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_strategy_summary() -> dict[str, Any]:
    """Get a summary of strategy engine state for the dashboard."""
    db = get_db()

    patterns = db.execute(
        "SELECT COUNT(*) as cnt FROM strategy_patterns WHERE status = 'active'"
    ).fetchone()
    actions = db.execute(
        "SELECT COUNT(*) as cnt FROM strategy_actions"
    ).fetchone()
    validated = db.execute(
        "SELECT COUNT(*) as cnt FROM strategy_actions WHERE status = 'validated'"
    ).fetchone()
    rolled_back = db.execute(
        "SELECT COUNT(*) as cnt FROM strategy_actions WHERE status = 'rolled_back'"
    ).fetchone()
    spawned = db.execute(
        "SELECT COUNT(*) as cnt FROM campaigns WHERE spawned_by IS NOT NULL"
    ).fetchone()
    top_pattern = db.execute(
        """SELECT pattern_key, estimated_revenue_impact, confidence
           FROM strategy_patterns WHERE status = 'active'
           ORDER BY estimated_revenue_impact DESC LIMIT 1"""
    ).fetchone()

    db.close()

    return {
        "active_patterns": patterns["cnt"] if patterns else 0,
        "total_actions": actions["cnt"] if actions else 0,
        "validated_actions": validated["cnt"] if validated else 0,
        "rolled_back_actions": rolled_back["cnt"] if rolled_back else 0,
        "spawned_campaigns": spawned["cnt"] if spawned else 0,
        "top_pattern": dict(top_pattern) if top_pattern else None,
    }
