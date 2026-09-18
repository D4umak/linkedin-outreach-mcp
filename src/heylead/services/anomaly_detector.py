"""Engagement anomaly detection — detect and alert on engagement irregularities.

Anomaly types:
1. Duplicate post engagement: Same post_id engaged multiple times by same account
2. Excessive same-company engagement: Multiple comments on same company's posts in 24h
3. Comment clustering: Burst of engagements in short time window
4. Failed engagement spike: Unusual number of failed engagements

Anomalies are stored as scheduler events and optionally trigger email alerts
via the backend API.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..db.async_bridge import run_db
from ..constants import (
    ANOMALY_ALERT_COOLDOWN_SECONDS,
    ANOMALY_BURST_THRESHOLD,
    ANOMALY_BURST_WINDOW_MINUTES,
    ANOMALY_DUPLICATE_POST_THRESHOLD,
    ANOMALY_FAILED_SPIKE_THRESHOLD,
    ANOMALY_SAME_COMPANY_THRESHOLD,
)
from ..db.queries import (
    get_duplicate_post_engagements,
    get_engagement_burst_count,
    get_failed_engagement_count,
    get_same_company_engagements,
    log_scheduler_event,
)

# Per-type cooldown tracker: {anomaly_type: last_alert_timestamp}
_alert_cooldowns: dict[str, float] = {}

logger = logging.getLogger(__name__)


@dataclass
class Anomaly:
    """Detected engagement anomaly."""

    anomaly_type: str  # 'duplicate_post', 'same_company_cluster', 'burst', 'failed_spike'
    severity: str  # 'warning', 'critical'
    summary: str  # Human-readable description
    details: dict = field(default_factory=dict)
    detected_at: int = 0

    def __post_init__(self) -> None:
        if not self.detected_at:
            self.detected_at = int(time.time())


def detect_anomalies(hours: int = 24) -> list[Anomaly]:
    """Run all anomaly detection checks. Returns list of detected anomalies."""
    anomalies: list[Anomaly] = []

    # 1. Duplicate post engagement (same post, same account)
    duplicates = get_duplicate_post_engagements(hours=hours)
    for dup in duplicates:
        if dup["cnt"] > ANOMALY_DUPLICATE_POST_THRESHOLD:
            anomalies.append(Anomaly(
                anomaly_type="duplicate_post",
                severity="critical" if dup["cnt"] >= 3 else "warning",
                summary=(
                    f"Post {dup['post_id'][:30]}... was engaged {dup['cnt']} times "
                    f"by account {(dup.get('account_id') or '?')[:10]}..."
                ),
                details={
                    "post_id": dup["post_id"],
                    "account_id": dup.get("account_id", ""),
                    "count": dup["cnt"],
                    "outreach_ids": dup["outreach_ids"],
                    "action_types": dup["action_types"],
                    "first_at": dup["first_at"],
                    "last_at": dup["last_at"],
                },
            ))

    # 2. Same-company clustering
    clusters = get_same_company_engagements(hours=hours)
    for cluster in clusters:
        if cluster["engagement_count"] > ANOMALY_SAME_COMPANY_THRESHOLD:
            anomalies.append(Anomaly(
                anomaly_type="same_company_cluster",
                severity="warning",
                summary=(
                    f"{cluster['engagement_count']} comments on posts related to "
                    f"'{cluster['company']}' in {hours}h"
                ),
                details={
                    "company": cluster["company"],
                    "prospect_name": cluster["prospect_name"],
                    "post_count": cluster["post_count"],
                    "engagement_count": cluster["engagement_count"],
                    "post_ids": cluster["post_ids"],
                },
            ))

    # 3. Burst detection
    burst_count = get_engagement_burst_count(minutes=ANOMALY_BURST_WINDOW_MINUTES)
    if burst_count > ANOMALY_BURST_THRESHOLD:
        anomalies.append(Anomaly(
            anomaly_type="burst",
            severity="warning" if burst_count < ANOMALY_BURST_THRESHOLD * 2 else "critical",
            summary=(
                f"{burst_count} engagements in the last {ANOMALY_BURST_WINDOW_MINUTES} min "
                f"(threshold: {ANOMALY_BURST_THRESHOLD})"
            ),
            details={
                "count": burst_count,
                "window_minutes": ANOMALY_BURST_WINDOW_MINUTES,
                "threshold": ANOMALY_BURST_THRESHOLD,
            },
        ))

    # 4. Failed engagement spike
    failed_count = get_failed_engagement_count(hours=hours)
    if failed_count > ANOMALY_FAILED_SPIKE_THRESHOLD:
        anomalies.append(Anomaly(
            anomaly_type="failed_spike",
            severity="warning" if failed_count < ANOMALY_FAILED_SPIKE_THRESHOLD * 2 else "critical",
            summary=(
                f"{failed_count} failed engagements in the last {hours}h "
                f"(threshold: {ANOMALY_FAILED_SPIKE_THRESHOLD})"
            ),
            details={
                "failed_count": failed_count,
                "hours": hours,
                "threshold": ANOMALY_FAILED_SPIKE_THRESHOLD,
            },
        ))

    # 5. Unipile API error rate spike (in-memory metrics, no DB query)
    try:
        from ..linkedin.api_metrics import api_metrics

        for endpoint, stats in api_metrics.summary().items():
            total = stats.get("total", 0)
            if total < 5:
                continue
            success_rate = stats.get("success_rate", 1.0)
            if success_rate <= 0.2:  # ≥80% failure
                anomalies.append(Anomaly(
                    anomaly_type="api_error_spike",
                    severity="critical",
                    summary=(
                        f"Unipile endpoint '{endpoint}' has {success_rate:.0%} success rate "
                        f"({stats['fail']}/{total} failed)"
                    ),
                    details={
                        "endpoint": endpoint,
                        "total": total,
                        "success": stats.get("success", 0),
                        "fail": stats.get("fail", 0),
                        "success_rate": success_rate,
                        "last_error": stats.get("last_error", ""),
                    },
                ))
            elif success_rate <= 0.5 and total >= 10:  # ≥50% failure with volume
                anomalies.append(Anomaly(
                    anomaly_type="api_error_spike",
                    severity="warning",
                    summary=(
                        f"Unipile endpoint '{endpoint}' degraded: {success_rate:.0%} success rate "
                        f"({stats['fail']}/{total} failed)"
                    ),
                    details={
                        "endpoint": endpoint,
                        "total": total,
                        "success": stats.get("success", 0),
                        "fail": stats.get("fail", 0),
                        "success_rate": success_rate,
                        "last_error": stats.get("last_error", ""),
                    },
                ))
    except Exception:
        pass  # api_metrics not available in tests or imports fail — skip silently

    # 6. Metric trend degradation (rolling window vs baseline)
    try:
        metric_anomalies = detect_metric_anomalies()
        anomalies.extend(metric_anomalies)
    except Exception:
        pass  # Metric trend data may not be available — skip silently

    return anomalies


def log_anomalies(anomalies: list[Anomaly]) -> None:
    """Log detected anomalies as scheduler events."""
    for anomaly in anomalies:
        log_scheduler_event(
            f"anomaly_{anomaly.anomaly_type}",
            context={
                "severity": anomaly.severity,
                "summary": anomaly.summary,
                **anomaly.details,
            },
        )
        logger.warning(
            "Engagement anomaly [%s/%s]: %s",
            anomaly.anomaly_type,
            anomaly.severity,
            anomaly.summary,
        )


async def run_anomaly_scan() -> list[Anomaly]:
    """Run a full anomaly scan, log results, and trigger alerts.

    Called by the scheduler every ANOMALY_SCAN_SECONDS.
    """
    anomalies = await run_db(detect_anomalies, hours=24)
    try:
        from .action_health import action_health_anomalies

        anomalies.extend(await run_db(action_health_anomalies))
    except Exception:
        pass

    if anomalies:
        await run_db(log_anomalies, anomalies)

        now = time.time()
        since = int(now) - ANOMALY_ALERT_COOLDOWN_SECONDS
        alertable: list[Anomaly] = []
        for a in anomalies:
            if a.severity != "critical":
                continue
            if a.anomaly_type == "action_health_firehose":
                from .action_health import was_action_health_alerted

                if await run_db(was_action_health_alerted, since):
                    continue
            elif now - _alert_cooldowns.get(a.anomaly_type, 0) < ANOMALY_ALERT_COOLDOWN_SECONDS:
                continue
            alertable.append(a)
        if alertable:
            sent = await _send_anomaly_alert(alertable)
            if sent:
                from .action_health import mark_action_health_alerted

                for a in alertable:
                    _alert_cooldowns[a.anomaly_type] = now
                    if a.anomaly_type == "action_health_firehose":
                        await run_db(mark_action_health_alerted, a, now=int(now))

    return anomalies


async def _send_anomaly_alert(anomalies: list[Anomaly]) -> bool:
    """Send alert via backend API endpoint. True when the backend accepted it."""
    try:
        from ..config import get_backend_config

        backend_url, jwt_token = get_backend_config()
        if not backend_url or not jwt_token:
            logger.debug("No backend configured, skipping anomaly alert email")
            return False

        import httpx

        payload = {"anomalies": [asdict(a) for a in anomalies]}

        # Shared header builder, not a hand-rolled Authorization line: it is the
        # only place X-Org-Id (the workspace the user has selected) is attached.
        from .cloud_sync import _headers

        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(
                f"{backend_url}/api/v1/alerts/engagement-anomaly",
                json=payload,
                headers=_headers(),
            )
            if resp.status_code == 200:
                logger.info("Anomaly alert sent to backend (%d anomalies)", len(anomalies))
                return True
            logger.warning(
                "Anomaly alert failed: HTTP %d — %s",
                resp.status_code,
                resp.text[:100],
            )
            return False
    except Exception as e:
        logger.warning("Failed to send anomaly alert: %s", e)
        return False


def check_post_engagement_anomaly(post_id: str, account_id: str = "") -> Anomaly | None:
    """Real-time check: call after each engagement to detect immediate duplicates.

    Returns an Anomaly if this post_id was already engaged more than once, or None.
    This is a lightweight single-post check, not a full scan.
    """
    if not post_id:
        return None

    from ..db.schema import get_db

    db = get_db()
    if account_id:
        row = db.execute(
            "SELECT COUNT(*) as c FROM engagements WHERE post_id = ? AND account_id = ?",
            (post_id, account_id),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT COUNT(*) as c FROM engagements WHERE post_id = ?",
            (post_id,),
        ).fetchone()
    db.close()

    count = row["c"] if row else 0
    if count > 1:  # Already engaged more than once
        return Anomaly(
            anomaly_type="duplicate_post",
            severity="critical" if count >= 3 else "warning",
            summary=f"Post {post_id[:30]}... now has {count} engagements from same account",
            details={"post_id": post_id, "account_id": account_id, "total_engagements": count},
        )
    return None


def detect_metric_anomalies(days: int = 14) -> list[Anomaly]:
    """Detect gradual degradation of key metrics over time.

    Compares last 3-day rolling window vs 14-day baseline.
    Flags if >20% degradation OR >2 standard deviations below baseline mean.
    """
    import math

    from ..constants import (
        ANOMALY_METRIC_DEGRADATION_PCT,
        ANOMALY_METRIC_MIN_DATAPOINTS,
        ANOMALY_METRIC_STD_DEV_THRESHOLD,
    )
    from ..db.queries import get_metric_trend_data

    data = get_metric_trend_data(days=days)
    anomalies: list[Anomaly] = []

    metric_labels = {
        "acceptance_rate": "Acceptance rate",
        "reply_rate": "Reply rate",
        "avg_job_duration_ms": "Avg job duration",
        "engagement_success_rate": "Engagement success rate",
    }
    # For duration, higher = worse (degradation means increase, not decrease)
    higher_is_worse = {"avg_job_duration_ms"}

    for metric_name, label in metric_labels.items():
        values = [d["value"] for d in data.get(metric_name, []) if d["value"] is not None]
        if len(values) < ANOMALY_METRIC_MIN_DATAPOINTS + 3:
            continue  # Not enough data

        # Split: baseline = all except last 3, recent = last 3
        baseline = values[:-3]
        recent = values[-3:]

        if len(baseline) < ANOMALY_METRIC_MIN_DATAPOINTS:
            continue

        baseline_mean = sum(baseline) / len(baseline)
        recent_mean = sum(recent) / len(recent)

        if baseline_mean == 0:
            continue

        # Standard deviation
        variance = sum((x - baseline_mean) ** 2 for x in baseline) / len(baseline)
        std_dev = math.sqrt(variance) if variance > 0 else 0

        # Check degradation direction
        if metric_name in higher_is_worse:
            # Higher duration = worse
            pct_change = (recent_mean - baseline_mean) / baseline_mean
            is_pct_degraded = pct_change > ANOMALY_METRIC_DEGRADATION_PCT
            is_std_degraded = (
                recent_mean > baseline_mean + ANOMALY_METRIC_STD_DEV_THRESHOLD * std_dev
                if std_dev > 0 else False
            )
            direction = "increased"
        else:
            # Lower rate = worse
            pct_change = (baseline_mean - recent_mean) / baseline_mean
            is_pct_degraded = pct_change > ANOMALY_METRIC_DEGRADATION_PCT
            is_std_degraded = (
                recent_mean < baseline_mean - ANOMALY_METRIC_STD_DEV_THRESHOLD * std_dev
                if std_dev > 0 else False
            )
            direction = "decreased"

        if is_pct_degraded or is_std_degraded:
            severity = "critical" if is_pct_degraded and is_std_degraded else "warning"
            anomalies.append(Anomaly(
                anomaly_type="metric_degradation",
                severity=severity,
                summary=(
                    f"{label} {direction}: "
                    f"{_fmt_metric(baseline_mean, metric_name)} -> "
                    f"{_fmt_metric(recent_mean, metric_name)} "
                    f"({pct_change:+.0%} vs {days}d baseline)"
                ),
                details={
                    "metric": metric_name,
                    "baseline_mean": round(baseline_mean, 4),
                    "recent_mean": round(recent_mean, 4),
                    "std_dev": round(std_dev, 4),
                    "pct_change": round(pct_change, 4),
                    "baseline_days": len(baseline),
                    "recent_days": 3,
                },
            ))

    return anomalies


def _fmt_metric(value: float, metric_name: str) -> str:
    """Format a metric value for display."""
    if "rate" in metric_name:
        return f"{value:.1%}"
    if "ms" in metric_name:
        return f"{value:.0f}ms"
    return f"{value:.2f}"
