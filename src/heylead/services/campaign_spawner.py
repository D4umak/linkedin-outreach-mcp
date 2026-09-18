"""Autonomous campaign creation for the strategy engine.

When the pattern detector finds high-performing segments that are not
covered by any active campaign, this module auto-creates new campaigns
to exploit those segments.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..db.async_bridge import run_db
from ..constants import (
    STRATEGY_MAX_SPAWNED_CAMPAIGNS,
    STRATEGY_SPAWN_MIN_CONFIDENCE,
    STRATEGY_SPAWN_MIN_REVENUE,
)
from ..db.queries import list_campaigns
from ..db.strategy_queries import (
    get_active_campaign_icps,
    save_strategy_action,
)

logger = logging.getLogger(__name__)


async def evaluate_spawn_opportunities(
    patterns: list[dict[str, Any]],
    llm_spawn_candidates: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Check which high-performing patterns are not covered by active campaigns.

    A pattern qualifies for spawning if:
    1. Confidence >= STRATEGY_SPAWN_MIN_CONFIDENCE
    2. Estimated revenue impact >= STRATEGY_SPAWN_MIN_REVENUE
    3. No active campaign already targets this segment

    Args:
        patterns: Discovered patterns from the pattern detector.
        llm_spawn_candidates: Optional spawn candidates from LLM analysis.

    Returns:
        List of spawn-worthy opportunities.
    """
    active_icps = await run_db(get_active_campaign_icps)
    active_targets = _extract_active_targets(active_icps)

    candidates: list[dict[str, Any]] = []

    # Check patterns
    for p in patterns:
        if p.get("confidence", 0) < STRATEGY_SPAWN_MIN_CONFIDENCE:
            continue
        if p.get("estimated_revenue_impact", 0) < STRATEGY_SPAWN_MIN_REVENUE:
            continue
        if p.get("pattern_type") not in ("icp_segment", "revenue_segment"):
            continue

        pattern_key = p.get("pattern_key", "")
        if _is_covered(pattern_key, active_targets):
            continue

        candidates.append({
            "source": "pattern",
            "pattern_id": p.get("id"),
            "pattern_key": pattern_key,
            "description": p.get("description", ""),
            "target_description": _pattern_to_target(p),
            "estimated_revenue": p.get("estimated_revenue_impact", 0),
            "confidence": p.get("confidence", 0),
        })

    # Check LLM spawn candidates
    if llm_spawn_candidates:
        for sc in llm_spawn_candidates:
            conf = sc.get("confidence", 0)
            rev = sc.get("estimated_revenue_per_prospect", 0)
            if conf < STRATEGY_SPAWN_MIN_CONFIDENCE or rev < STRATEGY_SPAWN_MIN_REVENUE:
                continue

            target = sc.get("target_description", "")
            if not target:
                continue

            if _is_covered(target.lower(), active_targets):
                continue

            candidates.append({
                "source": "llm",
                "pattern_id": None,
                "pattern_key": target.lower().replace(" ", "+")[:50],
                "description": sc.get("segment_description", ""),
                "target_description": target,
                "estimated_revenue": rev,
                "confidence": conf,
            })

    # Sort by revenue and limit
    candidates.sort(key=lambda c: c.get("estimated_revenue", 0), reverse=True)
    return candidates[:STRATEGY_MAX_SPAWNED_CAMPAIGNS]


