"""Async wrappers for all DB functions — safe to call from async code.

Usage::

    from ..db import aio as db

    campaigns = await db.list_campaigns(status="active")
    await db.save_setting("key", "value")

Every public function from ``queries``, ``signal_queries``, ``strategy_queries``,
``post_queries``, ``global_contact_queries``, and ``strategist_queries`` is
available here as an ``async def`` that delegates through :func:`run_db`
(single-thread executor), keeping the event loop unblocked.

New functions added to any source module are automatically available here —
no maintenance required.
"""

from __future__ import annotations

import functools
import inspect
import types
from typing import Any

from .async_bridge import run_db


def _wrap(fn: Any) -> Any:
    """Create an async wrapper that delegates *fn* through run_db."""

    @functools.wraps(fn)
    async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
        return await run_db(fn, *args, **kwargs)

    return _async_wrapper


def _export_module(mod: types.ModuleType) -> dict[str, Any]:
    """Return {name: async_wrapper} for every public callable in *mod*."""
    out: dict[str, Any] = {}
    for name in dir(mod):
        if name.startswith("_"):
            continue
        obj = getattr(mod, name)
        if callable(obj) and not isinstance(obj, type) and not inspect.ismodule(obj):
            out[name] = _wrap(obj)
    return out


# --- Auto-wrap all DB modules -------------------------------------------

from . import queries as _queries  # noqa: E402
from . import signal_queries as _signal_queries  # noqa: E402
from . import strategy_queries as _strategy_queries  # noqa: E402
from . import post_queries as _post_queries  # noqa: E402
from . import global_contact_queries as _global_contact_queries  # noqa: E402
from . import strategist_queries as _strategist_queries  # noqa: E402

# Populate module namespace — later modules override earlier on name collisions,
# but in practice there are none.
_ns = globals()
for _mod in (
    _queries,
    _signal_queries,
    _strategy_queries,
    _post_queries,
    _global_contact_queries,
    _strategist_queries,
):
    _ns.update(_export_module(_mod))

# Clean up helper references from module namespace
del _ns, _mod
