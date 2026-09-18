"""Run the scheduler as a standalone process, independent of any MCP client.

The scheduler used to live inside MCP server processes, so it started when a
chat window opened and died when it closed. Nothing accumulated overnight: a
full prospect scan of an 841-contact campaign needs roughly 42 hours of
continuous running and therefore never finished. Leadership churned constantly
— 23 acquisitions and 113 follower notices in three days on one machine.

This module is the same SchedulerEngine and cloud-sync loop, hosted by a
process whose only job is to keep running.

Once `scheduler_daemon` is set in config, MCP servers stop scheduling entirely
and become thin clients. That removes the eight-processes-racing-for-a-lock
problem, and it has one failure mode worth naming: if the daemon is not
running, nothing schedules at all. So the flag is only ever set by
`heylead daemon --install`, and show_status reports loudly when the flag is on
but no daemon holds the lock.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from .logging_setup import setup_logging

logger = logging.getLogger(__name__)

PLIST_LABEL = "dev.heylead.scheduler"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"


def _log_dir() -> Path:
    from . import config
    return config._heylead_home() / "logs"


def build_plist(executable: str | None = None) -> str:
    """launchd agent definition. KeepAlive restarts it if it dies or is killed."""
    from xml.sax.saxutils import escape

    exe = escape(executable or sys.executable)
    logs = _log_dir()
    out_log = escape(str(logs / "daemon.out.log"))
    err_log = escape(str(logs / "daemon.err.log"))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exe}</string>
        <string>-m</string>
        <string>heylead</string>
        <string>daemon</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HEYLEAD_LAUNCHD</key>
        <string>1</string>
    </dict>
    <key>StandardOutPath</key>
    <string>{out_log}</string>
    <key>StandardErrorPath</key>
    <string>{err_log}</string>
</dict>
</plist>
"""


def daemon_status() -> dict:
    """What is scheduling right now, if anything.

    `flag_on and not alive` is the state that matters: MCP servers have stood
    down because the daemon was installed, and the daemon is not there, so
    nothing is scheduling and every dashboard would otherwise look normal.
    """
    from . import config
    from .scheduler import leader

    cfg = config.load_config()
    flag_on = bool(cfg.get("scheduler_daemon", False))
    info = leader.read_leader_info()
    pid = info.get("pid")

    alive = False
    if pid:
        try:
            os.kill(int(pid), 0)  # signal 0 = liveness probe only
            alive = True
        except (OSError, ValueError, TypeError):
            alive = False

    from .services.cloud_sync import install_source, local_scheduler_engine_enabled

    src = install_source()
    sync_only = (
        config.is_backend_mode()
        and config.get_sending_host() == "cloud"
        and not local_scheduler_engine_enabled()
    )
    return {
        "daemon_configured": flag_on,
        "plist_installed": _plist_path().exists(),
        "leader_pid": pid,
        "leader_kind": info.get("kind"),
        "leader_version": info.get("version"),
        "leader_alive": alive,
        "healthy": (not flag_on) or (alive and info.get("kind") == "daemon"),
        "sync_only": sync_only,
        "install_source": src,
    }


_ACQUIRE_RETRY_SECONDS = 30

# After stepping down for a newer process, wait before trying again, so the
# requester actually gets the lock instead of losing a race back to us.
_POST_YIELD_GRACE_SECONDS = 60


async def _await_lock(stop: asyncio.Event) -> bool:
    """Keep trying to become leader until we win or are asked to stop.

    The daemon must not exit when another process holds the lock. Exiting is
    what made the first version churn: launchd's KeepAlive restarts it, it
    fails to acquire again, and the machine spends its time starting and
    killing daemons. Waiting in-process means exactly one daemon exists,
    holding the lock or queued for it.
    """
    from .scheduler.leader import acquire_leader_with_handover

    while not stop.is_set():
        if await asyncio.to_thread(acquire_leader_with_handover, "daemon"):
            return True
        info = daemon_status()
        logger.warning(
            "Another process holds the scheduler lock (pid=%s kind=%s version=%s) — "
            "retrying in %ds",
            info.get("leader_pid"), info.get("leader_kind"),
            info.get("leader_version"), _ACQUIRE_RETRY_SECONDS,
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=_ACQUIRE_RETRY_SECONDS)
        except asyncio.TimeoutError:
            pass
    return False


