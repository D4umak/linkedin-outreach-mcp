"""Async bridge — run sync DB calls off the event loop.

Provides ``run_db()`` which executes a synchronous callable in a dedicated
single-thread executor.  This prevents SQLite I/O (and any lock-retry
``time.sleep`` inside ``_UnclosableConnection._retry``) from blocking the
asyncio event loop that drives the MCP server and scheduler.

The executor uses exactly **one** worker thread so that all DB operations are
serialised, matching SQLite's single-writer constraint and satisfying the
``check_same_thread=False`` contract (only one non-main thread ever touches
the connection).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import threading
from typing import Any, Callable, TypeVar

T = TypeVar("T")

_db_executor: concurrent.futures.ThreadPoolExecutor | None = None
_lock = threading.Lock()

# Modules that only forward a call onwards. Attributing a DB write to one of
# them says nothing about who wanted the write.
_PLUMBING_FILES = frozenset({"async_bridge.py", "aio.py"})

# The caller that entered run_db, readable from inside the DB thread. A
# function running there cannot find it by walking its own stack: the thread's
# stack begins at the executor callable, and the caller is on the event loop's
# stack in a different thread entirely. queries.update_outreach uses this to
# record who changed an outreach status.
_call_source = threading.local()


def _caller_of_run_db() -> str:
    """``filename:lineno`` of the first frame outside the DB plumbing."""
    try:
        frame: Any = sys._getframe(1)
    except (AttributeError, ValueError):  # pragma: no cover - non-CPython
        return ""
    while frame is not None:
        name = frame.f_code.co_filename.rsplit("/", 1)[-1]
        if name not in _PLUMBING_FILES:
            return f"{name}:{frame.f_lineno}"
        frame = frame.f_back
    return ""


def current_call_source() -> str:
    """Caller that entered run_db for the call now running on the DB thread."""
    return getattr(_call_source, "value", "")


def _get_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Return (lazily-created) single-thread executor for DB work."""
    global _db_executor
    if _db_executor is None:
        with _lock:
            if _db_executor is None:
                _db_executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="heylead-db",
                )
    return _db_executor


async def run_db(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a sync DB function in the dedicated DB thread.

    Usage::

        result = await run_db(get_setting, "setup_complete", False)
        campaigns = await run_db(list_campaigns, status="active")
    """
    loop = asyncio.get_running_loop()
    source = _caller_of_run_db()

    def _call() -> T:
        _call_source.value = source
        try:
            return fn(*args, **kwargs)
        finally:
            _call_source.value = ""

    return await loop.run_in_executor(_get_executor(), _call)
