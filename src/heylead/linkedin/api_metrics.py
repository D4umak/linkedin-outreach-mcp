"""Lightweight in-memory metrics for Unipile API calls.

Collects per-endpoint success/failure counts, latency, and status code
distribution.  The metrics are flushed to ``scheduler_events`` each tick
by the engine — no new DB table is required.

Usage::

    from heylead.linkedin.api_metrics import api_metrics

    api_metrics.record("send_invitation", status_code=200, duration_ms=350)
    api_metrics.record("send_invitation", status_code=422, duration_ms=120, error="temporary_provider_limit")

    summary = api_metrics.summary()  # dict keyed by endpoint
    api_metrics.reset()               # call after flushing to scheduler_events
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field


@dataclass
class _EndpointMetrics:
    success: int = 0
    fail: int = 0
    total_ms: int = 0
    total_backend_ms: int = 0
    status_codes: Counter = field(default_factory=Counter)
    last_error: str = ""
    last_error_at: float = 0.0


class UnipileApiMetrics:
    """Collect per-endpoint API metrics in memory."""

    def __init__(self) -> None:
        self._endpoints: dict[str, _EndpointMetrics] = defaultdict(_EndpointMetrics)

    def record(
        self,
        endpoint: str,
        *,
        status_code: int,
        duration_ms: int = 0,
        error: str = "",
        backend_ms: int = 0,
    ) -> None:
        """Record the outcome of an API call."""
        m = self._endpoints[endpoint]
        m.status_codes[status_code] += 1
        m.total_ms += duration_ms
        m.total_backend_ms += backend_ms
        if 200 <= status_code < 300:
            m.success += 1
        else:
            m.fail += 1
            if error:
                m.last_error = error
                m.last_error_at = time.time()

    def summary(self) -> dict[str, dict]:
        """Return per-endpoint stats suitable for JSON serialisation."""
        result: dict[str, dict] = {}
        for ep, m in self._endpoints.items():
            total = m.success + m.fail
            entry: dict = {
                "total": total,
                "success": m.success,
                "fail": m.fail,
                "success_rate": round(m.success / total, 2) if total else 1.0,
                "avg_ms": round(m.total_ms / total) if total else 0,
                "status_codes": dict(m.status_codes),
                "last_error": m.last_error,
            }
            if m.total_backend_ms:
                entry["avg_backend_ms"] = round(m.total_backend_ms / total) if total else 0
            result[ep] = entry
        return result

    def failure_rate(self, endpoint: str, *, min_calls: int = 5) -> float | None:
        """Return failure rate for *endpoint*, or None if insufficient data."""
        m = self._endpoints.get(endpoint)
        if not m:
            return None
        total = m.success + m.fail
        if total < min_calls:
            return None
        return m.fail / total

    def reset(self) -> None:
        """Clear all tracked data (call after flushing to scheduler_events)."""
        self._endpoints.clear()


# Module-level singleton
api_metrics = UnipileApiMetrics()
