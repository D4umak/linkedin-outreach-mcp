"""SQLite CRUD helpers for posts and post authors."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

from .schema import get_db


# ──────────────────────────────────────────────
# Posts
# ──────────────────────────────────────────────

def _carries_metrics(metrics_json: str | None) -> bool:
    """Whether a metrics payload actually says anything.

    Guarding on the literals ''/'{}'/'[]'/'null' was not enough: the collectors
    build dicts with every counter present and zero
    (``{"reactions_count": 0, "comments_count": 0, ...}``) when the API omits
    them, and those sailed through and still erased real counts.

    All-falsy means "no data", not "engagement dropped to zero" — LinkedIn
    counters do not go back down, so a post genuinely at zero loses nothing by
    keeping the zero already stored.
    """
    if not metrics_json:
        return False
    text = metrics_json.strip()
    if text in ("{}", "[]", "null"):
        return False
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        # Unparseable but non-empty: preserve the old conservative behaviour of
        # writing it rather than silently dropping something we cannot read.
        return True
    if isinstance(parsed, dict):
        return any(bool(v) for v in parsed.values())
    return bool(parsed)


def upsert_post(
    post_id: str,
    *,
    author_linkedin_id: str = "",
    author_name: str = "",
    text: str = "",
    metrics_json: str = "",
    source: str = "search",
    # Expanded v2 columns
    hashtags: str = "",
    media_type: str = "",
    media_url: str = "",
    language: str = "",
    visibility: str = "",
    engagement_rate: float | None = None,
    author_followers: int | None = None,
    reactions_breakdown: str = "",
    reposts_count: int = 0,
    is_repost: int = 0,
    original_post_id: str = "",
    # Phase 5 columns
    impressions_count: int = 0,
) -> str:
    """Insert or update a post. Returns the internal row ID.

    If the post_id already exists, updates last_seen_at and metrics_json.
    Also upserts the post author if author_linkedin_id is provided.
    """
    now = int(time.time())
    db = get_db()

    row = db.execute(
        "SELECT id FROM posts WHERE post_id = ?", (post_id,)
    ).fetchone()

    is_new_post = row is None

    if row:
        # Update existing — refresh metrics, last_seen, and expanded fields if provided
        updates = ["last_seen_at = ?"]
        params_list: list[Any] = [now]
        # metrics_json used to be the ONE unconditional field here while every
        # neighbour below is guarded by `if col_val:`. A re-sight that carried
        # no metrics therefore erased real ones: engage_prospect's search_posts
        # fallback builds "metrics": {} -> '', and the contacts/connection-sync
        # enrichment passes the literal '{}' when the API omits counters. A row
        # recorded with 240 reactions became NULL, and LinkedIn will not serve
        # those historical counts again. The asymmetry was the tell — the
        # guarded columns survived the same re-sight.
        if _carries_metrics(metrics_json):
            updates.append("metrics_json = ?")
            params_list.append(metrics_json)
        # Conditionally update expanded v2 fields if new values are provided
        for col_name, col_val in [
            ("hashtags", hashtags), ("media_type", media_type),
            ("media_url", media_url), ("language", language),
            ("visibility", visibility), ("reactions_breakdown", reactions_breakdown),
        ]:
            if col_val:
                updates.append(f"{col_name} = ?")
                params_list.append(col_val)
        if engagement_rate is not None:
            updates.append("engagement_rate = ?")
            params_list.append(engagement_rate)
        if author_followers is not None:
            updates.append("author_followers = ?")
            params_list.append(author_followers)
        if reposts_count:
            updates.append("reposts_count = ?")
            params_list.append(reposts_count)
        if is_repost:
            updates.append("is_repost = ?")
            params_list.append(is_repost)
        if original_post_id:
            updates.append("original_post_id = ?")
            params_list.append(original_post_id)
        if impressions_count:
            updates.append("impressions_count = ?")
            params_list.append(impressions_count)
        params_list.append(post_id)
        db.execute(
            f"UPDATE posts SET {', '.join(updates)} WHERE post_id = ?",
            params_list,
        )
        db.commit()
        db.close()
        row_id = row["id"]
    else:
        # Insert new with all expanded columns
        row_id = uuid.uuid4().hex[:12]
        db.execute(
            """INSERT INTO posts
               (id, post_id, author_linkedin_id, author_name, text, metrics_json,
                source, first_seen_at, last_seen_at,
                hashtags, media_type, media_url, language, visibility,
                engagement_rate, author_followers, reactions_breakdown,
                reposts_count, is_repost, original_post_id, impressions_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row_id, post_id, author_linkedin_id or None, author_name or None,
             text[:2000] if text else None, metrics_json or None,
             source, now, now,
             hashtags or None, media_type or None, media_url or None,
             language or None, visibility or None,
             engagement_rate, author_followers,
             reactions_breakdown or None,
             reposts_count, is_repost, original_post_id or None,
             impressions_count),
        )
        db.commit()
        db.close()

    # Upsert author if we have an ID.
    #
    # count_post is the INSERT branch only. This ran on both, and
    # upsert_post_author's update does `posts_seen = posts_seen + 1`, so one
    # post seen twice — two watchlist keywords in a single run, a second
    # collector, or the same trending post tomorrow — counted as two posts.
    # campaign_refill_service gates on `posts_seen >= 2` under the comment
    # "Only active authors", and that gate is what puts someone into the
    # outreach pool, so a one-time poster was sourced as a regular one.
    if author_linkedin_id:
        upsert_post_author(
            linkedin_id=author_linkedin_id,
            name=author_name,
            count_post=is_new_post,
        )

    return row_id


