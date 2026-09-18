"""Post research service — deep contact research, backfill, viral detection, watchlist tuning.

Orchestrates proactive post intelligence:
1. research_pending_contacts() — deep research per contact before outreach
2. backfill_post_analysis() — analyze posts that were collected without analysis
3. detect_viral_posts() — flag posts with explosive engagement growth
4. auto_tune_watchlists() — disable low-ROI keywords, surface recommendations
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from statistics import mean
from typing import Any

from ..db.async_bridge import run_db
from ..constants import (
    AUTHOR_PATTERN_CONFIDENCE_BOOST,
    AUTHOR_PATTERN_MIN_POSTS,
    AUTHOR_PATTERN_WINDOW_DAYS,
    DISTRIBUTED_POST_LIMIT,
    DISTRIBUTED_SCAN_CONCURRENCY,
    POST_ANALYSIS_BATCH_SIZE,
    RESEARCH_BATCH_SIZE,
    RESEARCH_POST_LIMIT,
    SIGNAL_PROSPECT_POST,
    SIGNAL_TTL_PROSPECT_POST,
    SIGNAL_TTL_VIRAL_POST,
    SIGNAL_VIRAL_POST,
    VIRAL_GROWTH_RATE_THRESHOLD,
    VIRAL_MIN_ABSOLUTE_ENGAGEMENT,
    WATCHLIST_LOW_ROI_THRESHOLD,
    WATCHLIST_TUNE_MIN_DAYS,
    WATCHLIST_TUNE_MIN_SIGNALS,
)
from ..db.post_queries import (
    get_contacts_pending_research,
    get_post_analyses_by_author,
    reclaim_stale_research,
    get_posts_with_metric_growth,
    get_recent_topics_by_author,
    get_unanalyzed_posts,
    update_contact_research,
    update_post_analysis,
    upsert_post,
)
from ..db.signal_queries import (
    list_watchlists,
    save_signal,
    signal_exists,
    update_watchlist,
    upsert_signal_account,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 1. Contact Research Pipeline
# ──────────────────────────────────────────────


async def research_pending_contacts() -> str:
    """Research contacts that haven't been deeply analyzed yet.

    Distributes research across pool accounts when available.
    Each contact gets: post fetch → analysis → pattern detection → summary.

    Returns:
        Summary string of research results.
    """
    import asyncio

    from ..linkedin import get_account_id, get_linkedin_client

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected — skipping research."

    await run_db(reclaim_stale_research)
    contacts = await run_db(get_contacts_pending_research, limit=RESEARCH_BATCH_SIZE)
    if not contacts:
        return "No contacts pending research."

    client = get_linkedin_client()
    start_ms = int(time.time() * 1000)

    try:
        # Try to get pool accounts for distribution
        accounts = await _get_healthy_accounts(client)
        if not accounts:
            accounts = [{"account_id": account_id}]

        # Distribute contacts across accounts
        sem = asyncio.Semaphore(DISTRIBUTED_SCAN_CONCURRENCY)
        tasks = []
        for i, contact in enumerate(contacts):
            acct = accounts[i % len(accounts)]
            tasks.append(_research_single_contact(
                client, sem, acct["account_id"], contact,
            ))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        completed = sum(1 for r in results if isinstance(r, dict))
        errors = sum(1 for r in results if isinstance(r, Exception))
        total_posts = sum(
            r.get("posts", 0) for r in results if isinstance(r, dict)
        )

        duration_ms = int(time.time() * 1000) - start_ms
        summary = (
            f"Research: {completed}/{len(contacts)} contacts researched, "
            f"{total_posts} posts analyzed"
        )
        if errors:
            summary += f", {errors} errors"
        summary += f" ({duration_ms}ms)"

        logger.info(summary)
        return summary

    finally:
        await client.close()


async def _research_single_contact(
    client: Any,
    sem: "asyncio.Semaphore",
    account_id: str,
    contact: dict,
) -> dict:
    """Deep research a single contact — fetch posts, analyze, build summary."""
    import asyncio

    async with sem:
        contact_id = contact["id"]
        linkedin_id = contact["linkedin_id"]
        contact_name = contact.get("name", "Unknown")
        contact_title = contact.get("title", "")

        # Mark as in-progress
        await run_db(update_contact_research, contact_id, "in_progress")

        finished = False
        try:
            # 1. Fetch their recent posts
            posts = await client.get_user_posts(
                account_id, linkedin_id, limit=RESEARCH_POST_LIMIT,
            )

            # Handle error sentinel
            if posts and isinstance(posts[0], dict) and "_status_code" in posts[0]:
                await run_db(update_contact_research, contact_id, "failed")
                finished = True
                return {"posts": 0, "error": "api_error"}

            if not posts:
                # No posts = still complete, just nothing to analyze
                summary = json.dumps({
                    "total_posts_analyzed": 0,
                    "posting_frequency": "silent",
                    "top_topics": [],
                    "avg_engagement": {"likes": 0, "comments": 0},
                    "engagement_level": "none",
                    "pain_points": [],
                    "buying_signals": [],
                    "content_style": "silent",
                })
                await run_db(update_contact_research, contact_id, "complete", summary)
                finished = True
                return {"posts": 0}

            posts_analyzed = 0

            # 2. Store and analyze each post
            for post in posts:
                post_id = post.get("id", "")
                post_text = post.get("text", "")
                if not post_id or not post_text:
                    continue

                metrics = post.get("metrics", {})
                await run_db(upsert_post,
                    post_id,
                    author_linkedin_id=linkedin_id,
                    author_name=contact_name,
                    text=post_text[:2000],
                    metrics_json=json.dumps(metrics),
                    source="research",
                )

                # Inline analysis
                try:
                    analysis = await _analyze_post(post_text)
                    if analysis:
                        await run_db(update_post_analysis,
                            post_id,
                            analysis.get("topic", "unknown"),
                            json.dumps(analysis),
                        )
                        posts_analyzed += 1
                except Exception:
                    pass

            # 3. Build research summary from all analyzed data
            topics = await run_db(get_recent_topics_by_author, linkedin_id, days=30)
            all_analyses = await run_db(get_post_analyses_by_author, linkedin_id, limit=30)

            all_pain_points: list[str] = []
            all_buying_signals: list[str] = []
            for a in all_analyses:
                all_pain_points.extend(a.get("pain_points", []))
                all_buying_signals.extend(a.get("buying_signals", []))

            # Engagement stats
            post_metrics = []
            for p in posts:
                m = p.get("metrics", {})
                likes = int(m.get("likes", 0) or m.get("numLikes", 0) or 0)
                comments = int(m.get("comments", 0) or m.get("numComments", 0) or 0)
                post_metrics.append({"likes": likes, "comments": comments})

            avg_likes = mean([m["likes"] for m in post_metrics]) if post_metrics else 0
            avg_comments = mean([m["comments"] for m in post_metrics]) if post_metrics else 0

            # Classify patterns
            posting_freq = _classify_frequency(len(posts))
            engagement_level = _classify_engagement(avg_likes, avg_comments)

            # Top posts by engagement
            top_post_ids = sorted(
                [(p.get("id", ""), sum(int(p.get("metrics", {}).get(k, 0) or 0) for k in ("likes", "comments")))
                 for p in posts if p.get("id")],
                key=lambda x: -x[1],
            )[:3]

            summary_data = {
                "total_posts_analyzed": posts_analyzed,
                "posting_frequency": posting_freq,
                "top_topics": list(topics.keys())[:5],
                "repeated_topics": {t: c for t, c in topics.items() if c >= 2},
                "avg_engagement": {"likes": round(avg_likes, 1), "comments": round(avg_comments, 1)},
                "engagement_level": engagement_level,
                "pain_points": list(set(all_pain_points))[:10],
                "buying_signals": list(set(all_buying_signals))[:10],
                "top_post_ids": [pid for pid, _ in top_post_ids],
                "content_style": _classify_style(all_analyses),
            }

            await run_db(update_contact_research,
                contact_id, "complete", json.dumps(summary_data),
            )

            finished = True
            return {"posts": posts_analyzed}

        except Exception as e:
            logger.warning("Research failed for %s: %s", contact_name, e)
            await run_db(update_contact_research, contact_id, "failed")
            finished = True
            raise
        finally:
            if not finished:
                try:
                    await asyncio.shield(
                        run_db(update_contact_research, contact_id, "pending")
                    )
                except Exception:
                    logger.warning(
                        "Could not reclaim research for %s", contact_id, exc_info=True
                    )


# ──────────────────────────────────────────────
# 2. Backfill Post Analysis
# ──────────────────────────────────────────────


async def backfill_post_analysis() -> str:
    """Analyze posts that were collected without analysis (topic IS NULL).

    Processes in batches to avoid overwhelming the LLM.

    Returns:
        Summary string of backfill results.
    """
    posts = await run_db(get_unanalyzed_posts, limit=POST_ANALYSIS_BATCH_SIZE * 4)
    if not posts:
        return "No unanalyzed posts to backfill."

    analyzed = 0
    errors = 0

    for post in posts:
        text = post.get("text", "")
        post_id = post.get("post_id", "")
        if not text or not post_id:
            continue

        try:
            result = await _analyze_post(text)
            if result:
                await run_db(update_post_analysis,
                    post_id,
                    result.get("topic", "unknown"),
                    json.dumps(result),
                )
                analyzed += 1
        except Exception as e:
            logger.debug("Backfill analysis failed for post %s: %s", post_id[:8], e)
            errors += 1

    summary = f"Backfill: {analyzed}/{len(posts)} posts analyzed"
    if errors:
        summary += f", {errors} errors"
    logger.info(summary)
    return summary


# ──────────────────────────────────────────────
# 3. Viral Post Detection
# ──────────────────────────────────────────────


async def detect_viral_posts() -> str:
    """Detect posts with explosive engagement growth from metric snapshots.

    Compares earliest and latest snapshots. Posts with 3x+ growth AND
    20+ total engagement are flagged as viral signals.

    Returns:
        Summary string of detection results.
    """
    viral_candidates = await run_db(get_posts_with_metric_growth,
        min_snapshots=2,
        hours=48,
        min_engagement=VIRAL_MIN_ABSOLUTE_ENGAGEMENT,
        min_growth_rate=VIRAL_GROWTH_RATE_THRESHOLD,
    )

    if not viral_candidates:
        return "Viral detection: no viral posts found."

    created = 0
    now = int(time.time())

    for candidate in viral_candidates:
        post_id = candidate["post_id"]

        # Skip if already flagged
        if await run_db(signal_exists, SIGNAL_VIRAL_POST, post_id=post_id):
            continue

        # Look up the post to get author info
        from ..db.post_queries import get_post
        post = await run_db(get_post, post_id)
        if not post:
            continue

        linkedin_id = post.get("author_linkedin_id", "")
        author_name = post.get("author_name", "Unknown")

        # Look up campaign association
        campaign_id = await run_db(_find_campaign_for_linkedin_id, linkedin_id)

        expires_at = now + SIGNAL_TTL_VIRAL_POST
        await run_db(save_signal,
            signal_type=SIGNAL_VIRAL_POST,
            source="viral_detection",
            prospect_id=None,
            prospect_name=author_name,
            prospect_title="",
            linkedin_id=linkedin_id,
            campaign_id=campaign_id,
            content=post.get("text", "")[:500],
            post_id=post_id,
            metadata_json=json.dumps({
                "growth_rate": candidate["growth_rate"],
                "early_engagement": candidate["early_engagement"],
                "late_engagement": candidate["late_engagement"],
                "earliest_at": candidate["earliest_at"],
                "latest_at": candidate["latest_at"],
            }),
            expires_at=expires_at,
        )

        if linkedin_id:
            await run_db(upsert_signal_account,
                linkedin_id=linkedin_id,
                prospect_name=author_name,
                company="",
            )

        created += 1

    summary = f"Viral detection: {len(viral_candidates)} candidates, {created} new signals created."
    logger.info(summary)
    return summary


# ──────────────────────────────────────────────
# 4. Keyword Watchlist Auto-Tuning
# ──────────────────────────────────────────────


async def auto_tune_watchlists() -> str:
    """Evaluate keyword watchlists and disable low-ROI keywords.

    A keyword is low-ROI if it generated WATCHLIST_TUNE_MIN_SIGNALS signals
    but 0% were actioned (i.e., pure noise).

    Returns:
        Summary of tuning actions.
    """
    from ..db.schema import get_db

    watchlists = await run_db(list_watchlists, is_active=True, watch_type="keyword")
    if not watchlists:
        return "No active keyword watchlists to tune."

    now = int(time.time())
    min_age = WATCHLIST_TUNE_MIN_DAYS * 86400

    tuned = 0
    keywords_disabled = 0

    for wl in watchlists:
        wl_id = wl["id"]
        created_at = wl.get("created_at", 0) or 0

        # Skip if too young
        if (now - created_at) < min_age:
            continue

        keywords_list = wl.get("keywords_list", [])
        if not keywords_list:
            continue

        # Get signal stats per keyword via watchlist_id
        def _get_watchlist_stats(wid):
            db = get_db()
            total_s = db.execute(
                "SELECT COUNT(*) as cnt FROM signals WHERE watchlist_id = ?",
                (wid,),
            ).fetchone()
            actioned_s = db.execute(
                "SELECT COUNT(*) as cnt FROM signals WHERE watchlist_id = ? AND status = 'actioned'",
                (wid,),
            ).fetchone()
            db.close()
            return total_s, actioned_s

        total_signals, actioned_signals = await run_db(_get_watchlist_stats, wl_id)

        total = total_signals["cnt"] if total_signals else 0
        actioned = actioned_signals["cnt"] if actioned_signals else 0

        if total < WATCHLIST_TUNE_MIN_SIGNALS:
            continue

        roi = actioned / total if total > 0 else 0

        if roi <= WATCHLIST_LOW_ROI_THRESHOLD:
            # This watchlist generated only noise — mark keywords as disabled
            existing_disabled = []
            try:
                existing_disabled = json.loads(wl.get("disabled_keywords", "[]") or "[]")
            except (json.JSONDecodeError, TypeError):
                pass

            # Disable all keywords in this watchlist (don't delete — allow manual re-enable)
            newly_disabled = [k for k in keywords_list if k not in existing_disabled]
            if newly_disabled:
                all_disabled = existing_disabled + newly_disabled
                await run_db(update_watchlist,
                    wl_id,
                    disabled_keywords=json.dumps(all_disabled),
                    auto_tuned_at=now,
                )
                keywords_disabled += len(newly_disabled)
                tuned += 1
                logger.info(
                    "Auto-tuned watchlist %s: disabled %d keywords (0/%d actioned)",
                    wl_id[:8], len(newly_disabled), total,
                )

    summary = (
        f"Watchlist tuning: {tuned} watchlists tuned, "
        f"{keywords_disabled} keywords disabled."
    )
    logger.info(summary)
    return summary


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


async def _get_healthy_accounts(client: Any) -> list[dict]:
    """Get healthy pool accounts via network pool status."""
    try:
        status = await client.network_pool_status()
        if not status or "accounts" not in status:
            return []
        return [
            a for a in status["accounts"]
            if a.get("health_status") == "healthy" and a.get("account_id")
        ]
    except Exception:
        return []


async def _analyze_post(text: str) -> dict | None:
    """Run post analysis. Returns analysis dict or None on failure."""
    if not text or len(text) < 20:
        return None

    try:
        from ..ai.post_analyzer import analyze_post
        result = await analyze_post(text)
        if result and hasattr(result, "topic"):
            return {
                "topic": result.topic,
                "subtopics": getattr(result, "subtopics", []),
                "sentiment": getattr(result, "sentiment", "neutral"),
                "key_themes": getattr(result, "key_themes", []),
                "pain_points": getattr(result, "pain_points", []),
                "buying_signals": getattr(result, "buying_signals", []),
                "engagement_hook": getattr(result, "engagement_hook", ""),
            }
        if isinstance(result, dict):
            return result
    except Exception:
        pass
    return None


def _classify_frequency(post_count: int) -> str:
    """Classify posting frequency from recent post count."""
    if post_count >= 15:
        return "very_active"
    if post_count >= 8:
        return "active"
    if post_count >= 3:
        return "moderate"
    if post_count >= 1:
        return "rare"
    return "silent"


def _classify_engagement(avg_likes: float, avg_comments: float) -> str:
    """Classify engagement level from average metrics."""
    total = avg_likes + avg_comments
    if total >= 50:
        return "high"
    if total >= 15:
        return "medium"
    if total >= 3:
        return "low"
    return "none"


def _classify_style(analyses: list[dict]) -> str:
    """Classify content style from post analyses."""
    if not analyses:
        return "unknown"

    topics = [a.get("topic", "") for a in analyses if a.get("topic")]
    sentiments = [a.get("sentiment", "") for a in analyses if a.get("sentiment")]

    # Check for thought leadership indicators
    thought_leader_topics = {"leadership", "strategy", "innovation", "opinion", "advice"}
    industry_topics = {"industry_news", "market_update", "news", "trends"}
    personal_topics = {"personal", "story", "career", "reflection"}
    promotional_topics = {"product", "launch", "promotion", "sales", "marketing"}

    topic_set = set(t.lower() for t in topics)

    if topic_set & thought_leader_topics:
        return "thought_leader"
    if topic_set & industry_topics:
        return "industry_news"
    if topic_set & personal_topics:
        return "personal"
    if topic_set & promotional_topics:
        return "promotional"
    return "mixed"


def _find_campaign_for_linkedin_id(linkedin_id: str) -> str | None:
    """Find the campaign a contact belongs to."""
    if not linkedin_id:
        return None

    from ..db.schema import get_db

    db = get_db()
    row = db.execute(
        """SELECT campaign_id FROM contacts
           WHERE linkedin_id = ? AND campaign_id IS NOT NULL
           LIMIT 1""",
        (linkedin_id,),
    ).fetchone()
    db.close()
    return row["campaign_id"] if row else None
