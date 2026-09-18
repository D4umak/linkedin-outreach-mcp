"""Competitor mention signal collector — monitors LinkedIn posts mentioning competitors.

Polls `search_posts()` for each competitor name in watchlists where
`watch_type='competitor'`. This collector is the only one that searches them:
keyword_collector used to search every active watchlist, competitor ones
included, so every term went to LinkedIn twice a poll (issue #76).

Cross-references post authors against campaign contacts and ICPs. Signals
competitor mentions that indicate evaluation or switching intent — a high-value
buying signal.

Runs every 30 minutes via the scheduler, against its own slice of the daily
search budget (SIGNAL_DAILY_COMPETITOR_SEARCHES) and its own counter. It used
to read the keyword collector's counter and record nothing at all: the cap was
therefore spent by searches it did not make, and its own searches were invisible
to every cap in the system.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Where the next run starts in the flattened competitor term list. The daily
# share is smaller than the term list on any sizeable install, and without a
# cursor every run restarts at term 0 — so the tail is never searched on any
# day. Same defect the keyword collector carried in #8 and #66.
_TERM_CURSOR_KEY = "competitor_search_cursor"


def _build_search_terms(watchlists: list[dict]) -> list[tuple[str, dict]]:
    """Flatten competitor watchlists into (term, watchlist) pairs."""
    terms: list[tuple[str, dict]] = []
    for wl in watchlists:
        for name in wl.get("keywords_list", []):
            if isinstance(name, str) and name.strip():
                terms.append((name.strip(), wl))
    return terms


def _rotate(terms: list[tuple[str, dict]], cursor: int) -> list[tuple[str, dict]]:
    """Start the run at `cursor`, wrapping — a permutation, not a truncation."""
    if not terms:
        return []
    start = cursor % len(terms)
    return terms[start:] + terms[:start]


async def collect_competitor_signals() -> str:
    """Collect signals from LinkedIn posts mentioning competitors.

    Iterates competitor watchlists and searches for each competitor name.
    Deduplicates against existing signals. Cross-references authors against
    campaign contacts for higher-value signals.

    Every search issued is booked against this collector's own daily counter,
    and the run stops at SIGNAL_DAILY_COMPETITOR_SEARCHES. Terms are rotated so
    a share smaller than the term list still reaches every term over a few days.

    Returns summary string.
    """
    from ..constants import (
        SIGNAL_COMPETITOR_MENTION,
        SIGNAL_DAILY_COMPETITOR_SEARCHES,
        SIGNAL_SEARCH_TYPE_COMPETITOR,
        SIGNAL_TTL_COMPETITOR_MENTION,
    )
    from ..db.async_bridge import run_db
    from ..db.post_queries import upsert_post
    from ..db.queries import get_setting, save_setting
    from ..db.signal_queries import (
        batch_get_contacts_by_linkedin_ids,
        batch_get_contacts_by_public_slugs,
        get_daily_signal_search_count,
        list_watchlists,
        record_signal_search,
        save_signal,
        signal_exists,
        update_watchlist,
        upsert_signal_account,
    )
    from ..linkedin import UnipileError, get_account_id, get_linkedin_client
    from ..linkedin.search_traffic import search_scope

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected."

    # This collector's own slice of the day, and its own counter — the two have
    # to be the same key or the cap governs somebody else's searches (#76).
    daily_count = await run_db(get_daily_signal_search_count, SIGNAL_SEARCH_TYPE_COMPETITOR)
    if daily_count >= SIGNAL_DAILY_COMPETITOR_SEARCHES:
        return (
            f"Daily competitor search limit reached "
            f"({daily_count}/{SIGNAL_DAILY_COMPETITOR_SEARCHES})."
        )

    # Get competitor watchlists only
    watchlists = await run_db(list_watchlists, is_active=True, watch_type="competitor")
    if not watchlists:
        return "No active competitor watchlists."

    all_terms = _build_search_terms(watchlists)
    if not all_terms:
        return "No competitor names to search."

    cursor = await run_db(get_setting, _TERM_CURSOR_KEY, 0)
    try:
        cursor = max(0, int(cursor))
    except (TypeError, ValueError):
        cursor = 0
    search_terms = _rotate(all_terms, cursor)

    try:
        client = get_linkedin_client()
    except UnipileError as e:
        return f"LinkedIn client error: {e}"

    total_new = 0
    total_matched = 0
    total_searched = 0
    total_posts_stored = 0
    attempted = 0
    unissued = 0
    errors = 0
    now = int(time.time())
    remaining = SIGNAL_DAILY_COMPETITOR_SEARCHES - daily_count
    polled_watchlists: dict[str, dict] = {}

    try:
        for competitor_name, wl in search_terms:
            if total_searched >= remaining:
                break

            wl_id = wl["id"]
            campaign_id = wl.get("campaign_id")
            wl_name = wl.get("name", "")
            attempted += 1

            try:
                # One unit for one request that reached LinkedIn — no sooner,
                # so a local outage cannot spend the day's allowance, and no
                # later, so a 429 storm is not free to retry at full speed.
                # The client counts what it puts on the wire because it is the
                # only layer that can: search_posts swallows transport errors
                # and returns ([], None). The scope keeps that count to THIS
                # call — in backend mode one client is shared, and the
                # scheduler runs five jobs at once. See search_traffic.py.
                search_error: Exception | None = None
                posts: list[dict] = []
                with search_scope(client) as scope:
                    try:
                        posts, _ = await client.search_posts(
                            account_id, competitor_name, limit=25
                        )
                    except Exception as e:
                        search_error = e
                if scope.request_was_issued(search_error):
                    total_searched += 1
                    polled_watchlists[wl_id] = wl
                    await run_db(
                        record_signal_search, SIGNAL_SEARCH_TYPE_COMPETITOR, 1
                    )
                else:
                    unissued += 1
                    logger.debug(
                        "Competitor search for '%s' never left this machine (%s) "
                        "— no budget spent",
                        competitor_name, search_error or "no request issued",
                    )
                if search_error is not None:
                    raise search_error

                # Issue #64: batch the contact cross-reference per search
                # (provider ids AND public slugs) — no per-signal queries.
                valid_posts = [
                    p for p in posts if p.get("post_id") and p.get("text")
                ]
                all_author_ids = [
                    p.get("author_id", "") for p in valid_posts if p.get("author_id")
                ]
                contacts_by_lid = (
                    await run_db(batch_get_contacts_by_linkedin_ids, all_author_ids)
                    if all_author_ids else {}
                )
                all_author_slugs = [
                    p.get("author_public_id", "")
                    for p in valid_posts if p.get("author_public_id")
                ]
                contacts_by_slug = (
                    await run_db(batch_get_contacts_by_public_slugs, all_author_slugs)
                    if all_author_slugs else {}
                )

                for post in valid_posts:
                    post_id = post.get("post_id", "")
                    post_text = post.get("text", "")
                    author_id = post.get("author_id", "")
                    author_public_id = post.get("author_public_id", "")
                    author_numeric_id = post.get("author_numeric_id", "")
                    author_name = post.get("author_name", "")
                    author_headline = post.get("author_headline", "")

                    # Store the post itself, not only the signal. These posts
                    # used to reach the posts table through keyword_collector,
                    # which searched competitor watchlists too; now that this
                    # collector owns them it has to keep that history, or
                    # signal_classifier's get_recent_topics_by_author silently
                    # loses every competitor-search author.
                    await run_db(
                        upsert_post,
                        post_id,
                        author_linkedin_id=(
                            author_id or author_public_id or author_numeric_id
                        ),
                        author_name=author_name,
                        text=post_text[:2000],
                        metrics_json=json.dumps({
                            "reactions_count": post.get("reactions_count", 0),
                            "comments_count": post.get("comments_count", 0),
                        }),
                        source="competitor_search",
                        visibility=post.get("visibility", ""),
                        media_type=post.get("media_type", ""),
                        is_repost=1 if post.get("is_repost") else 0,
                    )
                    total_posts_stored += 1

                    # Dedup
                    if await run_db(
                        signal_exists,
                        SIGNAL_COMPETITOR_MENTION, post_id=post_id
                    ):
                        continue

                    from ..author_identity import sendable_person_id
                    person_id = sendable_person_id(
                        provider_id=author_id, public_id=author_public_id,
                    )
                    if not person_id:
                        continue

                    # Analyze mention context
                    mention_context = _analyze_competitor_mention(
                        post_text, competitor_name
                    )

                    # Cross-reference author against contacts — by
                    # provider id when we have one, else by public slug.
                    linked_prospect_id = None
                    linked_campaign_id = campaign_id
                    is_known_contact = False
                    contact = contacts_by_lid.get(author_id) if author_id else None
                    if contact is None and author_public_id:
                        contact = contacts_by_slug.get(author_public_id)
                    if contact:
                        linked_prospect_id = contact["id"]
                        linked_campaign_id = (
                            linked_campaign_id
                            or contact.get("campaign_id")
                        )
                        is_known_contact = True

                    # Build metadata
                    metadata = {
                        "competitor_name": competitor_name,
                        "watchlist_id": wl_id,
                        "watchlist_name": wl_name,
                        "mention_context": mention_context,
                        "is_known_contact": is_known_contact,
                        "author_headline": author_headline,
                        "author_url": post.get("author_url", ""),
                        "author_public_id": author_public_id,
                        "author_numeric_id": author_numeric_id,
                        "reactions_count": post.get("reactions_count", 0),
                        "comments_count": post.get("comments_count", 0),
                        "timestamp": post.get("timestamp", ""),
                    }

                    await run_db(
                        save_signal,
                        signal_type=SIGNAL_COMPETITOR_MENTION,
                        source="competitor_search",
                        prospect_name=author_name or None,
                        prospect_title=author_headline or None,
                        linkedin_id=person_id,
                        prospect_id=linked_prospect_id,
                        campaign_id=linked_campaign_id,
                        content=post_text[:1000],
                        post_id=post_id,
                        metadata_json=json.dumps(metadata),
                        expires_at=now + SIGNAL_TTL_COMPETITOR_MENTION,
                    )
                    total_new += 1
                    if linked_prospect_id:
                        total_matched += 1

                    # Update signal account
                    if person_id:
                        await run_db(
                            upsert_signal_account,
                            linkedin_id=person_id,
                            prospect_name=author_name or None,
                        )

            except Exception as e:
                logger.warning(
                    "Competitor search failed for '%s': %s",
                    competitor_name,
                    e,
                )
                errors += 1

        # Only the watchlists this run actually reached — a term list longer
        # than the daily share leaves the rest for the next run, and a term
        # whose request never left the machine did not poll anything either.
        for polled_id in polled_watchlists:
            await run_db(update_watchlist, polled_id, last_polled_at=now)

    finally:
        await client.close()

    # Resume at the first term this run did not get to, so a share smaller
    # than the term list walks the whole list instead of re-searching its
    # head. Advanced by terms ATTEMPTED, not by searches booked: a term whose
    # request never left the machine was still walked past, and a cursor that
    # counted only the booked ones would name a resume point ahead of terms
    # this run had already consumed.
    next_cursor: int | None = None
    if attempted:
        candidate = (cursor + attempted) % len(all_terms)
        try:
            await run_db(save_setting, _TERM_CURSOR_KEY, candidate)
            next_cursor = candidate
        except Exception as e:  # a lost cursor costs fairness, not correctness
            logger.warning("Could not persist the competitor term cursor: %s", e)

    # "Searched" counts requests that reached LinkedIn, so a run whose calls
    # all died locally reports 0 rather than the length of the list it walked.
    summary = (
        f"Searched {total_searched} competitor terms, "
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

    unsearched = len(all_terms) - total_searched
    if unsearched > 0:
        summary += (
            f". {unsearched} terms unsearched this run"
            f" ({daily_count + total_searched}/{SIGNAL_DAILY_COMPETITOR_SEARCHES}"
            f" competitor searches used today)"
        )
        # Only claimed when the cursor was actually written — a run that could
        # not persist it will re-search this same head.
        if next_cursor is not None:
            summary += f"; the next run resumes at term {next_cursor + 1} of {len(all_terms)}"
    return summary


def _analyze_competitor_mention(post_text: str, competitor_name: str) -> str:
    """Analyze the context of a competitor mention in a post.

    Returns a brief context classification:
    - 'switching_from': Post mentions leaving/replacing the competitor
    - 'evaluating': Post mentions comparing/evaluating competitors
    - 'complaint': Post contains negative sentiment about competitor
    - 'recommendation': Post recommends the competitor (less valuable)
    - 'general': Neutral mention
    """
    text_lower = post_text.lower()
    comp_lower = competitor_name.lower()

    # Switching signals
    switching_phrases = [
        f"switching from {comp_lower}",
        f"replacing {comp_lower}",
        f"moving away from {comp_lower}",
        f"left {comp_lower}",
        f"leaving {comp_lower}",
        f"migrating from {comp_lower}",
        f"ditching {comp_lower}",
        f"dropped {comp_lower}",
        f"cancelled {comp_lower}",
        f"canceled {comp_lower}",
        "looking for alternatives",
        "looking for an alternative",
        "need a replacement",
    ]
    if any(phrase in text_lower for phrase in switching_phrases):
        return "switching_from"

    # Evaluation signals
    eval_phrases = [
        f"{comp_lower} vs",
        f"vs {comp_lower}",
        f"compared to {comp_lower}",
        f"alternative to {comp_lower}",
        f"alternatives to {comp_lower}",
        f"better than {comp_lower}",
        f"instead of {comp_lower}",
        "which tool",
        "which platform",
        "evaluating",
        "comparing",
        "shopping for",
    ]
    if any(phrase in text_lower for phrase in eval_phrases):
        return "evaluating"

    # Complaint signals
    complaint_phrases = [
        f"frustrated with {comp_lower}",
        f"disappointed with {comp_lower}",
        f"hate {comp_lower}",
        f"problem with {comp_lower}",
        f"issues with {comp_lower}",
        f"broken {comp_lower}",
        f"{comp_lower} is terrible",
        f"{comp_lower} sucks",
        f"{comp_lower} doesn't work",
        f"{comp_lower} is too expensive",
        f"{comp_lower} pricing",
    ]
    if any(phrase in text_lower for phrase in complaint_phrases):
        return "complaint"

    # Recommendation (less valuable — they like the competitor)
    rec_phrases = [
        f"love {comp_lower}",
        f"recommend {comp_lower}",
        f"{comp_lower} is amazing",
        f"{comp_lower} is great",
        f"use {comp_lower}",
        f"switched to {comp_lower}",
    ]
    if any(phrase in text_lower for phrase in rec_phrases):
        return "recommendation"

    return "general"
