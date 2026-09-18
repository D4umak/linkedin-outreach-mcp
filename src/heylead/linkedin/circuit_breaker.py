"""Circuit breaker for Unipile API calls in collectors.

Tracks consecutive failures (timeouts AND rate limits) and trips after
CIRCUIT_BREAKER_THRESHOLD consecutive failures. Once tripped, all calls
fail fast for CIRCUIT_BREAKER_RESET_SECONDS before allowing another attempt.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Coroutine, TypeVar

from ..constants import (
    CIRCUIT_BREAKER_RESET_SECONDS,
    CIRCUIT_BREAKER_THRESHOLD,
    COLLECTOR_API_TIMEOUT,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitBreakerOpen(Exception):
    """Raised when the circuit breaker is open (too many consecutive failures)."""


class CollectorCircuitBreaker:
    """Per-collector circuit breaker for Unipile API calls."""

    # Exception messages containing these substrings are treated as rate limits
    _RATE_LIMIT_MARKERS = ("rate limit", "429", "too many requests")

    def __init__(self, name: str) -> None:
        self.name = name
        self._consecutive_failures = 0
        self._tripped_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._tripped_at is None:
            return False
        elapsed = time.time() - self._tripped_at
        if elapsed >= CIRCUIT_BREAKER_RESET_SECONDS:
            # Reset — allow a probe call
            self._tripped_at = None
            self._consecutive_failures = 0
            logger.info("Circuit breaker [%s] reset after %ds cooldown", self.name, int(elapsed))
            return False
        return True

    @staticmethod
    def _is_rate_limit(exc: Exception) -> bool:
        """Check if an exception represents a rate limit error."""
        msg = str(exc).lower()
        return any(m in msg for m in CollectorCircuitBreaker._RATE_LIMIT_MARKERS)

    def _record_failure(self, label: str, reason: str) -> None:
        """Record a failure and trip the breaker if threshold reached."""
        self._consecutive_failures += 1
        logger.warning(
            "Circuit breaker [%s]: %s %s (%d/%d consecutive)",
            self.name, label or "API call", reason,
            self._consecutive_failures, CIRCUIT_BREAKER_THRESHOLD,
        )
        if self._consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
            self._tripped_at = time.time()
            logger.error(
                "Circuit breaker [%s] TRIPPED after %d consecutive failures — "
                "skipping all calls for %ds",
                self.name, self._consecutive_failures, CIRCUIT_BREAKER_RESET_SECONDS,
            )

    async def call(
        self,
        coro: Coroutine[Any, Any, T],
        *,
        timeout: float = COLLECTOR_API_TIMEOUT,
        label: str = "",
    ) -> T:
        """Execute an API coroutine with timeout and circuit breaker protection.

        Args:
            coro: The awaitable API call.
            timeout: Per-call timeout in seconds.
            label: Human-readable label for logging.

        Returns:
            The result of the API call.

        Raises:
            CircuitBreakerOpen: If too many consecutive failures occurred.
            asyncio.TimeoutError: If the call times out (after recording).
            Exception: Re-raises rate limit errors (after recording).
        """
        if self.is_open:
            # The caller already built the coroutine
            # (``_cb.call(client.get_followers(...))``). Raising without
            # closing it leaks "never awaited" under gather().
            try:
                if inspect.iscoroutine(coro):
                    coro.close()
            finally:
                raise CircuitBreakerOpen(
                    f"Circuit breaker [{self.name}] is open — "
                    f"skipping {label or 'API call'} for {CIRCUIT_BREAKER_RESET_SECONDS}s"
                )

        try:
            result = await asyncio.wait_for(coro, timeout=timeout)
            # Success — reset counter
            if self._consecutive_failures > 0:
                logger.debug(
                    "Circuit breaker [%s] reset: %s succeeded after %d failures",
                    self.name, label or "call", self._consecutive_failures,
                )
            self._consecutive_failures = 0
            return result
        except asyncio.TimeoutError:
            self._record_failure(label, f"timed out after {timeout}s")
            raise
        except Exception as e:
            if self._is_rate_limit(e):
                self._record_failure(label, f"rate limited: {e}")
                raise
            # Non-rate-limit errors don't count toward circuit breaker
            raise
