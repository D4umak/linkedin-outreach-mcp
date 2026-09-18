"""Collector: comment_mining_collector — Mine comments from competitor & industry posts.

Extracts leads from:
1. Competitor company post commenters (competitor_post_commenter)
2. High-engagement industry posts (industry_thread_participant)
3. Known prospects commenting on competitor posts (prospect_comments_on_competitor)

These are Tier 3 signals — higher effort but massive lead gen potential.
Each commenter is cross-referenced against campaign ICPs for relevance.

Runs every 2 hours via the scheduler (SIGNAL_COMMENT_MINING_SECONDS).
"""

from __future__ import annotations

import json
from ..textutil import contains_term
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_COMPETITOR_WL_CURSOR_KEY = "comment_mining_competitor_wl_cursor"
_COMPETITOR_KW_CURSOR_KEY = "comment_mining_competitor_kw_cursor"
_KEYWORD_WL_CURSOR_KEY = "comment_mining_keyword_wl_cursor"
_KEYWORD_KW_CURSOR_KEY = "comment_mining_keyword_kw_cursor"

_COMPETITOR_WL_CAP = 5
_COMPETITOR_KW_CAP = 3
_KEYWORD_WL_CAP = 3
_KEYWORD_KW_CAP = 2


def _rotate(items: list, cursor: int) -> list:
    """Start at `cursor`, wrapping — a permutation, not a truncation."""
    if not items:
        return []
    start = cursor % len(items)
    return items[start:] + items[:start]


async def _read_cursor(run_db, get_setting, key: str) -> int:
    raw = await run_db(get_setting, key, 0)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


