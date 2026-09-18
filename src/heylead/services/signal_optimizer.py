"""Signal self-optimization — daily automated tuning of weights, thresholds, keywords, and warm-up.

Runs once per day via JOB_OPTIMIZE_SIGNALS. Four sub-optimizers:
1. Weight optimizer — applies recommend_weight_adjustments() with guardrails
2. Keyword discoverer — adds high-performing keywords from successful outreaches
3. Warm-up adjuster — disables warm-up when data shows it hurts
4. Threshold tuner — adjusts activation thresholds based on conversion rates

All changes are logged to optimization_history for auditability and rollback.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..db.async_bridge import run_db

logger = logging.getLogger(__name__)

# Safety guardrails
MAX_WEIGHT_CHANGE_PCT = 0.20       # ±20% per cycle
WEIGHT_FLOOR = 0.05
WEIGHT_CEILING = 0.65
MIN_WEIGHT_CHANGE = 0.01           # Skip negligible changes
MIN_DATA_POINTS = 10               # Per signal type
MAX_THRESHOLD_CHANGE_PCT = 0.15    # ±15% per cycle
THRESHOLD_FLOOR = 0.30
THRESHOLD_CEILING = 0.85
WARMUP_IMPROVEMENT_THRESHOLD = 10  # Direct must beat warmup by 10pp

# Outreach status is a progression, so anything past 'connected' still counts
# as a conversion for optimization purposes. 'won' is not a status — the 'won'
# outcome is stored as 'closed_happy'.
_CONVERTED_STATUSES = (
    "connected", "messaged", "replied", "hot_lead", "closed_happy", "reverse_pitch",
)
_CONVERTED_SQL_IN = "(" + ", ".join(f"'{s}'" for s in _CONVERTED_STATUSES) + ")"


async def run_signal_optimization() -> dict[str, Any]:
    """Run the full daily optimization loop. Returns summary counts."""
    result = {
        "weights_adjusted": 0,
        "keywords_added": 0,
        "warmup_changes": 0,
        "threshold_changes": 0,
        "errors": [],
    }

    for name, fn in [
        ("weights", _optimize_signal_weights),
        ("keywords", _discover_keywords),
        ("warmup", _optimize_warmup),
        ("thresholds", _optimize_thresholds),
    ]:
        try:
            count = await fn()
            if name == "weights":
                result["weights_adjusted"] = count
            elif name == "keywords":
                result["keywords_added"] = count
            elif name == "warmup":
                result["warmup_changes"] = count
            elif name == "thresholds":
                result["threshold_changes"] = count
        except Exception as e:
            logger.error("Signal optimizer %s failed: %s", name, e, exc_info=True)
            result["errors"].append(f"{name}: {e}")

    total = sum(result[k] for k in ("weights_adjusted", "keywords_added", "warmup_changes", "threshold_changes"))
    logger.info(
        "Signal optimization complete: %d weights, %d keywords, %d warmup, %d thresholds",
        result["weights_adjusted"], result["keywords_added"],
        result["warmup_changes"], result["threshold_changes"],
    )
    return result


async def _optimize_signal_weights() -> int:
    """Apply weight recommendations from signal_analytics with guardrails."""
    from ..services.signal_scorer import _get_effective_weight, invalidate_weight_cache
    from ..db.signal_queries import upsert_weight_override, save_optimization_history

    def _get_recommendations():
        from ..services.signal_analytics import recommend_weight_adjustments
        return recommend_weight_adjustments(days=60, min_data_points=MIN_DATA_POINTS)

    recommendations = await run_db(_get_recommendations)
    if not recommendations:
        return 0

    count = 0
    for rec in recommendations:
        sig_type = rec["signal_type"]
        current = await run_db(_get_effective_weight, sig_type)

        # Skip negative/penalty signals
        if current < 0:
            continue

        suggested = rec["suggested_weight"]

        # Clamp to ±20% of current
        max_up = current * (1 + MAX_WEIGHT_CHANGE_PCT)
        max_down = current * (1 - MAX_WEIGHT_CHANGE_PCT)
        clamped = max(max_down, min(max_up, suggested))

        # Absolute bounds
        clamped = max(WEIGHT_FLOOR, min(WEIGHT_CEILING, clamped))
        clamped = round(clamped, 3)

        if rec.get("direction") == "increase" and clamped < current:
            logger.warning(
                "Skipping weight 'increase' for %s that would lower %.3f → %.3f",
                sig_type, current, clamped,
            )
            continue

        # Skip negligible
        if abs(clamped - current) < MIN_WEIGHT_CHANGE:
            continue

        await run_db(
            upsert_weight_override,
            signal_type=sig_type,
            weight=clamped,
            previous_weight=current,
            source="auto",
            applied_by="optimizer",
        )
        await run_db(
            save_optimization_history,
            optimization_type="weight",
            target=sig_type,
            before_value=str(round(current, 3)),
            after_value=str(clamped),
            reason=rec.get("reason", ""),
            data_points=rec.get("volume", 0),
            confidence=rec.get("performance_score", 0.0),
        )
        count += 1
        logger.info("Optimized weight: %s %.3f → %.3f (%s)", sig_type, current, clamped, rec.get("direction"))

    if count:
        invalidate_weight_cache()

    return count


async def _discover_keywords() -> int:
    """Find keywords from successful signal-triggered outreaches and add to watchlists."""
    from ..db.signal_queries import save_optimization_history

    now = int(time.time())
    cutoff = now - 30 * 86400

    # Find keywords that led to successful outreaches (connected or replied)
    def _find_winning_keywords():
        from ..db.schema import get_db
        db = get_db()
        rows = db.execute(
            f"""SELECT s.metadata_json, s.content
               FROM signals s
               JOIN outreaches o ON o.signal_id = s.id
               WHERE s.signal_type = 'keyword_mention'
                 AND s.detected_at >= ?
                 AND o.status IN {_CONVERTED_SQL_IN}""",
            (cutoff,),
        ).fetchall()
        db.close()
        return [dict(r) for r in rows]

    winning = await run_db(_find_winning_keywords)
    if not winning:
        return 0

    # Extract keywords
    keyword_counts: dict[str, int] = {}
    for row in winning:
        try:
            meta = json.loads(row.get("metadata_json") or "{}")
            for kw in meta.get("keywords_matched", []):
                kw_lower = kw.strip().lower()
                if kw_lower and len(kw_lower) > 3:
                    keyword_counts[kw_lower] = keyword_counts.get(kw_lower, 0) + 1
        except (json.JSONDecodeError, TypeError):
            pass

    # Get keywords the user explicitly removed — never re-add these
    def _get_user_removed_keywords():
        from ..db.signal_queries import list_optimization_history
        entries = list_optimization_history(optimization_type="user_removed_keyword", limit=500)
        return {e["target"].strip().lower() for e in entries}

    user_removed = await run_db(_get_user_removed_keywords)

    # Filter to keywords appearing in 2+ successful conversions, excluding user-removed
    strong_keywords = {
        kw: cnt for kw, cnt in keyword_counts.items()
        if cnt >= 2 and kw not in user_removed
    }
    if not strong_keywords:
        return 0

    # Get existing watchlist keywords to dedup
    def _get_existing_keywords():
        from ..db.schema import get_db
        db = get_db()
        rows = db.execute(
            "SELECT keywords FROM signal_watchlists WHERE is_active = 1 AND watch_type = 'keyword'"
        ).fetchall()
        db.close()
        existing = set()
        for r in rows:
            try:
                for kw in json.loads(r["keywords"] or "[]"):
                    existing.add(kw.strip().lower())
            except (json.JSONDecodeError, TypeError):
                pass
        return existing

    existing = await run_db(_get_existing_keywords)
    new_keywords = {kw: cnt for kw, cnt in strong_keywords.items() if kw not in existing}
    if not new_keywords:
        return 0

    # Take top 5 by count
    top = sorted(new_keywords.items(), key=lambda x: -x[1])[:5]

    # Find first active keyword watchlist to add to
    def _get_first_watchlist():
        from ..db.schema import get_db
        db = get_db()
        row = db.execute(
            "SELECT id, keywords FROM signal_watchlists WHERE is_active = 1 AND watch_type = 'keyword' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        db.close()
        return dict(row) if row else None

    wl = await run_db(_get_first_watchlist)
    if not wl:
        return 0

    # Add keywords to watchlist
    def _add_keywords(wl_id, current_json, new_kws):
        from ..db.schema import get_db
        db = get_db()
        try:
            current = json.loads(current_json or "[]")
        except (json.JSONDecodeError, TypeError):
            current = []
        current.extend(new_kws)
        db.execute(
            "UPDATE signal_watchlists SET keywords = ? WHERE id = ?",
            (json.dumps(current), wl_id),
        )
        db.commit()
        db.close()

    new_kw_list = [kw for kw, _ in top]
    await run_db(_add_keywords, wl["id"], wl.get("keywords"), new_kw_list)

    for kw, cnt in top:
        await run_db(
            save_optimization_history,
            optimization_type="keyword_added",
            target=kw,
            before_value="",
            after_value=f"added to watchlist {wl['id'][:8]}",
            reason=f"Appeared in {cnt} successful signal-triggered outreaches",
            data_points=cnt,
        )

    logger.info("Discovered %d new keywords from winning outreaches: %s", len(top), new_kw_list)
    return len(top)


async def _optimize_warmup() -> int:
    """Disable warm-up on campaigns where direct invite outperforms."""
    from ..db.signal_queries import save_optimization_history

    def _get_warmup_stats():
        from ..db.schema import get_db
        db = get_db()
        # Get active autopilot campaigns
        campaigns = db.execute(
            "SELECT id, name, config_json FROM campaigns WHERE status = 'active'"
        ).fetchall()
        results = []
        for camp in campaigns:
            camp_dict = dict(camp)
            camp_id = camp_dict["id"]
            # Count outreaches with warm-up engagements before invite
            warmup_stats = db.execute(
                f"""SELECT
                     COUNT(DISTINCT CASE WHEN e.id IS NOT NULL THEN o.id END) as with_warmup,
                     COUNT(DISTINCT CASE WHEN e.id IS NULL THEN o.id END) as without_warmup,
                     COUNT(DISTINCT CASE WHEN e.id IS NOT NULL AND o.status IN {_CONVERTED_SQL_IN} THEN o.id END) as warmup_connected,
                     COUNT(DISTINCT CASE WHEN e.id IS NULL AND o.status IN {_CONVERTED_SQL_IN} THEN o.id END) as direct_connected
                   FROM outreaches o
                   LEFT JOIN engagements e ON e.outreach_id = o.id AND e.created_at < o.invited_at
                   WHERE o.campaign_id = ? AND o.status != 'pending' AND o.invited_at > 0""",
                (camp_id,),
            ).fetchone()
            results.append({
                "id": camp_id,
                "name": camp_dict["name"],
                "config_json": camp_dict.get("config_json"),
                **dict(warmup_stats),
            })
        db.close()
        return results

    campaigns = await run_db(_get_warmup_stats)
    count = 0

    # Check which campaigns had user-set warmup preferences — don't override those
    def _get_user_warmup_decisions():
        from ..db.signal_queries import list_optimization_history
        entries = list_optimization_history(optimization_type="user_changed_warmup", limit=500)
        # Extract campaign names from target format "CampaignName::setting_key"
        return {e["target"].split("::")[0] for e in entries}

    user_managed_campaigns = await run_db(_get_user_warmup_decisions)

    for camp in campaigns:
        with_warmup = camp.get("with_warmup", 0)
        without_warmup = camp.get("without_warmup", 0)

        # Skip campaigns where user already manually set warmup preferences
        if camp.get("name") in user_managed_campaigns:
            continue

        # Need enough data in both groups
        if with_warmup < MIN_DATA_POINTS or without_warmup < MIN_DATA_POINTS:
            continue

        warmup_rate = camp.get("warmup_connected", 0) / with_warmup * 100
        direct_rate = camp.get("direct_connected", 0) / without_warmup * 100

        # Direct must outperform by WARMUP_IMPROVEMENT_THRESHOLD pp
        if direct_rate - warmup_rate < WARMUP_IMPROVEMENT_THRESHOLD:
            continue

        # Check current config
        try:
            config = json.loads(camp.get("config_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            config = {}

        # Already disabled?
        warmup_keys = ["enable_profile_views", "enable_follows", "enable_endorsements", "enable_engagements"]
        active_warmup = [k for k in warmup_keys if config.get(k, "on") == "on"]
        if not active_warmup:
            continue

        # Disable warm-up
        before_config = {k: config.get(k, "on") for k in warmup_keys}
        for k in warmup_keys:
            config[k] = "off"

        # Only the four warm-up switches this decision turns off. The config
        # was read at the top of this loop, and writing all of it back
        # reverted anything saved since (heylead-api#482, same shape here).
        from ..db import queries as _queries

        await run_db(
            _queries.merge_campaign_config,
            camp["id"],
            {k: "off" for k in warmup_keys},
        )
        await run_db(
            save_optimization_history,
            optimization_type="warmup",
            target=camp["name"],
            before_value=json.dumps(before_config),
            after_value=json.dumps({k: "off" for k in warmup_keys}),
            reason=f"Direct invite ({direct_rate:.0f}%) outperforms warm-up ({warmup_rate:.0f}%) by {direct_rate - warmup_rate:.0f}pp",
            data_points=with_warmup + without_warmup,
        )
        count += 1
        logger.info("Disabled warm-up on '%s': direct %.0f%% vs warmup %.0f%%", camp["name"], direct_rate, warmup_rate)

    return count


async def _optimize_thresholds() -> int:
    """Adjust signal activation thresholds based on conversion data."""
    from ..db.signal_queries import upsert_threshold_override, save_optimization_history
    from ..constants import SIGNAL_AUTO_OUTREACH_THRESHOLD, SIGNAL_BEHAVIORAL_AUTO_THRESHOLD

    now = int(time.time())
    cutoff = now - 30 * 86400

    def _get_activation_stats():
        from ..db.schema import get_db
        db = get_db()

        # How many signals were activated vs below threshold
        activated = db.execute(
            """SELECT COUNT(*) as cnt FROM signals
               WHERE action_taken IN ('outreach_created', 'behavioral_outreach_created', 'campaign_added')
                 AND detected_at >= ?""",
            (cutoff,),
        ).fetchone()["cnt"]

        below = db.execute(
            """SELECT COUNT(*) as cnt FROM signals
               WHERE action_taken = 'below_threshold'
                 AND detected_at >= ?""",
            (cutoff,),
        ).fetchone()["cnt"]

        # Of activated, how many converted
        converted = db.execute(
            f"""SELECT COUNT(DISTINCT o.id) as cnt FROM outreaches o
               JOIN signals s ON s.id = o.signal_id
               WHERE s.detected_at >= ?
                 AND o.status IN {_CONVERTED_SQL_IN}""",
            (cutoff,),
        ).fetchone()["cnt"]

        db.close()
        return {"activated": activated, "below_threshold": below, "converted": converted}

    stats = await run_db(_get_activation_stats)
    activated = stats["activated"]
    below = stats["below_threshold"]
    converted = stats["converted"]

    if activated < 20:
        return 0  # Not enough data

    conversion_rate = converted / activated if activated > 0 else 0
    count = 0

    # Tune auto_outreach threshold
    from ..services.signal_activator import _get_effective_threshold, invalidate_threshold_cache
    current_auto = await run_db(
        _get_effective_threshold, "auto_outreach", SIGNAL_AUTO_OUTREACH_THRESHOLD
    )

    if conversion_rate < 0.10 and activated > 20:
        # Too permissive — raise threshold
        new_val = min(current_auto * (1 + MAX_THRESHOLD_CHANGE_PCT), THRESHOLD_CEILING)
        new_val = round(new_val, 3)
        if abs(new_val - current_auto) >= 0.01:
            await run_db(
                upsert_threshold_override,
                threshold_name="auto_outreach",
                value=new_val,
                previous_value=current_auto,
            )
            await run_db(
                save_optimization_history,
                optimization_type="threshold",
                target="auto_outreach",
                before_value=str(current_auto),
                after_value=str(new_val),
                reason=f"Low conversion ({conversion_rate:.0%}) with {activated} activated — raising threshold",
                data_points=activated,
                confidence=conversion_rate,
            )
            count += 1
            logger.info("Raised auto_outreach threshold: %.3f → %.3f (conversion %.0f%%)", current_auto, new_val, conversion_rate * 100)

    elif conversion_rate > 0.40 and below > 50:
        # Leaving opportunities — lower threshold
        new_val = max(current_auto * (1 - MAX_THRESHOLD_CHANGE_PCT), THRESHOLD_FLOOR)
        new_val = round(new_val, 3)
        if abs(new_val - current_auto) >= 0.01:
            await run_db(
                upsert_threshold_override,
                threshold_name="auto_outreach",
                value=new_val,
                previous_value=current_auto,
            )
            await run_db(
                save_optimization_history,
                optimization_type="threshold",
                target="auto_outreach",
                before_value=str(current_auto),
                after_value=str(new_val),
                reason=f"High conversion ({conversion_rate:.0%}) with {below} below threshold — lowering to capture more",
                data_points=activated,
                confidence=conversion_rate,
            )
            count += 1
            logger.info("Lowered auto_outreach threshold: %.3f → %.3f (conversion %.0f%%)", current_auto, new_val, conversion_rate * 100)

    if count:
        invalidate_threshold_cache()

    return count
