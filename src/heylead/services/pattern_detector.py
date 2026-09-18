"""Cross-campaign pattern detection for the strategy engine.

Aggregates performance data across ALL campaigns to discover
revenue-maximizing patterns in ICP segments, messaging, engagement,
and timing. Uses both SQL aggregation and LLM analysis.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..db.async_bridge import run_db
from ..ai.strategy_prompts import PATTERN_DETECTION_PROMPT, STRATEGY_ANALYSIS_SYSTEM
from ..constants import STRATEGY_MIN_SAMPLE_SIZE
from ..db.strategy_queries import (
    get_engagement_strategy_correlation,
    get_fit_score_outcome_correlation,
    get_lost_deal_profiles,
    get_revenue_weighted_funnel,
    get_segment_performance,
    get_timing_heatmap,
    get_won_deal_profiles,
    upsert_strategy_pattern,
)

logger = logging.getLogger(__name__)


async def detect_patterns() -> list[dict[str, Any]]:
    """Run cross-campaign pattern detection.

    1. Aggregate data via SQL queries
    2. Build snapshot for LLM
    3. Get LLM-identified patterns
    4. Persist discovered patterns

    Returns list of pattern dicts (including ID).
    """
    snapshot = await run_db(_build_pattern_snapshot)
    if not snapshot:
        logger.debug("Pattern detector: not enough data for analysis")
        return []

    # Run LLM analysis
    llm_result = await _analyze_patterns_llm(snapshot)
    if not llm_result:
        return []

    # Also detect heuristic patterns (no LLM needed)
    heuristic_patterns = await run_db(_detect_heuristic_patterns, snapshot)

    # Persist all patterns
    all_patterns = []

    for p in llm_result.get("patterns", []):
        try:
            pattern_id = await run_db(upsert_strategy_pattern,
                pattern_type=p.get("pattern_type", "icp_segment"),
                pattern_key=p.get("pattern_key", "unknown"),
                evidence_json=json.dumps({"evidence": p.get("evidence", ""), "source": "llm"}),
                description=p.get("description", ""),
                confidence=float(p.get("confidence", 0.5)),
                estimated_revenue_impact=float(p.get("estimated_revenue_impact", 0)),
            )
            p["id"] = pattern_id
            all_patterns.append(p)
        except Exception as e:
            logger.warning("Failed to persist LLM pattern: %s", e)

    for p in heuristic_patterns:
        try:
            pattern_id = await run_db(upsert_strategy_pattern,
                pattern_type=p["pattern_type"],
                pattern_key=p["pattern_key"],
                evidence_json=json.dumps(p.get("evidence", {})),
                description=p.get("description", ""),
                confidence=p.get("confidence", 0.5),
                estimated_revenue_impact=p.get("estimated_revenue_impact", 0),
                acceptance_rate=p.get("acceptance_rate"),
                reply_rate=p.get("reply_rate"),
                conversion_rate=p.get("conversion_rate"),
                sample_size=p.get("sample_size", 0),
            )
            p["id"] = pattern_id
            all_patterns.append(p)
        except Exception as e:
            logger.warning("Failed to persist heuristic pattern: %s", e)

    if all_patterns:
        logger.info("Pattern detector: found %d patterns", len(all_patterns))

    return all_patterns


def _build_pattern_snapshot() -> dict[str, Any] | None:
    """Aggregate all cross-campaign data for analysis."""
    segments = get_segment_performance()
    if not segments:
        return None

    # Check minimum data volume
    total_prospects = sum(s.get("total", 0) for s in segments)
    if total_prospects < STRATEGY_MIN_SAMPLE_SIZE:
        return None

    timing = get_timing_heatmap()
    engagement = get_engagement_strategy_correlation()
    fit_corr = get_fit_score_outcome_correlation()
    revenue_funnel = get_revenue_weighted_funnel()
    won_profiles = get_won_deal_profiles(limit=10)
    lost_profiles = get_lost_deal_profiles(limit=10)

    return {
        "segments": segments,
        "timing": timing,
        "engagement": engagement,
        "fit_correlation": fit_corr,
        "revenue_funnel": revenue_funnel,
        "won_profiles": won_profiles,
        "lost_profiles": lost_profiles,
        "total_prospects": total_prospects,
    }


async def _analyze_patterns_llm(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Send aggregated snapshot to LLM for pattern recognition."""
    # Format snapshot sections for the prompt
    segment_data = _format_segments(snapshot.get("segments", []))
    engagement_data = _format_engagement(snapshot.get("engagement", []))
    timing_data = _format_timing(snapshot.get("timing", []))
    fit_score_data = _format_fit_correlation(snapshot.get("fit_correlation", []))
    won_profiles = _format_profiles(snapshot.get("won_profiles", []), "won")
    lost_profiles = _format_profiles(snapshot.get("lost_profiles", []), "lost")
    revenue_funnel = json.dumps(snapshot.get("revenue_funnel", {}), indent=2)

    prompt = PATTERN_DETECTION_PROMPT.format(
        segment_data=segment_data,
        engagement_data=engagement_data,
        timing_data=timing_data,
        fit_score_data=fit_score_data,
        won_profiles=won_profiles,
        lost_profiles=lost_profiles,
        revenue_funnel=revenue_funnel,
    )

    from ..config import has_local_llm_key, is_backend_mode

    try:
        if is_backend_mode() and not has_local_llm_key():
            from ..linkedin import get_linkedin_client
            client = get_linkedin_client()
            try:
                result = await client.analyze_strategy(prompt)
            finally:
                await client.close()
            return result
        else:
            from ..ai.llm import LLMClient
            from ..ai.llm import loads_json_object as parse_json

            llm_client = LLMClient()
            raw = await llm_client.generate(
                prompt,
                system=STRATEGY_ANALYSIS_SYSTEM,
                temperature=0.3,
                max_tokens=3000,
            )
            return parse_json(raw, fallback={
                "patterns": [],
                "spawn_candidates": [],
                "campaign_adjustments": [],
            })
    except Exception as e:
        logger.error("Strategy LLM analysis failed: %s", e, exc_info=True)
        return None


