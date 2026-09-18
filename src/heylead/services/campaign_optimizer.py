"""Per-campaign real-time optimizer for the strategy engine.

Adjusts running campaigns based on performance data and learned patterns:
1. Skip underperforming segments (low acceptance after sufficient sample)
2. Reorder pending outreaches by revenue-weighted score
3. Adjust messaging preferences based on winning patterns
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..db.async_bridge import run_db
from ..constants import (
    STRATEGY_MIN_SAMPLE_SIZE,
    STRATEGY_SKIP_ACCEPTANCE_THRESHOLD,
)
from ..db.queries import (
    list_campaigns,
    update_outreach,
)
from ..db.strategy_queries import (
    get_campaign_segment_performance,
    get_pending_outreaches_for_reorder,
    save_strategy_action,
)

logger = logging.getLogger(__name__)

# The two optimisers that write to prospect rows. On a hosted account the
# backend also learns from outcomes and also orders the queue
# (app/services/campaign_preferences.py, 9 Sep 2026), so leaving these live
# would give one campaign two graders with different vocabularies, different
# sample floors and no shared clock: the backend orders a refill batch by fit
# x a bounded preference, and minutes later _reorder_by_revenue overwrites
# contacts.fit_score itself with a composite the backend has never seen.
#
# One owner for fit_score, and it is the backend. These two become log-only
# on hosted accounts; self-hosted behaviour is untouched, because there the
# client IS the only grader.
_HOSTED_DISABLED_OPTIMISERS = ("skip_underperformers", "reorder_by_revenue")


def _backend_owns_ranking(campaign_id: str) -> bool:
    """True when the cloud sends for this campaign and so owns its ranking."""
    try:
        from .cloud_sync import cloud_owns_outbound

        return bool(cloud_owns_outbound(campaign_id))
    except Exception:
        # Unknowable means "do not write". A false negative costs an ordering
        # nudge; a false positive costs a fit_score fight with the backend.
        logger.debug("Could not determine outbound ownership", exc_info=True)
        return True


async def optimize_campaign(
    campaign_id: str,
    patterns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Optimize a single active campaign based on patterns.

    Args:
        campaign_id: Campaign to optimize.
        patterns: Discovered patterns from the pattern detector.

    Returns:
        List of actions taken.
    """
    actions: list[dict[str, Any]] = []
    hosted = _backend_owns_ranking(campaign_id)
    if hosted:
        logger.info(
            "Campaign optimizer: %s are no-ops for campaign %s — the backend "
            "owns fit_score and ranking for hosted accounts",
            ", ".join(_HOSTED_DISABLED_OPTIMISERS), campaign_id[:8],
        )

    # 1. Skip underperforming segments
    if not hosted:
        skip_actions = await run_db(_skip_underperformers, campaign_id)
        actions.extend(skip_actions)

    # 2. Reorder pending by revenue
    if not hosted:
        reorder_action = await run_db(_reorder_by_revenue, campaign_id, patterns)
        if reorder_action:
            actions.append(reorder_action)

    # 3. Adjust messaging preferences
    msg_action = await run_db(_adjust_messaging, campaign_id, patterns)
    if msg_action:
        actions.append(msg_action)

    if actions:
        logger.info(
            "Campaign optimizer: %d actions for campaign %s",
            len(actions), campaign_id[:8],
        )

    return actions


