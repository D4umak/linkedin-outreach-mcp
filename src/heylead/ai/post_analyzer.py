"""AI: post_analyzer — structured post intelligence extraction.

3-stage pipeline (mirrors signal_classifier.py):
1. Fast rules: keyword matching for obvious topics
2. Backend proxy: POST /llm/analyze-post
3. Local LLM fallback

Extracts topic, sentiment, pain points, buying signals, and engagement hooks
from LinkedIn post content.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..constants import LLM_TIER_FAST

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Output schema
# ──────────────────────────────────────────────


@dataclass
class PostAnalysis:
    """Result of analyzing a LinkedIn post."""

    topic: str  # e.g. "hiring", "product_launch", "sales_automation"
    subtopics: list[str] = field(default_factory=list)
    sentiment: str = "neutral"  # positive, negative, neutral, frustrated, excited
    key_themes: list[str] = field(default_factory=list)
    pain_points: list[str] = field(default_factory=list)
    buying_signals: list[str] = field(default_factory=list)
    engagement_hook: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "subtopics": self.subtopics,
            "sentiment": self.sentiment,
            "key_themes": self.key_themes,
            "pain_points": self.pain_points,
            "buying_signals": self.buying_signals,
            "engagement_hook": self.engagement_hook,
        }


# ──────────────────────────────────────────────
# Stage 1: Fast rule-based topic detection
# ──────────────────────────────────────────────

_TOPIC_RULES: list[tuple[str, list[str]]] = [
    ("hiring", [
        "we're hiring", "we are hiring", "join our team", "open role",
        "looking for a", "open position", "job opening", "now hiring",
        "come work with us", "growing the team",
    ]),
    ("fundraising", [
        "raised", "series a", "series b", "series c", "seed round",
        "funding round", "pre-seed", "investors", "closed our round",
        "venture capital", "backed by",
    ]),
    ("product_launch", [
        "just launched", "we launched", "announcing", "introducing",
        "new feature", "product update", "now available", "v2",
        "big release", "shipped",
    ]),
    ("leadership", [
        "leadership lesson", "as a leader", "managing teams",
        "culture matters", "leadership is", "leading a team",
        "management tip",
    ]),
    ("sales", [
        "cold outreach", "outbound", "pipeline", "deal closed",
        "quota", "revenue", "sdr", "sales tips", "prospecting",
        "closing deals", "sales strategy", "booking meetings",
    ]),
    ("career_advice", [
        "career advice", "career tip", "if you want to grow",
        "years ago i", "lesson learned", "my journey", "career change",
        "what i wish i knew",
    ]),
    ("industry_insight", [
        "the industry is", "market trend", "state of", "prediction",
        "the future of", "ai in", "automation", "digital transformation",
    ]),
    ("personal_story", [
        "personal update", "grateful for", "excited to share",
        "thrilled to announce", "i'm joining", "new chapter",
        "leaving", "moving on",
    ]),
    ("pain_point", [
        "struggling with", "biggest challenge", "frustrated with",
        "pain point", "bottleneck", "time-consuming", "manual process",
        "broken process", "not working",
    ]),
]

_SENTIMENT_RULES: list[tuple[str, list[str]]] = [
    ("positive", [
        "excited", "thrilled", "grateful", "amazing", "love",
        "congratulations", "milestone", "proud", "incredible",
    ]),
    ("negative", [
        "frustrated", "disappointed", "terrible", "worst",
        "failed", "broken", "hate", "annoyed",
    ]),
    ("excited", [
        "can't wait", "so pumped", "let's go", "game changer",
        "blown away", "mind-blowing",
    ]),
    ("frustrated", [
        "sick of", "tired of", "enough of", "stop doing",
        "why do we still", "makes no sense",
    ]),
]


def _classify_fast(text: str) -> PostAnalysis | None:
    """Stage 1: Fast rule-based topic detection.

    Returns analysis if strong signal detected, None otherwise.
    """
    text_lower = text.lower()

    # Detect topic
    best_topic = ""
    best_count = 0
    for topic, keywords in _TOPIC_RULES:
        matched = sum(1 for kw in keywords if kw in text_lower)
        if matched > best_count:
            best_count = matched
            best_topic = topic

    if best_count < 2:
        return None  # Not confident enough for fast path

    # Detect sentiment
    sentiment = "neutral"
    for sent, keywords in _SENTIMENT_RULES:
        if any(kw in text_lower for kw in keywords):
            sentiment = sent
            break

    return PostAnalysis(
        topic=best_topic,
        subtopics=[],
        sentiment=sentiment,
        key_themes=[],
        pain_points=[],
        buying_signals=[],
        engagement_hook="",
    )


# ──────────────────────────────────────────────
# Stage 3: LLM classification
# ──────────────────────────────────────────────

_ANALYZE_SYSTEM = """You are an expert B2B content analyst. Given a LinkedIn post, extract structured intelligence.