def _cloud_sync_loop():
    """Indirection so the scheduler loop can be run without importing the MCP
    server stack — heylead.server pulls in FastMCP, which a daemon does not
    otherwise need and which tests should not have to load."""
    from .logging_setup import drop_launchd_stderr_handlers
    from .server import _cloud_sync_loop as loop

    # FastMCP (imported via server) can attach an unformatted stderr handler.
    drop_launchd_stderr_handlers()
    return loop()


def _session_health_loop():
    """Same indirection for the LinkedIn session-health probe."""
    from .logging_setup import drop_launchd_stderr_handlers
    from .server import _session_health_loop as loop

    drop_launchd_stderr_handlers()
    return loop()


async def _serve_until_stopped(stop: asyncio.Event) -> None:
    """Hold leadership and run the scheduler until stopped or asked to yield."""
    from . import config
    from .scheduler.engine import SchedulerEngine
    from .scheduler.leader import release_leader

    from .db.async_bridge import run_db
    from .services.cloud_sync import (
        local_scheduler_engine_enabled,
        stand_down_engine_off_leftovers,
        warn_if_pypi_refresh_rolled_back,
    )

    warn_if_pypi_refresh_rolled_back()

    engine: SchedulerEngine | None = None
    if local_scheduler_engine_enabled():
        engine = SchedulerEngine()
        await engine.start()
        logger.info(
            "heylead daemon scheduling (pid=%d mode=%s)", os.getpid(), config.get_scheduler_mode(),
        )
    else:
        logger.info(
            "heylead daemon sync-only (pid=%d) — hosted cloud owns every job",
            os.getpid(),
        )
        try:
            cancelled = await run_db(stand_down_engine_off_leftovers)
            if cancelled:
                logger.info("Cancelled %d leftover local jobs (engine off)", cancelled)
        except Exception as e:
            logger.debug("Cancel cloud-owned jobs failed (non-fatal): %s", e)

    sync_task = asyncio.create_task(_cloud_sync_loop())
    health_task = asyncio.create_task(_session_health_loop())

    async def _err_log_watch() -> None:
        while True:
            await asyncio.sleep(_DAEMON_ERR_WATCH_SECONDS)
            _rotate_daemon_err_log(copy_truncate=True)

    err_log_task = asyncio.create_task(_err_log_watch())
    try:
        # Wait on the engine as well as the stop signal. The engine releases
        # the lock itself when a newer process asks for it; waiting only on
        # `stop` left the daemon alive but scheduling nothing, and because the
        # process never exited, launchd's KeepAlive never restarted it.
        waiters: list[asyncio.Task] = [asyncio.create_task(stop.wait())]
        if engine is not None and engine._task is not None:
            waiters.append(engine._task)
        done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for t in waiters:
            if engine is None or t is not engine._task:
                if not t.done():
                    t.cancel()
        if engine is not None and engine._task in done and not stop.is_set():
            logger.warning("Scheduler engine stopped on its own — standing down")
    finally:
        logger.info("heylead daemon releasing leadership")
        sync_task.cancel()
        health_task.cancel()
        err_log_task.cancel()
        try:
            await sync_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await health_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await err_log_task
        except (asyncio.CancelledError, Exception):
            pass
        if engine is not None:
            await engine.stop()
        release_leader()


async def _run() -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-unix
            pass

    while not stop.is_set():
        if not await _await_lock(stop):
            break
        await _serve_until_stopped(stop)
        if stop.is_set():
            break
        # We stepped down for a newer process. Give it time to take the lock
        # rather than immediately racing it back.
        logger.info("Stood down; waiting %ds before contending again", _POST_YIELD_GRACE_SECONDS)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_POST_YIELD_GRACE_SECONDS)
        except asyncio.TimeoutError:
            pass

    logger.info("heylead daemon stopped")
    return 0


