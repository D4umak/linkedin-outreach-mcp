"""Voice signature analyzer — the #1 killer feature.

Analyzes the user's LinkedIn profile and posts to generate a voice signature
(tone, vocabulary, patterns) and expertise map. This is what makes every
outreach message sound like the user, not a bot.

From customer discovery: 7 of 12 interested users had their strongest
positive reaction to voice matching.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..db.async_bridge import run_db
from ..linkedin.experience import format_experience
from .llm import LLMClient
from .llm import loads_json_object as parse_json
from .prompt_loader import get_prompt_temperature, has_prompt, render_prompt
from .voice_block import VOICE_MIN_POSTS, is_usable_voice, voice_excerpts, with_record

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
Analyze their writing style from their own posts only. If the posts section says they have no posts of their own, leave every voice field empty and fill only the expertise map: a voice guessed from a headline is not theirs.

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


class VoiceAnalysisFailed(RuntimeError):
    """The analysis gave no usable voice for posts the user did write.

    Until api #3499 a local parse failure returned the "Could not analyze —
    check your LLM API key" default as the user's voice, setup stored it,
    and every writer rendered it as the tone. Callers now keep what they had.
    """


_NO_OWN_POSTS = (
    "No posts of their own found, so there is no writing to learn a voice from. "
    "Leave every voice field empty and fill only the expertise map, from the profile data."
)


def own_posts(
    posts: list[dict] | None, published: set[str] | None = None,
) -> list[dict]:
    """The posts the user wrote: no reshares, nothing HeyLead published.

    A reshare is someone else's writing. Analysing one as the user's voice is
    how a signature ends up describing a blend of several people — the account
    owner's had a FlyerOne job ad and a Leica role in it (9 Sep 2026). A post
    HeyLead published is the product's own prose; learning a voice from it
    teaches the writer to imitate itself (api #3499).

    ``published`` is the set of published post numbers; None reads it here.
    """
    from ..services.voice_examples import (
        _is_known_own_writing,
        is_published_by_heylead,
        published_numbers,
    )

    if published is None:
        published = published_numbers()
    return [
        p for p in (posts or [])
        if isinstance(p, dict) and _is_known_own_writing(p)
        and not is_published_by_heylead(p, published)
    ]


def _format_posts(posts: list[dict], published: set[str] | None = None) -> str:
    """The user's own posts, formatted for the prompt."""
    lines = []
    for post in own_posts(posts, published):
        text = post.get("text", "").strip()
        if text:
            lines.append(f"Post {len(lines) + 1}:\n{text[:500]}\n")
    if not lines:
        return _NO_OWN_POSTS
    return "\n".join(lines)


def _int_or(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _backend_answer(answer: Any, own_count: int) -> dict[str, Any]:
    """The route's answer in the client's shape, the route's record kept.

    The route counts the posts itself (it also knows the workspace's
    published posts); a route from before #3499 sends no count, so the
    client's own count stands in.
    """
    answer = answer if isinstance(answer, dict) else {}
    voice = answer.get("voice") if isinstance(answer.get("voice"), dict) else {}
    expertise = answer.get("expertise") if isinstance(answer.get("expertise"), dict) else {}
    post_count = _int_or(answer.get("post_count"), own_count)
    if post_count <= 0:
        # The zero-posts answer: no voice, expertise kept. A route from before
        # #3499 still invents a voice from the headline here; it is dropped.
        return {"voice": {}, "expertise": expertise, "post_count": 0, "thin": False}
    if not is_usable_voice(voice):
        raise VoiceAnalysisFailed("the backend returned no usable voice")
    thin = answer.get("thin")
    if not isinstance(thin, bool):
        thin = 0 < post_count < VOICE_MIN_POSTS
    return {"voice": voice, "expertise": expertise, "post_count": post_count, "thin": thin}


async def analyze_voice(profile: dict[str, Any]) -> dict[str, Any]:
    """Analyze a LinkedIn profile and return voice signature + expertise map.

    Args:
        profile: Dict from linkedin.profile.fetch_own_profile()

    Returns:
        ``{"voice", "expertise", "post_count", "thin"}``. Zero own posts give
        ``voice == {}`` (the expertise map is still built). A local build's
        voice carries its record (voice_block.with_record, source "client").

    Raises:
        VoiceAnalysisFailed: own posts exist but no usable voice came back.
    """
    # Route through backend if in backend mode and no local LLM key
    from ..config import has_local_llm_key, is_backend_mode
    from ..services.voice_examples import apply_reshare_flags, published_numbers
    posts = profile.get("posts") or []
    try:
        await run_db(
            apply_reshare_flags, posts,
            author_linkedin_id=str(profile.get("provider_id") or ""),
        )
    except Exception as e:
        logger.debug("reshare backfill skipped: %s", e)

    published = await run_db(published_numbers)
    own = own_posts(posts, published)

    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client
        client = get_linkedin_client()
        try:
            # The backend builds its own prompt from this list, so
            # _format_posts never runs for a hosted account. Filter here or
            # the reshares reach the analyzer anyway.
            answer = await client.analyze_voice(profile, own)
        finally:
            await client.close()
        return _backend_answer(answer, len(own))

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
        "post_count": str(len(own)),
        "posts": _format_posts(posts, published),
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

    try:
        parsed = parse_json(raw)
    except ValueError as e:
        raise VoiceAnalysisFailed(f"the model's answer did not parse: {e}") from e
    if not isinstance(parsed, dict):
        raise VoiceAnalysisFailed("the model's answer was not an object")
    expertise = parsed.get("expertise") if isinstance(parsed.get("expertise"), dict) else {}

    if not own:
        # Nothing the user wrote: the model was asked for the expertise map
        # only, and whatever voice it gave anyway is discarded.
        return {"voice": {}, "expertise": expertise, "post_count": 0, "thin": False}

    voice = parsed.get("voice") if isinstance(parsed.get("voice"), dict) else {}
    if not is_usable_voice(voice):
        raise VoiceAnalysisFailed("the model returned no usable voice")
    recorded = with_record(
        voice, post_count=len(own), excerpts=voice_excerpts(own), source="client",
        now=int(time.time()),
    )
    return {
        "voice": recorded, "expertise": expertise,
        "post_count": len(own), "thin": bool(recorded["thin"]),
    }


# ── Prompt rendering ──

# The renderer lives in voice_block (a leaf module: prompt_loader needs it
# and this module imports prompt_loader). Re-exported for existing callers.
from .voice_block import voice_prompt_block  # noqa: E402,F401
