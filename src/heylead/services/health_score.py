"""LinkedIn Health Score — 0-100 composite safety & performance metric.

Combines four dimensions (25 pts each):
  1. SSI Score (Social Selling Index) — LinkedIn's own health metric
  2. Acceptance Rate — signal of outreach quality and targeting
  3. Volume Safety — how conservatively you're sending relative to limits
  4. Activity Consistency — regular daily activity avoids burst patterns

Thresholds:
  80-100  GREEN   Excellent — safe to ramp up
  60-79   YELLOW  Caution — monitor closely
  40-59   ORANGE  Warning — slow down
  0-39    RED     Danger — pause outreach immediately
"""

from __future__ import annotations

from dataclasses import dataclass, field



# ── Daily invitation cap ──

# The hosted service reports "no daily limit" two different ways and neither
# survives a plain ``dict.get(key, default)``: ``GET /api/v1/stats`` sends the
# key with a JSON **null**, and ``/api/v1/scheduler/changes`` sends **0**. A
# default only fires when the key is absent, so both slipped through — the null
# reached ``compute_health_score``'s ``if daily_limit > 0`` and raised, and the
# 0 was written into the local ``rate_limits`` column, where it has sat on
# every row since 2026-08-29 and made the operator's dashboard read "3/0".
DEFAULT_DAILY_INVITE_LIMIT = 15


def coerce_daily_limit(value: object, fallback: int = DEFAULT_DAILY_INVITE_LIMIT) -> int:
    """A usable daily invitation cap: never None, never zero, never negative.

    Neither null nor 0 is a limit worth dividing by or printing, so both fall
    back. Lives here because this module imports nothing from heylead, so the
    readers (show_status, daily_digest, scheduler_status, suggest_next_action)
    and the writer (cloud_sync) can all reach it without a cycle.
    """
    if isinstance(value, bool):
        return fallback  # int(True) is 1, which would read as a cap of one
    try:
        limit = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return fallback
    return limit if limit > 0 else fallback


# ── Thresholds ──

HEALTH_GREEN = 80
HEALTH_YELLOW = 60
HEALTH_ORANGE = 40


@dataclass
class HealthScore:
    """Composite LinkedIn health score."""

    total: int = 0
    ssi_score_raw: float = 0.0
    ssi_points: float = 0.0
    acceptance_rate: float = 0.0
    acceptance_points: float = 0.0
    volume_ratio: float = 0.0
    volume_points: float = 0.0
    consistency_ratio: float = 0.0
    consistency_points: float = 0.0
    level: str = "green"
    warnings: list[str] = field(default_factory=list)


