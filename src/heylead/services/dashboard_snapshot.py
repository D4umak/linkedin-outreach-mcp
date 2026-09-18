"""Dashboard links and snapshot cards for hosted status replies.

A status tool calls ``status_footer`` while it builds its reply. That returns
the footer lines linking the matching dashboard page and, when the page has a
snapshot card, records which card to fetch. The server wrapper runs the tool
inside ``attach_snapshot``, which reads that record after the tool returns and
turns the reply into ``[text, Image]`` when the backend serves the PNG.

The card is a courtesy. Any failure — disabled, self-hosted, 404, auth, slow,
malformed — returns the text unchanged; it never raises and never costs the
reply. The link is always in the text because some clients (Claude Code CLI)
show images only to the model.
"""

from __future__ import annotations

import hashlib
import logging
import time
from contextvars import ContextVar
from typing import Any, Awaitable, NamedTuple
from urllib.parse import quote

import httpx

from .. import config
from ..dashboard_links import (
    SNAPSHOT_HINT,
    backend_base_url,
    campaign_url,
    dashboard_footer,
    page_url,
)

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60.0
# Backend cards are 50-80 KB; anything far past that is not a card.
MAX_SNAPSHOT_BYTES = 300_000
_TIMEOUT = httpx.Timeout(5.0, connect=3.0)

# Kinds that have a snapshot card on the backend.
_CARD_KINDS = frozenset({"overview", "campaign"})


class SnapshotTarget(NamedTuple):
    kind: str  # "overview" | "campaign"
    campaign_id: str = ""
    fresh: bool = False
    url: str = ""  # the dashboard link the footer printed


_target: ContextVar[SnapshotTarget | None] = ContextVar(
    "heylead_dashboard_snapshot_target", default=None,
)

# (snapshot url, X-Org-Id, sha256(Authorization)[:16]) → (monotonic expiry, png).
# Keyed by identity because switching workspace changes only X-Org-Id and a
# new setup changes only the JWT: a URL-only key would serve one workspace's
# card under another workspace's text.
_cache: dict[tuple[str, str, str], tuple[float, bytes]] = {}


def status_footer(
    kind: str,
    campaign_id: str = "",
    *,
    outreach_id: str = "",
    snapshot: bool = True,
    fresh: bool = False,
    label: str = "Open in the dashboard",
) -> list[str]:
    """Footer lines linking the matching dashboard page; [] outside backend mode.

    ``kind`` is "campaign" or a page kind from ``dashboard_links.PAGES``. When
    ``snapshot`` is set and the kind has a card, this call also requests that
    card: ``attach_snapshot`` fetches it after the tool returns. ``fresh``
    skips the cache for that card (use it after a change).

    The request lives in a context variable, so it must be made on the
    coroutine chain the tool wrapper awaits directly. A call from a thread
    (``run_db``, ``asyncio.to_thread``), from ``asyncio.create_task`` or
    ``gather``, or inside ``asyncio.wait_for(...)`` (which runs a task on
    Python 3.10/3.11) writes to a copied context and the card request is
    silently lost. The link lines are returned either way.
    """
    if not config.is_backend_mode():
        return []
    if kind == "campaign":
        url = campaign_url(campaign_id, outreach_id)
    else:
        url = page_url(kind)
    if snapshot and kind in _CARD_KINDS:
        _target.set(SnapshotTarget(kind, campaign_id, fresh, url))
    return ["", dashboard_footer(url, label=label)]


def snapshots_enabled() -> bool:
    return config.is_backend_mode() and config.dashboard_snapshots_enabled()


def invalidate_snapshot_cache() -> None:
    _cache.clear()


def _cache_key(url: str, headers: dict[str, str]) -> tuple[str, str, str]:
    token = headers.get("Authorization", "")
    return (
        url,
        headers.get("X-Org-Id", ""),
        hashlib.sha256(token.encode()).hexdigest()[:16],
    )


async def _fetch(url: str, *, fresh: bool = False) -> bytes | None:
    """GET a snapshot PNG; None on anything but a usable image."""
    try:
        if not snapshots_enabled():
            logger.debug("Dashboard snapshots disabled; not fetching %s", url)
            return None

        from . import cloud_sync

        headers = {
            k: v for k, v in cloud_sync._headers().items()
            if k.lower() != "content-type"
        }
        headers["Accept"] = "image/png"
        key = _cache_key(url, headers)
        if fresh:
            _cache.pop(key, None)
        hit = _cache.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 404:
            logger.debug("Dashboard snapshot %s not found", url)
            return None
        if resp.status_code != 200:
            logger.info("Dashboard snapshot %s returned %s", url, resp.status_code)
            return None
        ctype = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if ctype != "image/png":
            logger.info("Dashboard snapshot %s had content-type %r", url, ctype)
            return None
        png = resp.content
        if not png or len(png) > MAX_SNAPSHOT_BYTES:
            logger.info("Dashboard snapshot %s had unusable size %d", url, len(png))
            return None
        _cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, png)
        return png
    except Exception as exc:  # the card must never cost the reply
        logger.info("Dashboard snapshot %s failed: %s", url, exc)
        return None


async def fetch_campaign_snapshot(campaign_id: str, *, fresh: bool = False) -> bytes | None:
    try:
        cid = quote(str(campaign_id), safe="")
        url = f"{backend_base_url()}/api/v1/campaigns/{cid}/snapshot.png"
    except Exception as exc:
        logger.info("Dashboard snapshot URL failed: %s", exc)
        return None
    return await _fetch(url, fresh=fresh)


async def fetch_overview_snapshot(*, fresh: bool = False) -> bytes | None:
    try:
        url = f"{backend_base_url()}/api/v1/stats/snapshot.png"
    except Exception as exc:
        logger.info("Dashboard snapshot URL failed: %s", exc)
        return None
    return await _fetch(url, fresh=fresh)


async def attach_snapshot(coro: Awaitable[Any]) -> Any:
    """Await a tool coroutine; add the snapshot card its footer asked for.

    The card is attached only when the reply still carries the footer's link,
    so the hint's "open the link above" never points at a missing link.
    """
    from mcp.server.fastmcp import Image

    token = _target.set(None)
    try:
        result = await coro
        target = _target.get()
    finally:
        _target.reset(token)

    if target is None or not isinstance(result, str) or target.url not in result:
        return result
    if target.kind == "campaign":
        png = await fetch_campaign_snapshot(target.campaign_id, fresh=target.fresh)
    else:
        png = await fetch_overview_snapshot(fresh=target.fresh)
    if png is None:
        return result
    return [result.rstrip("\n") + "\n" + SNAPSHOT_HINT, Image(data=png, format="png")]
