"""Keyword signal collector — monitors LinkedIn posts for watchlist keywords.

Distributes searches across all pool accounts (DISTRIBUTED_SEARCH_PER_ACCOUNT_
DAILY searches/account/day = 250/day with 10 accounts). Supports cursor-based
pagination to fetch up to 3 pages per keyword search (75 results vs 25).

Searches keyword, company and person watchlists. Competitor watchlists belong
to competitor_collector and are skipped here — searching them in both places
issued every competitor term to LinkedIn twice while only one of the two passes
was ever counted against a budget (issue #76).

Runs every 30 minutes via the scheduler.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_ROTATION_KEY = "keyword_search_rotation"

# Where the next run starts in the watchlist order.
#
# The keyword offset above cycles keywords WITHIN a watchlist. This cycles the
# watchlists themselves, and it exists because interleaving alone was not
# enough: the daily budget is spent by several runs a day, every run rebuilt
# the queue from watchlist 0, so a run that could only afford nine searches
# always re-covered the same nine lists. Measured on 2026-08-19 —
#   "Searched 6 keywords ... (50/50 used today); 9 watchlists got no search"
# — with two watchlists last polled 36 hours earlier while the budget was
# fully spent both days. Same starvation as the concatenated queue it
# replaced, one level up.
_WATCHLIST_CURSOR_KEY = "keyword_search_watchlist_cursor"

# Watchlist types this collector does NOT search, because another collector
# owns them. Competitor terms were searched here AND by competitor_collector:
# two identical search_posts calls per term per day, with only the pass made
# here recorded against any budget (issue #76).
_FOREIGN_WATCH_TYPES = frozenset({"competitor"})


def _first_few(names: list[str], limit: int = 5) -> str:
    """Join a few names, saying so when the list is cut short.

    A summary that prints five of ten names and no ellipsis reads as the
    whole list; the count in front of it then looks like a bug.
    """
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f" and {len(names) - limit} more"


def _rotation_offset(state: dict, watchlist_id: Any) -> int:
    """Read a watchlist's stored rotation offset, tolerating junk values."""
    try:
        return max(0, int(state.get(str(watchlist_id), 0)))
    except (TypeError, ValueError):
        return 0


def _build_search_queue(
    watchlists: list[dict],
    rotation_state: dict,
    watchlist_cursor: int = 0,
) -> tuple[list[tuple[str, dict]], list[tuple[dict, list[str]]]]:
    """Build the keyword search queue, interleaved across watchlists.

    The queue used to be a plain concatenation of every watchlist's keywords.
    The daily budget is almost always smaller than the total keyword count, and
    the queue is truncated at exactly the same point every run, so watchlists
    past that point were never searched on any day — they showed "Active",
    reported successful polls, and returned nothing, forever.

    Interleaving means the budget is shared: with a budget of B across N
    watchlists, every watchlist gets at least floor(B/N) searches. Each
    watchlist's keywords are additionally rotated by a persisted offset so one
    with more keywords than its per-run share works through all of them across
    runs instead of re-searching the same head every time.

    Watchlists owned by another collector (_FOREIGN_WATCH_TYPES) are dropped
    here rather than at the query, so the queue this function returns is the
    whole truth about what the collector will search.

    Returns (queue, per_watchlist) where per_watchlist carries the rotated
    keyword lists, needed to advance the offsets afterwards.
    """
    per_watchlist: list[tuple[dict, list[str]]] = []
    for wl in watchlists:
        if wl.get("watch_type") in _FOREIGN_WATCH_TYPES:
            continue
        disabled: set[str] = set()
        try:
            disabled = set(json.loads(wl.get("disabled_keywords", "[]") or "[]"))
        except (json.JSONDecodeError, TypeError):
            pass
        kws = [kw for kw in wl.get("keywords_list", []) if kw not in disabled]
        if kws:
            offset = _rotation_offset(rotation_state, wl["id"]) % len(kws)
            per_watchlist.append((wl, kws[offset:] + kws[:offset]))

    # Start this run at the watchlist the last run stopped on. Without this the
    # interleave is fair only within a run that can afford a full round, and
    # the budget is routinely spread across several short runs a day.
    if per_watchlist:
        start = watchlist_cursor % len(per_watchlist)
        per_watchlist = per_watchlist[start:] + per_watchlist[:start]

    queue: list[tuple[str, dict]] = []
    if per_watchlist:
        for slot in range(max(len(k) for _, k in per_watchlist)):
            for wl, kws in per_watchlist:
                if slot < len(kws):
                    queue.append((kws[slot], wl))
    return queue, per_watchlist


