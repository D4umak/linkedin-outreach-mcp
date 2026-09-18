#!/usr/bin/env python3
"""heylead-logs: CLI tool for analyzing HeyLead JSON logs.

Usage:
    heylead-logs summary              # 24h overview
    heylead-logs errors               # Recent errors with categorization
    heylead-logs slow --threshold 5000  # Jobs slower than 5s
    heylead-logs search "rate_limit"  # Search logs by pattern
    heylead-logs tail                 # Live tail of JSON log
    heylead-logs correlate <CID>      # Find all entries for a correlation ID

Reads from ~/.heylead/logs/heylead.json.log
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


_JSON_TS_FMT = "%Y-%m-%dT%H:%M:%S"


def _entry_epoch(entry: dict) -> float | None:
    """Parse JsonFormatter ``ts`` (offset or naive local), or None if undated."""
    ts = entry.get("ts")
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        try:
            return datetime.strptime(ts, _JSON_TS_FMT).timestamp()
        except ValueError:
            return None


def _log_path() -> Path:
    return Path.home() / ".heylead" / "logs" / "heylead.json.log"


def _read_entries(hours: int = 24) -> list[dict]:
    """Read JSON log entries from the last N hours."""
    path = _log_path()
    if not path.exists():
        print(f"Log file not found: {path}", file=sys.stderr)
        return []

    cutoff = time.time() - (hours * 3600)
    entries = []

    # Also check rotated files
    for suffix in ("", ".1", ".2", ".3"):
        p = Path(str(path) + suffix) if suffix else path
        if not p.exists():
            continue
        try:
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    epoch = _entry_epoch(entry)
                    if epoch is None or epoch < cutoff:
                        continue
                    entries.append(entry)
        except IOError:
            continue

    # Rotated files are older than the live file, but concatenating live
    # then .1–.3 leaves errors[-20:] as an arbitrary slice of whichever
    # file was read last — sort by epoch before any tail.
    entries.sort(key=lambda e: _entry_epoch(e) or 0.0)
    return entries


def cmd_summary(args: argparse.Namespace) -> None:
    """Show 24h summary of log activity."""
    entries = _read_entries(args.hours)
    if not entries:
        print("No log entries found.")
        return

    levels = Counter(e.get("level", "?") for e in entries)
    loggers = Counter(e.get("logger", "?") for e in entries)

    print(f"=== HeyLead Log Summary ({args.hours}h) ===")
    print(f"Total entries: {len(entries)}")
    print()

    print("By level:")
    for level, cnt in levels.most_common():
        print(f"  {level}: {cnt}")
    print()

    print("Top loggers:")
    for logger_name, cnt in loggers.most_common(10):
        print(f"  {logger_name}: {cnt}")
    print()

    events = Counter(e.get("event") for e in entries if e.get("event"))
    if events:
        print("By event:")
        for event, cnt in events.most_common(15):
            print(f"  {event}: {cnt}")
        print()

    # Job metrics from log entries
    job_entries = [e for e in entries if e.get("job_type")]
    if job_entries:
        by_type = defaultdict(list)
        for e in job_entries:
            by_type[e["job_type"]].append(e)
        print("Job types:")
        for jtype, jentries in sorted(by_type.items()):
            durations = [e.get("duration_ms", 0) for e in jentries if e.get("duration_ms")]
            avg_dur = sum(durations) / len(durations) if durations else 0
            print(f"  {jtype}: {len(jentries)} entries (avg {avg_dur:.0f}ms)")


def _is_error_entry(entry: dict) -> bool:
    if entry.get("level") in ("ERROR", "WARNING"):
        return True
    if entry.get("outcome") == "error":
        return True
    if entry.get("event") == "outbound_send_result" and entry.get("success") is False:
        return True
    return False


def cmd_errors(args: argparse.Namespace) -> None:
    """Show recent errors with categorization."""
    entries = _read_entries(args.hours)
    errors = [e for e in entries if _is_error_entry(e)]

    if not errors:
        print("No errors found.")
        return

    # Categorize
    categories = Counter()
    for e in errors:
        cat = e.get("error_category", "uncategorized")
        categories[cat] += 1

    print(f"=== Errors ({args.hours}h) ===")
    print(f"Total: {len(errors)}")
    print()

    print("By category:")
    for cat, cnt in categories.most_common():
        print(f"  {cat}: {cnt}")
    print()

    print("Recent errors:")
    for e in errors[-20:]:
        ts = e.get("ts", "?")
        level = e.get("level", "?")
        msg = e.get("msg", "")[:100]
        cat = e.get("error_category", "")
        cat_str = f" [{cat}]" if cat else ""
        print(f"  [{ts}] {level}{cat_str}: {msg}")


def cmd_slow(args: argparse.Namespace) -> None:
    """Show jobs slower than threshold."""
    entries = _read_entries(args.hours)
    slow = [
        e for e in entries
        if e.get("duration_ms") and e["duration_ms"] > args.threshold
    ]

    if not slow:
        print(f"No jobs slower than {args.threshold}ms.")
        return

    slow.sort(key=lambda e: e.get("duration_ms", 0), reverse=True)

    print(f"=== Slow Jobs (>{args.threshold}ms, {args.hours}h) ===")
    print(f"Found: {len(slow)}")
    print()

    for e in slow[:30]:
        ts = e.get("ts", "?")
        dur = e.get("duration_ms", 0)
        jtype = e.get("job_type", "?")
        cid = e.get("correlation_id", "")[:8]
        msg = e.get("msg", "")[:60]
        print(f"  [{ts}] {dur}ms {jtype} cid={cid} — {msg}")


def cmd_search(args: argparse.Namespace) -> None:
    """Search logs by pattern."""
    entries = _read_entries(args.hours)
    pattern = args.pattern.lower()

    matches = [
        e for e in entries
        if pattern in json.dumps(e).lower()
    ]

    if not matches:
        print(f"No matches for '{args.pattern}'.")
        return

    print(f"=== Search: '{args.pattern}' ({len(matches)} matches) ===")
    print()

    for e in matches[-30:]:
        ts = e.get("ts", "?")
        level = e.get("level", "?")
        msg = e.get("msg", "")[:100]
        print(f"  [{ts}] {level}: {msg}")


def cmd_correlate(args: argparse.Namespace) -> None:
    """Find all log entries for a correlation ID."""
    entries = _read_entries(args.hours)
    cid = args.correlation_id

    matches = [
        e for e in entries
        if str(e.get("correlation_id") or "").startswith(cid)
        or str(e.get("request_id") or "").startswith(cid)
    ]

    if not matches:
        print(f"No entries for correlation ID: {cid}")
        return

    print(f"=== Correlation: {cid} ({len(matches)} entries) ===")
    print()

    for e in matches:
        ts = e.get("ts", "?")
        level = e.get("level", "?")
        logger_name = e.get("logger", "?").split(".")[-1]
        dur = e.get("duration_ms")
        dur_str = f" [{dur}ms]" if dur else ""
        msg = e.get("msg", "")[:100]
        print(f"  [{ts}] {level} {logger_name}{dur_str}: {msg}")


def cmd_tail(args: argparse.Namespace) -> None:
    """Live tail of JSON log (last N lines then follow)."""
    path = _log_path()
    if not path.exists():
        print(f"Log file not found: {path}", file=sys.stderr)
        return

    import subprocess
    try:
        subprocess.run(
            ["tail", "-f", "-n", str(args.lines), str(path)],
            check=False,
        )
    except KeyboardInterrupt:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="heylead-logs",
        description="Analyze HeyLead JSON logs",
    )
    parser.add_argument("--hours", type=int, default=24, help="Lookback window (hours)")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("summary", help="24h overview")

    sub.add_parser("errors", help="Recent errors")

    slow = sub.add_parser("slow", help="Slow jobs")
    slow.add_argument("--threshold", type=int, default=5000, help="Threshold in ms")

    search = sub.add_parser("search", help="Search logs")
    search.add_argument("pattern", help="Search pattern")

    corr = sub.add_parser("correlate", help="Trace by correlation ID")
    corr.add_argument("correlation_id", help="Correlation ID (or prefix)")

    tail = sub.add_parser("tail", help="Live tail")
    tail.add_argument("--lines", "-n", type=int, default=50, help="Initial lines")

    args = parser.parse_args()

    cmds = {
        "summary": cmd_summary,
        "errors": cmd_errors,
        "slow": cmd_slow,
        "search": cmd_search,
        "correlate": cmd_correlate,
        "tail": cmd_tail,
    }

    if args.command in cmds:
        cmds[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
