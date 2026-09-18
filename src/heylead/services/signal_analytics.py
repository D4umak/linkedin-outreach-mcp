"""Service: signal_analytics — Signal correlation analytics + weight tuning.

Tracks which signal types actually correlate with conversions and provides
weight recommendations to auto-tune the signal scoring engine.

Key analytics:
- Per-signal-type conversion rates (signal → outreach → accept → reply → won)
- Signal ROI: which signal types produce the most revenue per detection
- Weight tuning recommendations based on actual conversion data
- Compound intent effectiveness tracking
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def compute_signal_conversion_rates(
    days: int = 30,
    campaign_id: str = "",
) -> dict[str, dict[str, Any]]:
    """Analyze which signal types correlate with conversions.

    For each signal_type:
    - How many signals detected?
    - How many signals → outreach created?
    - How many outreaches → accepted (connected)?
    - How many accepted → replied?
    - How many replied → won?

    Args:
        days: Lookback window in days.
        campaign_id: Optional campaign filter.

    Returns:
        {signal_type: {volume, outreach_rate, acceptance_rate, reply_rate, won_rate, won}}
    """
    from ..db.signal_queries import get_signal_to_conversion_funnel

    funnel = get_signal_to_conversion_funnel(days=days, campaign_id=campaign_id)
    by_type = funnel.get("by_type", {})

    result: dict[str, dict[str, Any]] = {}

    for sig_type, stats in by_type.items():
        signals = stats.get("signals", 0)
        outreaches = stats.get("outreaches", 0)
        connected = stats.get("connected", 0)
        replied = stats.get("replied", 0)
        won = stats.get("won", 0)

        result[sig_type] = {
            "volume": signals,
            "outreaches": outreaches,
            "connected": connected,
            "replied": replied,
            "won": won,
            "outreach_rate": outreaches / signals if signals > 0 else 0,
            "acceptance_rate": connected / outreaches if outreaches > 0 else 0,
            "reply_rate": replied / connected if connected > 0 else 0,
            "won_rate": won / outreaches if outreaches > 0 else 0,
            "activation_rate": stats.get("activation_rate", 0),
        }

    return result


def compute_signal_roi(days: int = 30) -> dict[str, dict[str, Any]]:
    """Compute signal ROI: won deals per signal detection.

    Returns per-type efficiency metrics that show which signal types
    produce the most value per detection.

    Returns:
        {signal_type: {detections, won, roi_per_signal, avg_time_to_won_days}}
    """
    from ..db.schema import get_db

    cutoff = int(time.time()) - (days * 86400)
    db = get_db()

    rows = db.execute(
        """SELECT
            s.signal_type,
            COUNT(DISTINCT s.id) as detections,
            COUNT(DISTINCT CASE WHEN o.status = 'closed_happy' THEN o.id END) as won,
            COUNT(DISTINCT CASE WHEN o.id IS NOT NULL THEN o.id END) as outreaches,
            AVG(CASE WHEN o.status = 'closed_happy' THEN o.updated_at - s.detected_at END) as avg_time_to_won
        FROM signals s
        LEFT JOIN outreaches o ON o.signal_id = s.id
        WHERE s.detected_at >= ?
        GROUP BY s.signal_type
        ORDER BY won DESC, detections DESC""",
        (cutoff,),
    ).fetchall()

    db.close()

    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        sig_type = row["signal_type"]
        detections = row["detections"] or 0
        won = row["won"] or 0
        avg_time = row["avg_time_to_won"]

        result[sig_type] = {
            "detections": detections,
            "outreaches": row["outreaches"] or 0,
            "won": won,
            "roi_per_signal": won / detections if detections > 0 else 0,
            "avg_time_to_won_days": (avg_time / 86400) if avg_time else None,
        }

    return result


def recommend_weight_adjustments(
    days: int = 60,
    min_data_points: int = 5,
) -> list[dict[str, Any]]:
    """Recommend weight adjustments based on actual conversion data.

    Compares actual conversion rates against current signal weights.
    Suggests increasing weights for high-converting types and decreasing
    weights for low-converting types.

    Args:
        days: Lookback window for conversion data.
        min_data_points: Minimum outreaches per signal type to make a recommendation.

    Returns:
        List of recommendations: [{signal_type, current_weight, suggested_weight,
                                    reason, conversion_rate, volume}]
    """
    from ..services.signal_scorer import SIGNAL_WEIGHTS, _get_effective_weight

    conversion_rates = compute_signal_conversion_rates(days=days)

    if not conversion_rates:
        return []

    # Calculate overall average conversion rate as baseline
    total_outreaches = sum(v["outreaches"] for v in conversion_rates.values())
    total_connected = sum(v["connected"] for v in conversion_rates.values())
    total_replied = sum(v["replied"] for v in conversion_rates.values())

    if total_outreaches == 0:
        return []

    avg_acceptance = total_connected / total_outreaches if total_outreaches > 0 else 0
    avg_reply = total_replied / total_connected if total_connected > 0 else 0

    recommendations: list[dict[str, Any]] = []

    for sig_type, stats in conversion_rates.items():
        outreaches = stats["outreaches"]
        if outreaches < min_data_points:
            continue  # Not enough data to recommend

        current_weight = _get_effective_weight(sig_type)
        acceptance_rate = stats["acceptance_rate"]
        reply_rate = stats["reply_rate"]

        # Composite performance score (weighted: 40% acceptance, 60% reply)
        if avg_acceptance > 0 and avg_reply > 0:
            performance = (
                0.4 * (acceptance_rate / avg_acceptance)
                + 0.6 * (reply_rate / avg_reply)
            )
        elif avg_acceptance > 0:
            performance = acceptance_rate / avg_acceptance
        else:
            continue

        # Map performance to weight adjustment
        # performance > 1.3 → increase weight by up to 30%
        # performance < 0.7 → decrease weight by up to 30%
        # 0.7 - 1.3 → no change
        if performance > 1.3:
            adjustment = min(performance - 1.0, 0.3)
            suggested = current_weight * (1 + adjustment)
            # An "increase" must never recommend a lower number than today.
            # The old 0.6 cap demoted curated types (0.70–0.90) while
            # logging "increase".
            suggested = max(current_weight, suggested)
            reason = (
                f"High conversion: {acceptance_rate:.0%} accept "
                f"({abs(acceptance_rate - avg_acceptance) / avg_acceptance:.0%} above avg), "
                f"{reply_rate:.0%} reply"
            )
            recommendations.append({
                "signal_type": sig_type,
                "current_weight": current_weight,
                "suggested_weight": round(suggested, 2),
                "direction": "increase",
                "reason": reason,
                "performance_score": round(performance, 2),
                "volume": outreaches,
                "acceptance_rate": acceptance_rate,
                "reply_rate": reply_rate,
            })
        elif performance < 0.7:
            adjustment = min(1.0 - performance, 0.3)
            suggested = max(current_weight * (1 - adjustment), 0.1)
            reason = (
                f"Low conversion: {acceptance_rate:.0%} accept "
                f"({abs(avg_acceptance - acceptance_rate) / avg_acceptance:.0%} below avg), "
                f"{reply_rate:.0%} reply"
            )
            recommendations.append({
                "signal_type": sig_type,
                "current_weight": current_weight,
                "suggested_weight": round(suggested, 2),
                "direction": "decrease",
                "reason": reason,
                "performance_score": round(performance, 2),
                "volume": outreaches,
                "acceptance_rate": acceptance_rate,
                "reply_rate": reply_rate,
            })

    # Sort: increases first (most impactful), then decreases
    recommendations.sort(key=lambda r: (-1 if r["direction"] == "increase" else 1, -r["volume"]))

    return recommendations


def compute_compound_intent_effectiveness(days: int = 30) -> dict[str, Any]:
    """Analyze effectiveness of compound intent events.

    Compares outcomes for prospects with compound intent events vs those
    with only individual signals.

    Returns:
        {
            "compound_stats": {total, actioned, outreaches, connected, replied, won},
            "single_stats": {total, outreaches, connected, replied, won},
            "lift": {acceptance, reply},
            "by_event_type": {type: {count, outreaches, won}},
        }
    """
    from ..db.schema import get_db

    cutoff = int(time.time()) - (days * 86400)
    db = get_db()

    # Compound intent events stats
    compound_row = db.execute(
        """SELECT
            COUNT(DISTINCT ie.id) as total,
            SUM(CASE WHEN ie.action_taken IS NOT NULL THEN 1 ELSE 0 END) as actioned
        FROM intent_events ie
        WHERE ie.detected_at >= ?""",
        (cutoff,),
    ).fetchone()

    # Outreaches for prospects WITH compound intent events
    # (any outreach for a linkedin_id that has an intent event)
    compound_outreach = db.execute(
        """SELECT
            COUNT(DISTINCT o.id) as outreaches,
            SUM(CASE WHEN o.status IN ('connected', 'messaged', 'replied', 'hot_lead', 'closed_happy', 'closed_unhappy', 'reverse_pitch', 'opted_out') THEN 1 ELSE 0 END) as connected,
            SUM(CASE WHEN o.status IN ('replied', 'hot_lead', 'closed_happy', 'closed_unhappy', 'reverse_pitch', 'opted_out') THEN 1 ELSE 0 END) as replied,
            SUM(CASE WHEN o.status = 'closed_happy' THEN 1 ELSE 0 END) as won
        FROM outreaches o
        JOIN contacts c ON o.contact_id = c.id
        WHERE c.linkedin_id IN (
            SELECT DISTINCT linkedin_id FROM intent_events WHERE detected_at >= ?
        )""",
        (cutoff,),
    ).fetchone()

    # By event type
    by_type_rows = db.execute(
        """SELECT
            ie.event_type,
            COUNT(DISTINCT ie.id) as count,
            COUNT(DISTINCT o.id) as outreaches,
            SUM(CASE WHEN o.status = 'closed_happy' THEN 1 ELSE 0 END) as won
        FROM intent_events ie
        LEFT JOIN contacts c ON c.linkedin_id = ie.linkedin_id
        LEFT JOIN outreaches o ON o.contact_id = c.id
        WHERE ie.detected_at >= ?
        GROUP BY ie.event_type
        ORDER BY count DESC""",
        (cutoff,),
    ).fetchall()

    db.close()

    compound_stats = {
        "total": (compound_row["total"] or 0) if compound_row else 0,
        "actioned": (compound_row["actioned"] or 0) if compound_row else 0,
        "outreaches": (compound_outreach["outreaches"] or 0) if compound_outreach else 0,
        "connected": (compound_outreach["connected"] or 0) if compound_outreach else 0,
        "replied": (compound_outreach["replied"] or 0) if compound_outreach else 0,
        "won": (compound_outreach["won"] or 0) if compound_outreach else 0,
    }

    by_type = {}
    for row in by_type_rows:
        by_type[row["event_type"]] = {
            "count": row["count"] or 0,
            "outreaches": row["outreaches"] or 0,
            "won": row["won"] or 0,
        }

    return {
        "compound_stats": compound_stats,
        "by_event_type": by_type,
    }


def generate_signal_report(
    days: int = 30,
    campaign_id: str = "",
) -> str:
    """Generate a comprehensive signal analytics report.

    Includes:
    - Signal conversion rates by type
    - Signal ROI analysis
    - Weight tuning recommendations
    - Compound intent effectiveness
    - Decay health metrics

    Returns formatted string for the signal_report MCP tool.
    """
    lines: list[str] = []
    lines.append("══ Signal Intelligence Report ══\n")

    # ── Section 1: Conversion Rates by Signal Type ──
    try:
        rates = compute_signal_conversion_rates(days=days, campaign_id=campaign_id)
        if rates:
            lines.append("── Signal Conversion Rates ──\n")
            lines.append(
                f"{'Signal Type':<22} {'Vol':>4} {'Out':>4} "
                f"{'Acc%':>5} {'Reply%':>7} {'Won':>4}"
            )
            lines.append("─" * 55)

            for sig_type, stats in sorted(
                rates.items(), key=lambda x: -x[1]["volume"]
            ):
                label = sig_type.replace("_", " ").title()[:20]
                vol = stats["volume"]
                out = stats["outreaches"]
                acc = stats["acceptance_rate"]
                reply = stats["reply_rate"]
                won = stats["won"]

                lines.append(
                    f"{label:<22} {vol:>4} {out:>4} "
                    f"{acc:>5.0%} {reply:>7.0%} {won:>4}"
                )

            lines.append("")
        else:
            lines.append("No signal conversion data available yet.\n")
    except Exception as e:
        logger.debug("Conversion rates section failed: %s", e)

    # ── Section 2: Signal ROI ──
    try:
        roi = compute_signal_roi(days=days)
        has_won = any(v["won"] > 0 for v in roi.values())
        if roi and has_won:
            lines.append("── Signal ROI (Won Deals) ──\n")
            for sig_type, stats in sorted(
                roi.items(), key=lambda x: -x[1]["won"]
            ):
                if stats["won"] == 0:
                    continue
                label = sig_type.replace("_", " ").title()
                det = stats["detections"]
                won = stats["won"]
                roi_val = stats["roi_per_signal"]
                time_str = ""
                if stats["avg_time_to_won_days"] is not None:
                    time_str = f" (avg {stats['avg_time_to_won_days']:.0f}d to close)"

                lines.append(
                    f"  {label}: {won} won / {det} detected "
                    f"({roi_val:.1%} ROI){time_str}"
                )
            lines.append("")
    except Exception as e:
        logger.debug("Signal ROI section failed: %s", e)

    # ── Section 3: Weight Tuning Recommendations ──
    try:
        recs = recommend_weight_adjustments(days=max(days, 60))
        if recs:
            lines.append("── Weight Tuning Recommendations ──\n")
            for rec in recs:
                label = rec["signal_type"].replace("_", " ").title()
                direction = "↑" if rec["direction"] == "increase" else "↓"
                current = rec["current_weight"]
                suggested = rec["suggested_weight"]
                reason = rec["reason"]
                vol = rec["volume"]

                lines.append(
                    f"  {direction} {label}: {current:.2f} → {suggested:.2f} "
                    f"({vol} outreaches)"
                )
                lines.append(f"    {reason}")
            lines.append("")
        else:
            lines.append("Weight tuning: Not enough data yet (need 5+ outreaches per type).\n")
    except Exception as e:
        logger.debug("Weight recommendations section failed: %s", e)

    # ── Section 4: Compound Intent Effectiveness ──
    try:
        compound = compute_compound_intent_effectiveness(days=days)
        cs = compound["compound_stats"]
        if cs["total"] > 0:
            lines.append("── Compound Intent Effectiveness ──\n")
            lines.append(f"Total intent events: {cs['total']} ({cs['actioned']} actioned)")

            if cs["outreaches"] > 0:
                acc_rate = cs["connected"] / cs["outreaches"] if cs["outreaches"] > 0 else 0
                reply_rate = cs["replied"] / cs["connected"] if cs["connected"] > 0 else 0
                lines.append(
                    f"Outreach: {cs['outreaches']} → {cs['connected']} connected "
                    f"({acc_rate:.0%}) → {cs['replied']} replied ({reply_rate:.0%}) "
                    f"→ {cs['won']} won"
                )

            by_type = compound["by_event_type"]
            if by_type:
                lines.append("")
                lines.append("By Event Type:")
                for etype, edata in sorted(
                    by_type.items(), key=lambda x: -x[1]["count"]
                ):
                    label = etype.replace("_", " ").title()
                    cnt = edata["count"]
                    out = edata["outreaches"]
                    won = edata["won"]
                    parts = [f"{cnt} events"]
                    if out:
                        parts.append(f"{out} outreaches")
                    if won:
                        parts.append(f"{won} won")
                    lines.append(f"  {label}: {' → '.join(parts)}")
            lines.append("")
    except Exception as e:
        logger.debug("Compound intent section failed: %s", e)

    # ── Section 5: Signal Health Metrics ──
    try:
        lines.append("── Signal Health ──\n")
        _add_signal_health(lines, days)
        lines.append("")
    except Exception as e:
        logger.debug("Signal health section failed: %s", e)

    return "\n".join(lines)


def _add_signal_health(lines: list[str], days: int) -> None:
    """Add signal health metrics to the report."""
    from ..db.schema import get_db

    now = int(time.time())
    cutoff = now - (days * 86400)
    db = get_db()

    # Total signals and status distribution
    status_rows = db.execute(
        "SELECT status, COUNT(*) as cnt FROM signals WHERE detected_at >= ? GROUP BY status",
        (cutoff,),
    ).fetchall()
    by_status = {r["status"]: r["cnt"] for r in status_rows}
    total = sum(by_status.values())

    # Expired count
    expired = db.execute(
        "SELECT COUNT(*) as cnt FROM signals WHERE status = 'expired' AND detected_at >= ?",
        (cutoff,),
    ).fetchone()
    expired_count = expired["cnt"] if expired else 0

    # Signals expiring soon (within 24h)
    expiring_soon = db.execute(
        "SELECT COUNT(*) as cnt FROM signals WHERE expires_at BETWEEN ? AND ? AND status NOT IN ('expired', 'actioned', 'dismissed')",
        (now, now + 86400),
    ).fetchone()
    expiring_count = expiring_soon["cnt"] if expiring_soon else 0

    # Average signal age
    avg_age = db.execute(
        "SELECT AVG(? - detected_at) as avg_age FROM signals WHERE detected_at >= ? AND status NOT IN ('expired')",
        (now, cutoff),
    ).fetchone()
    avg_age_days = (avg_age["avg_age"] / 86400) if avg_age and avg_age["avg_age"] else 0

    # Watchlist health
    watchlist_count = db.execute(
        "SELECT COUNT(*) as cnt FROM signal_watchlists WHERE is_active = 1"
    ).fetchone()

    db.close()

    lines.append(f"Total signals (last {days}d): {total}")
    pipeline_parts = []
    for status in ("new", "classified", "actioned", "expired", "dismissed"):
        cnt = by_status.get(status, 0)
        if cnt > 0:
            pipeline_parts.append(f"{status}: {cnt}")
    if pipeline_parts:
        lines.append(f"  Status: {' | '.join(pipeline_parts)}")

    if expired_count:
        lines.append(f"  Expired (processed): {expired_count}")
    if expiring_count:
        lines.append(f"  Expiring within 24h: {expiring_count}")
    if avg_age_days > 0:
        lines.append(f"  Avg signal age: {avg_age_days:.1f} days")

    wl_count = watchlist_count["cnt"] if watchlist_count else 0
    lines.append(f"Active watchlists: {wl_count}")
