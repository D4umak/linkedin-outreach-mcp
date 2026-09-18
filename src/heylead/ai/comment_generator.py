"""Comment generator — voice-matched LinkedIn post comments.

Generates engaging comments on prospect's LinkedIn posts that:
1. Sound like the sender wrote them (voice-matched)
2. Reference specific content from the post
3. Build trust without any sales pitch
4. Are concise (under 200 chars by default)

Backend routing: if in backend mode with no local LLM key,
proxies through the backend API (same pattern as followup_generator.py).
"""

from __future__ import annotations

from ..textutil import first_name

import logging
from typing import Any

from . import schemas
from .length_fixer import shorten_to_limit
from .llm import LLMClient
from .prompt_loader import (
    build_context_block,
    get_prompt_temperature,
    has_prompt,
    load_expertise_map,
    render_prompt,
)

logger = logging.getLogger(__name__)

COMMENT_MAX_CHARS = 200

COMMENT_SYSTEM = """You are an expert at writing LinkedIn comments. You write genuine, engaging comments that build relationships and trust — never selling, never pitching.

Key principles:
- Sound like a real person, not a bot or salesperson
- Reference the post's theme loosely — paraphrase, never quote or mirror exact words/phrases
- Never open with the prospect's first name. Start with the substance.
- Never mention specific company names, place names, or people's names from the post
- Never use buzzy adjectives: "massive", "cool", "impressive", "serious", "game-changing"
- Never use template openers: "Spot on.", "So true.", "Love this.", "Agreed."
- Never use generic flattery: "cool to see someone who...", "great perspective on..."
- Match the sender's voice and tone exactly
- Keep comments concise (1-3 short sentences, under 200 characters)
- Build trust and familiarity — the goal is to stay on their radar
- Vary comment styles naturally: insights, questions, encouragement, common ground, light humor
- Match the language of the post (if the post is in Spanish, comment in Spanish)
- NEVER mention your product, service, or company
- NEVER use flattery or overreaction

You output a JSON object with three fields: "style", "reasoning", and "comment".
No markdown, no code fences, no explanation outside the JSON."""

COMMENT_PROMPT = """Write a LinkedIn comment on this post.

## SENDER (you are writing AS this person)
Name: {sender_name}
Title: {sender_title}
Company: {sender_company}
Voice: {voice_tone}
Sentence style: {voice_sentence}
Vocabulary preferences: {voice_vocab}
No-go (NEVER use these): {voice_nogo}

## POST AUTHOR (the person whose post you're commenting on)
Name: {prospect_name}
Title: {prospect_title}
Company: {prospect_company}
Headline: {prospect_headline}

## PROSPECT INTELLIGENCE (from analysis)
{prospect_intelligence}

## THE POST
{post_text}

## GOAL PRIORITIZATION (ordered)
1. Build trust & familiarity
2. Stay on the author's radar
3. Encourage author to reply to the comment
4. Subtly reinforce understanding of their challenges

## CONSTRAINTS
- Do NOT open with the prospect's first name. Start with the substance of your comment.
- 1-3 short sentences, MAXIMUM {max_chars} characters
- Personal and natural — no sales pitch, no buzzwords, no cliches
- Reference the post's theme loosely — paraphrase, never quote or mirror exact words
- Match the sender's voice exactly
- Match the post's language
- NO emojis unless the sender's voice uses them
- NO flattery or overreaction
- NO mention of your product, service, or company

## ANTI-PATTERNS (NEVER do these):
- Do not quote exact words or phrases from the post. Paraphrase loosely.
- Do not use quotation marks to reference post content.
- Do not mention specific company/place/people names from the post.
- Do not use template openers: "Spot on.", "So true.", "Love this.", "Agreed."
- Do not use buzzy adjectives: "massive", "cool", "impressive", "serious", "game-changing"
- Do not use "the" or "that" to point back at exact post wording

## EXAMPLES (follow this tone):
BAD: "Sarah, the SLC mountains look amazing! Nothing beats getting out of the office."
GOOD: "Hope you actually got a moment to enjoy yourself out there - mountains around should make that pretty easy."

BAD: "Fascinating approach to the healing space, Maya. The intersection of memory and care is a massive design challenge."
GOOD: "Work around memory and healing needs a different kind of care. Good to see someone staying close to what matters while they build."

## COMMENT STYLE
Choose the most appropriate style silently (do NOT name it in your comment):
- Insightful Contribution: Add a brief perspective or data point
- Question for Engagement: Ask a thoughtful open-ended question
- Encouragement/Congrats: Genuine support for their achievement or insight
- Resource Sharing: Mention a relevant resource or parallel
- Common-Ground Connection: Connect through shared experience
- Light Humor: Only if the post's tone clearly permits it

## OUTPUT
Return ONLY valid JSON (no markdown):
{{
    "style": "the comment style chosen (e.g., 'Question for Engagement')",
    "reasoning": {{
        "post_hook": "the specific idea/quote from the post you're referencing",
        "angle": "how this builds trust without selling"
    }},
    "comment": "the actual comment text"
}}"""