async def mine_post_comments() -> str:
    """Mine comments from competitor/industry posts for lead signals.

    Flow:
    1. Get competitor watchlists → fetch their recent posts
    2. For high-engagement posts (20+ comments), extract all commenters
    3. Cross-reference commenters against active ICPs
    4. Create signals for ICP-matching commenters
    5. Also check if known prospects are commenting on competitor posts

    Returns summary string.
    """
    from ..constants import (
        COMMENT_MINING_MAX_COMMENTERS_PER_POST,
        COMMENT_MINING_MAX_POSTS,
        COMMENT_MINING_MIN_COMMENTS,
        SIGNAL_COMPETITOR_POST_COMMENTER,
        SIGNAL_INDUSTRY_THREAD_PARTICIPANT,
        SIGNAL_PROSPECT_COMMENTS_ON_COMPETITOR,
        SIGNAL_TTL_COMPETITOR_POST_COMMENTER,
        SIGNAL_TTL_INDUSTRY_THREAD_PARTICIPANT,
        SIGNAL_TTL_PROSPECT_COMMENTS_ON_COMPETITOR,
        SIGNAL_SEARCH_TYPE_COMMENT_MINING,
    )
    from ..db.async_bridge import run_db
    from ..db.queries import get_setting, save_setting
    from ..db.signal_queries import (
        list_watchlists,
        record_signal_search,
        is_post_mined,
        mark_post_mined,
        save_signal,
        signal_exists,
        upsert_signal_account,
    )
    from ..linkedin import get_account_id, get_linkedin_client
    from ..linkedin.search_traffic import search_scope

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected — skipping comment mining."

    client = get_linkedin_client()
    now = int(time.time())

    # Get competitor and keyword watchlists
    watchlists = await run_db(list_watchlists, is_active=True)
    competitor_wls = [w for w in watchlists if w.get("watch_type") == "competitor"]
    keyword_wls = [
        w for w in watchlists if w.get("watch_type") in ("keyword", "industry")
    ]

    if not competitor_wls and not keyword_wls:
        return "No watchlists configured for comment mining."

    # Load ICP titles/industries for matching
    icp_matcher = await run_db(_load_icp_matcher)

    total_new = 0
    posts_mined = 0
    errors = 0

    try:
        # ── Phase 1: Mine competitor post comments ──
        # Rotate so the oldest watchlists and the 4th+ keywords are not
        # starved by the per-run cap (ORDER BY created_at DESC + [:5][:3]).
        comp_wl_cursor = await _read_cursor(run_db, get_setting, _COMPETITOR_WL_CURSOR_KEY)
        comp_kw_cursor = await _read_cursor(run_db, get_setting, _COMPETITOR_KW_CURSOR_KEY)
        for wl in _rotate(competitor_wls, comp_wl_cursor)[:_COMPETITOR_WL_CAP]:
            keywords_list = wl.get("keywords_list", [])
            campaign_id = wl.get("campaign_id")
            wl_name = wl.get("name", "")

            for keyword in _rotate(keywords_list, comp_kw_cursor)[:_COMPETITOR_KW_CAP]:
                search_error: Exception | None = None
                results: list[dict[str, Any]] = []
                with search_scope(client) as scope:
                    try:
                        results, _ = await client.search_posts(
                            account_id, keyword, limit=10,
                        )
                    except Exception as e:
                        search_error = e
                if scope.request_was_issued(search_error):
                    await run_db(record_signal_search, SIGNAL_SEARCH_TYPE_COMMENT_MINING, 1)
                if search_error is not None:
                    logger.debug("Competitor post search failed for '%s': %s", keyword, search_error)
                    errors += 1
                    continue

                # Filter to high-engagement posts
                viral_posts = [
                    p for p in results
                    if int(p.get("comments_count", 0) or 0) >= COMMENT_MINING_MIN_COMMENTS
                ]

                for post in viral_posts[:COMMENT_MINING_MAX_POSTS]:
                    post_id = post.get("post_id", "")
                    post_text = (post.get("text") or "")[:300]
                    comments_count = int(post.get("comments_count", 0) or 0)

                    if not post_id:
                        continue

                    # Skip if already mined (dedup by post_id + source)
                    mine_key = f"mine:{post_id}"
                    if await run_db(is_post_mined, mine_key):
                        continue

                    try:
                        comments = await client.get_post_comments(
                            account_id, post_id,
                            limit=COMMENT_MINING_MAX_COMMENTERS_PER_POST,
                        )
                    except Exception as e:
                        logger.debug("get_post_comments failed for %s: %s", post_id, e)
                        errors += 1
                        continue

                    posts_mined += 1

                    for comment in comments:
                        author_id, author_public_id = _commenter_ids(comment)
                        author_name = comment.get("author_name", "")
                        author_headline = comment.get("author_headline", "")
                        comment_text = comment.get("text", "")
                        person_id = _commenter_person_id(author_id, author_public_id)
                        if not person_id:
                            continue

                        # Check if this is a known prospect
                        known_contact = await run_db(
                            _resolve_commenter_contact, person_id, author_public_id,
                        )

                        if known_contact:
                            # Known prospect commenting on competitor → high-value signal
                            dedup_key = f"pcoc:{post_id}:{person_id}"
                            if not await run_db(
                                signal_exists,
                                SIGNAL_PROSPECT_COMMENTS_ON_COMPETITOR,
                                post_id=dedup_key,
                            ):
                                metadata = {
                                    "competitor_keyword": keyword,
                                    "post_text": post_text,
                                    "comment_text": comment_text[:200],
                                    "comments_on_post": comments_count,
                                    "contact_name": known_contact.get("name", ""),
                                    "contact_campaign_id": known_contact.get("campaign_id", ""),
                                }
                                await run_db(
                                    save_signal,
                                    signal_type=SIGNAL_PROSPECT_COMMENTS_ON_COMPETITOR,
                                    source="comment_mining",
                                    prospect_id=known_contact.get("id"),
                                    prospect_name=author_name,
                                    prospect_title=author_headline or known_contact.get("title"),
                                    linkedin_id=person_id,
                                    campaign_id=known_contact.get("campaign_id") or campaign_id,
                                    content=f"Commented on competitor post about '{keyword}': {comment_text[:200]}",
                                    post_id=dedup_key,
                                    metadata_json=json.dumps(metadata),
                                    expires_at=now + SIGNAL_TTL_PROSPECT_COMMENTS_ON_COMPETITOR,
                                )
                                await run_db(
                                    upsert_signal_account,
                                    linkedin_id=person_id,
                                    prospect_name=author_name,
                                )
                                total_new += 1
                        else:
                            # Unknown person — check ICP match
                            if not _matches_icp(author_headline, icp_matcher):
                                continue

                            dedup_key = f"cpc_mine:{post_id}:{person_id}"
                            if not await run_db(
                                signal_exists,
                                SIGNAL_COMPETITOR_POST_COMMENTER,
                                post_id=dedup_key,
                            ):
                                is_question = comment_text.strip().endswith("?")
                                metadata = {
                                    "competitor_keyword": keyword,
                                    "post_text": post_text,
                                    "comment_text": comment_text[:200],
                                    "author_headline": author_headline,
                                    "is_question": is_question,
                                    "comments_on_post": comments_count,
                                }
                                # Boost confidence for questions (stronger intent)
                                confidence = 0.60 if is_question else 0.45

                                await run_db(
                                    save_signal,
                                    signal_type=SIGNAL_COMPETITOR_POST_COMMENTER,
                                    source="comment_mining",
                                    prospect_name=author_name,
                                    prospect_title=author_headline,
                                    linkedin_id=person_id,
                                    campaign_id=campaign_id,
                                    content=f"Commented on competitor post: {comment_text[:300]}",
                                    post_id=dedup_key,
                                    metadata_json=json.dumps(metadata),
                                    expires_at=now + SIGNAL_TTL_COMPETITOR_POST_COMMENTER,
                                    confidence=confidence,
                                )
                                await run_db(
                                    upsert_signal_account,
                                    linkedin_id=person_id,
                                    prospect_name=author_name,
                                )
                                total_new += 1

                    await run_db(
                        mark_post_mined,
                        mine_key,
                        {"mined_at": now, "comments_extracted": len(comments)},
                    )

        if competitor_wls:
            await run_db(
                save_setting,
                _COMPETITOR_WL_CURSOR_KEY,
                (comp_wl_cursor + min(_COMPETITOR_WL_CAP, len(competitor_wls)))
                % len(competitor_wls),
            )
            max_kw = max((len(w.get("keywords_list") or []) for w in competitor_wls), default=0)
            if max_kw:
                await run_db(
                    save_setting,
                    _COMPETITOR_KW_CURSOR_KEY,
                    (comp_kw_cursor + _COMPETITOR_KW_CAP) % max_kw,
                )

        # ── Phase 2: Mine industry keyword post comments ──
        kw_wl_cursor = await _read_cursor(run_db, get_setting, _KEYWORD_WL_CURSOR_KEY)
        kw_kw_cursor = await _read_cursor(run_db, get_setting, _KEYWORD_KW_CURSOR_KEY)
        for wl in _rotate(keyword_wls, kw_wl_cursor)[:_KEYWORD_WL_CAP]:
            keywords_list = wl.get("keywords_list", [])
            campaign_id = wl.get("campaign_id")

            for keyword in _rotate(keywords_list, kw_kw_cursor)[:_KEYWORD_KW_CAP]:
                search_error = None
                results = []
                with search_scope(client) as scope:
                    try:
                        results, _ = await client.search_posts(
                            account_id, keyword, limit=10,
                        )
                    except Exception as e:
                        search_error = e
                if scope.request_was_issued(search_error):
                    await run_db(record_signal_search, SIGNAL_SEARCH_TYPE_COMMENT_MINING, 1)
                if search_error is not None:
                    logger.debug("Industry post search failed for '%s': %s", keyword, search_error)
                    errors += 1
                    continue

                # Only mine very high-engagement posts for industry threads
                viral_posts = [
                    p for p in results
                    if int(p.get("comments_count", 0) or 0) >= COMMENT_MINING_MIN_COMMENTS * 2
                ]

                for post in viral_posts[:3]:  # Max 3 viral posts per keyword
                    post_id = post.get("post_id", "")
                    post_text = (post.get("text") or "")[:300]

                    if not post_id:
                        continue

                    mine_key = f"mine_ind:{post_id}"
                    if await run_db(is_post_mined, mine_key):
                        continue

                    try:
                        comments = await client.get_post_comments(
                            account_id, post_id,
                            limit=COMMENT_MINING_MAX_COMMENTERS_PER_POST,
                        )
                    except Exception as e:
                        logger.debug("get_post_comments failed for industry post %s: %s", post_id, e)
                        errors += 1
                        continue

                    posts_mined += 1

                    for comment in comments:
                        author_id, author_public_id = _commenter_ids(comment)
                        author_name = comment.get("author_name", "")
                        author_headline = comment.get("author_headline", "")
                        comment_text = comment.get("text", "")
                        person_id = _commenter_person_id(author_id, author_public_id)
                        if not person_id:
                            continue

                        # Check ICP match
                        if not _matches_icp(author_headline, icp_matcher):
                            continue

                        dedup_key = f"itp:{post_id}:{person_id}"
                        if await run_db(signal_exists, SIGNAL_INDUSTRY_THREAD_PARTICIPANT, post_id=dedup_key):
                            continue

                        is_question = comment_text.strip().endswith("?")
                        metadata = {
                            "industry_keyword": keyword,
                            "post_text": post_text,
                            "comment_text": comment_text[:200],
                            "author_headline": author_headline,
                            "is_question": is_question,
                        }

                        confidence = 0.55 if is_question else 0.40

                        await run_db(
                            save_signal,
                            signal_type=SIGNAL_INDUSTRY_THREAD_PARTICIPANT,
                            source="comment_mining",
                            prospect_name=author_name,
                            prospect_title=author_headline,
                            linkedin_id=person_id,
                            campaign_id=campaign_id,
                            content=f"Active in industry thread about '{keyword}': {comment_text[:300]}",
                            post_id=dedup_key,
                            metadata_json=json.dumps(metadata),
                            expires_at=now + SIGNAL_TTL_INDUSTRY_THREAD_PARTICIPANT,
                            confidence=confidence,
                        )
                        await run_db(
                            upsert_signal_account,
                            linkedin_id=person_id,
                            prospect_name=author_name,
                        )
                        total_new += 1

                    await run_db(mark_post_mined, mine_key, {"mined_at": now})

        if keyword_wls:
            await run_db(
                save_setting,
                _KEYWORD_WL_CURSOR_KEY,
                (kw_wl_cursor + min(_KEYWORD_WL_CAP, len(keyword_wls)))
                % len(keyword_wls),
            )
            max_kw = max((len(w.get("keywords_list") or []) for w in keyword_wls), default=0)
            if max_kw:
                await run_db(
                    save_setting,
                    _KEYWORD_KW_CURSOR_KEY,
                    (kw_kw_cursor + _KEYWORD_KW_CAP) % max_kw,
                )

    finally:
        await client.close()

    summary = (
        f"Comment mining: {posts_mined} posts mined, "
        f"{total_new} new signals created"
    )
    if errors:
        summary += f", {errors} errors"
    logger.info(summary)
    return summary