def get_post(post_id: str) -> Optional[dict]:
    """Get a post by LinkedIn post URN."""
    db = get_db()
    row = db.execute("SELECT * FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    db.close()
    return dict(row) if row else None


def list_posts(
    *,
    author_linkedin_id: str | None = None,
    topic: str | None = None,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """List posts with optional filters."""
    conditions: list[str] = []
    params: list[Any] = []

    if author_linkedin_id:
        conditions.append("author_linkedin_id = ?")
        params.append(author_linkedin_id)
    if topic:
        conditions.append("topic = ?")
        params.append(topic)
    if source:
        conditions.append("source = ?")
        params.append(source)

    where = " AND ".join(conditions) if conditions else "1=1"
    db = get_db()
    rows = db.execute(
        f"SELECT * FROM posts WHERE {where} ORDER BY last_seen_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_unanalyzed_posts(limit: int = 20) -> list[dict]:
    """Get posts that haven't been analyzed yet (topic IS NULL)."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM posts WHERE topic IS NULL AND text IS NOT NULL "
        "ORDER BY last_seen_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def update_post_analysis(post_id: str, topic: str, analysis_json: str) -> None:
    """Update a post's topic and analysis results."""
    db = get_db()
    db.execute(
        "UPDATE posts SET topic = ?, analysis_json = ? WHERE post_id = ?",
        (topic, analysis_json, post_id),
    )
    db.commit()
    db.close()


def is_post_known(post_id: str) -> bool:
    """Check if a post_id exists in the posts table."""
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM posts WHERE post_id = ? LIMIT 1", (post_id,)
    ).fetchone()
    db.close()
    return row is not None


def get_posts_by_author(author_linkedin_id: str, limit: int = 20) -> list[dict]:
    """Get posts by a specific author."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM posts WHERE author_linkedin_id = ? ORDER BY last_seen_at DESC LIMIT ?",
        (author_linkedin_id, limit),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────
# Post Authors
# ──────────────────────────────────────────────

def upsert_post_author(
    linkedin_id: str,
    *,
    name: str = "",
    headline: str = "",
    company: str = "",
    count_post: bool = True,
) -> None:
    """Insert or update a post author.

    ``count_post`` says whether this sighting is a NEW post by this author.
    posts_seen is meant to be "how many distinct posts have we seen", and the
    >=2 gate in campaign_refill_service treats it that way.
    """
    now = int(time.time())
    db = get_db()

    row = db.execute(
        "SELECT linkedin_id, posts_seen FROM post_authors WHERE linkedin_id = ?",
        (linkedin_id,),
    ).fetchone()

    if row:
        updates = ["last_post_at = ?"]
        params: list[Any] = [now]
        if count_post:
            updates.insert(0, "posts_seen = posts_seen + 1")
        if name:
            updates.append("name = ?")
            params.append(name)
        if headline:
            updates.append("headline = ?")
            params.append(headline)
        if company:
            updates.append("company = ?")
            params.append(company)
        params.append(linkedin_id)
        db.execute(
            f"UPDATE post_authors SET {', '.join(updates)} WHERE linkedin_id = ?",
            params,
        )
    else:
        db.execute(
            """INSERT INTO post_authors
               (linkedin_id, name, headline, company, posts_seen, first_seen_at, last_post_at)
               VALUES (?, ?, ?, ?, 1, ?, ?)""",
            (linkedin_id, name or None, headline or None, company or None, now, now),
        )

    db.commit()
    db.close()

    # Auto-link to contact if one exists with this linkedin_id
    _auto_link_contact(linkedin_id)


def _auto_link_contact(linkedin_id: str) -> None:
    """If a contact exists with this linkedin_id, link the author to it."""
    db = get_db()
    contact_row = db.execute(
        "SELECT id FROM contacts WHERE linkedin_id = ? LIMIT 1",
        (linkedin_id,),
    ).fetchone()
    if contact_row:
        db.execute(
            "UPDATE post_authors SET contact_id = ? WHERE linkedin_id = ? AND contact_id IS NULL",
            (contact_row["id"], linkedin_id),
        )
        db.commit()
    db.close()


def get_post_author(linkedin_id: str) -> Optional[dict]:
    """Get a post author by LinkedIn ID."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM post_authors WHERE linkedin_id = ?", (linkedin_id,)
    ).fetchone()
    db.close()
    return dict(row) if row else None


def list_post_authors(
    *,
    company: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """List post authors with optional filters."""
    conditions: list[str] = []
    params: list[Any] = []

    if company:
        conditions.append("company = ?")
        params.append(company)

    where = " AND ".join(conditions) if conditions else "1=1"
    db = get_db()
    rows = db.execute(
        f"SELECT * FROM post_authors WHERE {where} ORDER BY last_post_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def link_author_to_contact(linkedin_id: str, contact_id: str) -> None:
    """Manually link a post author to a contact."""
    db = get_db()
    db.execute(
        "UPDATE post_authors SET contact_id = ? WHERE linkedin_id = ?",
        (contact_id, linkedin_id),
    )
    db.commit()
    db.close()


def update_author_topics(linkedin_id: str, topics: list[str]) -> None:
    """Update the accumulated topics list for an author."""
    db = get_db()
    # Merge with existing topics
    row = db.execute(
        "SELECT topics_json FROM post_authors WHERE linkedin_id = ?",
        (linkedin_id,),
    ).fetchone()
    if row:
        existing = []
        try:
            existing = json.loads(row["topics_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            pass
        merged = list(dict.fromkeys(existing + topics))[:20]  # Dedupe, cap at 20
        db.execute(
            "UPDATE post_authors SET topics_json = ? WHERE linkedin_id = ?",
            (json.dumps(merged), linkedin_id),
        )
        db.commit()
    db.close()


def find_known_authors(linkedin_ids: list[str]) -> list[dict]:
    """Batch check which authors are already tracked."""
    if not linkedin_ids:
        return []
    placeholders = ",".join("?" for _ in linkedin_ids)
    db = get_db()
    rows = db.execute(
        f"SELECT * FROM post_authors WHERE linkedin_id IN ({placeholders})",
        linkedin_ids,
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────
# Post Metric Snapshots
# ──────────────────────────────────────────────

def get_metric_snapshots(post_id: str, limit: int = 20) -> list[dict]:
    """Get metric snapshots for a post, ordered by time."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM post_metric_snapshots WHERE post_id = ? ORDER BY snapshot_at ASC LIMIT ?",
        (post_id, limit),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_posts_with_metric_growth(
    min_snapshots: int = 2,
    hours: int = 48,
    min_engagement: int = 20,
    min_growth_rate: float = 3.0,
) -> list[dict]:
    """Find posts with significant engagement growth between snapshots.

    Returns posts where (latest_total / earliest_total) >= min_growth_rate
    and latest_total >= min_engagement.
    """
    cutoff = int(time.time()) - (hours * 3600)
    db = get_db()

    # Get posts with multiple snapshots in the window
    post_ids = db.execute(
        """SELECT post_id, COUNT(*) as cnt
           FROM post_metric_snapshots WHERE snapshot_at >= ?
           GROUP BY post_id HAVING COUNT(*) >= ?""",
        (cutoff, min_snapshots),
    ).fetchall()

    results = []
    for row in post_ids:
        pid = row["post_id"]
        earliest = db.execute(
            "SELECT * FROM post_metric_snapshots WHERE post_id = ? AND snapshot_at >= ? ORDER BY snapshot_at ASC LIMIT 1",
            (pid, cutoff),
        ).fetchone()
        latest = db.execute(
            "SELECT * FROM post_metric_snapshots WHERE post_id = ? ORDER BY snapshot_at DESC LIMIT 1",
            (pid,),
        ).fetchone()

        if not earliest or not latest:
            continue

        early_total = (earliest["likes"] or 0) + (earliest["comments"] or 0)
        late_total = (latest["likes"] or 0) + (latest["comments"] or 0)

        if early_total > 0 and late_total >= min_engagement:
            growth = late_total / early_total
            if growth >= min_growth_rate:
                results.append({
                    "post_id": pid,
                    "growth_rate": round(growth, 2),
                    "early_engagement": early_total,
                    "late_engagement": late_total,
                    "earliest_at": earliest["snapshot_at"],
                    "latest_at": latest["snapshot_at"],
                })

    db.close()
    return sorted(results, key=lambda x: -x["growth_rate"])


# ──────────────────────────────────────────────
# Collection Stats
# ──────────────────────────────────────────────

def get_post_collection_stats(days: int = 1) -> dict:
    """Get post collection statistics for the dashboard."""
    cutoff = int(time.time()) - (days * 86400)
    db = get_db()

    posts_row = db.execute(
        "SELECT COUNT(*) as cnt FROM posts WHERE first_seen_at >= ?", (cutoff,)
    ).fetchone()
    analyzed_row = db.execute(
        "SELECT COUNT(*) as cnt FROM posts WHERE first_seen_at >= ? AND topic IS NOT NULL",
        (cutoff,),
    ).fetchone()
    authors_row = db.execute(
        "SELECT COUNT(DISTINCT author_linkedin_id) as cnt FROM posts WHERE first_seen_at >= ?",
        (cutoff,),
    ).fetchone()
    total_posts = db.execute("SELECT COUNT(*) as cnt FROM posts").fetchone()
    total_analyzed = db.execute(
        "SELECT COUNT(*) as cnt FROM posts WHERE topic IS NOT NULL"
    ).fetchone()

    db.close()
    return {
        "posts_collected_today": posts_row["cnt"] if posts_row else 0,
        "posts_analyzed_today": analyzed_row["cnt"] if analyzed_row else 0,
        "authors_scanned_today": authors_row["cnt"] if authors_row else 0,
        "total_posts": total_posts["cnt"] if total_posts else 0,
        "total_analyzed": total_analyzed["cnt"] if total_analyzed else 0,
        "analysis_coverage": (
            round((total_analyzed["cnt"] / total_posts["cnt"]) * 100, 1)
            if total_posts and total_posts["cnt"] > 0
            else 0.0
        ),
    }


# ──────────────────────────────────────────────
# Author Pattern Detection
# ──────────────────────────────────────────────

def get_recent_topics_by_author(linkedin_id: str, days: int = 14) -> dict[str, int]:
    """Get topic frequency for an author within the given window.

    Returns {topic: count} dict.
    """
    cutoff = int(time.time()) - (days * 86400)
    db = get_db()
    rows = db.execute(
        """SELECT topic, COUNT(*) as cnt FROM posts
           WHERE author_linkedin_id = ? AND topic IS NOT NULL
             AND last_seen_at >= ?
           GROUP BY topic ORDER BY cnt DESC""",
        (linkedin_id, cutoff),
    ).fetchall()
    db.close()
    return {r["topic"]: r["cnt"] for r in rows}


def get_post_analyses_by_author(linkedin_id: str, limit: int = 20) -> list[dict]:
    """Get analyzed posts for an author (with analysis_json)."""
    db = get_db()
    rows = db.execute(
        """SELECT analysis_json FROM posts
           WHERE author_linkedin_id = ? AND analysis_json IS NOT NULL
           ORDER BY last_seen_at DESC LIMIT ?""",
        (linkedin_id, limit),
    ).fetchall()
    db.close()

    results = []
    for r in rows:
        try:
            results.append(json.loads(r["analysis_json"]))
        except (json.JSONDecodeError, TypeError):
            pass
    return results


# ──────────────────────────────────────────────
# Research Pipeline
# ──────────────────────────────────────────────

def reclaim_stale_research() -> int:
    """Return in_progress rows to pending so a cancelled tick can retry them."""
    db = get_db()
    cur = db.execute(
        "UPDATE contacts SET research_status = 'pending' "
        "WHERE research_status = 'in_progress'"
    )
    db.commit()
    n = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
    db.close()
    return n


def get_contacts_pending_research(limit: int = 10) -> list[dict]:
    """Get contacts that need post research, ordered by priority."""
    db = get_db()
    rows = db.execute(
        """SELECT DISTINCT c.id, c.linkedin_id, c.name, c.title, c.company,
                  c.campaign_id, c.research_status,
                  COALESCE(o.status, 'pending') as outreach_status
           FROM contacts c
           LEFT JOIN outreaches o ON o.contact_id = c.id
           JOIN campaigns camp ON camp.id = c.campaign_id AND camp.status = 'active'
           WHERE c.linkedin_id IS NOT NULL AND c.linkedin_id != ''
             AND (c.research_status IS NULL OR c.research_status = 'pending')
           ORDER BY
             CASE WHEN o.status IN ('invited', 'pending') THEN 0 ELSE 1 END,
             c.created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def update_contact_research(
    contact_id: str,
    status: str,
    summary: str = "",
) -> None:
    """Update contact research status and summary."""
    now = int(time.time())
    db = get_db()
    db.execute(
        """UPDATE contacts
           SET research_status = ?, research_summary = ?, research_completed_at = ?
           WHERE id = ?""",
        (status, summary or None, now if status == "complete" else None, contact_id),
    )
    db.commit()
    db.close()
