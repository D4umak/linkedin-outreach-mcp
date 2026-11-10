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
    # A cut can land after a separator or a conjunction ("Engineer |",
    # "Innovation &"); neither is a word to end a line on.
    kept = kept.rstrip(" ,;:-\u2014\u2013.|/&+*\u2022\u00b7([{<")
    return (kept or text[:room]) + ELLIPSIS


def auto_campaign_name(target: str) -> str:
    """The name the strategy spawner gives a campaign it creates."""
    return AUTO_PREFIX + cut_at_word(plain_campaign_name(target), CAMPAIGN_NAME_MAX)


FALLBACK_CAMPAIGN_NAME = "New campaign"

# Names an ICP answer can carry that are not a name for this campaign (#3022).
_PLACEHOLDER_ICP_NAMES = frozenset({"primary", "generated icp", FALLBACK_CAMPAIGN_NAME.casefold()})


def default_campaign_name(target_description: str) -> str:
    """Name a campaign from its target: its first sentence, cut on a word.

    The hosted twin is heylead-api campaign_naming.default_campaign_name.
    """
    text = plain_campaign_name(target_description)
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0].rstrip(".!?").strip()
    if not first:
        return FALLBACK_CAMPAIGN_NAME
    return cut_at_word(first, CAMPAIGN_NAME_MAX)


def name_for_new_campaign(explicit: str | None, icp: object, target_description: str) -> str:
    """A typed name, else the ICP's ``campaign_name``, else the target default.

    api #3022: a dashboard campaign was named from its cut-off target text
    (e.g. "I want to reach founders of small logistics firms who…") while its
    ICP carried a short name (e.g. "Nordic Logistics Founders"). The hosted twin is heylead-api
    campaign_naming.name_for_new_campaign; an empty or placeholder ICP name
    ("Primary", "Generated ICP") is no name.
    """
    typed = plain_campaign_name(explicit or "")
    if typed:
        return typed
    raw = icp.get("campaign_name") if isinstance(icp, dict) else None
    from_icp = plain_campaign_name(raw if isinstance(raw, str) else "")
    if from_icp and from_icp.casefold() not in _PLACEHOLDER_ICP_NAMES:
        return cut_at_word(from_icp, CAMPAIGN_NAME_MAX)
    return default_campaign_name(target_description)