def _detect_heuristic_patterns(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Detect patterns via pure heuristics (no LLM).

    Finds:
    - High-revenue segments with above-average conversion
    - Engagement strategies that clearly outperform
    - Optimal timing windows
    """
    patterns: list[dict[str, Any]] = []

    # 1. Segment patterns: find segments with above-avg revenue AND conversion
    segments = snapshot.get("segments", [])
    if segments:
        avg_revenue = sum(s.get("avg_revenue", 0) or 0 for s in segments) / len(segments)
        for s in segments:
            seg_rev = s.get("avg_revenue", 0) or 0
            won = s.get("won", 0)
            total = s.get("total", 0)
            if seg_rev > avg_revenue * 1.5 and won > 0 and total >= STRATEGY_MIN_SAMPLE_SIZE:
                patterns.append({
                    "pattern_type": "revenue_segment",
                    "pattern_key": f"{s.get('segment_company', 'unknown')}+{s.get('segment_title', 'unknown')}",
                    "description": f"High-revenue segment: {s.get('segment_company', '')} / {s.get('segment_title', '')} "
                                   f"with ${seg_rev:,.0f} avg revenue and {s.get('conversion_rate', 0)}% conversion",
                    "confidence": min(0.9, 0.3 + (total / 100)),
                    "estimated_revenue_impact": seg_rev,
                    "acceptance_rate": s.get("acceptance_rate"),
                    "reply_rate": s.get("reply_rate"),
                    "conversion_rate": s.get("conversion_rate"),
                    "sample_size": total,
                    "evidence": {
                        "avg_revenue": seg_rev,
                        "total": total,
                        "won": won,
                        "benchmark_avg_revenue": avg_revenue,
                    },
                })

    # 2. Engagement strategy patterns
    engagement = snapshot.get("engagement", [])
    if len(engagement) >= 2:
        best = max(engagement, key=lambda e: e.get("won", 0))
        if best.get("total", 0) >= STRATEGY_MIN_SAMPLE_SIZE and best.get("won", 0) > 0:
            patterns.append({
                "pattern_type": "engagement_tactic",
                "pattern_key": best.get("strategy", "unknown"),
                "description": f"Best engagement strategy: {best['strategy']} "
                               f"({best.get('acceptance_rate', 0)}% accept, {best.get('won', 0)} won)",
                "confidence": min(0.8, 0.3 + (best["total"] / 100)),
                "estimated_revenue_impact": best.get("avg_revenue", 0) or 0,
                "acceptance_rate": best.get("acceptance_rate"),
                "reply_rate": best.get("reply_rate"),
                "sample_size": best.get("total", 0),
                "evidence": {
                    "strategy": best["strategy"],
                    "total": best["total"],
                    "won": best.get("won", 0),
                    "all_strategies": [
                        {"name": e["strategy"], "total": e["total"], "won": e.get("won", 0)}
                        for e in engagement
                    ],
                },
            })

    # 3. Timing patterns: find best day-of-week
    timing = snapshot.get("timing", [])
    if timing:
        # Group by day of week
        day_stats: dict[int, dict] = {}
        for t in timing:
            dow = t.get("dow", 0)
            if dow not in day_stats:
                day_stats[dow] = {"invites": 0, "accepted": 0}
            day_stats[dow]["invites"] += t.get("invites", 0)
            day_stats[dow]["accepted"] += t.get("accepted", 0)

        day_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        best_day = max(
            day_stats.items(),
            key=lambda x: x[1]["accepted"] / max(x[1]["invites"], 1),
        )
        if best_day[1]["invites"] >= 10:
            rate = best_day[1]["accepted"] / best_day[1]["invites"] * 100
            patterns.append({
                "pattern_type": "timing",
                "pattern_key": f"best_day_{day_names[best_day[0]]}",
                "description": f"Best day for invitations: {day_names[best_day[0]]} "
                               f"({rate:.0f}% acceptance from {best_day[1]['invites']} invites)",
                "confidence": min(0.7, 0.2 + (best_day[1]["invites"] / 50)),
                "estimated_revenue_impact": 0,
                "acceptance_rate": rate,
                "sample_size": best_day[1]["invites"],
                "evidence": {
                    "day": day_names[best_day[0]],
                    "invites": best_day[1]["invites"],
                    "accepted": best_day[1]["accepted"],
                    "rate": rate,
                },
            })

    return patterns


# ──────────────────────────────────────────────
# Formatting Helpers
# ──────────────────────────────────────────────

def _format_segments(segments: list[dict]) -> str:
    if not segments:
        return "No segment data available."
    lines = []
    for s in segments[:20]:  # Top 20 by volume
        lines.append(
            f"- {s.get('segment_company', '?')} / {s.get('segment_title', '?')}: "
            f"{s.get('total', 0)} prospects, {s.get('acceptance_rate', 0)}% accept, "
            f"{s.get('reply_rate', 0)}% reply, {s.get('won', 0)} won, "
            f"${s.get('avg_revenue', 0):,.0f} avg revenue"
        )
    return "\n".join(lines)


def _format_engagement(engagement: list[dict]) -> str:
    if not engagement:
        return "No engagement data available."
    lines = []
    for e in engagement:
        lines.append(
            f"- {e.get('strategy', '?')}: {e.get('total', 0)} prospects, "
            f"{e.get('acceptance_rate', 0)}% accept, {e.get('reply_rate', 0)}% reply, "
            f"{e.get('won', 0)} won, ${e.get('avg_revenue', 0) or 0:,.0f} avg revenue"
        )
    return "\n".join(lines)


def _format_timing(timing: list[dict]) -> str:
    if not timing:
        return "No timing data available."
    day_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    lines = []
    for t in timing[:15]:
        dow = t.get("dow", 0)
        lines.append(
            f"- {day_names[dow]} {t.get('hour', 0):02d}:00: "
            f"{t.get('invites', 0)} invites, {t.get('acceptance_rate', 0)}% acceptance"
        )
    return "\n".join(lines)


def _format_fit_correlation(fit_data: list[dict]) -> str:
    if not fit_data:
        return "No fit score correlation data available."
    lines = []
    for f in fit_data:
        lines.append(
            f"- Fit {f.get('fit_bucket', '?')}: {f.get('total', 0)} prospects, "
            f"{f.get('acceptance_rate', 0)}% accept, {f.get('reply_rate', 0)}% reply, "
            f"{f.get('won', 0)} won, ${f.get('avg_revenue', 0) or 0:,.0f} avg revenue"
        )
    return "\n".join(lines)


def _format_profiles(profiles: list[dict], label: str) -> str:
    if not profiles:
        return f"No {label} deal profiles available."
    lines = []
    for p in profiles[:5]:
        lines.append(
            f"- {p.get('name', '?')} ({p.get('title', '?')} at {p.get('company', '?')}): "
            f"fit={p.get('fit_score', 0):.2f}, revenue=${p.get('estimated_revenue', 0):,.0f}, "
            f"variant={p.get('variant', '?')}, campaign={p.get('campaign_name', '?')}"
        )
    return "\n".join(lines)
