"""File-based leader election for the scheduler singleton.

Ensures only one process runs the SchedulerEngine. Others serve MCP tools
normally and skip the scheduler.

Uses fcntl.flock() (Unix) or msvcrt.locking() (Windows) for a non-blocking
exclusive lock on ~/.heylead/data/scheduler.lock. The OS releases the lock when
the process exits, even on SIGKILL, so there are no stale-lock problems.

WHAT THE LOCK ALONE DOES NOT SOLVE. First-come-first-served means whichever
process starts first schedules, regardless of what code it is running. An
orphaned MCP server on 0.10.163 held leadership for hours while a process with
the fix sat as a follower — the bug stayed live because the old code owned the
lock. So the holder now publishes who it is, and a newer holder can ask it to
step down:

  - the leader writes {pid, version, started_at, kind} into the lock file
  - a process that fails to acquire reads that record; if its own version is
    strictly newer, it writes a yield request naming its version
  - the holder checks for a yield request on each tick and releases

Cooperative by design. Nothing signals or kills another process: a leader that
ignores the request (crashed, wedged, an older build without this code) simply
keeps the lock, which is exactly the pre-existing behaviour rather than a new
failure mode.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Optional

from .. import config

logger = logging.getLogger(__name__)

_LOCK_FILE = "scheduler.lock"
_RECORD_FILE = "scheduler.leader.json"
_YIELD_DIR = "scheduler.yield.d"

# A yield request older than this is ignored: the requester has gone away and
# we should not step down for a process that is no longer waiting.
# Must outlast the leader's error-backoff sleep (300s) plus one tick budget,
# or a broken leader wakes up, sees no request, and keeps the lock.
_YIELD_TTL_SECONDS = 480

# How long a requester keeps trying to take the lock after asking for it, and
# how often. Must outlast one healthy tick cycle (55s budget + 60s sleep);
# 90s lost the race to a slow-but-alive leader. A handover that nobody picks
# up leaves nothing scheduling at all, which is worse than the stale leader
# it replaced — so the yield file is left in place when this window ends.
_HANDOVER_WAIT_SECONDS = 180
_HANDOVER_POLL_SECONDS = 3

_lock_fd: Optional[int] = None

# Set by the last failed acquisition: did we ask the holder to stand down?
# acquire_leader_with_handover only waits when we did.
_requested_handover: bool = False


def _lock_path():
    return config._heylead_home() / "data" / _LOCK_FILE


def _record_path():
    """Sidecar for the leader record. Never byte-locked.

    On Windows ``msvcrt.locking`` exclusive-locks byte 0 of scheduler.lock,
    so ``read_text()`` of that file raises PermissionError and handover
    plus ``heylead daemon --status`` both see an empty holder.
    """
    return config._heylead_home() / "data" / _RECORD_FILE


def _yield_dir():
    """Yield requests are one file per requester.

    A single shared file meant whoever acquired the lock deleted whatever
    request happened to be sitting there — including a third process's, which
    then waited out its timeout for a handover that had already been consumed.
    """
    return config._heylead_home() / "data" / _YIELD_DIR


def _yield_path(pid: int | None = None):
    return _yield_dir() / f"{pid or os.getpid()}.json"


def _own_version() -> str:
    try:
        from .. import __version__
        return str(__version__)
    except Exception:
        return "0"


def _version_tuple(v: str) -> tuple[int, ...]:
    """Comparable form of a version string; unparsable parts sort lowest."""
    out: list[int] = []
    for part in str(v).split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def read_leader_info() -> dict[str, Any]:
    """Who currently holds the lock, as far as the file says.

    Returns {} when there is no readable record. A record can outlive its
    process — the OS releases the flock but nothing rewrites the file — so
    callers must treat this as a claim, not proof of liveness.
    """
    raw = ""
    for path in (_record_path(), _lock_path()):
        try:
            raw = path.read_text().strip()
        except Exception:
            continue
        if raw:
            break
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # Pre-0.10.196 lock files hold a bare pid and nothing else. Note a bare pid
    # is *valid JSON* — it parses to an int, not a dict — so this path has to
    # catch both a decode error and a successful parse of a non-object.
    pid = "".join(c for c in raw if c.isdigit())
    return {"pid": int(pid), "version": "0", "kind": "legacy"} if pid else {}


def request_yield(reason: str = "") -> None:
    """Ask the current leader to step down in favour of this process."""
    try:
        config.ensure_dirs()
        d = _yield_dir()
        d.mkdir(parents=True, exist_ok=True)
        _yield_path().write_text(json.dumps({
            "requested_by_pid": os.getpid(),
            "requested_by_version": _own_version(),
            "requested_at": int(time.time()),
            "reason": reason,
        }) + "\n")
    except Exception as e:
        logger.debug("Could not write yield request: %s", e)


def clear_yield_request(pid: int | None = None) -> None:
    """Remove this process's own yield request.

    Only ever removes the caller's own file. A leader stepping down must not
    delete the request, or the requester loses the record of why it is waiting;
    the requester clears it once it holds the lock.
    """
    try:
        _yield_path(pid).unlink()
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug("Could not clear yield request: %s", e)


def _read_yield_requests() -> list[dict[str, Any]]:
    """Fresh yield requests from other processes, stale files pruned."""
    out: list[dict[str, Any]] = []
    try:
        entries = list(_yield_dir().iterdir())
    except Exception:
        return out

    now = int(time.time())
    for path in entries:
        try:
            req = json.loads(path.read_text())
            if not isinstance(req, dict):
                raise ValueError("not an object")
        except Exception:
            _unlink_quietly(path)
            continue

        age = now - int(req.get("requested_at") or 0)
        if age > _YIELD_TTL_SECONDS or age < 0:
            _unlink_quietly(path)
            continue
        req_pid = int(req.get("requested_by_pid") or 0)
        if req_pid == os.getpid():
            continue
        # A requester that died mid-wait leaves its file behind for the full
        # TTL. Stepping down for it would leave nothing holding the lock and,
        # on an MCP-only install, nothing scheduling at all.
        if not _pid_alive(req_pid):
            _unlink_quietly(path)
            continue
        out.append(req)
    return out


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True
    return True


def _unlink_quietly(path) -> None:
    try:
        path.unlink()
    except Exception:
        pass


def should_yield() -> bool:
    """Has a newer process asked us to hand over leadership?

    Called by the engine each tick. False unless there is a fresh request from
    a strictly newer version — an equal version means a restart of the same
    build, and stepping down for that would just swap one leader for another.
    """
    if _lock_fd is None:
        return False
    mine = _version_tuple(_own_version())
    return any(
        _version_tuple(req.get("requested_by_version") or "0") > mine
        for req in _read_yield_requests()
    )


def try_acquire_leader(kind: str = "mcp") -> bool:
    """Try to become the scheduler leader.

    kind is recorded in the lock so operators (and show_status) can tell a
    daemon leader from an MCP-server leader.

    Returns True if this process holds the lock. On failure, if the holder is
    running an older version, leaves a yield request so it steps down and this
    process can win the next attempt.
    """
    global _lock_fd, _requested_handover

    config.ensure_dirs()
    lock_path = _lock_path()

    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)

        if sys.platform == "win32":
            import msvcrt
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                os.close(fd)
                _requested_handover = _note_follower(kind)
                return False
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                _requested_handover = _note_follower(kind)
                return False

        _lock_fd = fd
        record = {
            "pid": os.getpid(),
            "version": _own_version(),
            "started_at": int(time.time()),
            "kind": kind,
        }
        payload = (json.dumps(record) + "\n").encode()
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload)
        os.fsync(fd)
        try:
            _record_path().write_text(payload.decode())
        except Exception:
            logger.debug("Could not write leader sidecar", exc_info=True)
        # We are the leader now; any request that got us here is satisfied.
        clear_yield_request()
        logger.info(
            "Acquired scheduler leader lock (pid=%d version=%s kind=%s)",
            record["pid"], record["version"], kind,
        )
        return True

    except Exception as e:
        logger.warning("Leader election failed, running as follower: %s", e)
        return False


def _note_follower(kind: str) -> bool:
    """Log why we are a follower, and ask an older leader to stand aside.

    Returns True if a handover was requested, i.e. it is worth waiting.
    """
    holder = read_leader_info()
    mine, theirs = _own_version(), str(holder.get("version") or "0")
    if holder and _version_tuple(mine) > _version_tuple(theirs):
        logger.warning(
            "Scheduler leader (pid=%s) runs %s, older than this process's %s — "
            "requesting handover",
            holder.get("pid"), theirs, mine,
        )
        request_yield(f"{kind} {mine} supersedes {theirs}")
        return True
    logger.info("Another instance is scheduler leader, running as follower")
    return False


def acquire_leader_with_handover(
    kind: str = "mcp",
    wait_seconds: float = _HANDOVER_WAIT_SECONDS,
    poll_seconds: float = _HANDOVER_POLL_SECONDS,
    sleep=time.sleep,
) -> bool:
    """Acquire leadership, waiting out a handover we asked for.

    Requesting a handover without then taking the lock is worse than not asking
    at all: the old leader steps down, the requester has already moved on as a
    follower, and nothing schedules at all. So a process that asks must wait for
    the lock it asked for.

    Only waits when a handover was actually requested — an equal-version
    follower returns immediately rather than spinning behind a healthy leader.
    """
    if try_acquire_leader(kind):
        return True
    if not _requested_handover:
        return False

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        sleep(poll_seconds)
        if try_acquire_leader(kind):
            logger.info("Took scheduler leadership after handover")
            return True
    logger.warning(
        "Requested a handover but the leader did not step down within %ss — "
        "continuing as a follower; leaving the yield request so a late "
        "check still stands down", wait_seconds,
    )
    return False


def release_leader() -> None:
    """Release the scheduler leader lock and clear the record of who held it.

    Truncating matters: the OS releases the flock, but a record left behind
    names a pid that no longer schedules, and read_leader_info() has no way to
    tell that from a live leader.
    """
    global _lock_fd

    if _lock_fd is not None:
        try:
            os.ftruncate(_lock_fd, 0)
            os.fsync(_lock_fd)
        except OSError:
            pass
        try:
            _record_path().unlink()
        except FileNotFoundError:
            pass
        except Exception:
            logger.debug("Could not clear leader sidecar", exc_info=True)
        try:
            os.close(_lock_fd)  # closing the fd releases the flock
        except OSError:
            pass
        _lock_fd = None
        logger.info("Released scheduler leader lock")
