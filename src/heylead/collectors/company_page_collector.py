"""Collector: company_page_collector — Monitor engagement on company LinkedIn pages.

Polls company page posts for new commenters and reactors via Unipile API,
cross-references against campaign ICPs, and saves as signals.

Runs every 1 hour via the scheduler.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..constants import (
    SIGNAL_COMPANY_POST_COMMENT,
    SIGNAL_COMPANY_POST_REACTION,
    SIGNAL_SEARCH_TYPE_COMPANY_PAGE,
    SIGNAL_TTL_COMPANY_POST_COMMENT,
    SIGNAL_TTL_COMPANY_POST_REACTION,
)

logger = logging.getLogger(__name__)


async def collect_company_page_signals() -> str:
    """Collect engagement signals from company page posts.

    For each company watchlist entry:
    1. Fetch recent posts from the company page
    2. For each post, get comments and reactions
    3. Diff against known commenters/reactors
    4. Save new engagers as signals
    5. Update tracked post records

    Returns summary string.
    """
    from ..db.async_bridge import run_db
    from ..db.signal_queries import (
        batch_signal_exists,
        get_tracked_posts_for_watchlist,
        get_company_post_by_post_id,
        list_watchlists,
        save_company_post_tracked,
        record_signal_search,
        save_signal,
        update_company_post_tracked,
        upsert_signal_account,
    )
    from ..linkedin import get_account_id, get_linkedin_client
    from ..linkedin.search_traffic import search_scope
    from ..services.post_freshness import post_item_published_at

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected — skipping company page collection."

    client = get_linkedin_client()

    # Get company-type watchlists
    watchlists = await run_db(list_watchlists, is_active=True)
    company_watchlists = [w for w in watchlists if w.get("watch_type") == "company"]

    if not company_watchlists:
        return "No company watchlists configured."

    total_new = 0
    errors = 0
    now = int(time.time())

    for wl in company_watchlists:
        wl_id = wl["id"]
        keywords_list = wl.get("keywords_list", [])
        campaign_id = wl.get("campaign_id")

        # The first keyword is the company LinkedIn identifier (URL or name)
        company_identifier = keywords_list[0] if keywords_list else ""
        company_name = wl.get("name", "Unknown Company")

        if not company_identifier:
            continue

        try:
            # Fetch recent posts from this company page
            posts = []

            # Strategy 1: Voyager company feed for numeric IDs
            if company_identifier.isdigit() and hasattr(client, "get_company_feed"):
                posts = await client.get_company_feed(
                    account_id, company_identifier, limit=10,
                )

            # Strategy 2: User posts by identifier (works for public_id/provider_id)
            if not posts and not company_identifier.isdigit():
                try:
                    posts = await client.get_user_posts(
                        account_id, company_identifier, limit=10,
                    )
                except Exception:
                    pass

            # Strategy 3: Fallback — search posts by company name
            # Tag these as search-sourced (we don't own them, so comments/reactions API will 404)
            if not posts and hasattr(client, "search_posts"):
                search_error: Exception | None = None
                search_results: list[dict[str, Any]] = []
                with search_scope(client) as scope:
                    try:
                        search_results, _ = await client.search_posts(
                            account_id, company_name, limit=10,
                        )
                    except Exception as e:
                        search_error = e
                if scope.request_was_issued(search_error):
                    await run_db(record_signal_search, SIGNAL_SEARCH_TYPE_COMPANY_PAGE, 1)
                if search_error is not None:
                    logger.debug("Post search fallback failed for %s: %s", company_name, search_error)
                else:
                    # Normalize search results to match post format
                    for sr in search_results:
                        pid = sr.get("social_id") or sr.get("post_id") or sr.get("id", "")
                        if pid:
                            # Ensure social_id is set for downstream processing
                            if not sr.get("social_id"):
                                sr["social_id"] = pid
                            sr["_source"] = "search"  # Tag for downstream filtering
                            posts.append(sr)

            if not posts:
                continue

            for post in posts:
                post_id = post.get("social_id") or post.get("id", "")
                post_text = (post.get("text") or "")[:500]

                if not post_id:
                    continue
                # The engagement cites this post; activation reads its age.
                published_at = post_item_published_at(
                    post_id, post.get("timestamp") or post.get("date"), now,
                )

                # Ensure post is tracked
                tracked = await run_db(get_company_post_by_post_id, post_id)
                if not tracked:
                    await run_db(
                        save_company_post_tracked,
                        watchlist_id=wl_id,
                        post_id=post_id,
                        post_text=post_text,
                    )
                    tracked = await run_db(get_company_post_by_post_id, post_id)

                known_commenters = set(
                    json.loads(tracked.get("known_commenters") or "[]")
                )
                known_reactors = set(
                    json.loads(tracked.get("known_reactors") or "[]")
                )

                # For search-sourced posts, we don't own them so
                # get_post_comments/get_post_reactions will 404. Skip API calls
                # but still create signals from search result metadata (author info).
                is_search_sourced = post.get("_source") == "search"

                # Check comments (skip for search-sourced posts — will 404)
                new_comment_ids = []
                if not is_search_sourced:
                    try:
                        comments = await client.get_post_comments(
                            account_id, post_id, limit=20,
                        )
                        # Batch dedup for comments
                        comment_dedup_keys = []
                        for c in comments:
                            aid = c.get("author_id") or c.get("provider_id", "")
                            if aid and aid != "N/A" and aid not in known_commenters:
                                comment_dedup_keys.append(f"cpc:{post_id}:{aid}")
                        existing_comment_signals = await run_db(
                            batch_signal_exists,
                            SIGNAL_COMPANY_POST_COMMENT, post_ids=comment_dedup_keys,
                        ) if comment_dedup_keys else set()

                        for comment in comments:
                            author_id = comment.get("author_id") or comment.get("provider_id", "")
                            if not author_id or author_id == "N/A" or author_id in known_commenters:
                                continue

                            author_name = comment.get("author_name", "") or comment.get("name", "")
                            dedup_key = f"cpc:{post_id}:{author_id}"

                            if dedup_key not in existing_comment_signals:
                                metadata = {
                                    "company_name": company_name,
                                    "commenter_name": author_name,
                                    "commenter_id": author_id,
                                    "post_id": post_id,
                                    "published_at": published_at,
                                    "post_text": post_text[:200],
                                    "comment_text": (comment.get("text") or "")[:200],
                                }
                                await run_db(
                                    save_signal,
                                    signal_type=SIGNAL_COMPANY_POST_COMMENT,
                                    source="company_page",
                                    prospect_name=author_name,
                                    linkedin_id=author_id,
                                    campaign_id=campaign_id,
                                    content=f"Commented on {company_name} post: {post_text[:100]}",
                                    post_id=dedup_key,
                                    metadata_json=json.dumps(metadata),
                                    expires_at=now + SIGNAL_TTL_COMPANY_POST_COMMENT,
                                )
                                await run_db(
                                    upsert_signal_account,
                                    linkedin_id=author_id,
                                    prospect_name=author_name,
                                )
                                total_new += 1

                            new_comment_ids.append(author_id)

                    except Exception as e:
                        logger.debug("Failed to get comments for post %s: %s", post_id, e)

                # Check reactions (skip for search-sourced posts — will 404)
                # For search-sourced posts, create reaction signal from the post author instead
                new_reactor_ids = []
                if is_search_sourced:
                    # Create a reaction signal from the search result author
                    author = post.get("author") or {}
                    if isinstance(author, str):
                        author = {"name": author}
                    author_id = (
                        author.get("provider_id")
                        or author.get("id")
                        or post.get("author_id")
                        or ""
                    )
                    author_name = (
                        author.get("name")
                        or post.get("author_name")
                        or ""
                    )
                    if author_id and author_id != "N/A" and author_id not in known_reactors:
                        dedup_key = f"cpr:{post_id}:{author_id}"
                        search_existing = await run_db(
                            batch_signal_exists,
                            SIGNAL_COMPANY_POST_REACTION, post_ids=[dedup_key],
                        )
                        if dedup_key not in search_existing:
                            metadata = {
                                "company_name": company_name,
                                "reactor_name": author_name,
                                "reactor_id": author_id,
                                "post_id": post_id,
                                "published_at": published_at,
                                "reaction_type": "POST_MENTION",
                                "source": "search",
                            }
                            await run_db(
                                save_signal,
                                signal_type=SIGNAL_COMPANY_POST_REACTION,
                                source="company_page",
                                prospect_name=author_name,
                                linkedin_id=author_id,
                                campaign_id=campaign_id,
                                content=f"Posted about {company_name}",
                                post_id=dedup_key,
                                metadata_json=json.dumps(metadata),
                                expires_at=now + SIGNAL_TTL_COMPANY_POST_REACTION,
                                confidence=0.80,
                            )
                            await run_db(
                                upsert_signal_account,
                                linkedin_id=author_id,
                                prospect_name=author_name,
                            )
                            total_new += 1
                        new_reactor_ids.append(author_id)
                else:
                    try:
                        reactions = await client.get_post_reactions(
                            account_id, post_id, limit=50,
                        )
                        # Batch dedup for reactions
                        react_dedup_keys = []
                        for rx in reactions:
                            aid = rx.get("author_id") or rx.get("provider_id", "")
                            if aid and aid != "N/A" and aid not in known_reactors:
                                react_dedup_keys.append(f"cpr:{post_id}:{aid}")
                        existing_react_signals = await run_db(
                            batch_signal_exists,
                            SIGNAL_COMPANY_POST_REACTION, post_ids=react_dedup_keys,
                        ) if react_dedup_keys else set()

                        for reaction in reactions:
                            author_id = reaction.get("author_id") or reaction.get("provider_id", "")
                            if not author_id or author_id == "N/A" or author_id in known_reactors:
                                continue

                            author_name = reaction.get("author_name", "") or reaction.get("name", "")
                            dedup_key = f"cpr:{post_id}:{author_id}"

                            if dedup_key not in existing_react_signals:
                                metadata = {
                                    "company_name": company_name,
                                    "reactor_name": author_name,
                                    "reactor_id": author_id,
                                    "post_id": post_id,
                                    "published_at": published_at,
                                    "reaction_type": reaction.get("type", "LIKE"),
                                }
                                await run_db(
                                    save_signal,
                                    signal_type=SIGNAL_COMPANY_POST_REACTION,
                                    source="company_page",
                                    prospect_name=author_name,
                                    linkedin_id=author_id,
                                    campaign_id=campaign_id,
                                    content=f"Reacted to {company_name} post",
                                    post_id=dedup_key,
                                    metadata_json=json.dumps(metadata),
                                    expires_at=now + SIGNAL_TTL_COMPANY_POST_REACTION,
                                    confidence=0.80,
                                )
                                await run_db(
                                    upsert_signal_account,
                                    linkedin_id=author_id,
                                    prospect_name=author_name,
                                )
                                total_new += 1

                            new_reactor_ids.append(author_id)

                    except Exception as e:
                        logger.debug("Failed to get reactions for post %s: %s", post_id, e)

                # Update tracked post with new known engagers
                all_commenters = list(known_commenters | set(new_comment_ids))
                all_reactors = list(known_reactors | set(new_reactor_ids))
                await run_db(
                    update_company_post_tracked,
                    post_id=post_id,
                    known_commenters=all_commenters,
                    known_reactors=all_reactors,
                )

        except Exception as e:
            logger.warning("Company page collection failed for '%s': %s", company_name, e)
            errors += 1

    summary = f"Scanned {len(company_watchlists)} company pages, found {total_new} new engagement signals"
    if errors:
        summary += f", {errors} errors"
    return summary
