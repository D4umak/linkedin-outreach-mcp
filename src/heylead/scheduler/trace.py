"""Structured execution tracing for scheduler jobs (MCP client).

Collects step-by-step timing, API calls, LLM calls, decisions,
and errors within a single executor, then flushes as a rich event.
"""

from __future__ import annotations

import logging
import time
import traceback as _tb
from typing import Any

logger = logging.getLogger(__name__)


class ExecutionTrace:
    """Accumulates instrumentation data for one scheduler job execution."""

    def __init__(
        self,
        job_id: str,
        job_type: str,
        campaign_id: str = "",
        outreach_id: str = "",
    ) -> None:
        self.job_id = job_id
        self.job_type = job_type
        self.campaign_id = campaign_id
        self.outreach_id = outreach_id
        self.start_time = time.time()
        self.steps: list[dict[str, Any]] = []
        self.api_calls: list[dict[str, Any]] = []
        self.llm_calls: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self._current_step: str = ""
        self._step_start: float = 0.0

    def step(self, name: str) -> "ExecutionTrace":
        """Start a named step. Closes previous step automatically."""
        now = time.time()
        if self._current_step:
            self.steps.append({
                "name": self._current_step,
                "duration_ms": int((now - self._step_start) * 1000),
            })
        self._current_step = name
        self._step_start = now
        return self

    def log_api_call(
        self,
        endpoint: str,
        method: str,
        status_code: int,
        response_summary: str = "",
        duration_ms: int = 0,
        backend_ms: int = 0,
        cache_hit: bool | None = None,
    ) -> None:
        """Record a Unipile/LinkedIn API call."""
        entry: dict[str, Any] = {
            "endpoint": endpoint, "method": method, "status": status_code,
            "response": response_summary[:500], "ms": duration_ms,
        }
        if backend_ms:
            entry["backend_ms"] = backend_ms
        if cache_hit is not None:
            entry["cache_hit"] = cache_hit
        self.api_calls.append(entry)

    def log_llm_call(
        self,
        function: str,
        output_length: int = 0,
        duration_ms: int = 0,
        success: bool = True,
        error: str = "",
    ) -> None:
        """Record an LLM generation call."""
        entry: dict[str, Any] = {"fn": function, "out_len": output_length, "ms": duration_ms, "ok": success}
        if error:
            entry["error"] = error[:300]
        self.llm_calls.append(entry)

    def log_decision(self, decision: str, reason: str, details: dict[str, Any] | None = None) -> None:
        """Record a decision point."""
        entry: dict[str, Any] = {"decision": decision, "reason": reason[:300]}
        if details:
            entry["details"] = details
        self.decisions.append(entry)

    def log_error(self, error: Exception, context: str = "") -> None:
        """Record an error with type and stack frames."""
        frames = _tb.format_exception(type(error), error, error.__traceback__)
        short_tb = "".join(frames[-3:]) if len(frames) > 3 else "".join(frames)
        self.errors.append({
            "type": type(error).__name__, "msg": str(error)[:1000],
            "ctx": context[:200], "tb": short_tb[:1500],
        })

    def to_context(self) -> dict[str, Any]:
        """Build the full context dict."""
        now = time.time()
        if self._current_step:
            self.steps.append({"name": self._current_step, "duration_ms": int((now - self._step_start) * 1000)})
            self._current_step = ""
        ctx: dict[str, Any] = {"job_type": self.job_type, "total_ms": int((now - self.start_time) * 1000), "steps": self.steps}
        if self.api_calls:
            ctx["api_calls"] = self.api_calls
        if self.llm_calls:
            ctx["llm_calls"] = self.llm_calls
        if self.decisions:
            ctx["decisions"] = self.decisions
        if self.errors:
            ctx["errors"] = self.errors
        return ctx

    def flush(self, event_type: str = "job_trace") -> None:
        """Write the trace to the local event log. Fire-and-forget."""
        try:
            from ..db.queries import log_scheduler_event
            log_scheduler_event(
                event_type,
                outreach_id=self.outreach_id,
                campaign_id=self.campaign_id,
                job_id=self.job_id,
                context=self.to_context(),
            )
        except Exception as e:
            logger.warning("Trace flush failed (non-fatal): %s", e)
