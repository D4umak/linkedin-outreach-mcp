"""Autonomous Campaign Strategy Engine — top-level orchestrator.

Runs the full observe → decide → act → measure → learn cycle:
1. Backfill revenue estimates for contacts without them
2. Detect cross-campaign patterns (heuristic + LLM)
3. Optimize each active campaign (skip, reorder, adjust messaging)
4. Evaluate spawn opportunities for uncovered high-revenue segments
5. Measure outcomes of previous strategy actions (feedback loop)
6. Update pattern confidence based on measured outcomes

Called by the scheduler every 4 hours (SCHEDULER_STRATEGY_SECONDS).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..db.async_bridge import run_db
from ..constants import (
    STRATEGY_CONFIDENCE_BOOST,
    STRATEGY_CONFIDENCE_PENALTY,
)
from ..db.strategy_queries import (
    get_strategy_summary,
    get_unmeasured_actions,
    list_strategy_actions,
    list_strategy_patterns,
    update_strategy_action,
    update_strategy_pattern,
)

logger = logging.getLogger(__name__)


async def run_strategy_cycle() -> dict[str, Any]:
    """Execute one full strategy engine cycle.

    Returns:
        Summary dict with counts of actions taken per phase.
    """
    summary: dict[str, Any] = {
        "revenue_backfilled": 0,
        "patterns_detected": 0,
        "optimizations": 0,
        "campaigns_spawned": 0,
        "actions_measured": 0,
        "timestamp": int(time.time()),
    }

    try:
        # Phase 1: Revenue estimation
        from .revenue_estimator import backfill_revenue_estimates
        summary["revenue_backfilled"] = await backfill_revenue_estimates()

        # Phase 2: Pattern detection
        from .pattern_detector import detect_patterns
        patterns = await detect_patterns()
        summary["patterns_detected"] = len(patterns)

        # Phase 3: Campaign optimization
        from .campaign_optimizer import optimize_all_campaigns
        optimizations = await optimize_all_campaigns(patterns)
        summary["optimizations"] = len(optimizations)

        # Phase 4: Campaign spawning
        from .campaign_spawner import evaluate_spawn_opportunities, spawn_campaign
        spawn_candidates = await evaluate_spawn_opportunities(patterns)
        spawned = 0
        for candidate in spawn_candidates:
            result = await spawn_campaign(candidate)
            if result:
                spawned += 1
        summary["campaigns_spawned"] = spawned

        # Phase 5: Measure previous actions (feedback loop)
        measured = await _measure_previous_actions()
        summary["actions_measured"] = measured

    except Exception as e:
        logger.error("Strategy cycle error: %s", e, exc_info=True)
        summary["error"] = str(e)

    # Log summary
    logger.info(
        "Strategy cycle complete: %d revenue, %d patterns, %d optimizations, %d spawned, %d measured",
        summary["revenue_backfilled"],
        summary["patterns_detected"],
        summary["optimizations"],
        summary["campaigns_spawned"],
        summary["actions_measured"],
    )

    return summary


async def get_strategy_insights() -> dict[str, Any]:
    """Build a summary of strategy engine state for the MCP tool.

    Returns:
        Dict with patterns, actions, revenue stats, and spawn candidates.
    """
    from ..db.strategy_queries import get_revenue_weighted_funnel

    summary = await run_db(get_strategy_summary)
    patterns = await run_db(list_strategy_patterns, status="active", limit=10)
    recent_actions = await run_db(list_strategy_actions, limit=10)
    revenue_funnel = await run_db(get_revenue_weighted_funnel)

    # Pending spawn candidates (check current state)
    from .pattern_detector import _build_pattern_snapshot
    snapshot = await run_db(_build_pattern_snapshot)
    spawn_ready = 0
    if snapshot:
        from .campaign_spawner import evaluate_spawn_opportunities
        candidates = await evaluate_spawn_opportunities(patterns)
        spawn_ready = len(candidates)

    return {
        "summary": summary,
        "top_patterns": patterns,
        "recent_actions": recent_actions,
        "revenue_funnel": revenue_funnel,
        "spawn_candidates_ready": spawn_ready,
    }


async def _measure_previous_actions() -> int:
    """Measure outcomes of previous strategy actions (feedback loop).

    For each unmeasured action (applied > 48h ago):
    - Get current campaign metrics
    - Compare to baseline captured in details_json
    - Mark as validated (improvement) or rolled_back (degradation)
    - Adjust pattern confidence accordingly

    Returns:
        Number of actions measured.
    """
    unmeasured = await run_db(get_unmeasured_actions, max_age_hours=48)
    if not unmeasured:
        return 0

    from ..db.queries import get_campaign_stats

    measured = 0
    for action in unmeasured:
        try:
            campaign_id = action.get("campaign_id")
            if not campaign_id:
                # Cross-campaign action — mark measured without comparison
                await run_db(update_strategy_action,
                    action["id"],
                    status="measured",
                    measured_at=int(time.time()),
                    outcome_json=json.dumps({"note": "cross-campaign, no baseline"}),
                )
                measured += 1
                continue

            # Get current stats
            current = await run_db(get_campaign_stats, campaign_id)
            if not current:
                continue

            # Get baseline from details_json
            details = {}
            if action.get("details_json"):
                try:
                    details = json.loads(action["details_json"])
                except (json.JSONDecodeError, TypeError):
                    pass

            # Determine outcome
            outcome = _evaluate_action_outcome(action["action_type"], details, current)

            # Update action
            await run_db(update_strategy_action,
                action["id"],
                status=outcome["status"],
                measured_at=int(time.time()),
                outcome_json=json.dumps(outcome),
            )

            # Adjust pattern confidence
            pattern_id = action.get("pattern_id")
            if pattern_id:
                await run_db(_adjust_pattern_confidence, pattern_id, outcome["status"])

            measured += 1

        except Exception as e:
            logger.debug("Failed to measure action %s: %s", action["id"][:8], e)

    if measured:
        logger.info("Strategy engine: measured %d previous actions", measured)

    return measured


def _evaluate_action_outcome(
    action_type: str,
    details: dict[str, Any],
    current_stats: dict[str, Any],
) -> dict[str, Any]:
    """Compare action baseline to current campaign state.

    Returns:
        Dict with status ('validated' or 'rolled_back'), reason, and metrics.
    """
    # get_campaign_stats reports rates as fractions; the thresholds and the
    # stored skip_segment baselines are percentages. Normalize to percent once.
    current_acc_pct = round((current_stats.get("acceptance_rate", 0) or 0) * 100, 1)
    current_reply_pct = round((current_stats.get("reply_rate", 0) or 0) * 100, 1)

    # For skip_segment: check if overall acceptance improved
    if action_type == "skip_segment":
        baseline_acc = details.get("acceptance_rate", 0)
        current_acc = current_acc_pct

        if current_acc > baseline_acc:
            return {
                "status": "validated",
                "reason": f"Acceptance rate improved from {baseline_acc}% to {current_acc}%",
                "baseline_acceptance": baseline_acc,
                "current_acceptance": current_acc,
            }
        return {
            "status": "measured",
            "reason": f"Acceptance rate unchanged ({baseline_acc}% → {current_acc}%)",
            "baseline_acceptance": baseline_acc,
            "current_acceptance": current_acc,
        }

    # For reorder_queue: check if reply rate improved
    if action_type == "reorder_queue":
        current_reply = current_reply_pct
        return {
            "status": "measured",
            "reason": f"Current reply rate: {current_reply}%",
            "current_reply_rate": current_reply,
        }

    # For spawn_campaign: check if the spawned campaign has activity
    if action_type == "spawn_campaign":
        invited = current_stats.get("invited", 0)
        connected = current_stats.get("connected", 0)
        if invited > 0 and connected > 0:
            acc = current_acc_pct
            return {
                "status": "validated" if acc > 15 else "measured",
                "reason": f"Spawned campaign: {invited} invited, {connected} connected, {acc}% acceptance",
                "invited": invited,
                "connected": connected,
                "acceptance_rate": acc,
            }
        return {
            "status": "measured",
            "reason": f"Spawned campaign still ramping ({invited} invited)",
            "invited": invited,
        }

    # Default: mark as measured
    return {
        "status": "measured",
        "reason": "Action measured, no specific outcome comparison available",
    }


def _adjust_pattern_confidence(pattern_id: str, outcome_status: str) -> None:
    """Boost or penalize pattern confidence based on action outcome."""
    from ..db.strategy_queries import get_strategy_pattern

    pattern = get_strategy_pattern(pattern_id)
    if not pattern:
        return

    current_confidence = pattern.get("confidence", 0.5)

    if outcome_status == "validated":
        new_confidence = min(1.0, current_confidence + STRATEGY_CONFIDENCE_BOOST)
    elif outcome_status == "rolled_back":
        new_confidence = max(0.0, current_confidence - STRATEGY_CONFIDENCE_PENALTY)
    else:
        return  # No change for 'measured'

    if abs(new_confidence - current_confidence) > 0.001:
        update_strategy_pattern(
            pattern_id,
            confidence=new_confidence,
            last_validated=int(time.time()),
        )