async def spawn_campaign(candidate: dict[str, Any]) -> str | None:
    """Autonomously create a campaign for a spawn candidate.

    Args:
        candidate: Spawn opportunity from evaluate_spawn_opportunities.

    Returns:
        Campaign ID if successful, None otherwise.
    """
    target = candidate.get("target_description", "")
    if not target:
        return None

    # Spawned campaigns are created as drafts and wait for a human to launch
    # them, so the cap has to count drafts too — otherwise every strategy cycle
    # re-spawns the same segment because nothing it made is 'active' yet.
    all_campaigns = await run_db(list_campaigns)
    spawned = [
        c for c in all_campaigns
        if c.get("spawned_by") and c.get("status") not in ("archived", "completed")
    ]
    if len(spawned) >= STRATEGY_MAX_SPAWNED_CAMPAIGNS:
        logger.info(
            "Spawner: at max %d auto-spawned campaigns, skipping",
            STRATEGY_MAX_SPAWNED_CAMPAIGNS,
        )
        return None

    campaign_name = f"[Auto] {target[:60]}"

    if any(c.get("name") == campaign_name for c in all_campaigns):
        logger.info("Spawner: campaign '%s' already exists, skipping", campaign_name)
        return None

    try:
        from ..tools.create_campaign import run_create_campaign

        result = await run_create_campaign(
            target_description=target,
            campaign_name=campaign_name,
            mode="autopilot",
            _internal_source="strategy_spawn",
        )

        # Check if campaign was created (result contains campaign ID or success message)
        if "campaign" in result.lower() and "created" not in result.lower():
            # Likely an error message
            logger.warning("Spawner: campaign creation returned: %s", result[:200])
            return None

        # Find the newly created campaign by name
        def _find_and_tag_campaign():
            from ..db.schema import get_db
            db = get_db()
            row = db.execute(
                "SELECT id FROM campaigns WHERE name = ? ORDER BY created_at DESC LIMIT 1",
                (campaign_name,),
            ).fetchone()

            cid = None
            if row:
                cid = row["id"]
                # Tag it as auto-spawned
                db.execute(
                    "UPDATE campaigns SET spawned_by = ? WHERE id = ?",
                    (candidate.get("pattern_id", "strategy_engine"), cid),
                )
                db.commit()
            db.close()
            return cid

        campaign_id = await run_db(_find_and_tag_campaign)

        if campaign_id:
            # Log the action
            await run_db(save_strategy_action,
                action_type="spawn_campaign",
                details_json=json.dumps({
                    "campaign_id": campaign_id,
                    "campaign_name": campaign_name,
                    "target_description": target,
                    "estimated_revenue": candidate.get("estimated_revenue", 0),
                    "confidence": candidate.get("confidence", 0),
                    "source": candidate.get("source", "pattern"),
                }),
                campaign_id=campaign_id,
                pattern_id=candidate.get("pattern_id"),
            )
            logger.info(
                "Spawner: created DRAFT campaign '%s' (%s) for segment: %s "
                "— awaiting campaign(action='launch')",
                campaign_name, campaign_id[:8], target[:50],
            )

        return campaign_id

    except Exception as e:
        logger.error("Spawner: failed to create campaign: %s", e, exc_info=True)
        return None


# ──────────────────────────────────────────────
# Private Helpers
# ──────────────────────────────────────────────

def _extract_active_targets(campaign_icps: list[dict]) -> set[str]:
    """Extract target keywords from active campaign ICPs for coverage check."""
    targets: set[str] = set()
    for camp in campaign_icps:
        # Campaign name
        name = (camp.get("name", "") or "").lower()
        if name:
            targets.add(name)

        # ICP data
        icp_json = camp.get("icp_json")
        if not icp_json:
            continue
        try:
            icp = json.loads(icp_json) if isinstance(icp_json, str) else icp_json
        except (json.JSONDecodeError, TypeError):
            continue

        icps = icp.get("icps", [])
        if not isinstance(icps, list):
            continue

        for single_icp in icps:
            if not isinstance(single_icp, dict):
                continue
            # Industries
            for ind in single_icp.get("industries", []):
                targets.add(str(ind).lower())
            # Job titles
            for title in single_icp.get("job_titles", []):
                targets.add(str(title).lower())

    return targets


def _is_covered(pattern_key: str, active_targets: set[str]) -> bool:
    """Check if a pattern is already covered by active campaigns."""
    key_lower = pattern_key.lower()
    parts = key_lower.replace("+", " ").split()

    # Consider covered if >50% of pattern parts match active targets
    if not parts:
        return False

    matches = sum(
        1 for part in parts
        if any(part in t or t in part for t in active_targets)
    )
    return matches / len(parts) > 0.5


def _pattern_to_target(pattern: dict[str, Any]) -> str:
    """Convert a pattern to a target description for create_campaign."""
    key = pattern.get("pattern_key", "")
    desc = pattern.get("description", "")

    # If the pattern key has structured format like "fintech+VP+51-200"
    parts = key.split("+")
    if len(parts) >= 2:
        return f"{parts[1]}s at {parts[0]} companies" + (
            f" with {parts[2]} employees" if len(parts) >= 3 else ""
        )

    # Fall back to the description
    if desc:
        # Extract the actionable part
        return desc.split(":")[0] if ":" in desc else desc[:100]

    return key
