"""Database backup and restore utilities."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .. import config, constants

logger = logging.getLogger(__name__)

# A copy of a large database takes minutes. Say it is still going this often,
# so a process doing nothing else does not look hung.
BACKUP_PROGRESS_LOG_SECONDS = 30.0


def _log_backup_progress(dest_path: Path, started: float, stop: threading.Event) -> None:
    while not stop.wait(BACKUP_PROGRESS_LOG_SECONDS):
        try:
            written = dest_path.stat().st_size
        except OSError:
            written = 0
        logger.info(
            "Backup in progress: %s, %.1f MB on disk so far, %.0fs elapsed",
            dest_path.name, written / (1024 * 1024), time.monotonic() - started,
        )


@dataclass
class BackupInfo:
    """Metadata about a database backup."""

    path: Path
    reason: str
    timestamp: float
    size_bytes: int


def _claim_backup_path(dest_dir: Path, ts: int, safe_reason: str) -> Path:
    """Reserve an unused backup filename, atomically.

    Two backups inside the same millisecond used to compute the same path, and
    the second silently replaced the first — no error, and a caller counting
    files saw one where it had written two.

    Claiming with O_EXCL rather than checking exists() first matters: two
    processes can both find a name free and both go on to write it. The loser
    of the race takes the next millisecond instead of the winner's file.

    The millisecond is advanced rather than a suffix appended so the name keeps
    the ``heylead-{ts}-{reason}`` shape that list_backups() splits on and sorts
    by.
    """
    while True:
        candidate = dest_dir / f"heylead-{ts}-{safe_reason}.db"
        try:
            fd = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            ts += 1  # Someone holds this millisecond; take the next one.
            continue
        os.close(fd)  # SQLite initialises the claimed zero-byte file.
        return candidate


def create_backup(reason: str = "manual") -> Path | None:
    """Create a backup of the current database using SQLite's backup API.

    Uses sqlite3.backup() which safely handles WAL mode — the resulting
    backup file is a consistent snapshot even if writes are in progress.

    Returns the backup path, or None if there's no database to back up.
    """
    src_path = config.db_path()
    if not src_path.exists():
        logger.info("No database to back up")
        return None

    config.ensure_dirs()
    dest_dir = config.backups_dir()
    safe_reason = reason.replace(" ", "-").replace("/", "-")[:30]
    dest_path = _claim_backup_path(dest_dir, int(time.time() * 1000), safe_reason)

    started = time.monotonic()
    stop = threading.Event()
    watcher = threading.Thread(
        target=_log_backup_progress, args=(dest_path, started, stop),
        name="heylead-backup-progress", daemon=True,
    )
    watcher.start()
    src_conn = sqlite3.connect(str(src_path), timeout=10)
    try:
        dst_conn = sqlite3.connect(str(dest_path))
        try:
            # One step, as before: a paged copy restarts whenever another
            # process writes the source, which ten MCP servers do constantly.
            src_conn.backup(dst_conn)
            dst_conn.close()
        except Exception:
            dst_conn.close()
            if dest_path.exists():
                dest_path.unlink()
            raise
    finally:
        stop.set()
        src_conn.close()

    size = dest_path.stat().st_size
    logger.info(
        "Backup created: %s (%d bytes, %.1f MB) in %.1fs",
        dest_path.name, size, size / (1024 * 1024), time.monotonic() - started,
    )
    return dest_path


def list_backups() -> list[BackupInfo]:
    """List available backups, sorted newest first."""
    backup_dir = config.backups_dir()
    if not backup_dir.exists():
        return []

    backups = []
    for f in backup_dir.glob("heylead-*-*.db"):
        parts = f.stem.split("-", 2)  # heylead, timestamp, reason
        if len(parts) >= 3:
            try:
                ts = float(parts[1])
            except ValueError:
                ts = f.stat().st_mtime
            reason = parts[2]
        else:
            ts = f.stat().st_mtime
            reason = "unknown"
        backups.append(BackupInfo(
            path=f,
            reason=reason,
            timestamp=ts,
            size_bytes=f.stat().st_size,
        ))

    backups.sort(key=lambda b: b.timestamp, reverse=True)
    return backups


def restore_backup(backup_path: Path) -> None:
    """Restore a backup by replacing the current database.

    Closes the singleton connection, replaces the DB file using
    SQLite's backup API, then lets get_db() reconnect on next call.
    """
    from . import schema

    if not backup_path.exists():
        raise FileNotFoundError(f"Backup not found: {backup_path}")

    db_path = config.db_path()

    # Close the singleton connection so we can replace the file
    if schema._conn is not None:
        real = getattr(schema._conn, "_real", schema._conn)
        real.close()
        schema._conn = None

    # Use SQLite backup API to safely restore
    src_conn = sqlite3.connect(str(backup_path), timeout=10)
    try:
        dst_conn = sqlite3.connect(str(db_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    logger.info("Database restored from %s", backup_path.name)


def rotate_backups(keep: int = constants.MAX_BACKUPS) -> int:
    """Remove old backups, keeping only the most recent `keep` files.

    Returns the number of backups removed.
    """
    backups = list_backups()
    removed = 0
    for backup in backups[keep:]:
        backup.path.unlink(missing_ok=True)
        removed += 1
        logger.info("Removed old backup: %s", backup.path.name)
    return removed