Focus on:
- Topic categorization (hiring, fundraising, product_launch, sales, leadership, career_advice, industry_insight, personal_story, pain_point, or a custom topic)
- Subtopics (2-3 specific aspects)
- Sentiment (positive, negative, neutral, frustrated, excited)
- Key themes (2-4 high-level concepts)
- Pain points mentioned (problems the author or their audience faces)
- Buying signals (explicit needs, tool evaluations, requests for recommendations)
- Engagement hook (best angle for a thoughtful comment)

Output ONLY a JSON object with fields: topic, subtopics, sentiment, key_themes, pain_points, buying_signals, engagement_hook.
No markdown, no code fences, no explanation outside the JSON."""

_ANALYZE_PROMPT = """Analyze this LinkedIn post:

Author: {author_name} ({author_headline}, {author_company})
Post:
---
{post_text}
---

Extract topic, sentiment, pain points, buying signals, and engagement hooks."""


def _parse_analysis_response(raw: str) -> PostAnalysis:
    """Parse LLM JSON response into PostAnalysis."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                return PostAnalysis(topic="unknown")
        else:
            return PostAnalysis(topic="unknown")

    return PostAnalysis(
        topic=str(data.get("topic", "unknown")),
        subtopics=data.get("subtopics", []) or [],
        sentiment=str(data.get("sentiment", "neutral")),
        key_themes=data.get("key_themes", []) or [],
        pain_points=data.get("pain_points", []) or [],
        buying_signals=data.get("buying_signals", []) or [],
        engagement_hook=str(data.get("engagement_hook", "")),
    )


# ──────────────────────────────────────────────
# Main analysis function
# ──────────────────────────────────────────────


async def analyze_post(
    post_text: str,
    *,
    author_name: str = "",
    author_headline: str = "",
    author_company: str = "",
) -> PostAnalysis:
    """Analyze a LinkedIn post using the 3-stage pipeline.

    Args:
        post_text: The post content.
        author_name: Post author's name.
        author_headline: Author's LinkedIn headline.
        author_company: Author's company.

    Returns:
        PostAnalysis with topic, sentiment, pain points, and engagement hooks.
    """
    if not post_text or not post_text.strip():
        return PostAnalysis(topic="unknown")

    # ── Stage 1: Fast rules ──
    fast_result = _classify_fast(post_text)
    if fast_result:
        logger.info("Post analyzed (fast): topic=%s", fast_result.topic)
        return fast_result

    # ── Stage 2: Backend proxy ──
    from ..config import has_local_llm_key, is_backend_mode

    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client

        client = get_linkedin_client()
        try:
            result = await client.analyze_post(
                post_text=post_text,
                author_info={
                    "name": author_name,
                    "headline": author_headline,
                    "company": author_company,
                },
            )
            if result:
                logger.info("Post analyzed (backend): topic=%s", result.topic)
                return result
        except Exception as e:
            logger.warning("Backend analyze_post failed: %s, trying local LLM", e)
        finally:
            await client.close()

    # ── Stage 3: Local LLM ──
    try:
        from .llm import LLMClient

        llm = LLMClient()
        prompt = _ANALYZE_PROMPT.format(
            author_name=author_name or "Unknown",
            author_headline=author_headline or "Unknown",
            author_company=author_company or "Unknown",
            post_text=post_text[:1500],
        )

        raw = await llm.generate(prompt, system=_ANALYZE_SYSTEM, temperature=0.3, max_tokens=1000,
                             tier=LLM_TIER_FAST)
        result = _parse_analysis_response(raw)
        logger.debug("Post analyzed (LLM): topic=%s", result.topic)
        return result
    except Exception as e:
        logger.warning("Post analysis LLM failed: %s", e)
        return PostAnalysis(topic="unknown")


async def analyze_posts_batch(
    posts: list[dict[str, Any]],
    batch_size: int = 5,
) -> list[tuple[str, PostAnalysis]]:
    """Batch-analyze multiple posts. Returns list of (post_id, analysis) tuples."""
    results: list[tuple[str, PostAnalysis]] = []

    for post in posts[:batch_size]:
        post_id = post.get("post_id", post.get("id", ""))
        text = post.get("text", "")
        author_name = post.get("author_name", post.get("author", ""))
        author_headline = post.get("author_headline", "")
        author_company = post.get("author_company", "")

        if not text or not post_id:
            continue

        analysis = await analyze_post(
            post_text=text,
            author_name=author_name,
            author_headline=author_headline,
            author_company=author_company,
        )
        results.append((post_id, analysis))

    if results:
        logger.info("Analyzed %d posts (LLM)", len(results))
    return results
