"""The user's own posts, picked as few-shot examples for the post writer.

analyze_voice already reads these at setup, but it distils them into six
adjectives — "Direct, technical, slightly informal" — and the writer got the
adjectives. Adjectives do not carry rhythm, paragraph length, or how someone
opens; the posts themselves do, and they are already on disk.

Two sources, because neither is reliable alone: the posts table has metrics
and stays fresh as scans run, and profile["posts"] is what setup_profile
stored, which is all a brand-new install has.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Below this a post is a reaction, not a piece of writing — "Congrats!" and
# "Proud of this team" teach the model nothing except to be brief.
MIN_EXAMPLE_CHARS = 120

# LinkedIn truncates around 3,000; a longer example is a data-entry accident.
MAX_EXAMPLE_CHARS = 1400


def _engagement(metrics_json: str | None) -> int:
    """Rank posts by what they earned. A post with no metrics ranks last."""
    try:
        metrics = json.loads(metrics_json or "{}")
    except (ValueError, TypeError):
        return 0
    if not isinstance(metrics, dict):
        return 0

    def _count(key: str) -> int:
        try:
            return int(metrics.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    # A comment costs more than a reaction, a repost more than a comment.
    return _count("reactions_count") + 2 * _count("comments_count") + 3 * _count("reposts_count")


def apply_reshare_flags(
    posts: list[dict[str, Any]] | None,
    *,
    author_linkedin_id: str = "",
) -> int:
    """Write detected reshare flags onto stored owner rows.

    Old get_posts writes landed as is_repost = 0. select_example_posts keeps
    those. A later fetch that ran detect_reshare has to persist the 1 or the
    FlyerOne/Leica reshares stay 'own voice' forever.
    """
    from ..db.post_queries import upsert_post

    n = 0
    for post in posts or []:
        if not isinstance(post, dict) or not post.get("is_repost"):
            continue
        post_id = str(post.get("id") or post.get("urn") or post.get("post_id") or "")
        if not post_id:
            continue
        upsert_post(
            post_id,
            author_linkedin_id=author_linkedin_id or str(post.get("author_linkedin_id") or ""),
            text=str(post.get("text") or ""),
            is_repost=1,
            original_post_id=str(post.get("original_post_id") or ""),
            source="own_voice_backfill",
        )
        n += 1
    return n


def _is_known_own_writing(post: dict[str, Any]) -> bool:
    """False for a reshare, and for a post whose is_repost flag was never set.

    Old owner rows from get_posts stored no flag. Missing is unknown — do not
    treat that as the user's voice.
    """
    if post.get("is_repost"):
        return False
    if "is_repost" not in post:
        return False
    return True


def _usable(text: str) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) < MIN_EXAMPLE_CHARS:
        return ""
    return cleaned[:MAX_EXAMPLE_CHARS]


def select_example_posts(profile: dict[str, Any] | None, limit: int = 3) -> list[str]:
    """The user's best own posts, highest engagement first."""
    profile = profile or {}
    provider_id = profile.get("provider_id") or ""

    from ..db.post_queries import get_posts_by_author
    from ..db.queries import get_published_post_ids

    try:
        ours = get_published_post_ids()
    except Exception as e:  # a fresh DB, a migration mid-flight
        logger.debug("published post ids unavailable: %s", e)
        ours = set()

    candidates: list[tuple[int, str, str]] = []  # (score, post_id, text)

    if provider_id:
        try:
            rows = get_posts_by_author(provider_id, limit=50)
        except Exception as e:
            logger.debug("own posts unavailable: %s", e)
            rows = []
        for row in rows:
            if not _is_known_own_writing(row):
                continue
            post_id = str(row.get("post_id") or "")
            if post_id and post_id in ours:
                continue
            text = _usable(row.get("text"))
            if text:
                candidates.append((_engagement(row.get("metrics_json")), post_id, text))

    # What setup stored. No metrics, so these sort below anything measured.
    for post in profile.get("posts") or []:
        if not isinstance(post, dict):
            continue
        if not _is_known_own_writing(post):
            continue
        post_id = str(post.get("urn") or "")
        if post_id and post_id in ours:
            continue
        text = _usable(post.get("text"))
        if text:
            candidates.append((0, post_id, text))

    candidates.sort(key=lambda c: c[0], reverse=True)

    picked: list[str] = []
    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    for _score, post_id, text in candidates:
        if post_id and post_id in seen_ids:
            continue
        key = " ".join(text.split()).lower()
        if key in seen_text:
            continue
        if post_id:
            seen_ids.add(post_id)
        seen_text.add(key)
        picked.append(text)
        if len(picked) >= limit:
            break
    return picked


def example_posts_block(posts: list[str]) -> str:
    """The examples as a prompt section, or "" when there are none."""
    if not posts:
        return ""
    parts = [
        "POSTS THEY ACTUALLY WROTE\n"
        "These are here for rhythm, sentence length, and how they open and "
        "close. Write about the topic, not about anything in them: reuse none "
        "of their sentences, stories, names, companies or numbers.",
    ]
    for i, text in enumerate(posts, 1):
        parts.append(f"--- Example {i} ---\n{text}")
    return "\n\n".join(parts)


# A run this long is a lift, not a coincidence: eight content words in the
# same order do not recur by chance across two posts on the same topic.
_BORROWED_RUN_WORDS = 8


def _shingles(text: str, size: int) -> set[tuple[str, ...]]:
    words = "".join(
        ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in text or ""
    ).split()
    if len(words) < size:
        return set()
    return {tuple(words[i:i + size]) for i in range(len(words) - size + 1)}


def borrows_from_examples(draft: str, examples: list[str]) -> bool:
    """True when *draft* reuses a long verbatim run from one of *examples*.

    The examples are the user's real posts. A model that echoes one publishes
    last quarter's announcement as this morning's thought.
    """
    if not draft or not examples:
        return False
    drafted = _shingles(draft, _BORROWED_RUN_WORDS)
    if not drafted:
        return False
    return any(
        drafted & _shingles(example, _BORROWED_RUN_WORDS) for example in examples
    )
