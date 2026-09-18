"""Brand strategy service — persistence, baseline tracking, and progress.

Stores brand analysis results, strategy plans, and action completion logs
using the existing settings key-value store.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..db.queries import get_setting, list_campaigns, save_setting

logger = logging.getLogger(__name__)

# ── Settings keys ──

_KEY_ANALYSIS = "brand_analysis"
_KEY_PLAN = "brand_strategy"
_KEY_BASELINE = "brand_baseline"
_KEY_ACTIONS_LOG = "brand_actions_completed"


# ──────────────────────────────────────────────
# Analysis persistence
# ──────────────────────────────────────────────


def save_brand_analysis(analysis: dict[str, Any]) -> None:
    analysis["analyzed_at"] = int(time.time())
    save_setting(_KEY_ANALYSIS, analysis)


def load_brand_analysis() -> dict[str, Any] | None:
    return get_setting(_KEY_ANALYSIS)


# ──────────────────────────────────────────────
# Plan persistence
# ──────────────────────────────────────────────


def save_brand_plan(plan: dict[str, Any]) -> None:
    plan["created_at"] = int(time.time())
    save_setting(_KEY_PLAN, plan)


def load_brand_plan() -> dict[str, Any] | None:
    return get_setting(_KEY_PLAN)


# ──────────────────────────────────────────────
# Baseline
# ──────────────────────────────────────────────


def capture_baseline(
    profile: dict[str, Any],
    ssi_data: dict[str, Any],
    health_total: int,
    acceptance_rate: float,
) -> dict[str, Any]:
    return {
        "captured_at": int(time.time()),
        "ssi_score": ssi_data.get("score", 0),
        "ssi_pillars": ssi_data.get("pillars", []),
        "acceptance_rate": acceptance_rate,
        "headline": profile.get("headline", ""),
        "summary_length": len(profile.get("summary", "")),
        "connections": profile.get("connections", 0),
        "health_score": health_total,
    }


def save_brand_baseline(baseline: dict[str, Any]) -> None:
    save_setting(_KEY_BASELINE, baseline)


def load_brand_baseline() -> dict[str, Any] | None:
    return get_setting(_KEY_BASELINE)


# ──────────────────────────────────────────────
# Action tracking
# ──────────────────────────────────────────────


def get_next_pending_action(plan: dict[str, Any]) -> dict[str, Any] | None:
    """Find the next uncompleted action in the plan (week order)."""
    for week in plan.get("weeks", []):
        for action in week.get("actions", []):
            if action.get("status") == "pending":
                return action
    return None


def get_next_pending_action_by_type(
    plan: dict[str, Any], action_type: str,
) -> dict[str, Any] | None:
    """Find the next uncompleted action of a specific type (post/engagement/profile_optimize)."""
    for week in plan.get("weeks", []):
        for action in week.get("actions", []):
            if action.get("status") == "pending" and action.get("type") == action_type:
                return action
    return None


def get_next_pending_profile_action(
    plan: dict[str, Any],
    *,
    skip_optimize: bool = False,
) -> dict[str, Any] | None:
    """Next pending profile_optimize or photo_enhance, in week order."""
    allowed = ("photo_enhance",) if skip_optimize else ("profile_optimize", "photo_enhance")
    for week in plan.get("weeks", []):
        for action in week.get("actions", []):
            if action.get("status") == "pending" and action.get("type") in allowed:
                return action
    return None


def calendar_days(plan: dict[str, Any]) -> list[str]:
    return [
        str(entry.get("day", "")).strip()
        for entry in plan.get("content_calendar") or []
        if entry.get("day")
    ]


def calendar_entry_for(plan: dict[str, Any], weekday: str) -> dict[str, Any] | None:
    needle = weekday.strip().lower()
    for entry in plan.get("content_calendar") or []:
        if str(entry.get("day", "")).strip().lower() == needle:
            return entry
    return None


def weekly_post_target(plan: dict[str, Any]) -> int:
    from ..constants import BRAND_DEFAULT_WEEKLY_POSTS

    targets = plan.get("engagement_targets") or {}
    raw = targets.get("weekly_posts", BRAND_DEFAULT_WEEKLY_POSTS)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return BRAND_DEFAULT_WEEKLY_POSTS


def daily_comment_target(plan: dict[str, Any]) -> int:
    from ..constants import BRAND_DEFAULT_DAILY_COMMENTS

    targets = plan.get("engagement_targets") or {}
    raw = targets.get("daily_comments", BRAND_DEFAULT_DAILY_COMMENTS)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return BRAND_DEFAULT_DAILY_COMMENTS


def get_brand_age_days() -> int:
    """Return days since the current brand plan was created. -1 if no plan."""
    plan = load_brand_plan()
    if not plan or "created_at" not in plan:
        return -1
    return (int(time.time()) - plan["created_at"]) // 86400


def is_plan_complete(plan: dict[str, Any]) -> bool:
    """Check if all actions in the plan are completed."""
    completed, total = count_plan_progress(plan)
    return total > 0 and completed >= total


def mark_action_completed(action_id: str, result: str = "") -> None:
    """Mark a plan action as completed and log it."""
    plan = load_brand_plan()
    if not plan:
        return

    # Update action status in the plan
    for week in plan.get("weeks", []):
        for action in week.get("actions", []):
            if action.get("id") == action_id:
                action["status"] = "completed"
                action["completed_at"] = int(time.time())
                break

    save_setting(_KEY_PLAN, plan)

    # Append to completed actions log
    log: list[dict] = get_setting(_KEY_ACTIONS_LOG, [])
    if not isinstance(log, list):
        log = []
    log.append({
        "action_id": action_id,
        "completed_at": int(time.time()),
        "result": result,
    })
    save_setting(_KEY_ACTIONS_LOG, log)


def count_plan_progress(plan: dict[str, Any]) -> tuple[int, int]:
    """Return (completed, total) action counts."""
    total = 0
    completed = 0
    for week in plan.get("weeks", []):
        for action in week.get("actions", []):
            total += 1
            if action.get("status") == "completed":
                completed += 1
    return completed, total


def compute_progress(
    baseline: dict[str, Any],
    current_ssi: dict[str, Any],
    current_acceptance: float,
    current_health: int,
    plan: dict[str, Any],
    *,
    ssi_available: bool = True,
) -> dict[str, Any]:
    """Compare baseline to current metrics, count completed actions."""
    from ..db.queries import (
        count_brand_engagements_since,
        count_brand_posts_since,
        count_brand_profile_changes_since,
    )

    completed, total = count_plan_progress(plan)
    since = int(baseline.get("captured_at") or 0)

    return {
        "ssi_before": baseline.get("ssi_score", 0),
        "ssi_now": current_ssi.get("score", 0),
        "ssi_change": current_ssi.get("score", 0) - baseline.get("ssi_score", 0),
        "ssi_available": ssi_available,
        "acceptance_before": baseline.get("acceptance_rate", 0),
        "acceptance_now": current_acceptance,
        "acceptance_change": current_acceptance - baseline.get("acceptance_rate", 0),
        "health_before": baseline.get("health_score", 0),
        "health_now": current_health,
        "health_change": current_health - baseline.get("health_score", 0),
        "actions_completed": completed,
        "actions_total": total,
        "actions_remaining": total - completed,
        "days_since_start": (int(time.time()) - baseline.get("captured_at", int(time.time()))) // 86400,
        "posts_published": count_brand_posts_since(since),
        "brand_engagements": count_brand_engagements_since(since),
        "profile_changes": count_brand_profile_changes_since(since),
    }


# ──────────────────────────────────────────────
# ICP context for brand optimization
# ──────────────────────────────────────────────


def get_active_icp_context() -> dict[str, Any]:
    """Extract ICP context from active campaigns for brand optimization.

    Aggregates target titles, industries, pain points, and keywords from
    all active campaigns so brand analysis and planning can tailor
    recommendations to the actual target audience.
    """
    campaigns = list_campaigns(status="active")

    all_titles: list[str] = []
    all_industries: list[str] = []
    all_pain_points: list[str] = []
    all_keywords: list[str] = []
    all_hooks: list[str] = []

    for campaign in campaigns:
        icp_raw = campaign.get("icp_json")
        if not icp_raw:
            continue
        icp_data = json.loads(icp_raw) if isinstance(icp_raw, str) else icp_raw

        for segment in icp_data.get("segments", []):
            all_titles.extend(segment.get("titles", []))
            all_industries.extend(segment.get("industries", []))
            kw = segment.get("keywords", "")
            if kw:
                all_keywords.append(kw)

        # Full ICP with personas (from generate_icp)
        for persona in icp_data.get("personas", []):
            all_pain_points.extend(persona.get("pain_points", []))

        hook = icp_data.get("relevance_hook", "")
        if hook:
            all_hooks.append(hook)

    return {
        "target_titles": list(dict.fromkeys(all_titles))[:15],
        "target_industries": list(dict.fromkeys(all_industries))[:10],
        "pain_points": list(dict.fromkeys(all_pain_points))[:10],
        "keywords": list(dict.fromkeys(all_keywords))[:10],
        "relevance_hooks": all_hooks[:5],
        "has_active_campaigns": len(campaigns) > 0,
    }


def format_icp_context(icp_context: dict[str, Any] | None) -> str:
    """Format ICP context dict into a human-readable string for LLM prompts."""
    if not icp_context or not icp_context.get("has_active_campaigns"):
        return "No active campaigns — optimize for general professional branding."

    parts: list[str] = []
    if icp_context.get("target_titles"):
        parts.append(f"Target roles: {', '.join(icp_context['target_titles'][:10])}")
    if icp_context.get("target_industries"):
        parts.append(f"Target industries: {', '.join(icp_context['target_industries'][:8])}")
    if icp_context.get("pain_points"):
        parts.append(f"Their pain points: {', '.join(icp_context['pain_points'][:8])}")
    if icp_context.get("relevance_hooks"):
        parts.append(f"Value proposition: {icp_context['relevance_hooks'][0]}")
    return "\n".join(parts) if parts else "No ICP details available."
