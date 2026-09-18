"""File and JSON logging for MCP and the standalone daemon.

Must never write to stdout — that breaks MCP stdio. The daemon asks for a
stderr stream as well (launchd captures it); FastMCP already has one.
"""

from __future__ import annotations

import fcntl
import logging
import logging.handlers
import os
import sys
from datetime import datetime
from pathlib import Path

from . import config

# Kept open so the exclusive flock survives for the process lifetime.
_log_lock_handles: list = []


class JsonFormatter(logging.Formatter):
    """JSON Lines log formatter for machine-parseable logs."""

    def format(self, record: logging.LogRecord) -> str:
        import json as _json

        entry: dict = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(
                timespec="seconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        from .ops_log import _RESERVED_LOG_RECORD_KEYS
        for key, val in record.__dict__.items():
            if key in _RESERVED_LOG_RECORD_KEYS or key in entry or val is None:
                continue
            entry[key] = val
        if "correlation_id" not in entry:
            from .correlation import get_correlation_id
            cid = get_correlation_id()
            if cid:
                entry["correlation_id"] = cid
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)
        return _json.dumps(entry, default=str)


# Back-compat alias used by older tests / imports.
_JsonFormatter = JsonFormatter


def _under_pytest() -> bool:
    """True when this process is a test run.

    Checked via sys.modules rather than PYTEST_CURRENT_TEST: that variable is
    set per-test, and this function is consulted at import time (collection),
    when it would not yet exist.
    """
    return "pytest" in sys.modules


def _claim_log_rotation(lock_path: Path) -> bool:
    """Try to become the sole rotator for the shared log files.

    Returns True if this process won an exclusive non-blocking flock on
    ``lock_path``. The winner attaches ``RotatingFileHandler``; everyone
    else must watch, or concurrent rollover clobbers backups.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return False
    _log_lock_handles.append(fh)
    return True


def _shared_log_handler(
    path: str, *, rotate: bool, backup_count: int = 3,
) -> logging.Handler:
    """Rotating handler for the lock winner; watched handler for the rest."""
    if rotate:
        return logging.handlers.RotatingFileHandler(
            path,
            maxBytes=10 * 1024 * 1024,
            backupCount=backup_count,
        )
    return logging.handlers.WatchedFileHandler(path)


def _quiet_logger(name: str, env_key: str, default: str = "INFO") -> None:
    """Same knob style as HEYLEAD_HTTPX_LOG_LEVEL — default INFO to cut DEBUG."""
    level = getattr(
        logging, os.environ.get(env_key, default).upper(), logging.INFO,
    )
    logging.getLogger(name).setLevel(level)


def setup_logging(*, stderr: bool = False) -> None:
    """Configure logging to file (and optionally stderr). Never stdout.

    Two log files:
    - heylead.log      — plain text (human-readable, ``tail -f`` friendly)
    - heylead.json.log — JSON Lines (machine-parseable, ``jq`` friendly)

    FastMCP's Rich handler already writes to stderr for MCP stdio transport,
    so MCP callers pass stderr=False. The standalone daemon has no Rich
    handler and needs stderr=True so launchd's daemon.err.log is populated.

    No file handlers under pytest. This can run at module scope, so it fires
    on import — during collection, before conftest can redirect the home —
    and the duplicate-handler guard below then makes that binding permanent.
    """
    if _under_pytest():
        return

    config.ensure_dirs()
    log_file = config.log_path()
    json_log_file = config.json_log_path()

    root_logger = logging.getLogger("heylead")

    if root_logger.handlers:
        return

    root_logger.setLevel(logging.DEBUG)

    rotate = _claim_log_rotation(log_file.with_name("heylead.log.lock"))

    file_handler = _shared_log_handler(str(log_file), rotate=rotate)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root_logger.addHandler(file_handler)

    json_handler = _shared_log_handler(str(json_log_file), rotate=rotate, backup_count=7)
    json_handler.setLevel(logging.DEBUG)
    json_handler.setFormatter(JsonFormatter())
    root_logger.addHandler(json_handler)

    # launchd already owns StandardErrorPath (daemon.err.log). A StreamHandler
    # here duplicates every INFO+ line into an unbounded file, and FastMCP's
    # later import can add a second unformatted stderr handler on top.
    if stderr and not os.environ.get("HEYLEAD_LAUNCHD"):
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(logging.INFO)
        stream.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        root_logger.addHandler(stream)

    _httpx_level = getattr(
        logging, os.environ.get("HEYLEAD_HTTPX_LOG_LEVEL", "WARNING").upper(),
        logging.WARNING,
    )
    logging.getLogger("httpx").setLevel(_httpx_level)
    logging.getLogger("httpcore").setLevel(_httpx_level)
    # Measured 22–25 Aug: cloud_sync 42k DEBUG, heylead.ai.llm 31k, planner 17k.
    # post_analyzer was 1k DEBUG (the 26k mixed INFO/DEBUG window is not this logger).
    _quiet_logger("heylead.services.cloud_sync", "HEYLEAD_CLOUD_SYNC_LOG_LEVEL")
    _quiet_logger("heylead.ai.llm", "HEYLEAD_LLM_LOG_LEVEL")
    _quiet_logger("heylead.scheduler.planner", "HEYLEAD_PLANNER_LOG_LEVEL")


def drop_launchd_stderr_handlers() -> None:
    """Remove stderr StreamHandlers once launchd owns daemon.err.log."""
    if not os.environ.get("HEYLEAD_LAUNCHD"):
        return
    for name in ("heylead", ""):
        lg = logging.getLogger(name)
        for handler in list(lg.handlers):
            stream = getattr(handler, "stream", None)
            if isinstance(handler, logging.StreamHandler) and stream in (sys.stderr, sys.stdout):
                lg.removeHandler(handler)


# Back-compat alias.
_setup_logging = setup_logging
