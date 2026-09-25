"""Every MCP tool call on this client is a ``tool.called`` record (api #1204).

Until 24 Sep 2026 a tool call left nothing behind but ``Running <tool>...``
in the local log, so a failure a person met in Claude, ChatGPT or Cursor
reached us only if they wrote in.

:func:`instrument` wraps every tool the server registered, once, after
registration. Each MCP call is timed and becomes a record of seven fields:
the tool, its ``action`` (an identifier word, else "other"), the MCP host
from a closed list, ok, the exception class, the duration and the transport.
Never the arguments, the result, a prospect, a URL or a message.

Records wait in memory and are posted in batches (50 records, or 30 seconds
after the first one) through the backend client to
``POST /api/v1/product-events/tool-calls``, under the signed-in workspace.
They are dropped silently when the user turned telemetry off
(``heylead config telemetry off`` or ``DO_NOT_TRACK``) or is not signed in.
Telemetry never costs the call: every failure here is swallowed.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import threading
import time
import uuid
from typing import Any, Callable

from .textutil import contains_term

logger = logging.getLogger(__name__)

#: Set on every wrapped tool function; tests/test_tool_call_telemetry.py reads it.
MARKER = "__heylead_tool_telemetry__"
MAX_BATCH = 50
FLUSH_AFTER_SECONDS = 30.0

# First match wins: Cursor's handshake says "cursor-vscode", Codex is an
# OpenAI client, so each is asked before the name it contains.
_CLIENT_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cursor", ("cursor",)),
    ("codex", ("codex",)),
    ("chatgpt", ("chatgpt", "openai")),
    ("gemini", ("gemini",)),
    ("vscode", ("vscode", "visual studio code", "copilot")),
    ("claude", ("claude", "anthropic")),
)
_ACTION = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_TOOL = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ERROR_CLASS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")

_lock = threading.Lock()
_pending: list[dict[str, Any]] = []
_timer: asyncio.TimerHandle | None = None
_tasks: set[asyncio.Task] = set()


def classify_client(name: str | None) -> str:
    """The MCP host as one word from a closed list, never the raw string."""
    text = str(name or "").strip()
    if not text:
        return "other"
    for client, terms in _CLIENT_TERMS:
        if any(contains_term(text, term) for term in terms):
            return client
    return "other"


def safe_action(value: Any) -> str:
    """``action`` when it is an identifier word; a sentence becomes "other"."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        return "other"
    candidate = value.strip().lower()
    if not candidate:
        return ""
    return candidate if _ACTION.fullmatch(candidate) else "other"


def _enabled() -> bool:
    from . import config

    try:
        return config.telemetry_enabled() and config.is_backend_mode()
    except Exception:  # noqa: BLE001 - an unreadable config sends nothing
        return False


def _request_context() -> Any:
    from mcp.server.lowlevel.server import request_ctx

    try:
        return request_ctx.get()
    except LookupError:
        return None


def _client_and_transport(rctx: Any) -> tuple[str, str]:
    name = ""
    session = getattr(rctx, "session", None)
    params = getattr(session, "client_params", None)
    info = getattr(params, "clientInfo", None)
    if info is not None:
        name = str(getattr(info, "name", "") or "")
    request = getattr(rctx, "request", None)
    if not name and request is not None:
        headers = getattr(request, "headers", None) or {}
        name = str(headers.get("user-agent", "") or "")
    return classify_client(name), ("http" if request is not None else "stdio")


def build_record(
    *, tool: str, action: str, rctx: Any, duration_s: float,
    error: BaseException | None,
) -> dict[str, Any] | None:
    """The seven-field record, or None when a name falls outside its shape."""
    if not _TOOL.fullmatch(tool):
        return None
    error_class = type(error).__name__ if error is not None else ""
    if error_class and not _ERROR_CLASS.fullmatch(error_class):
        error_class = "Exception"
    client, transport = _client_and_transport(rctx)
    return {
        "call_id": uuid.uuid4().hex,
        "occurred_at": int(time.time()),
        "tool": tool,
        "action": action,
        "client": client,
        "ok": error is None,
        "error_class": error_class,
        "duration_ms": max(0, min(int(round(duration_s * 1000)), 3_600_000)),
        "transport": transport,
    }


