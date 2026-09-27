"""Work a server starts after a tool has answered, and its end when the server stops.

A task or timer only the event loop holds dies with the loop. An MCP host ends
a stdio session by closing stdin; the server returns, the loop closes, and
whatever was still waiting goes with it. On 26 Sep 2026 that was a
``tool.called`` record waiting for a 30-second flush (api #1204). A task
nothing references can also be garbage-collected while it runs.

:func:`spawn` starts such work and holds it until it ends. :func:`drain`,
awaited by the server's lifespan and the daemon when they stop, fires the
:func:`before_drain` hooks (a buffer flushing early), gives running tasks
``DRAIN_SECONDS``, then cancels the rest and names them in the log.
``tests/test_background_tasks.py`` fails when loop work starts anywhere else.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

#: The MCP SDK's stdio client waits 2 s after closing stdin before it sends
#: SIGTERM, and Python's default SIGTERM ends the process on the spot. The
#: drain finishes inside that.
DRAIN_SECONDS = 1.5

_tasks: set[asyncio.Task] = set()
_before_drain: list[Callable[[], Any]] = []


def spawn(coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task | None:
    """Run ``coro`` on the running loop, held until it ends.

    Without a running loop there is nowhere to run it: the coroutine is
    closed and None is returned.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return None
    task = loop.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def before_drain(hook: Callable[[], Any]) -> Callable[[], Any]:
    """Register ``hook`` to run first when the server stops; returns it."""
    _before_drain.append(hook)
    return hook


async def drain(timeout: float | None = None) -> list[str]:
    """Let spawned work finish, up to ``timeout`` (DRAIN_SECONDS); returns what was cut off.

    For a process that is ending: the stdio server's lifespan and the daemon.
    """
    timeout = DRAIN_SECONDS if timeout is None else timeout
    for hook in list(_before_drain):
        try:
            hook()
        except Exception:  # noqa: BLE001 - one broken hook must not cost the rest
            logger.warning("background drain hook %r failed", hook, exc_info=True)
    loop = asyncio.get_running_loop()
    running = [t for t in _tasks if not t.done() and t.get_loop() is loop]
    if not running:
        return []
    _, unfinished = await asyncio.wait(running, timeout=timeout)
    for task in unfinished:
        task.cancel()
    names = sorted(task.get_name() for task in unfinished)
    if names:
        logger.info(
            "stopped with %d background task(s) unfinished after %.1fs: %s",
            len(names), timeout, ", ".join(names),
        )
    return names
