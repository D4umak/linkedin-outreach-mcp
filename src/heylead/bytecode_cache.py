"""Keep a bytecode cache when the host forbids __pycache__ (api #4472).

The Claude desktop app starts Code sessions with PYTHONDONTWRITEBYTECODE=1,
and the `uvx heylead` MCP servers they spawn inherit it. uvx runs each release
from a fresh cache env, so no .pyc was ever written there and every start
recompiled mcp, pydantic and server.py: twice the CPU, and on a loaded Mac 23%
of connects passed Claude Code's 30 s limit on 27 Oct 2026.

Writing to a prefix under ~/.heylead keeps the host's wish (no __pycache__
beside any source) and gives the next start its bytecode. Stdlib only: this
runs before the heavy imports it exists to speed up.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path
from typing import Optional

# A version another session may still be starting stays this long.
KEEP_SECONDS = 2 * 86400


def default_root() -> Path:
    from .config import _heylead_home

    return _heylead_home() / "cache" / "pyc"


def keep_bytecode(root: Optional[Path] = None, version: Optional[str] = None) -> Optional[Path]:
    """Turn bytecode writing back on, into a HeyLead-owned prefix.

    Returns the prefix in use, or None when the host already allows bytecode
    (nothing is changed then). A prefix the user chose is kept.
    """
    if not sys.dont_write_bytecode:
        return None
    if sys.pycache_prefix:
        sys.dont_write_bytecode = False
        return Path(sys.pycache_prefix)
    if version is None:
        from . import __version__ as version
    root = root or default_root()
    prefix = root / version
    try:
        prefix.mkdir(parents=True, exist_ok=True)
        prefix.touch()  # mtime marks it in use for the prune below
    except OSError:
        return None  # a read-only home: start as before, only slower
    _prune(root, keep=prefix)
    sys.pycache_prefix = str(prefix)
    sys.dont_write_bytecode = False
    return prefix


def _prune(root: Path, keep: Path) -> None:
    """Remove other versions' caches unused for KEEP_SECONDS (~13 MB each)."""
    cutoff = time.time() - KEEP_SECONDS
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry != keep and entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue
