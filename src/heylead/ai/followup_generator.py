"""Follow-up message generator — 2-stage v63 pipeline with legacy fallback.

Generates LinkedIn follow-up DMs after a connection is accepted.
Unlike invitation messages (200 chars), follow-ups are full DMs (up to 500 chars).

v63 Pipeline (2-stage):
1. Reasoning (LLM): Strategic planning — link-gating, bullets policy, CTA rotation,
   length style, pain-first pattern, fresh opener selection
2. Generate (LLM): Write the message using reasoning decisions + voice

Legacy Pipeline (1-stage, fallback):
1. Generate (LLM): Single-shot with inline reasoning

Both are then passed through Improve → Validate → Fix in the tool layer.

Backend routing: if in backend mode with no local LLM key,
proxies through the backend API (same pattern as message_generator.py).
"""

from __future__ import annotations

from ..textutil import first_name

import json
import logging
from typing import Any

from ..db.async_bridge import run_db
from . import schemas
from .followup_schemas import FollowUpReasoning
from .length_fixer import shorten_to_limit
from .llm import LLMClient
from .llm import loads_json_object as parse_json
from .memory_extractor import extract_memory_from_reasoning
from .memory_formatter import format_memory_for_prompt
from .news_service import get_prospect_news
from .prompt_loader import (
    build_context_block,
    get_prompt_temperature,
    has_prompt,
    load_expertise_map,
    load_fragment,
    render_prompt,
)

logger = logging.getLogger(__name__)

# Maximum follow-up DM length (LinkedIn has no hard limit for DMs,
# but keeping it concise improves response rates)
FOLLOWUP_MAX_CHARS = 500

# First follow-up is a nudge, not a second pitch. 220 is length_style "short".
FIRST_FOLLOWUP_MAX_CHARS = 220


def followup_char_limit(followup_number: int) -> int:
    """Character cap for this touch — first follow-up stays scannable on mobile."""
    try:
        number = int(followup_number)
    except (TypeError, ValueError):
        number = 1
    return FIRST_FOLLOWUP_MAX_CHARS if number <= 1 else FOLLOWUP_MAX_CHARS

# ──────────────────────────────────────────────
# Legacy prompts — fallback if v63 JSON files are missing
# ──────────────────────────────────────────────

FOLLOWUP_SYSTEM_LEGACY = """You are an expert at writing LinkedIn follow-up DMs. You write genuine, personal follow-up messages that advance a relationship after a connection has been accepted.

Key principles:
- Sound like a real person, not a bot or salesperson
- Reference the CONVERSATION HISTORY — acknowledge what was said before
- Use a FRESH ANGLE — never repeat the same hook or pitch from earlier messages
- Match the sender's voice and tone exactly
- Keep messages concise (2-5 sentences, under 500 characters)
- End with a soft CTA — a question about their daily experience
- Never mirror exact words from conversation history or prospect profile. Paraphrase loosely.
- Never open with prospect's name. Start with substance.
- Ask about their daily experience, not about AI or industry trends
- Acknowledge silence casually — never pressure
- Keep tone understated, not buzzy or flattering
- Never use salesy phrases like "leverage", "synergy", "exciting opportunity"
- Never say "just following up" or "circling back" — these are lazy and obvious
- Never use buzzy adjectives: "impressive", "killing it", "serious move", "massive"
- Never use template openers: "Spot on.", "Love this.", "Agreed."

You output a JSON object with two fields: "reasoning" and "message".
No markdown, no code fences, no explanation outside the JSON."""

