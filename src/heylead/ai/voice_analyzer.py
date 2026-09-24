"""Voice signature analyzer — the #1 killer feature.

Analyzes the user's LinkedIn profile and posts to generate a voice signature
(tone, vocabulary, patterns) and expertise map. This is what makes every
outreach message sound like the user, not a bot.

From customer discovery: 7 of 12 interested users had their strongest
positive reaction to voice matching.
"""

from __future__ import annotations

import logging
from typing import Any

from ..db.async_bridge import run_db
from ..linkedin.experience import format_experience
from .llm import LLMClient
from .llm import loads_json_object as parse_json
from .prompt_loader import get_prompt_temperature, has_prompt, render_prompt

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Voice Analysis Prompt
# ──────────────────────────────────────────────

VOICE_ANALYSIS_SYSTEM = """You are an expert communication analyst specializing in LinkedIn professional communication styles. You analyze how people write and communicate to create a "voice signature" that can be used to generate messages that sound authentically like them.

You are precise, analytical, and output structured JSON only."""

VOICE_ANALYSIS_PROMPT = """Analyze this LinkedIn profile and their recent posts to create a detailed voice signature and expertise map.

## PROFILE DATA
Name: {name}
Headline: {headline}
Title: {title} at {company}
Location: {location}
Industry: {industry}
Summary: {summary}

## EXPERIENCE
{experience}

## SKILLS
{skills}

## RECENT POSTS ({post_count} posts)
{posts}

---

## TASK
Create two things:

### 1. VOICE SIGNATURE
Analyze their writing style from posts, headline, and summary. If they have no posts, infer from their headline, summary, and professional context.

Extract:
- **tone**: One-line description (e.g., "Direct, technical, slightly informal")
- **sentence_length**: Their typical pattern (e.g., "Short (avg 8 words)" or "Medium, uses compound sentences")
- **signature_pattern**: How they typically open and close communications (e.g., "Opens with first name, closes with question")
- **vocabulary_preferences**: Words/phrases they gravitate toward
- **no_go**: Things they would NEVER write (e.g., "Won't use emojis, won't say 'synergy'")
- **formality_level**: 1-10 scale (1=very casual, 10=very formal)
- **communication_style**: Specific recommendations for matching their voice

### 2. EXPERTISE MAP
From their profile, experience, and posts:
- **core**: Their primary expertise areas (comma-separated)
- **credible_topics**: Topics they can credibly discuss in outreach
- **off_limits**: Topics outside their domain they should NOT comment on
- **industry_context**: Brief description of their industry positioning
- **constraints**: Current vs past companies — never present a past venture as current

## RESPONSE FORMAT
Return ONLY valid JSON (no markdown, no explanation):
{{
    "voice": {{
        "tone": "...",
        "sentence_length": "...",
        "signature_pattern": "...",
        "vocabulary_preferences": ["...", "..."],
        "no_go": "...",
        "formality_level": 7,
        "communication_style": ["...", "..."]
    }},
    "expertise": {{
        "core": "...",
        "credible_topics": "...",
        "off_limits": "...",
        "industry_context": "...",
        "constraints": "..."
    }}
}}"""


def own_posts(posts: list[dict] | None) -> list[dict]:
    """The posts the user wrote, with the reshares dropped.

    A reshare is someone else's writing. Analysing one as the user's voice is
    how a signature ends up describing a blend of several people — the account
    owner's had a FlyerOne job ad and a Leica role in it (9 Sep 2026).
    """
    from ..services.voice_examples import _is_known_own_writing

    return [p for p in (posts or []) if isinstance(p, dict) and _is_known_own_writing(p)]


def _format_posts(posts: list[dict]) -> str:
    """The user's own posts, formatted for the prompt."""
    if not posts:
        return "No recent posts available — infer voice from profile data."
    lines = []
    for post in own_posts(posts):
        text = post.get("text", "").strip()
        if text:
            lines.append(f"Post {len(lines) + 1}:\n{text[:500]}\n")
    if not lines:
        return "No posts of their own found — infer voice from profile data."
    return "\n".join(lines)


async def analyze_voice(profile: dict[str, Any]) -> dict[str, Any]:
    """Analyze a LinkedIn profile and return voice signature + expertise map.

    Args:
        profile: Dict from linkedin.profile.fetch_own_profile()

    Returns:
        Dict with 'voice' and 'expertise' keys containing the analysis.
    """
    # Route through backend if in backend mode and no local LLM key
    from ..config import has_local_llm_key, is_backend_mode
    from ..services.voice_examples import apply_reshare_flags
    posts = profile.get("posts") or []
    try:
        await run_db(
            apply_reshare_flags, posts,
            author_linkedin_id=str(profile.get("provider_id") or ""),
        )
    except Exception as e:
        logger.debug("reshare backfill skipped: %s", e)

    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client
        client = get_linkedin_client()
        try:
            # The backend builds its own prompt from this list, so
            # _format_posts never runs for a hosted account. Filter here or
            # the reshares reach the analyzer anyway.
            return await client.analyze_voice(profile, own_posts(profile.get("posts")))
        finally:
            await client.close()

    # Build template variables
    tpl_vars = {
        "name": profile.get("name", "Unknown"),
        "headline": profile.get("headline", ""),
        "title": profile.get("title", ""),
        "company": profile.get("company", ""),
        "location": profile.get("location", ""),
        "industry": profile.get("industry", ""),
        "summary": profile.get("summary", ""),
        "experience": format_experience(profile.get("experience", [])),
        "skills": ", ".join(profile.get("skills", [])[:15]),
        "post_count": str(len(own_posts(profile.get("posts")))),
        "posts": _format_posts(profile.get("posts", [])),
    }

    # v63 path: use JSON prompt template
    if has_prompt("voice_analysis"):
        logger.debug("Using v63 voice_analysis prompt")
        prompt = render_prompt("voice_analysis", tpl_vars)
        temperature = get_prompt_temperature("voice_analysis")
    else:
        # Legacy fallback
        logger.debug("Using legacy voice_analysis prompt")
        prompt = VOICE_ANALYSIS_PROMPT.format(**tpl_vars)
        temperature = 0.5

    client = LLMClient()
    raw = await client.generate(prompt, system=VOICE_ANALYSIS_SYSTEM, temperature=temperature)

    # Parse JSON from response with 2-tier repair
    default_result = {
        "voice": {
            "tone": "Could not analyze — check your LLM API key",
            "sentence_length": "Unknown",
            "signature_pattern": "Unknown",
            "vocabulary_preferences": [],
            "no_go": "Unknown",
            "formality_level": 5,
            "communication_style": [],
        },
        "expertise": {
            "core": profile.get("headline", ""),
            "credible_topics": "",
            "off_limits": "",
            "industry_context": profile.get("industry", ""),
        },
    }

    return parse_json(raw, fallback=default_result)


# ── Prompt rendering ──

# The renderer lives in voice_block (a leaf module: prompt_loader needs it
# and this module imports prompt_loader). Re-exported for existing callers.
from .voice_block import voice_prompt_block  # noqa: E402,F401