async def optimize_all_campaigns(
    patterns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Optimize all active campaigns.

    Returns:
        Combined list of all actions taken.
    """
    campaigns = await run_db(list_campaigns, status="active")
    all_actions: list[dict[str, Any]] = []

    for camp in campaigns:
        try:
            actions = await optimize_campaign(camp["id"], patterns)
            all_actions.extend(actions)
        except Exception as e:
            logger.warning(
                "Failed to optimize campaign %s: %s", camp["id"][:8], e
            )

    return all_actions


def _skip_underperformers(campaign_id: str) -> list[dict[str, Any]]:
    """Skip pending prospects in segments with very low acceptance rates.

    Skips if segment has >= STRATEGY_MIN_SAMPLE_SIZE invited and
    acceptance rate < STRATEGY_SKIP_ACCEPTANCE_THRESHOLD.
    """
    actions: list[dict[str, Any]] = []
    segments = get_campaign_segment_performance(campaign_id)

    for seg in segments:
        invited = seg.get("invited", 0)
        connected = seg.get("connected", 0)
        if invited < STRATEGY_MIN_SAMPLE_SIZE:
            continue

        acc_rate = connected / invited if invited > 0 else 0
        if acc_rate >= STRATEGY_SKIP_ACCEPTANCE_THRESHOLD:
            continue

        seg_company = seg.get("segment_company", "") or ""
        seg_title = seg.get("segment_title", "") or ""
        # Segments are grouped by the exact (company, title) pair, so the skip
        # has to match both. An empty key is missing data, not a segment: it
        # would match every contact whose company we never learned.
        if not seg_company or not seg_title:
            continue

        # This segment is underperforming — skip remaining pending
        # Find pending outreaches matching this segment
        pending = get_pending_outreaches_for_reorder(campaign_id)
        skipped_count = 0
        for p in pending:
            if (p.get("company", "") == seg_company
                    and p.get("title", "") == seg_title):
                try:
                    update_outreach(p["id"], status="skipped")
                    skipped_count += 1
                except Exception:
                    pass

        if skipped_count > 0:
            details = {
                "segment_company": seg.get("segment_company", ""),
                "segment_title": seg.get("segment_title", ""),
                "acceptance_rate": round(acc_rate * 100, 1),
                "invited": invited,
                "connected": connected,
                "skipped_count": skipped_count,
                "reason": f"Acceptance rate {acc_rate*100:.1f}% < {STRATEGY_SKIP_ACCEPTANCE_THRESHOLD*100:.0f}% threshold after {invited} invites",
            }
            action_id = save_strategy_action(
                action_type="skip_segment",
                details_json=json.dumps(details),
                campaign_id=campaign_id,
            )
            actions.append({"id": action_id, "type": "skip_segment", **details})

    return actions


def _reorder_by_revenue(
    campaign_id: str,
    patterns: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Reorder pending outreaches by composite lead score.

    Composite = ICP_match*0.30 + revenue*0.25 + signal*0.20 + engagement*0.15 + segment*0.10
    Updates fit_score on contacts to reflect the new ordering.
    """
    from ..db.queries import get_campaign
    from .icp_match_scorer import compute_composite_lead_score

    pending = get_pending_outreaches_for_reorder(campaign_id)
    if len(pending) < 3:
        return None

    # Load campaign ICP for scoring
    campaign = get_campaign(campaign_id)
    campaign_icp_json = (campaign.get("icp_json", "") if campaign else "") or ""

    scored = [
        (
            p,
            compute_composite_lead_score(
                contact=p,
                campaign_icp_json=campaign_icp_json,
                patterns=patterns,
            ),
        )
        for p in pending
    ]

    # Degenerate batch: for fresh prospects revenue/signal/engagement are all
    # zero and segment is a cold-start constant, so distinct people can
    # collapse to one identical composite. That carries no ordering
    # information — writing it would only destroy the import-time scores
    # (observed 18 Aug 2026: eleven hand-imported founders all re-scored to
    # 0.2598 and skipped by the send gate).
    if len(scored) > 1 and len({round(s, 4) for _, s in scored}) == 1:
        logger.info(
            "Reorder skipped for campaign %s: all %d composites identical (%.4f)",
            campaign_id, len(scored), scored[0][1],
        )
        return None

    reordered = 0
    from ..db.schema import get_db
    db = get_db()

    for p, new_score in scored:
        old_score = p.get("fit_score", 0) or 0
        # Human-imported prospects keep their score as a floor — the
        # composite may raise them in the queue but never bury them.
        if p.get("source") == "csv_import" and new_score < old_score:
            continue
        if abs(new_score - old_score) > 0.01:
            db.execute(
                "UPDATE contacts SET fit_score = ? WHERE id = ?",
                (new_score, p["contact_id"]),
            )
            reordered += 1

    if reordered > 0:
        db.commit()
    db.close()

    if reordered == 0:
        return None

    from .fit_gate import restore_fit_skipped_for_campaign

    restore_fit_skipped_for_campaign(campaign_id)

    details = {
        "reordered_count": reordered,
        "total_pending": len(pending),
        "formula": "icp*0.30 + revenue*0.25 + signal*0.20 + engagement*0.15 + segment*0.10",
    }
    action_id = save_strategy_action(
        action_type="reorder_queue",
        details_json=json.dumps(details),
        campaign_id=campaign_id,
    )
    return {"id": action_id, "type": "reorder_queue", **details}


def _adjust_messaging(
    campaign_id: str,
    patterns: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Adjust campaign messaging preferences based on patterns.

    Writes winning patterns into campaign.context_json.campaign_preferences
    so the message generator picks them up.
    """
    # Find messaging-relevant patterns
    msg_patterns = [
        p for p in patterns
        if p.get("pattern_type") in ("messaging_tone", "icp_segment", "revenue_segment")
        and p.get("recommended_action")
    ]
    if not msg_patterns:
        return None

    # Build preference adjustments
    recommendations = []
    for p in msg_patterns[:3]:  # Top 3
        action = p.get("recommended_action", "")
        if action:
            recommendations.append(action)

    if not recommendations:
        return None

    # Read current context
    from ..db.schema import get_db
    db = get_db()
    row = db.execute(
        "SELECT context_json FROM campaigns WHERE id = ?", (campaign_id,)
    ).fetchone()

    ctx = {}
    if row and row["context_json"]:
        try:
            ctx = json.loads(row["context_json"])
        except (json.JSONDecodeError, TypeError):
            pass

    # Append strategy recommendations to campaign_preferences
    existing_prefs = ctx.get("campaign_preferences", "")
    strategy_note = " | Strategy engine: " + "; ".join(recommendations)
    if strategy_note not in (existing_prefs or ""):
        ctx["campaign_preferences"] = (existing_prefs or "") + strategy_note

        db.execute(
            "UPDATE campaigns SET context_json = ? WHERE id = ?",
            (json.dumps(ctx), campaign_id),
        )
        db.commit()

    db.close()

    details = {
        "recommendations": recommendations,
        "context_updated": True,
    }
    action_id = save_strategy_action(
        action_type="adjust_messaging",
        details_json=json.dumps(details),
        campaign_id=campaign_id,
    )
    return {"id": action_id, "type": "adjust_messaging", **details}