FOLLOWUP_PROMPT_LEGACY = """Generate a LinkedIn follow-up DM.

## SENDER (you are writing AS this person)
Name: {sender_name}
Title: {sender_title}
Company: {sender_company}
Voice: {voice_tone}
Sentence style: {voice_sentence}
Vocabulary preferences: {voice_vocab}
No-go (NEVER use these): {voice_nogo}

## PROSPECT (the person receiving this message)
Name: {prospect_name}
Title: {prospect_title}
Company: {prospect_company}
Headline: {prospect_headline}

## PROSPECT INTELLIGENCE (from analysis)
{prospect_intelligence}

## CAMPAIGN CONTEXT
Target: {campaign_target}
Relevance: {relevance_hook}

## BOOKING LINK
{booking_link_section}

## CONVERSATION HISTORY (oldest first)
{conversation_history}

{engagement_section}

## FOLLOW-UP NUMBER: {followup_number}
(1 = first follow-up after connection accepted, 2 = second, etc.)

## CONSTRAINTS
- Match the sender's voice exactly
- Reference something from the conversation or the prospect's profile — paraphrase loosely
- Use a COMPLETELY NEW ANGLE — do NOT repeat any hook, question, or observation from previous messages
- Keep under 500 characters
- Do NOT open with prospect's name
- Soft CTA: ask a question about THEIR daily experience
- NO emojis unless the sender's voice uses them
- NO "just following up", "circling back", "touching base", "checking in"
- NO salesy language
- NO buzzy adjectives or generic flattery
- NO company names (theirs or yours)
- If prospect hasn't replied, acknowledge silence casually ("if things went quiet, all good")

## ANTI-PATTERNS (NEVER do these)
- Do not mirror exact words from previous messages or prospect's profile
- Do not ask about AI, trends, or industry shifts generically
- Do not use template openers or cliche phrases

## EXAMPLES
BAD: "Hi Sarah, just wanted to follow up. Our AI-powered platform could really streamline your workflow."
GOOD: "If things went quiet, all good — retail shifts often push the creative load up. If video work is taking more time than you'd like, we've been building something that takes the heavy parts off your plate. Happy to share whenever it's useful."

## OUTPUT
Return ONLY valid JSON (no markdown):
{{
    "reasoning": {{
        "hook": "brief description of the fresh angle",
        "pain_point": "which pain point or interest this addresses",
        "cta_choice": "question|resource_offer|meeting_soft|value_share"
    }},
    "message": "the actual follow-up message text"
}}"""


def _format_conversation_history(messages: list[dict]) -> str:
    """Format conversation history for the prompt."""
    if not messages:
        return "No previous messages — this is the first follow-up after connection acceptance."
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        text = msg.get("text", "")
        label = "YOU (sender)" if role == "sdr" else "PROSPECT"
        lines.append(f"[{label}]: {text}")
    return "\n".join(lines) if lines else "No previous messages."


def _format_engagement_history(engagements: list[dict[str, Any]] | None) -> str:
    """Format warm-up engagements (comments, likes) for prompt injection."""
    if not engagements:
        return ""
    lines = []
    for eng in engagements:
        action = eng.get("action_type", "unknown")
        post_text = (eng.get("post_text") or "").strip()
        # Truncate long post text to a short summary
        if len(post_text) > 120:
            post_text = post_text[:117] + "..."
        if action == "comment":
            comment_text = (eng.get("text") or "").strip()
            if post_text and comment_text:
                lines.append(f'- [Comment] on post about "{post_text}": "{comment_text}"')
            elif comment_text:
                lines.append(f'- [Comment]: "{comment_text}"')
        elif action == "react":
            reaction = eng.get("reaction_type", "LIKE")
            if post_text:
                lines.append(f'- [{reaction}] on post about "{post_text}"')
            else:
                lines.append(f"- [{reaction}] on their post")
        elif action in ("follow", "endorse", "profile_view"):
            lines.append(f"- [{action.replace('_', ' ').title()}] on their profile")
    if not lines:
        return ""
    header = (
        "## PRIOR WARM-UP ENGAGEMENTS (things you already did on their posts)\n"
        + "\n".join(lines)
        + "\n\nIMPORTANT: Reference these interactions naturally — "
        "they are shared context the prospect may remember. "
        "Use them as an opener or bridge to the conversation."
    )
    return header


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
            parts.append(f"Jargon: {', '.join(jargon[:5])}")
    if pain_points and isinstance(pain_points, list):
        parts.append(f"Likely pain points: {'; '.join(str(p) for p in pain_points[:3])}")
    return "\n".join(parts) if parts else "No analysis available — use profile data only."


