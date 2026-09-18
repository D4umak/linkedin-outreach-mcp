"""Optional OpenTelemetry tracing — no-op when OTel is not installed.

Usage in scheduler executors or services:

    from ..tracing import tracer

    with tracer.start_as_current_span("execute_invite") as span:
        span.set_attribute("job.type", "invite")
        span.set_attribute("campaign.id", campaign_id)
        # ... do work ...

When ``opentelemetry-api`` is not installed, ``tracer`` is a no-op proxy
that silently ignores all calls. This avoids adding a hard dependency.

To enable real tracing:
    pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-gcp-trace
    # Then configure via env vars: OTEL_SERVICE_NAME=heylead, etc.
"""

from __future__ import annotations

import contextlib
from typing import Any


class _NoOpSpan:
    """Span that does nothing."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        pass

    def record_exception(self, exception: BaseException) -> None:
        pass

    def __enter__(self) -> "_NoOpSpan":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class _NoOpTracer:
    """Tracer that produces no-op spans."""

    @contextlib.contextmanager
    def start_as_current_span(self, name: str, **kwargs: Any):
        yield _NoOpSpan()


def _get_tracer():
    """Return a real OTel tracer if available, else a no-op."""
    try:
        from opentelemetry import trace
        return trace.get_tracer("heylead")
    except ImportError:
        return _NoOpTracer()


tracer = _get_tracer()
