"""Per-endpoint Voyager health tracker.

Tracks success/failure for each Voyager raw-route endpoint with a sliding
time window.  Lives entirely in memory — no DB table needed.

Usage::

    from heylead.linkedin.voyager_health import voyager_health

    voyager_health.record("follow", success=False)
    if not voyager_health.is_healthy("follow"):
        return {"success": False, "voyager_down": True}
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _EndpointStats:
    records: list[tuple[float, bool]] = field(default_factory=list)


class VoyagerHealthTracker:
    """Track per-endpoint Voyager health in memory (sliding window)."""

    def __init__(self, window_seconds: int = 3600) -> None:
        self._window = window_seconds
        self._endpoints: dict[str, _EndpointStats] = {}

    def _prune(self, stats: _EndpointStats) -> None:
        cutoff = time.time() - self._window
        stats.records = [(ts, ok) for ts, ok in stats.records if ts >= cutoff]

    def record(self, endpoint: str, *, success: bool) -> None:
        """Record the outcome of a Voyager API call."""
        stats = self._endpoints.setdefault(endpoint, _EndpointStats())
        stats.records.append((time.time(), success))
        self._prune(stats)

    def is_healthy(self, endpoint: str, *, min_success_rate: float = 0.3, min_calls: int = 3) -> bool:
        """Return True if the endpoint has an acceptable success rate.

        If fewer than *min_calls* have been recorded in the window, the
        endpoint is considered healthy (benefit of the doubt).
        """
        stats = self._endpoints.get(endpoint)
        if stats is None:
            return True
        self._prune(stats)
        if len(stats.records) < min_calls:
            return True
        successes = sum(1 for _, ok in stats.records if ok)
        return (successes / len(stats.records)) >= min_success_rate

    def summary(self) -> dict[str, dict]:
        """Return per-endpoint stats for diagnostics."""
        now = time.time()
        cutoff = now - self._window
        result: dict[str, dict] = {}
        for ep, stats in self._endpoints.items():
            self._prune(stats)
            total = len(stats.records)
            successes = sum(1 for _, ok in stats.records if ok)
            result[ep] = {
                "total": total,
                "success": successes,
                "fail": total - successes,
                "success_rate": round(successes / total, 2) if total else 1.0,
                "healthy": self.is_healthy(ep),
            }
        return result

    def reset(self) -> None:
        """Clear all tracked data."""
        self._endpoints.clear()


# Module-level singleton
voyager_health = VoyagerHealthTracker()