def _commenter_ids(comment: dict[str, Any]) -> tuple[str, str]:
    author_id = str(comment.get("author_id") or "").strip()
    if author_id == "N/A":
        author_id = ""
    public_id = str(comment.get("author_public_id") or "").strip()
    if not public_id:
        from ..author_identity import slug_from_profile_url
        public_id = slug_from_profile_url(comment.get("author_profile_url") or "")
    return author_id, public_id


def _commenter_person_id(author_id: str, author_public_id: str) -> str:
    from ..author_identity import sendable_person_id
    return sendable_person_id(provider_id=author_id, public_id=author_public_id)


def _resolve_commenter_contact(person_id: str, public_id: str) -> dict[str, Any] | None:
    from ..author_identity import looks_like_provider_id, normalize_public_slug
    from ..db.signal_queries import (
        batch_get_contacts_by_public_slugs,
        get_contact_by_linkedin_id,
    )

    if person_id:
        found = get_contact_by_linkedin_id(person_id)
        if found:
            return found
    slug = normalize_public_slug(public_id or person_id)
    if slug and not looks_like_provider_id(slug):
        return batch_get_contacts_by_public_slugs([slug]).get(slug)
    return None


def _load_icp_matcher() -> dict[str, Any]:
    """Load ICP data for lightweight headline matching."""
    from ..db.queries import list_icps

    matcher: dict[str, Any] = {
        "titles": set(),
        "industries": set(),
        "seniority_keywords": {
            "vp", "vice president", "director", "head of", "chief",
            "cto", "cmo", "cro", "coo", "cfo", "ceo", "founder",
            "co-founder", "partner", "lead", "manager", "senior",
            "principal", "svp", "evp",
        },
    }

    for icp_row in list_icps(status="active"):
        icp_json_str = icp_row.get("icp_json", "{}")
        try:
            parsed = json.loads(icp_json_str) if isinstance(icp_json_str, str) else icp_json_str
        except (json.JSONDecodeError, TypeError):
            continue

        for sub_icp in (parsed.get("icps", [parsed]) if parsed else []):
            # Extract job titles
            jt = sub_icp.get("job_titles", {})
            titles = jt.get("include", []) if isinstance(jt, dict) else (jt if isinstance(jt, list) else [])
            for title in titles:
                matcher["titles"].add(str(title).lower())

            # Extract industries
            ind = sub_icp.get("industries", {})
            industries = ind.get("include", []) if isinstance(ind, dict) else (ind if isinstance(ind, list) else [])
            for industry in industries:
                matcher["industries"].add(str(industry).lower())

    return matcher


def _matches_icp(headline: str, matcher: dict[str, Any]) -> bool:
    """Lightweight ICP match: check if headline contains ICP job titles or seniority keywords."""
    if not headline:
        return False

    headline_lower = headline.lower()

    # Check seniority keywords (fast filter — must have some seniority indicator)
    has_seniority = any(contains_term(headline_lower, kw) for kw in matcher.get("seniority_keywords", set()))
    if not has_seniority:
        return False

    # Check ICP title match (at least partial)
    titles = matcher.get("titles", set())
    if titles:
        for title in titles:
            if contains_term(headline_lower, title):
                return True

    # If no specific titles to match but has seniority, accept
    if not titles:
        return True

    return False