def enqueue(record: dict[str, Any]) -> None:
    """Buffer one record; flush at 50, or 30 seconds after the first."""
    global _timer
    if not _enabled():
        return
    with _lock:
        _pending.append(record)
        size = len(_pending)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if size >= MAX_BATCH:
        _spawn(loop)
    elif _timer is None:
        _timer = loop.call_later(FLUSH_AFTER_SECONDS, _spawn, loop)


def _spawn(loop: asyncio.AbstractEventLoop) -> None:
    task = loop.create_task(flush())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


_client: Any = None


def _backend() -> Any:
    """This module's own BackendClient for the signed-in workspace.

    Not ``get_linkedin_client()``: telemetry needs the backend only, never
    the direct-Unipile client, and holds its own so a swap of that factory
    (tests do it) cannot redirect or silence the post.
    """
    global _client
    from . import config
    from .linkedin.backend_client import BackendClient

    url, jwt_token = config.get_backend_config()
    if (
        _client is None
        or _client.base_url != url.rstrip("/")
        or _client.jwt_token != jwt_token
    ):
        _client = BackendClient(url, jwt_token)
    return _client


def _take() -> list[dict[str, Any]]:
    global _timer
    with _lock:
        batch = _pending[:MAX_BATCH]
        del _pending[:MAX_BATCH]
        if not _pending and _timer is not None:
            _timer.cancel()
            _timer = None
    return batch


async def flush() -> int:
    """Post what is waiting, 50 at a time; returns how many were posted.

    With telemetry off or no sign-in, the buffer is emptied and nothing is
    sent. A failed post drops its batch: a lost count beats a retry storm.
    """
    posted = 0
    while True:
        batch = _take()
        if not batch:
            return posted
        if not _enabled():
            continue
        try:
            status = await _backend().post_tool_calls(batch)
            if status < 400:
                posted += len(batch)
            else:
                logger.debug("tool-call telemetry refused: HTTP %s", status)
        except Exception:  # noqa: BLE001 - telemetry never costs the call
            logger.debug("tool-call telemetry post failed", exc_info=True)


def _record(tool: str, action: str, rctx: Any, started: float, error: BaseException | None) -> None:
    try:
        record = build_record(
            tool=tool, action=action, rctx=rctx,
            duration_s=time.perf_counter() - started, error=error,
        )
        if record is not None:
            enqueue(record)
    except Exception:  # noqa: BLE001 - telemetry never costs the call
        logger.debug("tool-call telemetry dropped for %s", tool, exc_info=True)


def telemetered(name: str, fn: Callable[..., Any], is_async: bool) -> Callable[..., Any]:
    """Wrap one tool function so each MCP call is timed and recorded.

    A call with no MCP request around it (the daemon or a test calling the
    function directly) is not an MCP tool call and is not recorded.
    """
    if getattr(fn, MARKER, False):
        return fn

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        rctx = _request_context()
        if rctx is None:
            result = fn(*args, **kwargs)
            return await result if is_async else result
        action = safe_action(kwargs.get("action"))
        started = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
            if is_async:
                result = await result
        except Exception as exc:
            _record(name, action, rctx, started, exc)
            raise
        _record(name, action, rctx, started, None)
        return result

    setattr(wrapper, MARKER, True)
    return wrapper


def instrument(mcp: Any) -> int:
    """Wrap every tool registered on ``mcp``; returns how many were wrapped.

    Runs after registration, so FastMCP has already read each function's
    signature for the argument schema; only the callable it runs changes.
    """
    wrapped = 0
    for tool in mcp._tool_manager.list_tools():
        if getattr(tool.fn, MARKER, False):
            continue
        tool.fn = telemetered(tool.name, tool.fn, tool.is_async)
        tool.is_async = True
        wrapped += 1
    return wrapped