async def collect_keyword_signals() -> str:
    """Collect signals from LinkedIn post searches for all active watchlists.

    Distributes keyword searches across pool accounts (round-robin).
    Each account has its own daily budget of DISTRIBUTED_SEARCH_PER_ACCOUNT_DAILY
    — this collector's share of that account's day, not the whole allowance;
    competitor_collector and hiring_collector hold the rest. That budget is
    held in a day-stamped per-account ledger, not a per-run tally: the
    round-robin index restarts at 0 every run, so a keyword queue shorter than
    the pool would otherwise send every run's first search to the same account.
    Fetches up to KEYWORD_SEARCH_MAX_PAGES pages per keyword for deeper results.

    For each active watchlist that this collector owns:
    1. Search LinkedIn posts using watchlist keywords (multi-account, paginated)
    2. Deduplicate against existing signals (same post_id + signal_type)
    3. Store posts in posts table for analysis
    4. Cross-reference post authors against campaign contacts
    5. Save new signals with watchlist_id tracking

    Returns summary string.
    """
    from ..constants import (
        DISTRIBUTED_SEARCH_PER_ACCOUNT_DAILY,
        KEYWORD_SEARCH_MAX_PAGES,
        SIGNAL_KEYWORD_MENTION,
        SIGNAL_SEARCH_TYPE_KEYWORD,
        SIGNAL_TTL_KEYWORD_MENTION,
    )
    from ..db.async_bridge import run_db
    from ..db.post_queries import upsert_post
    from ..db.queries import get_setting, save_setting
    from ..db.signal_queries import (
        batch_get_contacts_by_linkedin_ids,
        batch_get_contacts_by_public_slugs,
        batch_signal_exists,
        get_daily_account_search_counts,
        get_daily_signal_search_count,
        list_watchlists,
        record_account_search,
        record_signal_search,
        save_signal,
        update_watchlist,
        upsert_signal_account,
    )
    from ..linkedin import UnipileError, get_account_id, get_linkedin_client
    from ..linkedin.search_traffic import search_scope
    from ..services.post_freshness import post_item_published_at

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected."

    # Get active watchlists
    watchlists = await run_db(list_watchlists, is_active=True)
    if not watchlists:
        return "No active watchlists configured."

    try:
        client = get_linkedin_client()
    except UnipileError as e:
        return f"LinkedIn client error: {e}"

    # Get pool accounts for distributed searching
    accounts = await _get_pool_accounts(client, account_id)
    num_accounts = len(accounts)

    # Total daily budget = this collector's per-account share × number of accounts
    total_daily_budget = DISTRIBUTED_SEARCH_PER_ACCOUNT_DAILY * num_accounts

    # How many searches THIS collector has already booked today. Its own key:
    # competitor and hiring book against theirs, so nobody spends another
    # collector's allowance and nobody is starved by one (issue #76).
    daily_count = await run_db(get_daily_signal_search_count, SIGNAL_SEARCH_TYPE_KEYWORD)
    if daily_count >= total_daily_budget:
        await client.close()
        return (
            f"Daily keyword search limit reached "
            f"({daily_count}/{total_daily_budget} keyword searches "
            f"across {num_accounts} accounts)."
        )

    rotation_state = await run_db(get_setting, _ROTATION_KEY, {})
    if not isinstance(rotation_state, dict):
        rotation_state = {}

    watchlist_cursor = await run_db(get_setting, _WATCHLIST_CURSOR_KEY, 0)
    try:
        watchlist_cursor = max(0, int(watchlist_cursor))
    except (TypeError, ValueError):
        watchlist_cursor = 0

    search_queue, per_watchlist = _build_search_queue(
        watchlists, rotation_state, watchlist_cursor,
    )

    if not search_queue:
        await client.close()
        foreign = sum(
            1 for wl in watchlists if wl.get("watch_type") in _FOREIGN_WATCH_TYPES
        )
        if foreign and foreign == len(watchlists):
            return (
                f"No keywords to search — all {foreign} active watchlists are "
                f"competitor watchlists, collected by competitor_collector."
            )
        return "No keywords to search (all disabled or no watchlists)."

    total_new = 0
    total_matched = 0
    total_searched = 0
    total_posts_stored = 0
    unissued = 0
    errors = 0
    stopped_early = ""
    now = int(time.time())
    remaining = total_daily_budget - daily_count

    # Per-watchlist accounting, so a watchlist that is searched but never yields
    # is distinguishable from one that is never searched at all. Both used to
    # look identical from outside: "Active", polling, zero signals.
    searched_by_wl: dict[str, int] = {}
    yield_by_wl: dict[str, int] = {}

    # Per-account search counts for TODAY, not for this run. A per-run tally
    # cannot hold a per-account daily allowance: `i` restarts at 0 every run,
    # so a keyword queue shorter than the pool sends every run's first search
    # to the same account — 48 a day on the primary against an allowance of
    # DISTRIBUTED_SEARCH_PER_ACCOUNT_DAILY, with the rest of the pool idle.
    ledger = await run_db(get_daily_account_search_counts)
    account_searches: dict[str, int] = {aid: ledger.get(aid, 0) for aid in accounts}

    async def _book_one(acct: str, wl_key: str) -> None:
        """Charge one search unit — called only once a request has gone out."""
        nonlocal total_searched
        account_searches[acct] = account_searches.get(acct, 0) + 1
        total_searched += 1
        searched_by_wl[wl_key] = searched_by_wl.get(wl_key, 0) + 1
        await run_db(record_signal_search, SIGNAL_SEARCH_TYPE_KEYWORD, 1)
        await run_db(record_account_search, acct, 1)

    for i, (keyword, wl) in enumerate(search_queue):
        if total_searched >= remaining:
            stopped_early = "budget"
            break

        # Round-robin assign to account
        acct_id = accounts[i % num_accounts]
        if account_searches.get(acct_id, 0) >= DISTRIBUTED_SEARCH_PER_ACCOUNT_DAILY:
            # This account is done for the day, try next available
            acct_id = _next_available_account(
                accounts, account_searches, DISTRIBUTED_SEARCH_PER_ACCOUNT_DAILY,
            )
            if not acct_id:
                stopped_early = "accounts"
                break  # Every pool account has spent its day

        wl_id = wl["id"]
        watch_type = wl.get("watch_type", "keyword")
        campaign_id = wl.get("campaign_id")

        # Always a keyword mention: _build_search_queue drops the watch types
        # another collector owns, so a competitor watchlist never gets here.
        signal_type = SIGNAL_KEYWORD_MENTION
        ttl = SIGNAL_TTL_KEYWORD_MENTION

        try:
            # One unit is booked for one request that reached LinkedIn — no
            # sooner, so an outage cannot spend the day, and no later, so a
            # 429 storm is not free to retry at full speed. The client counts
            # what it puts on the wire because it is the only layer that can:
            # both clients swallow transport errors and return ([], None).
            # The scope keeps that count to THIS call: in backend mode one
            # client is shared, and the scheduler runs five jobs at once.
            search_error: Exception | None = None
            all_posts: list[dict] = []
            with search_scope(client) as scope:
                try:
                    # Paginated search — up to KEYWORD_SEARCH_MAX_PAGES pages
                    all_posts = await _search_keyword_paginated(
                        client, acct_id, keyword,
                        max_pages=KEYWORD_SEARCH_MAX_PAGES,
                    )
                except Exception as e:
                    search_error = e
            if scope.request_was_issued(search_error):
                await _book_one(acct_id, wl_id)
            else:
                unissued += 1
                logger.debug(
                    "Keyword search for '%s' never left this machine (%s) — "
                    "no budget spent", keyword, search_error or "no request issued",
                )
            if search_error is not None:
                raise search_error

            # Batch dedup: pre-fetch existing signals and contacts
            valid_posts = [p for p in all_posts if p.get("post_id") and p.get("text")]
            all_post_ids = [p["post_id"] for p in valid_posts]
            existing_signals = await run_db(batch_signal_exists, signal_type, post_ids=all_post_ids)
            all_author_ids = [p.get("author_id", "") for p in valid_posts if p.get("author_id")]
            contacts_by_lid = await run_db(batch_get_contacts_by_linkedin_ids, all_author_ids) if all_author_ids else {}
            # Issue #64: authors rarely carry a provider id — match their
            # public slug against slugs in contacts.linkedin_url as well.
            all_author_slugs = [p.get("author_public_id", "") for p in valid_posts if p.get("author_public_id")]
            contacts_by_slug = await run_db(batch_get_contacts_by_public_slugs, all_author_slugs) if all_author_slugs else {}

            for post in valid_posts:
                post_id = post.get("post_id", "")
                post_text = post.get("text", "")
                author_id = post.get("author_id", "")
                author_public_id = post.get("author_public_id", "")
                author_numeric_id = post.get("author_numeric_id", "")
                author_name = post.get("author_name", "")
                author_headline = post.get("author_headline", "")

                # Store post in posts table for analysis (with expanded fields)
                impressions = int(post.get("impressions_count") or 0)
                reposts = int(post.get("reposts_count") or 0)
                await run_db(
                    upsert_post,
                    post_id,
                    author_linkedin_id=author_id or author_public_id or author_numeric_id,
                    author_name=author_name,
                    text=post_text[:2000],
                    metrics_json=json.dumps({
                        "reactions_count": post.get("reactions_count", 0),
                        "comments_count": post.get("comments_count", 0),
                        "impressions_count": impressions,
                        "reposts_count": reposts,
                    }),
                    source="keyword_search",
                    visibility=post.get("visibility", ""),
                    media_type=post.get("media_type", ""),
                    is_repost=1 if post.get("is_repost") else 0,
                    impressions_count=impressions,
                    reposts_count=reposts,
                )
                total_posts_stored += 1

                # Dedup: skip signal if we already have one for this post
                if post_id in existing_signals:
                    continue

                from ..author_identity import sendable_person_id
                person_id = sendable_person_id(
                    provider_id=author_id, public_id=author_public_id,
                )
                if not person_id:
                    continue

                # Cross-reference: check if author is a campaign contact —
                # by provider id when we have one, else by public slug.
                linked_prospect_id = None
                linked_campaign_id = campaign_id
                contact = contacts_by_lid.get(author_id) if author_id else None
                if contact is None and author_public_id:
                    contact = contacts_by_slug.get(author_public_id)
                if contact:
                    linked_prospect_id = contact["id"]
                    linked_campaign_id = (
                        linked_campaign_id or contact.get("campaign_id")
                    )

                # Build metadata (with expanded fields)
                metadata = {
                    "keyword": keyword,
                    "watchlist_id": wl_id,
                    "watchlist_name": wl.get("name", ""),
                    "watch_type": watch_type,
                    "author_headline": author_headline,
                    "author_url": post.get("author_url", ""),
                    "author_public_id": author_public_id,
                    "author_numeric_id": author_numeric_id,
                    "reactions_count": post.get("reactions_count", 0),
                    "comments_count": post.get("comments_count", 0),
                    "impressions_count": impressions,
                    "reposts_count": reposts,
                    "timestamp": post.get("timestamp", ""),
                    "published_at": post_item_published_at(
                        post_id, post.get("timestamp"), now,
                    ),
                    "account_id": acct_id[:8],
                    "media_type": post.get("media_type", "text"),
                    "is_repost": bool(post.get("is_repost")),
                    "share_url": post.get("share_url", ""),
                }
                if linked_prospect_id:
                    metadata["matched_contact"] = True

                await run_db(
                    save_signal,
                    signal_type=signal_type,
                    source="keyword_search",
                    prospect_name=author_name or None,
                    prospect_title=author_headline or None,
                    linkedin_id=person_id,
                    prospect_id=linked_prospect_id,
                    campaign_id=linked_campaign_id,
                    content=post_text[:1000],
                    post_id=post_id,
                    metadata_json=json.dumps(metadata),
                    expires_at=now + ttl,
                    watchlist_id=wl_id,
                )
                total_new += 1
                if linked_prospect_id:
                    total_matched += 1
                yield_by_wl[wl_id] = yield_by_wl.get(wl_id, 0) + 1

                if person_id:
                    await run_db(
                        upsert_signal_account,
                        linkedin_id=person_id,
                        prospect_name=author_name or None,
                    )

        except Exception as e:
            logger.warning(
                "Keyword search failed for '%s' via account %s: %s",
                keyword, acct_id[:8], e,
            )
            errors += 1

    # "Last polled" means a search of this watchlist reached LinkedIn, so a
    # day of connect failures leaves every watchlist visibly unpolled instead
    # of reporting a full day of polling that never happened. searched_by_wl
    # is filled by _book_one, which runs only for a request that went out.
    for polled_id in searched_by_wl:
        await run_db(update_watchlist, polled_id, last_polled_at=now)

    await client.close()

    # Advance each watchlist's rotation by what it actually searched, so the
    # next run resumes at the next keyword rather than repeating this head.
    for wl, kws in per_watchlist:
        used = searched_by_wl.get(wl["id"], 0)
        if used:
            prev = _rotation_offset(rotation_state, wl["id"])
            rotation_state[str(wl["id"])] = (prev + used) % len(kws)
    try:
        await run_db(save_setting, _ROTATION_KEY, rotation_state)
    except Exception as e:  # a lost rotation costs fairness, not correctness
        logger.warning("Could not persist keyword rotation offsets: %s", e)

    # Advance the watchlist cursor by however many DISTINCT watchlists this run
    # actually reached, so the next run resumes at the first one it did not.
    # A run that completed a full round advances by len(per_watchlist), which is
    # 0 mod the list length — back to the start, which is correct.
    if per_watchlist:
        advanced = (watchlist_cursor + len(searched_by_wl)) % len(per_watchlist)
        try:
            await run_db(save_setting, _WATCHLIST_CURSOR_KEY, advanced)
        except Exception as e:
            logger.warning("Could not persist the watchlist cursor: %s", e)

    # "Searched" counts requests that reached LinkedIn. A run whose searches
    # all died in the local network reports 0, not the length of the queue it
    # walked — and says separately what happened to the rest.
    summary = (
        f"Searched {total_searched} keywords across {num_accounts} accounts, "
        f"found {total_new} new signals, stored {total_posts_stored} posts"
    )
    if total_new:
        summary += (
            f"; {total_matched} of {total_new} signals matched a campaign contact"
        )
    if errors:
        summary += f", {errors} errors"
    if unissued:
        summary += (
            f", {unissued} searches never left this machine"
            f" (no request reached LinkedIn, so they cost no budget)"
        )

    # Not "budget exhausted": the count includes keywords the run walked past
    # without a request leaving, which cost no budget at all. The used/total
    # figure beside it says whether the budget was in fact the binding limit.
    unsearched = len(search_queue) - total_searched
    if unsearched > 0:
        if stopped_early == "accounts":
            # A different limit from the collector's own, and it can bind
            # first: the pool's accounts are shared, this collector's counter
            # is not. Naming the collector budget here would be misleading.
            summary += (
                f". {unsearched} keywords unsearched — every pool account has"
                f" used its {DISTRIBUTED_SEARCH_PER_ACCOUNT_DAILY} keyword"
                f" searches for today"
            )
        else:
            summary += (
                f". {unsearched} keywords unsearched"
                f" ({total_searched + daily_count}/{total_daily_budget}"
                f" keyword searches used today)"
            )
    starved = sorted(
        wl.get("name", wl["id"])
        for wl, _ in per_watchlist
        if not searched_by_wl.get(wl["id"])
    )
    if starved:
        summary += (
            f"; {len(starved)} watchlists got no search this run: "
            + _first_few(starved)
        )

    zero_yield = [
        wl.get("name", wl["id"])
        for wl, _ in per_watchlist
        if searched_by_wl.get(wl["id"]) and not yield_by_wl.get(wl["id"])
    ]
    if zero_yield:
        summary += f". Searched but zero yield: {_first_few(sorted(zero_yield))}"
        logger.warning(
            "Watchlists searched with zero yield this run: %s", ", ".join(sorted(zero_yield))
        )
    return summary


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


