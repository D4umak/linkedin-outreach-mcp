"""Collector: post_intent_collector — Classify prospect posts for granular buyer intent.

Extends the basic SIGNAL_PROSPECT_POST with deeper intent classification:
- post_pain_point: Prospect posts about a problem our product solves
- post_tech_evaluation: Prospect evaluating tools/vendors
- post_budget_signal: Mentions budget, investment, ROI, timeline
- post_seeking_recs: Asks network for recommendations
- post_negative_experience: Complaints about competitor or product category

Also detects repost/share intelligence:
- reshares_competitor: Prospect reposts competitor content
- reshares_our_content: Prospect reposts our company content

Runs every 30 min via the scheduler (POST_INTENT_CLASSIFY_SECONDS).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


async def classify_post_intents() -> str:
    """Classify unclassified prospect posts for granular buyer intent signals.

    Flow:
    1. Get recent prospect_post signals that haven't been sub-classified yet
    2. For each, run keyword-based intent detection
    3. Create additional granular signals (post_pain_point, post_tech_evaluation, etc.)
    4. Also check reposts for competitor/our-content sharing

    Returns summary string.
    """
    from ..constants import (
        COMPANY_POST_TOPIC_KEYWORDS,
        POST_INTENT_BATCH_SIZE,
        POST_INTENT_KEYWORDS,
        SIGNAL_COMPANY_HIRING_POST,
        SIGNAL_COMPANY_MILESTONE_POST,
        SIGNAL_COMPANY_TECH_STACK_POST,
        SIGNAL_INTENT_BUYING,
        SIGNAL_INTENT_COMPETITOR_EVAL,
        SIGNAL_INTENT_PAIN_POINT,
        SIGNAL_POST_BUDGET_SIGNAL,
        SIGNAL_POST_NEGATIVE_EXPERIENCE,
        SIGNAL_POST_PAIN_POINT,
        SIGNAL_POST_SEEKING_RECS,
        SIGNAL_POST_TECH_EVALUATION,
        SIGNAL_PROSPECT_POST,
        SIGNAL_RESHARES_COMPETITOR,
        SIGNAL_RESHARES_OUR_CONTENT,
        SIGNAL_STATUS_CLASSIFIED,
        SIGNAL_TTL_COMPANY_HIRING_POST,
        SIGNAL_TTL_COMPANY_MILESTONE_POST,
        SIGNAL_TTL_COMPANY_TECH_STACK_POST,
        SIGNAL_TTL_POST_BUDGET_SIGNAL,
        SIGNAL_TTL_POST_NEGATIVE_EXPERIENCE,
        SIGNAL_TTL_POST_PAIN_POINT,
        SIGNAL_TTL_POST_SEEKING_RECS,
        SIGNAL_TTL_POST_TECH_EVALUATION,
        SIGNAL_TTL_RESHARES_COMPETITOR,
        SIGNAL_TTL_RESHARES_OUR_CONTENT,
    )
    from ..db.async_bridge import run_db
    from ..db.post_queries import get_post, list_posts
    from ..db.signal_queries import (
        list_signals,
        save_signal,
        signal_exists,
        update_signal,
        upsert_signal_account,
    )
    from ..services.post_freshness import post_id_published_at, post_published_at

    hook_types = (
        SIGNAL_POST_PAIN_POINT,
        SIGNAL_POST_TECH_EVALUATION,
        SIGNAL_POST_BUDGET_SIGNAL,
        SIGNAL_POST_SEEKING_RECS,
        SIGNAL_POST_NEGATIVE_EXPERIENCE,
    )
    category_intent = {
        "seeking_recommendations": SIGNAL_INTENT_BUYING,
        "budget_signal": SIGNAL_INTENT_BUYING,
        "tech_evaluation": SIGNAL_INTENT_COMPETITOR_EVAL,
        "negative_experience": SIGNAL_INTENT_COMPETITOR_EVAL,
        "pain_point": SIGNAL_INTENT_PAIN_POINT,
    }

    now = int(time.time())

    # Campaign scans and the network feed both carry buyer-intent text.
    cutoff = now - (7 * 86400)
    recent_posts: list[dict] = []
    seen_post_ids: set[str] = set()
    for source in ("prospect_scan", "network_scan"):
        for post in await run_db(
            list_posts, source=source, limit=POST_INTENT_BATCH_SIZE * 3,
        ):
            pid = post.get("post_id") or ""
            if not pid or pid in seen_post_ids:
                continue
            seen_post_ids.add(pid)
            recent_posts.append(post)

    if not recent_posts:
        return "No recent prospect posts to classify."

    # Filter to posts within window that haven't been intent-classified
    posts_to_classify = []
    for post in recent_posts:
        if (post.get("first_seen_at") or 0) < cutoff:
            continue
        post_id = post.get("post_id", "")
        already = False
        for hook_type in hook_types:
            if await run_db(signal_exists, hook_type, post_id=post_id):
                already = True
                break
            if await run_db(
                signal_exists, hook_type, post_id=f"pi:{post_id}:{hook_type}",
            ):
                already = True
                break
        if already:
            continue
        posts_to_classify.append(post)

    posts_to_classify = posts_to_classify[:POST_INTENT_BATCH_SIZE]
    if not posts_to_classify:
        return "No unclassified prospect posts found."

    # Load company watchlist names for repost detection
    our_company_names = await run_db(_get_our_company_names)
    competitor_names = await run_db(_get_competitor_names)

    total_signals = 0
    classified = 0

    for post in posts_to_classify:
        post_id = post.get("post_id", "")
        text = (post.get("text") or "").lower()
        author_id = post.get("author_linkedin_id", "")
        author_name = post.get("author_name", "")
        is_repost = bool(post.get("is_repost"))

        if not text or not post_id:
            continue

        classified += 1
        # Every signal derived here cites this post. The window above is on
        # first_seen_at, i.e. when we found it, which says nothing about when
        # it was written; activation reads this instead.
        published_at = post_id_published_at(post_id, now)

        # ── Keyword-based intent detection ──
        detected_intents: list[tuple[str, str, float, int]] = []  # (signal_type, category, confidence, ttl)

        for category, keywords in POST_INTENT_KEYWORDS.items():
            matched = [kw for kw in keywords if kw in text]
            if not matched:
                continue

            confidence = min(0.5 + len(matched) * 0.15, 0.95)

            if category == "seeking_recommendations":
                detected_intents.append((
                    SIGNAL_POST_SEEKING_RECS, category, confidence,
                    SIGNAL_TTL_POST_SEEKING_RECS,
                ))
            elif category == "budget_signal":
                detected_intents.append((
                    SIGNAL_POST_BUDGET_SIGNAL, category, confidence,
                    SIGNAL_TTL_POST_BUDGET_SIGNAL,
                ))
            elif category == "tech_evaluation":
                detected_intents.append((
                    SIGNAL_POST_TECH_EVALUATION, category, confidence,
                    SIGNAL_TTL_POST_TECH_EVALUATION,
                ))
            elif category == "negative_experience":
                detected_intents.append((
                    SIGNAL_POST_NEGATIVE_EXPERIENCE, category, confidence,
                    SIGNAL_TTL_POST_NEGATIVE_EXPERIENCE,
                ))
            elif category == "pain_point":
                detected_intents.append((
                    SIGNAL_POST_PAIN_POINT, category, confidence,
                    SIGNAL_TTL_POST_PAIN_POINT,
                ))

        # Save detected intent signals
        for signal_type, category, confidence, ttl in detected_intents:
            dedup_key = f"pi:{post_id}:{signal_type}"
            if await run_db(signal_exists, signal_type, post_id=dedup_key):
                continue

            # Find linked prospect from the original prospect_post signal
            prospect_signals = await run_db(
                list_signals,
                signal_type=SIGNAL_PROSPECT_POST, post_id=post_id, limit=1,
            )
            prospect_id = None
            campaign_id = None
            source_published_at = None
            if prospect_signals:
                prospect_id = prospect_signals[0].get("prospect_id")
                campaign_id = prospect_signals[0].get("campaign_id")
                source_published_at = post_published_at(prospect_signals[0], now)

            metadata = {
                "published_at": source_published_at or published_at,
                "intent_category": category,
                "post_text_preview": text[:200],
                "impressions_count": post.get("impressions_count", 0),
                "media_type": post.get("media_type", "text"),
            }

            sid = await run_db(
                save_signal,
                signal_type=signal_type,
                source="post_intent_scan",
                prospect_id=prospect_id,
                prospect_name=author_name or None,
                linkedin_id=author_id or None,
                campaign_id=campaign_id,
                content=text[:1000],
                post_id=dedup_key,
                metadata_json=json.dumps(metadata),
                expires_at=now + ttl,
                confidence=confidence,
            )
            if not sid:
                continue
            await run_db(
                update_signal,
                sid,
                status=SIGNAL_STATUS_CLASSIFIED,
                intent=category_intent.get(category, SIGNAL_INTENT_PAIN_POINT),
                classified_at=now,
            )
            if author_id:
                await run_db(
                    upsert_signal_account,
                    linkedin_id=author_id,
                    prospect_name=author_name or None,
                )
            total_signals += 1

        # ── Repost/share detection ──
        if is_repost and text:
            # Check if resharing competitor content
            for comp_name in competitor_names:
                if comp_name.lower() in text:
                    dedup_key = f"rsc:{post_id}"
                    if not await run_db(signal_exists, SIGNAL_RESHARES_COMPETITOR, post_id=dedup_key):
                        await run_db(
                            save_signal,
                            signal_type=SIGNAL_RESHARES_COMPETITOR,
                            source="post_intent_scan",
                            prospect_name=author_name or None,
                            linkedin_id=author_id or None,
                            content=f"Reshared competitor content mentioning {comp_name}",
                            post_id=dedup_key,
                            metadata_json=json.dumps({
                                "published_at": published_at,
                                "competitor_name": comp_name,
                                "post_preview": text[:200],
                            }),
                            expires_at=now + SIGNAL_TTL_RESHARES_COMPETITOR,
                        )
                        if author_id:
                            await run_db(
                                upsert_signal_account,
                                linkedin_id=author_id,
                                prospect_name=author_name or None,
                            )
                        total_signals += 1
                    break

            # Check if resharing our content
            for our_name in our_company_names:
                if our_name.lower() in text:
                    dedup_key = f"rso:{post_id}"
                    if not await run_db(signal_exists, SIGNAL_RESHARES_OUR_CONTENT, post_id=dedup_key):
                        await run_db(
                            save_signal,
                            signal_type=SIGNAL_RESHARES_OUR_CONTENT,
                            source="post_intent_scan",
                            prospect_name=author_name or None,
                            linkedin_id=author_id or None,
                            content=f"Reshared our content ({our_name})",
                            post_id=dedup_key,
                            metadata_json=json.dumps({
                                "published_at": published_at,
                                "our_company": our_name,
                                "post_preview": text[:200],
                            }),
                            expires_at=now + SIGNAL_TTL_RESHARES_OUR_CONTENT,
                        )
                        if author_id:
                            await run_db(
                                upsert_signal_account,
                                linkedin_id=author_id,
                                prospect_name=author_name or None,
                            )
                        total_signals += 1
                    break

    # ── Company post topic classification ──
    company_signals = await run_db(_classify_company_posts, now)
    total_signals += company_signals

    summary = (
        f"Post intent scan: {classified} posts classified, "
        f"{total_signals} new granular signals created"
    )
    logger.info(summary)
    return summary


def _classify_company_posts(now: int) -> int:
    """Classify company page posts by topic (hiring, milestone, tech_stack)."""
    from ..constants import (
        COMPANY_POST_TOPIC_KEYWORDS,
        SIGNAL_COMPANY_HIRING_POST,
        SIGNAL_COMPANY_MILESTONE_POST,
        SIGNAL_COMPANY_TECH_STACK_POST,
        SIGNAL_TTL_COMPANY_HIRING_POST,
        SIGNAL_TTL_COMPANY_MILESTONE_POST,
        SIGNAL_TTL_COMPANY_TECH_STACK_POST,
    )
    from ..db.post_queries import list_posts
    from ..db.signal_queries import save_signal, signal_exists, upsert_signal_account
    from ..services.post_freshness import post_id_published_at

    # Map topic categories to signal types and TTLs
    topic_map: dict[str, tuple[str, int]] = {
        "hiring": (SIGNAL_COMPANY_HIRING_POST, SIGNAL_TTL_COMPANY_HIRING_POST),
        "milestone": (SIGNAL_COMPANY_MILESTONE_POST, SIGNAL_TTL_COMPANY_MILESTONE_POST),
        "tech_stack": (SIGNAL_COMPANY_TECH_STACK_POST, SIGNAL_TTL_COMPANY_TECH_STACK_POST),
    }

    # Get company page posts (source=company_page or search results with company context)
    cutoff = now - (7 * 86400)
    company_posts = list_posts(source="company_page", limit=30)
    total = 0

    for post in company_posts:
        if (post.get("first_seen_at") or 0) < cutoff:
            continue

        post_id = post.get("post_id", "")
        text = (post.get("text") or "").lower()
        author_id = post.get("author_linkedin_id", "")
        author_name = post.get("author_name", "")

        if not text or not post_id:
            continue

        for category, keywords in COMPANY_POST_TOPIC_KEYWORDS.items():
            matched = [kw for kw in keywords if kw in text]
            if not matched:
                continue

            signal_type, ttl = topic_map.get(category, (None, 0))
            if not signal_type:
                continue

            dedup_key = f"cpt:{post_id}:{signal_type}"
            if signal_exists(signal_type, post_id=dedup_key):
                continue

            confidence = min(0.5 + len(matched) * 0.15, 0.90)

            save_signal(
                signal_type=signal_type,
                source="company_post_topic",
                prospect_name=author_name or None,
                linkedin_id=author_id or None,
                content=f"Company post about {category}: {text[:200]}",
                post_id=dedup_key,
                metadata_json=json.dumps({
                    "published_at": post_id_published_at(post_id, now),
                    "topic": category,
                    "matched_keywords": matched[:5],
                    "post_preview": text[:200],
                }),
                expires_at=now + ttl,
                confidence=confidence,
            )
            if author_id:
                upsert_signal_account(
                    linkedin_id=author_id,
                    prospect_name=author_name or None,
                )
            total += 1

    return total


def _get_our_company_names() -> list[str]:
    """Get company names associated with our campaigns."""
    from ..db.queries import list_campaigns

    names: set[str] = set()
    for campaign in list_campaigns(status="active"):
        context = campaign.get("company_context", "") or ""
        name = campaign.get("name", "") or ""
        if name:
            # Extract likely company name from campaign name
            for word in name.split():
                if len(word) > 3 and word[0].isupper():
                    names.add(word)
        # Also check ICP
        icp_json = campaign.get("icp_json", "")
        if icp_json:
            try:
                icp = json.loads(icp_json) if isinstance(icp_json, str) else icp_json
                company = icp.get("company_name", "") or icp.get("company", "")
                if company:
                    names.add(company)
            except (json.JSONDecodeError, TypeError):
                pass
    return list(names)


def _get_competitor_names() -> list[str]:
    """Get competitor names from watchlists."""
    from ..db.signal_queries import list_watchlists

    names: set[str] = set()
    for wl in list_watchlists(is_active=True):
        if wl.get("watch_type") == "competitor":
            keywords = wl.get("keywords_list", [])
            names.update(str(k) for k in keywords if k)
    return list(names)