async def generate_followup(
    prospect: dict[str, Any],
    sender_profile: dict[str, Any],
    voice_signature: dict[str, Any],
    campaign_context: dict[str, Any],
    conversation_history: list[dict[str, Any]],
    followup_number: int = 1,
    prospect_analysis: dict[str, Any] | None = None,
    campaign_ctx: dict[str, Any] | None = None,
    outreach_id: str = "",
    engagement_history: list[dict[str, Any]] | None = None,
    brief: Any = None,
    icp_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a personalized LinkedIn follow-up DM.

    Uses v63's 2-stage pipeline (reasoning → generate) if prompt files exist,
    otherwise falls back to legacy single-stage generation.

    Args:
        prospect: Prospect profile data (name, title, company, headline, etc.)
        sender_profile: User's own profile data
        voice_signature: Voice analysis results (tone, vocabulary, patterns)
        campaign_context: Campaign ICP and target description
        conversation_history: Previous messages in the thread (role + text)
        followup_number: Which follow-up this is (1=first, 2=second, etc.)
        prospect_analysis: Optional prospect analysis results
        campaign_ctx: Optional campaign context (offerings, case_studies, social_proofs)
        outreach_id: Optional outreach ID for loading conversation memory
        engagement_history: Optional list of warm-up engagements (comments, likes)

    Returns:
        Dict with "message" (str), "reasoning" (dict or str), and
        optional "memory_entry" (dict) for saving after send.
    """
    # Route through backend if in backend mode and no local LLM key
    from ..config import has_local_llm_key, is_backend_mode
    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client
        client = get_linkedin_client()
        try:
            return await client.generate_followup(
                sender=sender_profile,
                prospect=prospect,
                voice=voice_signature,
                campaign_context=campaign_context,
                conversation_history=conversation_history,
                followup_number=followup_number,
                engagement_history=engagement_history,
            )
        finally:
            await client.close()

    # ── Try v63 2-stage pipeline ──
    use_v63 = (
        has_prompt("followup_reasoning")
        and has_prompt("followup_message")
        and has_prompt("outreach_system")
    )

    if use_v63:
        return await _generate_v63(
            prospect, sender_profile, voice_signature, campaign_context,
            conversation_history, followup_number, prospect_analysis, campaign_ctx,
            outreach_id, engagement_history, brief, icp_data,
        )
    else:
        return await _generate_legacy(
            prospect, sender_profile, voice_signature, campaign_context,
            conversation_history, followup_number, prospect_analysis,
            engagement_history,
        )


async def _generate_v63(
    prospect: dict[str, Any],
    sender_profile: dict[str, Any],
    voice_signature: dict[str, Any],
    campaign_context: dict[str, Any],
    conversation_history: list[dict[str, Any]],
    followup_number: int,
    prospect_analysis: dict[str, Any] | None,
    campaign_ctx: dict[str, Any] | None,
    outreach_id: str = "",
    engagement_history: list[dict[str, Any]] | None = None,
    brief: Any = None,
    icp_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate using v63's 2-stage pipeline: Reasoning → Message → Memory."""
    logger.info("Using v63 2-stage pipeline for follow-up #%d", followup_number)

    max_chars = followup_char_limit(followup_number)
    engagement_text = _format_engagement_history(engagement_history)
    if outreach_id:
        from ..services.action_timeline import action_timeline_text
        timeline = await action_timeline_text(outreach_id)
        if timeline:
            engagement_text = (
                f"{engagement_text}\n\n{timeline}".strip()
                if engagement_text else timeline
            )
    if brief is None:
        from .brief_builder import build_message_brief
        from .intent import resolve_intent
        brief = build_message_brief(
            intent=resolve_intent(campaign_context),
            campaign_config=campaign_context,
            campaign_ctx=campaign_ctx,
            prospect=prospect,
            icp_data=icp_data,
            analysis=prospect_analysis,
            touch="followup",
        )
    ctx = build_context_block(
        sender=sender_profile,
        prospect=prospect,
        campaign_config=campaign_context,
        voice=voice_signature,
        campaign_context=campaign_ctx,
        history=conversation_history,
        analysis=prospect_analysis,
        max_chars=max_chars,
        engagement_history=engagement_text,
        expertise_map=await load_expertise_map(),
        brief=brief,
    )

    # ── Load conversation memory (anti-repetition) ──
    memory: list[dict] = []
    if outreach_id:
        try:
            from ..db.queries import get_outreach_memory
            memory = await run_db(get_outreach_memory, outreach_id)
            if memory:
                logger.info("Loaded %d memory entries for outreach %s", len(memory), outreach_id[:8])
        except Exception as e:
            logger.debug("Memory load skipped: %s", e)
    ctx["previously_used"] = format_memory_for_prompt(memory)
    ctx["followup_number"] = str(followup_number)

    llm_client = LLMClient()

    # ── STAGE 1: Reasoning ──
    from .intent import resolve_intent, select_prompt
    intent = resolve_intent(campaign_context or {})
    system_prompt = render_prompt(select_prompt("outreach_system", intent), ctx)
    reasoning_name = select_prompt("followup_reasoning", intent)
    reasoning_prompt = render_prompt(reasoning_name, ctx)
    reasoning_temp = get_prompt_temperature(reasoning_name)

    reasoning_raw = await llm_client.generate(
        reasoning_prompt, system=system_prompt, temperature=reasoning_temp,
    )
    logger.debug("Stage 1 reasoning: %s", reasoning_raw[:200])

    # ── Parse structured reasoning (FollowUpReasoning) ──
    reasoning_structured = _parse_structured_reasoning(reasoning_raw)

    # ── STAGE 2: Generate message using reasoning decisions ──
    # Pass flattened decisions + skeleton to Stage 2 for precise control
    if reasoning_structured:
        decisions_dict = reasoning_structured.decisions.model_dump()
        skeleton_dict = reasoning_structured.message_skeleton.model_dump()
        # Build a formatted reasoning block for the prompt
        ctx["reasoning"] = (
            f"DECISIONS:\n{json.dumps(decisions_dict, indent=2)}\n\n"
            f"MESSAGE SKELETON:\n{json.dumps(skeleton_dict, indent=2)}"
        )
        logger.info(
            "Structured reasoning parsed: length_style=%s, allow_link=%s, cta=%s",
            decisions_dict.get("length_style"),
            decisions_dict.get("allow_link"),
            decisions_dict.get("cta_choice", "")[:40],
        )
    else:
        # Fallback: pass raw text if structured parsing failed
        ctx["reasoning"] = reasoning_raw
        logger.warning("Structured reasoning parse failed, using raw text")

    message_name = select_prompt("followup_message", intent)
    message_prompt = render_prompt(message_name, ctx)
    message_temp = get_prompt_temperature(message_name)

    message_raw = await llm_client.generate(
        message_prompt, system=system_prompt, temperature=message_temp,
    )

    # v63 returns plain text (not JSON)
    message = message_raw.strip().strip('"').strip("'").strip()

    # ── STAGE 3: News enhancement (optional) ──
    news_context = None
    try:
        news_context = await get_prospect_news(
            prospect=prospect,
            sender=sender_profile,
            campaign_context=campaign_context,
        )
    except Exception as e:
        logger.debug("News lookup skipped: %s", e)

    if news_context and has_prompt("followup_news"):
        try:
            news_ctx = {
                "draft_message": message,
                "news_context": news_context,
                "symbols_limit": str(max_chars),
                "voice_rules": load_fragment("voice_rules"),
            }
            enhanced = await llm_client.generate(
                render_prompt("followup_news", news_ctx),
                system=system_prompt,
                temperature=get_prompt_temperature("followup_news"),
            )
            enhanced = enhanced.strip().strip('"').strip("'").strip()
            if enhanced and len(enhanced) <= max_chars:
                message = enhanced
                logger.info("News enhancement applied to follow-up")
        except Exception as e:
            logger.warning("News enhancement failed, using original: %s", e)

    # Enforce character limit (LLM-based shortening with retry)
    message = await shorten_to_limit(message, max_chars)

    # ── Extract conversation memory for this follow-up ──
    memory_entry = None
    try:
        memory_entry = await extract_memory_from_reasoning(
            reasoning_text=reasoning_raw,
            message_text=message,
            followup_number=followup_number,
        )
        logger.info("Extracted memory entry for follow-up #%d", followup_number)
    except Exception as e:
        logger.debug("Memory extraction skipped: %s", e)

    # Include structured reasoning in the result for downstream use (validate/fix)
    reasoning_output: dict[str, Any] | str = reasoning_raw
    if reasoning_structured:
        reasoning_output = {
            "decisions": reasoning_structured.decisions.model_dump(),
            "message_skeleton": reasoning_structured.message_skeleton.model_dump(),
            "_raw": reasoning_raw,
        }

    return {
        "message": message,
        "reasoning": reasoning_output,
        "memory_entry": memory_entry,
        "brief": brief,
    }


def _parse_structured_reasoning(raw: str) -> FollowUpReasoning | None:
    """Parse raw LLM output into a FollowUpReasoning model.

    Uses 2-tier JSON parsing (raw → regex cleanup) then Pydantic validation.
    Returns None if parsing fails — caller falls back to raw text.
    """
    try:
        parsed = parse_json(raw, fallback=None)
        if parsed:
            return FollowUpReasoning.model_validate(parsed)
    except Exception as e:
        logger.debug("Structured reasoning parse attempt 1 failed: %s", e)

    # Try extracting just decisions + message_skeleton from a larger JSON
    try:
        parsed = parse_json(raw, fallback={})
        if "decisions" in parsed and "message_skeleton" in parsed:
            return FollowUpReasoning.model_validate(parsed)
        # Sometimes LLM wraps in extra layer
        for key in ("reasoning", "result", "output"):
            if key in parsed and isinstance(parsed[key], dict):
                inner = parsed[key]
                if "decisions" in inner:
                    return FollowUpReasoning.model_validate(inner)
    except Exception as e:
        logger.debug("Structured reasoning parse attempt 2 failed: %s", e)

    return None


async def _generate_legacy(
    prospect: dict[str, Any],
    sender_profile: dict[str, Any],
    voice_signature: dict[str, Any],
    campaign_context: dict[str, Any],
    conversation_history: list[dict[str, Any]],
    followup_number: int,
    prospect_analysis: dict[str, Any] | None,
    engagement_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Generate using legacy single-stage prompt (fallback)."""
    logger.info("Using legacy prompts for follow-up #%d (v63 not found)", followup_number)

    # Extract voice details
    voice_vocab = voice_signature.get("vocabulary_preferences", [])
    if isinstance(voice_vocab, list):
        voice_vocab = ", ".join(voice_vocab)

    prospect_name = first_name(prospect.get("name"), "there")
    prospect_intelligence = _build_intelligence_text(prospect_analysis)

    # User's outbound booking link (from campaign config, set via edit_campaign)
    booking_link = campaign_context.get("booking_link", "")
    if booking_link and followup_number >= 2:
        booking_link_section = (
            f"Available: {booking_link} — if it fits naturally, "
            "weave the booking link into the CTA to make scheduling easy."
        )
    elif booking_link:
        booking_link_section = (
            f"Available: {booking_link} — only include if the conversation "
            "has progressed enough to suggest a call (follow-up #2+)."
        )
    else:
        booking_link_section = "No booking link configured."

    engagement_text = _format_engagement_history(engagement_history)
    prompt = FOLLOWUP_PROMPT_LEGACY.format(
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
        campaign_target=campaign_context.get("target_description", ""),
        relevance_hook=campaign_context.get("relevance_hook", ""),
        booking_link_section=booking_link_section,
        conversation_history=_format_conversation_history(conversation_history),
        engagement_section=engagement_text,
        followup_number=followup_number,
    )

    llm_client = LLMClient()
    result = await llm_client.generate_json(
        prompt, schemas.MESSAGE, system=FOLLOWUP_SYSTEM_LEGACY, temperature=0.7,
    )
    message = result["message"]
    reasoning = result.get("reasoning", "")

    # Clean up
    message = message.strip().strip('"').strip("'").strip()

    # Enforce character limit (LLM-based shortening with retry)
    message = await shorten_to_limit(message, followup_char_limit(followup_number))

    return {"message": message, "reasoning": reasoning}