async def _get_pool_accounts(client: Any, fallback_account_id: str) -> list[str]:
    """Get list of account IDs to use for searching."""
    try:
        status = await client.network_pool_status()
        if status and "accounts" in status:
            healthy = [
                a["account_id"] for a in status["accounts"]
                if a.get("health_status") == "healthy" and a.get("account_id")
            ]
            if healthy:
                return healthy
    except Exception:
        pass
    return [fallback_account_id]


async def _search_keyword_paginated(
    client: Any,
    account_id: str,
    keyword: str,
    max_pages: int = 3,
) -> list[dict]:
    """Search posts with cursor-based pagination for deeper results."""
    all_posts: list[dict] = []
    cursor = None

    for page in range(max_pages):
        try:
            results, next_cursor = await client.search_posts(
                account_id, keyword, limit=25, cursor=cursor,
            )
            all_posts.extend(results)

            if not next_cursor or not results:
                break
            cursor = next_cursor
        except Exception as e:
            if page == 0:
                raise  # First page failure is fatal
            logger.debug("Pagination stopped at page %d for '%s': %s", page, keyword, e)
            break

    return all_posts


def _next_available_account(
    accounts: list[str],
    account_searches: dict[str, int],
    per_account_limit: int,
) -> str | None:
    """Find the next account that hasn't hit its daily limit."""
    for aid in accounts:
        if account_searches.get(aid, 0) < per_account_limit:
            return aid
    return None
