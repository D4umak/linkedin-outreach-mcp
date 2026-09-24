"""Distributed multi-account post collector.

Uses all available pool accounts as parallel data collection servers to
maximize post intelligence throughput. Falls back to single-account
collection when the pool is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any

from ..constants import (
    DISTRIBUTED_POST_LIMIT,
    DISTRIBUTED_POST_LOOKBACK_DAYS,
    DISTRIBUTED_RESCAN_HOURS,
    DISTRIBUTED_SCAN_BATCH_PER_ACCOUNT,
    DISTRIBUTED_SCAN_CONCURRENCY,
    METRIC_SNAPSHOT_MIN_INTERVAL,
    SIGNAL_PROSPECT_POST,
    SIGNAL_TTL_PROSPECT_POST,
)
from ..db.async_bridge import run_db
from ..db.post_queries import upsert_post, update_post_analysis
from ..db.signal_queries import save_signal, signal_exists, upsert_signal_account
from ..linkedin.circuit_breaker import CircuitBreakerOpen, CollectorCircuitBreaker
from ..linkedin.unipile import detect_reshare
from ..services.post_freshness import post_item_published_at

logger = logging.getLogger(__name__)

_cb = CollectorCircuitBreaker("distributed_posts")


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────


async def collect_posts_distributed() -> str:
    """Distribute post collection across all healthy pool accounts.

    Falls back to single-account collection when the pool is unavailable.

    Returns:
        Summary string of results.
    """
    from ..linkedin import get_account_id, get_linkedin_client

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected — skipping distributed post scan."

    client = get_linkedin_client()
    start_ms = int(time.time() * 1000)

    try:
        # Try to get pool accounts
        accounts = await _get_healthy_accounts(client)

        if not accounts:
            # Fallback: single-account collection
            accounts = [{"account_id": account_id, "account_name": "primary"}]

        # Get all scannable contacts across all campaigns
        scannable = await run_db(
            _get_scannable_contacts,
            rescan_hours=DISTRIBUTED_RESCAN_HOURS,
            limit=len(accounts) * DISTRIBUTED_SCAN_BATCH_PER_ACCOUNT,
        )

        if not scannable:
            return "No contacts due for scanning — skipping."

        # Partition contacts across accounts (round-robin)
        account_batches = _partition_contacts(scannable, accounts)

        # Run in parallel with concurrency limit
        sem = asyncio.Semaphore(DISTRIBUTED_SCAN_CONCURRENCY)
        tasks = [
            _scan_batch(client, sem, aid, contacts)
            for aid, contacts in account_batches.items()
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Aggregate results
        total_posts = 0
        total_signals = 0
        total_scanned = 0
        total_analyzed = 0
        errors = 0
        for r in results:
            if isinstance(r, Exception):
                errors += 1
                logger.warning("Distributed scan batch error: %s", r)
            elif isinstance(r, dict):
                total_posts += r.get("posts", 0)
                total_signals += r.get("signals", 0)
                total_scanned += r.get("scanned", 0)
                total_analyzed += r.get("analyzed", 0)
                errors += r.get("errors", 0)

        duration_ms = int(time.time() * 1000) - start_ms

        # Save collection metrics
        await run_db(
            _save_collection_metrics,
            collector_type="distributed_scan",
            account_id=None,
            contacts_scanned=total_scanned,
            posts_collected=total_posts,
            posts_analyzed=total_analyzed,
            signals_created=total_signals,
            errors=errors,
            duration_ms=duration_ms,
        )

        summary = (
            f"Distributed post scan: {total_scanned} contacts across "
            f"{len(accounts)} accounts, {total_posts} posts collected, "
            f"{total_signals} signals, {total_analyzed} analyzed"
        )
        if errors:
            summary += f", {errors} errors"
        summary += f" ({duration_ms}ms)"

        logger.info(summary)
        return summary

    finally:
        await client.close()


# ──────────────────────────────────────────────
# Internals
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
    except Exception as e:
        logger.debug("Pool status unavailable (expected for non-pool users): %s", e)
        return []


def _get_scannable_contacts(
    rescan_hours: int,
    limit: int,
) -> list[dict]:
    """Get contacts due for post scanning across all campaigns.

    Priority: active outreach first, then by oldest scan time.
    """
    from ..db.queries import get_db

    now = int(time.time())
    scan_cutoff = now - (rescan_hours * 3600)

    db = get_db()
    rows = db.execute(
        """SELECT DISTINCT c.id, c.linkedin_id, c.name, c.title, c.company,
                  c.campaign_id, c.last_scanned_at,
                  COALESCE(o.status, 'pending') as outreach_status
           FROM contacts c
           LEFT JOIN outreaches o ON o.contact_id = c.id
           JOIN campaigns camp ON camp.id = c.campaign_id AND camp.status = 'active'
           WHERE c.linkedin_id IS NOT NULL AND c.linkedin_id != ''
             AND (c.last_scanned_at IS NULL OR c.last_scanned_at < ?)
           ORDER BY
             CASE WHEN o.status IN ('invited', 'connected', 'messaged') THEN 0 ELSE 1 END,
             COALESCE(c.last_scanned_at, 0) ASC
           LIMIT ?""",
        (scan_cutoff, limit),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def _partition_contacts(
    contacts: list[dict],
    accounts: list[dict],
) -> dict[str, list[dict]]:
    """Round-robin assign contacts to accounts."""
    batches: dict[str, list[dict]] = {
        a["account_id"]: [] for a in accounts
    }
    account_ids = [a["account_id"] for a in accounts]

    for i, contact in enumerate(contacts):
        aid = account_ids[i % len(account_ids)]
        if len(batches[aid]) < DISTRIBUTED_SCAN_BATCH_PER_ACCOUNT:
            batches[aid].append(contact)

    return {k: v for k, v in batches.items() if v}


async def _scan_batch(
    client: Any,
    sem: asyncio.Semaphore,
    account_id: str,
    contacts: list[dict],
) -> dict[str, int]:
    """Scan a batch of contacts using a specific account."""
    async with sem:
        now = int(time.time())
        lookback_cutoff = now - (DISTRIBUTED_POST_LOOKBACK_DAYS * 86400)
        posts_count = 0
        signals_count = 0
        scanned = 0
        analyzed = 0
        errors = 0

        for contact in contacts:
            linkedin_id = contact["linkedin_id"]
            contact_id = contact.get("id", "")
            contact_name = contact.get("name", "Unknown")
            contact_title = contact.get("title", "")
            campaign_id = contact.get("campaign_id", "")

            try:
                posts = await _cb.call(
                    client.get_user_posts(
                        account_id, linkedin_id, limit=DISTRIBUTED_POST_LIMIT,
                    ),
                    label=f"get_user_posts({contact_name})",
                )

                # Detect error sentinel
                if posts and isinstance(posts[0], dict) and "_status_code" in posts[0]:
                    await run_db(_update_last_scanned, contact_id, now)
                    scanned += 1
                    continue

                if not posts:
                    await run_db(_update_last_scanned, contact_id, now)
                    scanned += 1
                    continue

                for post in posts:
                    post_id = post.get("id", "")
                    post_text = post.get("text", "")
                    post_date = post.get("date", "")

                    if not post_text or not post_id:
                        continue

                    # Skip posts older than the lookback window, and posts
                    # with no knowable publication time. The second used to
                    # pass as fresh: this module's own date parser read "3mo"
                    # (Unipile's usual `date`) as None, and None cleared an
                    # `if post_ts and post_ts < cutoff` test. The id dates the
                    # post exactly when the string cannot.
                    post_ts = post_item_published_at(post_id, post_date, now)
                    if post_ts is None or post_ts < lookback_cutoff:
                        continue

                    # Deduplicate
                    if await run_db(signal_exists, SIGNAL_PROSPECT_POST, post_id=post_id):
                        # Still save metric snapshot for time-series
                        await run_db(_save_metric_snapshot, post_id, post.get("metrics", {}))
                        continue

                    # Extract expanded post data
                    metrics = post.get("metrics", {})
                    expanded = _extract_expanded_data(post_text, post, metrics)

                    # Persist post + author
                    await run_db(
                        upsert_post,
                        post_id,
                        author_linkedin_id=linkedin_id,
                        author_name=contact_name,
                        text=post_text[:2000],
                        metrics_json=json.dumps(metrics),
                        source="prospect_scan",
                        **expanded,
                    )
                    posts_count += 1

                    # Save metric snapshot
                    await run_db(_save_metric_snapshot, post_id, metrics)

                    # Inline post analysis
                    try:
                        analysis_result = await _analyze_post_inline(post_text)
                        if analysis_result:
                            await run_db(
                                update_post_analysis,
                                post_id,
                                analysis_result.get("topic", "unknown"),
                                json.dumps(analysis_result),
                            )
                            analyzed += 1
                    except Exception:
                        pass  # Analysis failure doesn't block signal creation

                    # Save signal
                    expires_at = now + SIGNAL_TTL_PROSPECT_POST
                    await run_db(
                        save_signal,
                        signal_type=SIGNAL_PROSPECT_POST,
                        source="prospect_scan",
                        prospect_id=contact_id,
                        prospect_name=contact_name,
                        prospect_title=contact_title,
                        linkedin_id=linkedin_id,
                        campaign_id=campaign_id,
                        content=post_text[:2000],
                        post_id=post_id,
                        metadata_json=json.dumps({
                            "published_at": post_ts,
                            "post_date": post_date,
                            "metrics": metrics,
                            "contact_company": contact.get("company", ""),
                            "hashtags": expanded.get("hashtags", "[]"),
                            "media_type": expanded.get("media_type", "text"),
                        }),
                        expires_at=expires_at,
                    )
                    signals_count += 1

                    await run_db(
                        upsert_signal_account,
                        linkedin_id=linkedin_id,
                        prospect_name=contact_name,
                        company=contact.get("company", ""),
                    )

                await run_db(_update_last_scanned, contact_id, now)
                scanned += 1

            except CircuitBreakerOpen as e:
                logger.warning("Skipping remaining contacts in batch: %s", e)
                break
            except Exception as e:
                logger.warning(
                    "Error scanning posts for %s via account %s: %s",
                    contact_name, account_id[:8], e,
                )
                errors += 1

        return {
            "posts": posts_count,
            "signals": signals_count,
            "scanned": scanned,
            "analyzed": analyzed,
            "errors": errors,
        }


def _extract_expanded_data(
    text: str,
    post: dict,
    metrics: dict,
) -> dict[str, Any]:
    """Extract expanded post metadata (hashtags, media type, etc.)."""
    result: dict[str, Any] = {}

    # Hashtags
    hashtags = re.findall(r"#(\w+)", text) if text else []
    result["hashtags"] = json.dumps(hashtags[:20])

    # Media type detection
    media_type = "text"
    if post.get("image") or post.get("images"):
        media_type = "image"
    elif post.get("video") or post.get("video_url"):
        media_type = "video"
    elif post.get("article") or post.get("article_url"):
        media_type = "article"
    elif post.get("document") or post.get("document_url"):
        media_type = "document"
    elif post.get("poll"):
        media_type = "poll"
    result["media_type"] = media_type

    # Media URL
    media_url = (
        post.get("image_url")
        or post.get("video_url")
        or post.get("article_url")
        or post.get("document_url")
        or ""
    )
    if media_url:
        result["media_url"] = media_url

    # Repost detection — one spelling of the question, shared with the client.
    # This copy read the nested fields but not the explicit flag, and the
    # client's read the flag but not the nested fields, so between them a
    # reshare could arrive either way and be recorded as an original.
    is_repost, original = detect_reshare(post)
    result["is_repost"] = 1 if is_repost else 0
    if is_repost and original:
        result["original_post_id"] = original

    # Reposts count
    reposts = metrics.get("reposts", 0) or metrics.get("numShares", 0) or metrics.get("shares", 0)
    result["reposts_count"] = int(reposts) if reposts else 0

    return result


def _save_metric_snapshot(post_id: str, metrics: dict) -> None:
    """Save a post metric snapshot for time-series tracking.

    Skips if a snapshot already exists within METRIC_SNAPSHOT_MIN_INTERVAL.
    """
    if not metrics:
        return

    from ..db.schema import get_db

    now = int(time.time())
    cutoff = now - METRIC_SNAPSHOT_MIN_INTERVAL

    db = get_db()
    # Check recent snapshot exists
    recent = db.execute(
        "SELECT 1 FROM post_metric_snapshots WHERE post_id = ? AND snapshot_at > ? LIMIT 1",
        (post_id, cutoff),
    ).fetchone()

    if recent:
        db.close()
        return

    snap_id = uuid.uuid4().hex[:12]
    likes = int(metrics.get("likes", 0) or metrics.get("numLikes", 0) or metrics.get("reactions_count", 0) or 0)
    comments = int(metrics.get("comments", 0) or metrics.get("numComments", 0) or metrics.get("comments_count", 0) or 0)
    reposts = int(metrics.get("reposts", 0) or metrics.get("numShares", 0) or metrics.get("shares", 0) or 0)

    db.execute(
        """INSERT INTO post_metric_snapshots (id, post_id, likes, comments, reposts, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (snap_id, post_id, likes, comments, reposts, now),
    )
    db.commit()
    db.close()


