"""Correlation ID propagation via contextvars.

Set a correlation ID before executing a scheduler job, and it will
automatically appear in:
  - JSON log entries (via JsonFormatter in logging_setup.py)
  - HTTP headers (via BackendClient._headers())
  - Event log context (via engine.py)
"""

from __future__ import annotations

import contextvars
import uuid

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)


def new_correlation_id() -> str:
    """Generate and set a new correlation ID. Returns the new ID."""
    cid = str(uuid.uuid4())
    correlation_id.set(cid)
    return cid


def get_correlation_id() -> str:
    """Return the current correlation ID, or empty string."""
    return correlation_id.get()


def clear_correlation_id() -> None:
    """Reset the correlation ID."""
    correlation_id.set("")