def _build_intelligence_text(prospect_analysis: dict[str, Any] | None) -> str:
    """Build a prospect intelligence text block for prompt injection."""
    if not prospect_analysis:
        return "No analysis available — use profile data only."
    tone = prospect_analysis.get("tone", {})
    pain_points = prospect_analysis.get("pain_points", [])
    summary = prospect_analysis.get("summary", "")
    parts = []
    if summary:
        parts.append(f"Profile: {summary}")
    if tone.get("recommended_approach"):
        parts.append(f"Approach: {tone['recommended_approach']}")
    if tone.get("formality_level"):
        parts.append(f"Their formality: {tone['formality_level']}/10")
    if tone.get("industry_jargon"):
        jargon = tone["industry_jargon"]
        if isinstance(jargon, list):
            parts.append(f"Their jargon: {', '.join(jargon[:5])}")
    if pain_points and isinstance(pain_points, list):
        parts.append(f"Likely pain points: {'; '.join(str(p) for p in pain_points[:3])}")
    return "\n".join(parts) if parts else "No analysis available — use profile data only."


async def generate_comment(
    prospect: dict[str, Any],
    sender_profile: dict[str, Any],
    voice_signature: dict[str, Any],
    post_data: dict[str, Any],
    max_chars: int = COMMENT_MAX_CHARS,
    prospect_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a personalized LinkedIn comment on a prospect's post.

    Args:
        prospect: Prospect profile data (name, title, company, headline)
        sender_profile: User's own profile data
        voice_signature: Voice analysis results (tone, vocabulary, patterns)
        post_data: The post to comment on (text, date, metrics)
        max_chars: Maximum comment length (default 200)

    Returns:
        Dict with "comment" (str), "style" (str), and "reasoning" (str).

        "reasoning" is a STRING on both paths — the v63 branch hardcodes ""
        and the legacy branch reads it through schemas.COMMENT, which declares
        it _STR. This docstring said "dict" and a caller believed it, splatting
        the value into a literal; "" is not a mapping, so every comment that
        LinkedIn accepted was lost after the fact. Callers must not assume a
        mapping here.
    """
    # Route through backend if in backend mode with no local LLM key
    from ..config import has_local_llm_key, is_backend_mode
    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client
        client = get_linkedin_client()
        try:
            return await client.generate_comment(
                sender=sender_profile,
                prospect=prospect,
                voice=voice_signature,
                post_data=post_data,
            )
        finally:
            await client.close()

    # ── Try v63 comment prompt ──
    # Comments stay non-selling: do not use outreach_system (qualify / book a meeting).
    use_v63 = has_prompt("comment_main")

    if use_v63:
        logger.debug("Using v63 comment prompt")
        ctx = build_context_block(
            sender=sender_profile,
            prospect=prospect,
            campaign_config={},
            voice=voice_signature,
            analysis=prospect_analysis,
            max_chars=max_chars,
            expertise_map=await load_expertise_map(),
        )
        # Add post-specific context
        ctx["post_text"] = (post_data.get("text", "") or "")[:1000]
        ctx["post_date"] = post_data.get("date", "")
        ctx["post_metrics"] = post_data.get("metrics", "")

        system = COMMENT_SYSTEM
        prompt = render_prompt("comment_main", ctx)
        temp = get_prompt_temperature("comment_main")

        llm_client = LLMClient()
        raw = await llm_client.generate(prompt, system=system, temperature=temp)

        # v63 returns plain text, not JSON.
        comment = raw.strip().strip('"').strip("'").strip()
        style = ""
        reasoning = ""
    else:
        # ── Fallback to legacy prompts ──
        logger.debug("Using legacy comment prompt (v63 not found)")

        # Extract voice details
        voice_vocab = voice_signature.get("vocabulary_preferences", [])
        if isinstance(voice_vocab, list):
            voice_vocab = ", ".join(voice_vocab)

        # Get prospect's first name
        prospect_name = first_name(prospect.get("name"), "there")

        # Build prospect intelligence section
        prospect_intelligence = _build_intelligence_text(prospect_analysis)

        prompt = COMMENT_PROMPT.format(
            sender_name=sender_profile.get("name", ""),
            sender_title=sender_profile.get("title", ""),
            sender_company=sender_profile.get("company", ""),
            voice_tone=voice_signature.get("tone", "Professional, direct"),
            voice_sentence=voice_signature.get("sentence_length", "Medium"),
            voice_vocab=voice_vocab or "None specified",
            voice_nogo=voice_signature.get("no_go", "Generic sales phrases"),
            prospect_name=prospect_name,
            prospect_title=prospect.get("title", ""),
            prospect_company=prospect.get("company", ""),
            prospect_headline=prospect.get("headline", ""),
            prospect_intelligence=prospect_intelligence,
            post_text=(post_data.get("text", "") or "")[:1000],
            max_chars=max_chars,
        )

        llm_client = LLMClient()
        result = await llm_client.generate_json(
            prompt, schemas.COMMENT, system=COMMENT_SYSTEM, temperature=0.7,
        )
        comment = result["comment"]
        style = result.get("style", "")
        reasoning = result.get("reasoning", "")

    # Clean up
    comment = comment.strip().strip('"').strip("'").strip()

    # Enforce character limit (LLM-based shortening with retry)
    comment = await shorten_to_limit(comment, max_chars)

    return {"comment": comment, "style": style, "reasoning": reasoning}
