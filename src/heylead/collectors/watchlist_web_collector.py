"""Off-LinkedIn watchlist collector — Reddit, HN, G2, and public ATS boards.

Searches existing watchlist terms via Serper ``site:`` queries (search
endpoint, not news). Rule-based domain classification, no LLM.

Runs every 4 hours via the scheduler. Caps at 30 searches per run and
rotates the (term, source) queue so a long watchlist is covered over days.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_CURSOR_KEY = "watchlist_web_search_cursor"
_MAX_SEARCHES = 30
_RESULTS_PER_SEARCH = 5

SOURCE_REDDIT = "reddit"
SOURCE_HN = "hn"
SOURCE_G2 = "g2"
SOURCE_ATS = "ats"

_SOURCES_BY_WATCH_TYPE: dict[str, tuple[str, ...]] = {
    "keyword": (SOURCE_REDDIT, SOURCE_HN),
    "competitor": (SOURCE_REDDIT, SOURCE_HN, SOURCE_G2),
    "company": (SOURCE_ATS,),
}

_ATS_HOSTS = (
    "jobs.ashbyhq.com",
    "boards.greenhouse.io",
    "jobs.lever.co",
    "apply.workable.com",
)

_TTL_BY_TYPE: dict[str, str] = {
    "reddit_mention": "SIGNAL_TTL_REDDIT_MENTION",
    "hn_mention": "SIGNAL_TTL_HN_MENTION",
    "g2_review": "SIGNAL_TTL_G2_REVIEW",
    "ats_hiring": "SIGNAL_TTL_ATS_HIRING",
}


def _sources_for_watch_type(watch_type: str) -> tuple[str, ...]:
    """Return the Serper site scopes this watchlist type is allowed to hit."""
    return _SOURCES_BY_WATCH_TYPE.get((watch_type or "").strip().lower(), ())


def _build_query(term: str, source: str) -> str:
    """Build a ``site:`` query for one watchlist term and one source."""
    quoted = f'"{term.strip()}"'
    if source == SOURCE_REDDIT:
        return f"site:reddit.com {quoted}"
    if source == SOURCE_HN:
        return f"site:news.ycombinator.com {quoted}"
    if source == SOURCE_G2:
        return f"site:g2.com {quoted}"
    if source == SOURCE_ATS:
        return (
            f"{quoted} (site:jobs.ashbyhq.com OR site:boards.greenhouse.io "
            f"OR site:jobs.lever.co OR site:apply.workable.com)"
        )
    raise ValueError(f"unknown watchlist web source: {source}")


def _flatten_pairs(watchlists: list[dict[str, Any]]) -> list[tuple[str, str, dict]]:
    """Flatten watchlists into (term, source, watchlist) triples."""
    pairs: list[tuple[str, str, dict]] = []
    for wl in watchlists:
        sources = _sources_for_watch_type(wl.get("watch_type", ""))
        if not sources:
            continue
        for name in wl.get("keywords_list", []):
            if not isinstance(name, str) or not name.strip():
                continue
            term = name.strip()
            for source in sources:
                pairs.append((term, source, wl))
    return pairs


def _rotate(
    pairs: list[tuple[str, str, dict]], cursor: int,
) -> list[tuple[str, str, dict]]:
    """Start the run at ``cursor``, wrapping — a permutation, not a truncation."""
    if not pairs:
        return []
    start = cursor % len(pairs)
    return pairs[start:] + pairs[:start]


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def _path(url: str) -> str:
    try:
        return (urlparse(url).path or "").rstrip("/")
    except Exception:
        return ""


def signal_type_for_url(url: str) -> str | None:
    """Map a result URL to a web signal type, or None if the host is unknown."""
    from ..constants import (
        SIGNAL_ATS_HIRING,
        SIGNAL_G2_REVIEW,
        SIGNAL_HN_MENTION,
        SIGNAL_REDDIT_MENTION,
    )

    host = _host(url)
    if host == "reddit.com" or host.endswith(".reddit.com"):
        return SIGNAL_REDDIT_MENTION
    if host in ("news.ycombinator.com", "ycombinator.com"):
        return SIGNAL_HN_MENTION
    if host == "g2.com" or host.endswith(".g2.com"):
        return SIGNAL_G2_REVIEW
    if host in _ATS_HOSTS:
        return SIGNAL_ATS_HIRING
    return None


def is_junk_url(url: str) -> bool:
    """Drop homepages, category indexes, and ATS listing shells."""
    if not url:
        return True
    host = _host(url)
    path = _path(url).lower()
    lower = url.lower()

    if host == "reddit.com" or host.endswith(".reddit.com"):
        # Threads are /r/{sub}/comments/{id}/...
        return "/comments/" not in path

    if host in ("news.ycombinator.com", "ycombinator.com"):
        return "item?id=" not in lower

    if host == "g2.com" or host.endswith(".g2.com"):
        return "/categories/" in path or "/compare/" in path or path in ("", "/")

    if host == "boards.greenhouse.io":
        # Real jobs: /company/jobs/123. Listing shell: /company or /company/jobs
        parts = [p for p in path.split("/") if p]
        return len(parts) < 3 or parts[1] != "jobs"

    if host == "jobs.ashbyhq.com":
        # Real jobs: /company/role-slug. Listing: /company
        parts = [p for p in path.split("/") if p]
        return len(parts) < 2

    if host == "jobs.lever.co":
        parts = [p for p in path.split("/") if p]
        return len(parts) < 2

    if host == "apply.workable.com":
        return "/j/" not in path

    return False


def _normalize_url(url: str) -> str:
    """Strip fragments and trailing slashes so the same thread dedups."""
    try:
        parsed = urlparse(url.strip())
        path = (parsed.path or "").rstrip("/")
        query = parsed.query
        host = (parsed.hostname or "").lower().removeprefix("www.")
        scheme = parsed.scheme or "https"
        normalized = f"{scheme}://{host}{path}"
        if query:
            normalized += f"?{query}"
        return normalized
    except Exception:
        return url.strip().lower()


def dedup_key(source: str, url: str) -> str:
    """Stable post_id: md5(source + normalized_url), 16 hex chars."""
    normalized = f"{source}:{_normalize_url(url)}"
    return hashlib.md5(normalized.encode()).hexdigest()[:16]


def _ttl_for_type(signal_type: str) -> int:
    from .. import constants

    attr = _TTL_BY_TYPE.get(signal_type, "SIGNAL_TTL_DEFAULT")
    return int(getattr(constants, attr, constants.SIGNAL_TTL_DEFAULT))


async def collect_watchlist_web_signals() -> str:
    """Collect Reddit / HN / G2 / ATS signals for active watchlist terms.

    1. Flatten (term, source, watchlist) from keyword/competitor/company lists
    2. Rotate from the stored cursor
    3. Search via Serper (cap 30)
    4. Classify by domain, drop junk, dedup, save

    Returns a summary string.
    """
    from ..ai.news_service import _fetch_serper_search
    from ..db.async_bridge import run_db
    from ..db.queries import get_setting, save_setting
    from ..db.signal_queries import (
        batch_signal_exists,
        list_watchlists,
        save_signal,
        upsert_signal_account,
    )

    watchlists = await run_db(list_watchlists, is_active=True)
    if not watchlists:
        return "No active watchlists."

    all_pairs = _flatten_pairs(watchlists)
    if not all_pairs:
        return "No watchlist terms mapped to web sources."

    cursor = await run_db(get_setting, _CURSOR_KEY, 0)
    try:
        cursor = max(0, int(cursor))
    except (TypeError, ValueError):
        cursor = 0
    queue = _rotate(all_pairs, cursor)

    total_searched = 0
    total_new = 0
    errors = 0
    now = int(time.time())

    for term, source, wl in queue:
        if total_searched >= _MAX_SEARCHES:
            break

        query = _build_query(term, source)
        try:
            items = await _fetch_serper_search(
                query=query,
                num_results=_RESULTS_PER_SEARCH,
                time_range="pastMonth",
            )
            total_searched += 1
        except Exception as e:
            logger.warning("Watchlist web search failed for %r/%s: %s", term, source, e)
            errors += 1
            total_searched += 1
            continue

        if not items:
            continue

        classified: list[tuple[dict, str, str]] = []  # (item, signal_type, dedup)
        keys_by_type: dict[str, list[str]] = {}
        for item in items:
            link = (item.get("link") or "").strip()
            title = (item.get("title") or "").strip()
            if not link or not title:
                continue
            if is_junk_url(link):
                continue
            sig_type = signal_type_for_url(link)
            if not sig_type:
                continue
            key = dedup_key(source, link)
            classified.append((item, sig_type, key))
            keys_by_type.setdefault(sig_type, []).append(key)

        existing: set[str] = set()
        for sig_type, keys in keys_by_type.items():
            existing.update(await run_db(batch_signal_exists, sig_type, post_ids=keys))

        wl_id = wl.get("id") or None
        campaign_id = wl.get("campaign_id") or None

        for item, sig_type, key in classified:
            if key in existing:
                continue
            title = (item.get("title") or "").strip()
            snippet = (item.get("snippet") or "").strip()
            link = (item.get("link") or "").strip()
            metadata = {
                "term": term,
                "web_source": source,
                "watch_type": wl.get("watch_type", ""),
                "watchlist_name": wl.get("name", ""),
                "title": title,
                "source": item.get("source", ""),
                "date": item.get("date", ""),
                "link": link,
            }
            if source in (SOURCE_ATS, SOURCE_G2):
                metadata["company_name"] = term
            elif source in (SOURCE_REDDIT, SOURCE_HN):
                # Pain-point chatter has no person id. Keep the row only when
                # a pending contact already sits at a company that matches
                # the search term — otherwise it parks as no_in_campaign_match.
                from ..db.queries import list_pending_contacts_at_company

                matches = await run_db(
                    list_pending_contacts_at_company, term, campaign_id,
                )
                if not matches:
                    continue

            await run_db(
                save_signal,
                signal_type=sig_type,
                source="serper_watchlist",
                prospect_name=term,
                campaign_id=campaign_id,
                content=f"{title}\n{snippet}"[:1000],
                post_id=key,
                metadata_json=json.dumps(metadata),
                expires_at=now + _ttl_for_type(sig_type),
                watchlist_id=wl_id,
            )
            total_new += 1
            existing.add(key)

            if source in (SOURCE_ATS, SOURCE_G2):
                slug = term.lower().replace(" ", "_")
                await run_db(
                    upsert_signal_account,
                    linkedin_id=f"company:{slug}",
                    prospect_name=term,
                    company=term,
                )

    new_cursor = (cursor + total_searched) % len(all_pairs) if all_pairs else 0
    await run_db(save_setting, _CURSOR_KEY, new_cursor)

    summary = (
        f"Searched {total_searched} watchlist web queries, "
        f"found {total_new} new signals"
    )
    if errors:
        summary += f", {errors} errors"
    return summary