def compute_health_score(
    *,
    ssi_score: float = 0.0,
    acceptance_rate: float = 0.0,
    total_sent: int = 0,
    daily_sent: int = 0,
    daily_limit: int = 40,
    weekly_sent: int = 0,
    weekly_limit: int = 100,
    sending_days_7d: int = 0,
) -> HealthScore:
    """Compute a 0-100 health score from account metrics.

    Args:
        ssi_score: LinkedIn SSI (0-100).
        acceptance_rate: Cumulative acceptance rate (0.0-1.0).
        total_sent: Total invitations sent (lifetime).
        daily_sent: Invitations sent today.
        daily_limit: Current adaptive daily limit.
        weekly_sent: Invitations sent in last 7 days.
        weekly_limit: Weekly cap.
        sending_days_7d: Number of days with invitations sent in last 7 days.
    """
    hs = HealthScore()
    hs.ssi_score_raw = ssi_score
    hs.acceptance_rate = acceptance_rate

    # No outreach yet — score based on SSI only, default healthy
    if total_sent == 0 and weekly_sent == 0:
        hs.ssi_points = min(ssi_score / 4.0, 25.0) if ssi_score > 0 else 20.0
        hs.acceptance_points = 20.0
        hs.volume_points = 25.0
        hs.consistency_points = 15.0
        raw = hs.ssi_points + hs.acceptance_points + hs.volume_points + hs.consistency_points
        hs.total = max(0, min(100, int(round(raw))))
        hs.level = "green" if hs.total >= HEALTH_GREEN else "yellow"
        return hs

    # ── 1. SSI Component (0-25) ──
    # SSI is already 0-100, scale to 0-25
    # If SSI not available, give a neutral 12.5
    hs.ssi_points = min(ssi_score / 4.0, 25.0) if ssi_score > 0 else 12.5

    # ── 2. Acceptance Rate Component (0-25) ──
    if total_sent < 5:
        # Not enough data — give benefit of the doubt
        hs.acceptance_points = 15.0
    elif acceptance_rate >= 0.40:
        hs.acceptance_points = 25.0
    elif acceptance_rate >= 0.30:
        hs.acceptance_points = 22.0
    elif acceptance_rate >= 0.25:
        hs.acceptance_points = 18.0
    elif acceptance_rate >= 0.15:
        hs.acceptance_points = 12.0
    elif acceptance_rate >= 0.10:
        hs.acceptance_points = 6.0
    else:
        hs.acceptance_points = 2.0

    # ── 3. Volume Safety (0-25) ──
    # How far below the limits are you? Staying under 70% of limits is ideal.
    daily_ratio = daily_sent / daily_limit if daily_limit > 0 else 0.0
    weekly_ratio = weekly_sent / weekly_limit if weekly_limit > 0 else 0.0
    hs.volume_ratio = max(daily_ratio, weekly_ratio)

    if hs.volume_ratio <= 0.5:
        hs.volume_points = 25.0  # Very conservative
    elif hs.volume_ratio <= 0.7:
        hs.volume_points = 20.0  # Good
    elif hs.volume_ratio <= 0.85:
        hs.volume_points = 14.0  # Moderate
    elif hs.volume_ratio <= 0.95:
        hs.volume_points = 7.0   # Pushing it
    else:
        hs.volume_points = 2.0   # At/over limit

    # ── 4. Activity Consistency (0-25) ──
    # Sending spread across multiple days is safer than burst patterns.
    if sending_days_7d == 0:
        # No recent activity — neutral
        hs.consistency_ratio = 0.0
        hs.consistency_points = 12.0
    else:
        hs.consistency_ratio = sending_days_7d / 7.0
        if hs.consistency_ratio >= 0.7:
            hs.consistency_points = 25.0  # 5+ days out of 7
        elif hs.consistency_ratio >= 0.5:
            hs.consistency_points = 20.0  # 4 days
        elif hs.consistency_ratio >= 0.3:
            hs.consistency_points = 14.0  # 2-3 days
        else:
            hs.consistency_points = 8.0   # 1 day burst

    # ── Total ──
    raw = hs.ssi_points + hs.acceptance_points + hs.volume_points + hs.consistency_points
    hs.total = max(0, min(100, int(round(raw))))

    # ── Level ──
    if hs.total >= HEALTH_GREEN:
        hs.level = "green"
    elif hs.total >= HEALTH_YELLOW:
        hs.level = "yellow"
    elif hs.total >= HEALTH_ORANGE:
        hs.level = "orange"
    else:
        hs.level = "red"

    # ── Warnings ──
    if total_sent >= 5 and acceptance_rate < 0.15:
        hs.warnings.append("Acceptance rate below 15% — improve targeting or messaging")
    if hs.volume_ratio > 0.85:
        hs.warnings.append("Approaching daily/weekly send limits — slow down")
    if sending_days_7d >= 3 and hs.consistency_ratio < 0.3:
        # Only warn if they've been active but in bursts
        pass
    if sending_days_7d == 1 and weekly_sent > 20:
        hs.warnings.append("Burst pattern detected — spread sends across more days")
    if ssi_score > 0 and ssi_score < 40:
        hs.warnings.append("SSI score is low — engage with content and grow your network")

    return hs


def format_health_score(hs: HealthScore) -> str:
    """Format health score for chat display."""
    level_display = {
        "green": "GREEN — Excellent",
        "yellow": "YELLOW — Caution",
        "orange": "ORANGE — Warning",
        "red": "RED — Danger",
    }

    lines = [
        f"Health Score: {hs.total}/100 ({level_display.get(hs.level, hs.level)})",
    ]

    # Component breakdown
    lines.append(f"├── SSI: {hs.ssi_points:.0f}/25" + (f" (SSI {hs.ssi_score_raw:.0f}/100)" if hs.ssi_score_raw > 0 else " (no data)"))
    lines.append(f"├── Acceptance: {hs.acceptance_points:.0f}/25" + (f" ({hs.acceptance_rate:.0%})" if hs.acceptance_rate > 0 else ""))
    lines.append(f"├── Volume: {hs.volume_points:.0f}/25" + (f" ({hs.volume_ratio:.0%} of limits)" if hs.volume_ratio > 0 else ""))
    lines.append(f"└── Consistency: {hs.consistency_points:.0f}/25" + (f" ({hs.consistency_ratio:.0%} of days)" if hs.consistency_ratio > 0 else ""))

    if hs.warnings:
        lines.append("")
        for w in hs.warnings:
            lines.append(f"⚠️  {w}")

    return "\n".join(lines)
