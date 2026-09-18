"""MCP tool: show the strategy engine's current insights and learned patterns."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


async def run_show_strategy(campaign_id: str = "") -> str:
    """Build the strategy dashboard output."""
    from ..services.strategy_engine import get_strategy_insights

    insights = await get_strategy_insights()
    return _format_strategy(insights, campaign_id)


def _format_strategy(insights: dict[str, Any], campaign_id: str) -> str:
    """Format strategy insights for display."""
    out: list[str] = []
    summary = insights.get("summary", {})

    out.append("\U0001f9e0 **Strategy Engine Dashboard**\n")

    # ── Playbook Stats ──
    active_patterns = summary.get("active_patterns", 0)
    total_actions = summary.get("total_actions", 0)
    validated = summary.get("validated_actions", 0)
    rolled_back = summary.get("rolled_back_actions", 0)
    spawned = summary.get("spawned_campaigns", 0)

    out.append("**Playbook Stats:**")
    out.append(f"  Patterns discovered: {active_patterns}")
    out.append(f"  Actions taken: {total_actions} (validated: {validated}, rolled back: {rolled_back})")
    out.append(f"  Campaigns auto-spawned: {spawned}")

    # ── Revenue Pipeline ──
    funnel = insights.get("revenue_funnel", {})
    if funnel and funnel.get("total_pipeline"):
        out.append("\n**Revenue Pipeline:**")
        out.append(f"  Total pipeline: ${funnel.get('total_pipeline', 0):,.0f}")
        out.append(f"  Invited value: ${funnel.get('invited_value', 0):,.0f}")
        out.append(f"  Connected value: ${funnel.get('connected_value', 0):,.0f}")
        out.append(f"  Replied value: ${funnel.get('replied_value', 0):,.0f}")
        out.append(f"  Hot leads value: ${funnel.get('hot_value', 0):,.0f}")
        out.append(f"  Won value: ${funnel.get('won_value', 0):,.0f}")
        avg = funnel.get("avg_revenue", 0) or 0
        out.append(f"  Avg estimated revenue/prospect: ${avg:,.0f}")
    else:
        out.append("\n**Revenue Pipeline:** No data yet (revenue estimation runs automatically)")

    # ── Top Patterns ──
    patterns = insights.get("top_patterns", [])
    if patterns:
        out.append("\n**Top Patterns Discovered:**")
        for i, p in enumerate(patterns[:5], 1):
            ptype = p.get("pattern_type", "?")
            pkey = p.get("pattern_key", "?")
            desc = p.get("description", "")
            conf = p.get("confidence", 0)
            rev = p.get("estimated_revenue_impact", 0) or 0
            icon = _pattern_icon(ptype)
            out.append(f"  {i}. {icon} **{pkey}** ({ptype})")
            if desc:
                out.append(f"     {desc[:120]}")
            out.append(f"     Confidence: {conf:.0%} | Revenue impact: ${rev:,.0f}")
    else:
        out.append("\n**Patterns:** None yet (need more campaign data)")

    # ── Recent Actions ──
    actions = insights.get("recent_actions", [])
    if actions:
        out.append("\n**Recent Strategy Actions:**")
        for a in actions[:7]:
            atype = a.get("action_type", "?")
            status = a.get("status", "?")
            ts = a.get("created_at", 0)
            ts_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d %H:%M") if ts else "?"
            icon = _action_icon(atype)
            status_icon = _status_icon(status)

            # Extract key info from details
            details = {}
            if a.get("details_json"):
                import json
                try:
                    details = json.loads(a["details_json"])
                except (json.JSONDecodeError, TypeError):
                    pass

            detail_str = _format_action_detail(atype, details)
            out.append(f"  {icon} {atype} {status_icon} ({ts_str}) {detail_str}")
    else:
        out.append("\n**Actions:** None yet")

    # ── Spawn Candidates ──
    spawn_ready = insights.get("spawn_candidates_ready", 0)
    if spawn_ready > 0:
        out.append(f"\n**Spawn Candidates:** {spawn_ready} segments ready for auto-campaign creation")

    # ── Top Pattern Detail ──
    top = summary.get("top_pattern")
    if top:
        out.append(f"\n**Best Pattern:** {top.get('pattern_key', '?')} "
                    f"(${top.get('estimated_revenue_impact', 0):,.0f}/prospect, "
                    f"{top.get('confidence', 0):.0%} confidence)")

    out.append("\n\u2500" * 40)
    out.append("Strategy engine runs every 4 hours automatically.")
    out.append("Use campaign_report() for per-campaign strategy details.")

    return "\n".join(out)


def _pattern_icon(ptype: str) -> str:
    icons = {
        "icp_segment": "\U0001f3af",
        "revenue_segment": "\U0001f4b0",
        "messaging_tone": "\U0001f4ac",
        "engagement_tactic": "\U0001f91d",
        "timing": "\u23f0",
    }
    return icons.get(ptype, "\U0001f4ca")


def _action_icon(atype: str) -> str:
    icons = {
        "skip_segment": "\u23ed",
        "reorder_queue": "\U0001f504",
        "adjust_messaging": "\u270f\ufe0f",
        "spawn_campaign": "\U0001f680",
        "shift_budget": "\U0001f4b8",
    }
    return icons.get(atype, "\u2022")


def _status_icon(status: str) -> str:
    icons = {
        "applied": "\U0001f7e1",
        "measured": "\U0001f535",
        "validated": "\u2705",
        "rolled_back": "\u274c",
    }
    return icons.get(status, "\u2753")


def _format_action_detail(atype: str, details: dict) -> str:
    if atype == "skip_segment":
        return f"Skipped {details.get('skipped_count', '?')} prospects ({details.get('reason', '')[:60]})"
    if atype == "reorder_queue":
        return f"Reordered {details.get('reordered_count', '?')}/{details.get('total_pending', '?')} pending"
    if atype == "adjust_messaging":
        recs = details.get("recommendations", [])
        return f"{len(recs)} recommendations applied"
    if atype == "spawn_campaign":
        return f"Created '{details.get('campaign_name', '?')}'"
    return ""