async def _analyze_post_inline(text: str) -> dict | None:
    """Run post analysis inline during collection.

    Uses fast rules first, falls back to LLM only for ambiguous content.
    Returns analysis dict or None on failure.
    """
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
    except Exception as e:
        logger.debug("Inline post analysis failed: %s", e)

    return None


def _update_last_scanned(contact_id: str, timestamp: int) -> None:
    """Update the contact's last_scanned_at timestamp."""
    if not contact_id:
        return
    from ..db.queries import get_db

    db = get_db()
    try:
        db.execute(
            "UPDATE contacts SET last_scanned_at = ? WHERE id = ?",
            (timestamp, contact_id),
        )
        db.commit()
    except Exception as e:
        logger.warning("Failed to update last_scanned_at for %s: %s", contact_id, e)
    finally:
        db.close()


def _save_collection_metrics(
    collector_type: str,
    account_id: str | None,
    contacts_scanned: int,
    posts_collected: int,
    posts_analyzed: int,
    signals_created: int,
    errors: int,
    duration_ms: int,
) -> None:
    """Save collection run metrics for observability."""
    from ..db.schema import get_db

    row_id = uuid.uuid4().hex[:12]
    now = int(time.time())

    db = get_db()
    try:
        db.execute(
            """INSERT INTO collection_metrics
               (id, collector_type, account_id, contacts_scanned, posts_collected,
                posts_analyzed, signals_created, errors, duration_ms, run_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row_id, collector_type, account_id, contacts_scanned, posts_collected,
             posts_analyzed, signals_created, errors, duration_ms, now),
        )
        db.commit()
    except Exception as e:
        logger.warning("Failed to save collection metrics: %s", e)
    finally:
        db.close()