def prepare_process() -> None:
    """Everything that must happen in sync context, before the event loop.

    get_db() deliberately raises when called from the event loop thread, and
    get_account_id() reads a setting on first call and caches it in module
    state. So the first call has to happen HERE — server.main() has done this
    since long before the daemon existed, with the same reasoning.

    Without it the daemon started and ticked normally while every job that
    touches the account id died on `Sync DB call on event loop thread`. It was
    not a small subset: keyword and competitor collectors, prospect and network
    post scans, reply checks, inbound processing and profile backfill all go
    through get_account_id(), so the scheduler looked healthy and collected
    nothing. 760 failures in one night.
    """
    from . import config
    from .db.schema import get_db
    from .linkedin import get_account_id

    config.ensure_dirs()
    db = get_db()  # create/migrate the schema while still off the loop
    db.close()
    get_account_id()  # populate the module cache; every later call is memory

    # Same hazard, different cache: select_channel() runs on the loop inside
    # the invitation executor and reads the email account id on first call.
    from .services.channel_selector import get_email_account_id
    get_email_account_id()

    # One-shot: score signals written before signal_score was persisted
    # (issue #65). Sync context on purpose — the scorer reads the DB. Failure
    # is logged, never fatal: the daemon must start even if the backfill
    # cannot run, and the next startup simply tries again.
    from .services.signal_scorer import backfill_signal_scores
    try:
        backfill_signal_scores()
    except Exception as e:
        logger.warning("Signal score backfill failed: %s", e)

    from .services.signal_activator import backfill_signal_stamps
    try:
        backfill_signal_stamps()
    except Exception as e:
        logger.warning("Signal stamp backfill failed: %s", e)


_DAEMON_ERR_MAX_BYTES = 10 * 1024 * 1024
_DAEMON_ERR_WATCH_SECONDS = 300


def _rotate_daemon_err_log(*, copy_truncate: bool = True) -> None:
    """Cap launchd's StandardErrorPath without changing its inode.

    Rename-and-touch at process start left launchd writing to the renamed
    ``.1`` (114 MB from one long-lived process). Copy-truncate keeps the
    fd launchd opened and still bounds the file.
    """
    import shutil

    path = _log_dir() / "daemon.err.log"
    try:
        if not path.exists() or path.stat().st_size < _DAEMON_ERR_MAX_BYTES:
            return
        for i in range(3, 1, -1):
            older = path.with_name(f"daemon.err.log.{i}")
            newer = path.with_name(f"daemon.err.log.{i - 1}")
            if newer.exists():
                if older.exists():
                    older.unlink()
                newer.rename(older)
        dest = path.with_name("daemon.err.log.1")
        if dest.exists():
            dest.unlink()
        if copy_truncate:
            shutil.copy2(path, dest)
            with path.open("w"):
                pass
        else:
            path.rename(dest)
            path.touch()
    except OSError:
        pass


def run_daemon() -> int:
    """Entry point for `heylead daemon`."""
    # Logging first: prepare_process() opens the database, and opening it can
    # run a long migration (pre-migration backup of a ~700MB file). Configured
    # after, that whole pass logged nothing and the daemon looked hung for
    # ~13 minutes after the 0.10.338 -> 0.10.340 upgrade (10 Sep 2026).
    setup_logging(stderr=not os.environ.get("HEYLEAD_LAUNCHD"))
    _rotate_daemon_err_log()
    prepare_process()
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0


def install(load: bool = False) -> str:
    """Write the launchd agent and switch MCP servers to thin-client mode.

    Writing the plist is inert on its own — launchd only acts on it once the
    agent is loaded, which is deliberately a separate step.
    """
    from . import config

    _log_dir().mkdir(parents=True, exist_ok=True)
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_plist())

    cfg = config.load_config()
    cfg["scheduler_daemon"] = True
    config.save_config(cfg)

    lines = [
        f"Wrote {path}",
        "Set scheduler_daemon=true — MCP servers will no longer run the scheduler.",
    ]
    if load:
        import subprocess
        r = subprocess.run(
            ["launchctl", "load", "-w", str(path)], capture_output=True, text=True,
        )
        lines.append(
            "Loaded the agent." if r.returncode == 0
            else f"launchctl load failed: {r.stderr.strip() or r.returncode}"
        )
    else:
        lines.append(f"Not loaded. To start it at login:  launchctl load -w {path}")
    return "\n".join(lines)


def uninstall(unload: bool = True) -> str:
    """Remove the agent and hand scheduling back to MCP servers."""
    from . import config

    path = _plist_path()
    lines = []
    if unload and path.exists():
        import subprocess
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True, text=True)
        lines.append("Unloaded the agent.")
    if path.exists():
        path.unlink()
        lines.append(f"Removed {path}")

    cfg = config.load_config()
    cfg["scheduler_daemon"] = False
    config.save_config(cfg)
    lines.append("Set scheduler_daemon=false — MCP servers will schedule again.")
    return "\n".join(lines)
