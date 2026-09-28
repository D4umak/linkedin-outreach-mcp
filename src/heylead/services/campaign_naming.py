"""Campaign names as plain text, cut on a whole word (#1582).

The hosted twin is heylead-api app/services/campaign_naming.py; the api also
normalises every name it stores, including the ones this client pushes.

Two names reached the dashboard's Campaigns list damaged on 27 Sep 2026:
"[List] ICP2 — Telematics &amp; TMS" (a caller wrote "&" as "&amp;"
and nothing decoded it) and "[Auto] ... hands-on execution suppo" (the
strategy spawner sliced its target at 60 characters).
"""

from __future__ import annotations

import html
import re

CAMPAIGN_NAME_MAX = 60
ELLIPSIS = "…"
AUTO_PREFIX = "[Auto] "

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def plain_campaign_name(name: str) -> str:
    """Entities decoded until nothing changes, whitespace collapsed.

    A real "<" or "&" stays exactly that: nothing is escaped on the way in.
    """
    text = str(name or "")
    for _ in range(5):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return " ".join(_CONTROL.sub(" ", text).split())


def cut_at_word(text: str, limit: int = CAMPAIGN_NAME_MAX) -> str:
    """``text`` within ``limit`` characters, ending on a whole word.

    A cut text ends with an ellipsis, counted inside the limit.
    """
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    room = max(limit - len(ELLIPSIS), 1)
    head, sep, _tail = text[: room + 1].rpartition(" ")
    kept = head if sep and head.strip() else text[:room]
    kept = kept.rstrip(" ,;:-—–.")
    return (kept or text[:room]) + ELLIPSIS


def auto_campaign_name(target: str) -> str:
    """The name the strategy spawner gives a campaign it creates."""
    return AUTO_PREFIX + cut_at_word(plain_campaign_name(target), CAMPAIGN_NAME_MAX)
