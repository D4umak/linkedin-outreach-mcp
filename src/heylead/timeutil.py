"""Normalising provider timestamps to unix epoch seconds.

Providers report message times inconsistently — Unipile uses ISO-8601 strings,
some endpoints return epoch seconds, others milliseconds.  Storing any of those
unnormalised loses chronology, which is how a year-old reply once looked
like it arrived minutes after a 2026 outreach.

Post listings are worse: they report *age* rather than time, as "1d", "2w",
"3mo". Those parsed as None, so a collector's "recent posts only" filter
silently accepted everything.
"""

from __future__ import annotations

import math
import re
import time as _time
from datetime import datetime
from typing import Any

# Anything past this is milliseconds, not seconds (year 5138 in epoch seconds).
_MILLIS_THRESHOLD = 100_000_000_000

# Order matters: "mo" must be tried before "m", or a month reads as a minute.
_RELATIVE_UNITS: tuple[tuple[str, int], ...] = (
    ("mo", 2_592_000),   # 30 days
    ("y", 31_536_000),
    ("w", 604_800),
    ("d", 86_400),
    ("h", 3_600),
    ("m", 60),
    ("s", 1),
)

_RELATIVE_RE = re.compile(r"^(\d+)\s*([a-z]+)$")


def _from_relative(text: str, now: int) -> int | None:
    """Parse an age like '1d' or '2w' into an absolute epoch."""
    if text in ("now", "just now"):
        return now
    match = _RELATIVE_RE.match(text)
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2)
    for suffix, seconds in _RELATIVE_UNITS:
        if unit == suffix:
            return now - amount * seconds
    return None


def to_epoch(value: Any, now: int | None = None) -> int | None:
    """Return unix epoch seconds, or None if there is no usable timestamp.

    Args:
        value: ISO-8601 string, epoch seconds, epoch milliseconds, or a
            relative age such as "1d", "2w", "3mo".
        now: Reference point for relative ages. Defaults to the current time;
            pass it explicitly to keep tests deterministic.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return int(value / 1000) if value >= _MILLIS_THRESHOLD else int(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError):
            pass
        try:
            number = float(text)
        except ValueError:
            number = None
        if number is not None:
            return to_epoch(number, now) if math.isfinite(number) else None
        return _from_relative(text.lower(), now if now is not None else int(_time.time()))
    return None
