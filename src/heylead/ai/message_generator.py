"""Personalized message generator — the core product engine.

Generates LinkedIn invitation messages that:
1. Sound like the user (voice signature matching)
2. Reference prospect-specific details
3. Stay under 200 chars (regular LinkedIn limit)
4. Follow sales methodology from v63 production prompts
5. Return structured reasoning for debugging/quality analysis

v63 prompt features:
- Pain-first messaging pattern
- No company names (theirs or ours)
- No industries/domains/roles/titles
- No links in first message
- CTA library with soft nudges
- No "noticed", "saw", "impressed by", no em dashes
"""

from __future__ import annotations

from ..textutil import first_name

import logging
from typing import Any

from . import schemas
from .length_fixer import shorten_to_limit
from .llm import LLMClient
from .news_service import get_prospect_news
from .prompt_loader import (
    build_context_block,
    get_prompt_temperature,
    has_prompt,
    load_expertise_map,
    render_prompt,
)

logger = logging.getLogger(__name__)

# Campaign types that use a dedicated first-touch prompt. The default prompt
# forbids naming companies, which is right for cold outreach and wrong for a
# job search, where the company and role are the substance of the message.
_PROMPT_BY_CAMPAIGN_TYPE = {
    "job_search": "outreach_job_search",
}


def select_outreach_prompt(campaign_config: Any) -> str:
    """Pick the first-touch prompt for a campaign.

    Accepts the config as a dict or as the raw config_json string. Anything
    unrecognised falls back to the default prompt rather than guessing.
    """
    import json as _json

    if isinstance(campaign_config, str):
        try:
            campaign_config = _json.loads(campaign_config or "{}")
        except (ValueError, TypeError):
            campaign_config = {}
    if not isinstance(campaign_config, dict):
        campaign_config = {}

    campaign_type = str(campaign_config.get("campaign_type") or "").strip().lower()
    prompt = _PROMPT_BY_CAMPAIGN_TYPE.get(campaign_type, "")
    if prompt:
        if has_prompt(prompt):
            return prompt
        logger.warning("Prompt %s missing — falling back to default", prompt)
        return "outreach_invitation"

    # No campaign_type route — campaign intent picks the stance variant
    # (falls back to the base template when the variant is unauthored).
    from .intent import resolve_intent, select_prompt
    return select_prompt("outreach_invitation", resolve_intent(campaign_config))

# ──────────────────────────────────────────────
# Legacy prompts — fallback if v63 JSON files are missing
# ──────────────────────────────────────────────

MESSAGE_SYSTEM_LEGACY = """You are an expert at writing LinkedIn connection request messages. You write brief, genuine, personal messages that get accepted.

Key principles:
- Sound like a real person, not a bot or salesperson
- Reference the prospect's situation loosely — paraphrase, never quote exact words
- Never open with the prospect's name (e.g., "Cole," or "Hi Arina,"). Start with substance.
- Never mention company names (theirs or yours), place names, or product names
- Never use buzzy adjectives: "impressive", "killing it", "serious move", "massive", "cool", "game-changing"
- Never use salesy phrases like "leverage", "synergy", "exciting opportunity", "I noticed"
- Ask about their daily experience, not about AI or industry trends
- Keep it under 200 characters
- End with a question about THEIR daily experience, not a pitch or meeting request
- Match the sender's voice and tone exactly
- No template openers: "Spot on.", "Nothing beats...", "Love this."
- No generic flattery: "cool to see someone who...", "impressive work at..."
- Do NOT pitch your product or mention what you do in the invite

You output a JSON object with two fields: "reasoning" and "message".
No markdown, no code fences, no explanation outside the JSON."""

MESSAGE_PROMPT_LEGACY = """Write a LinkedIn connection request message.

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
Location: {prospect_location}

## PROSPECT INTELLIGENCE (from analysis)
{prospect_intelligence}

## CAMPAIGN CONTEXT
Target: {campaign_target}
Relevance: {relevance_hook}

## CONSTRAINTS
- MAXIMUM 200 characters (this is a hard limit — LinkedIn enforces it)
- Use the sender's voice — match their tone, formality, and vocabulary
- Do NOT open with prospect's name. Start with an observation or statement.
- Do NOT mention any company names, place names, or product names
- Do NOT pitch your product or describe what you do
- Soft CTA: end with a question about THEIR daily experience. NEVER say "let's hop on a call"
- NO emojis unless the sender's voice uses them
- NO "I noticed you..." or "I came across your profile..."
- NO buzzy adjectives: "impressive", "killing it", "serious move", "massive", "hands-on", "in the weeds", "hits differently"
- NO generic AI/trend questions: "How do you see AI changing..."

## ANTI-PATTERNS (NEVER do these)
- Do not mirror exact words from prospect's profile or posts
- Do not use quotation marks around referenced content
- Do not use template openers or generic flattery

## EXAMPLES
BAD: "Cole, QuotaPath's killing it. Where do you see AI impacting sales workflows soon?"
GOOD: "Retail tends to push content needs up fast once things start growing. Do you still handle that creative load yourself, or do you have support for it?"

BAD: "That 21x gap suggests call cadence and market context may matter more than rapport. How much hands-on work does it take to make those habits repeatable?"
WHY BAD: Points at a specific stat from their content, then wraps it in sales-methodology jargon the sender would not say.
GOOD: "Keeping a big gap between the best and the rest on the phones is one thing. Do you still run that rhythm yourself, or does the team carry it?"

## OUTPUT
Return ONLY valid JSON (no markdown):
{{
    "reasoning": "brief explanation of your approach — what angle you chose and why",
    "message": "the actual message text, under 200 characters"
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
    # Deep profile hooks (education, certs, publications)
    edu_hooks = prospect_analysis.get("education_hooks")
    if edu_hooks and isinstance(edu_hooks, list):
        parts.append(f"Education: {', '.join(edu_hooks[:2])}")
    cert_hooks = prospect_analysis.get("certification_hooks")
    if cert_hooks and isinstance(cert_hooks, list):
        parts.append(f"Certifications: {', '.join(cert_hooks[:2])}")
    if prospect_analysis.get("has_publications"):
        parts.append("Published author — thought leader angle available")
    # Signal context injection (from signal_activator.py)
    signal_ctx = prospect_analysis.get("signal_context")
    if signal_ctx and isinstance(signal_ctx, dict):
        parts.append("")  # blank line separator
        parts.append("SIGNAL CONTEXT (reference naturally, don't be creepy):")
        if signal_ctx.get("engagement_hook"):
            parts.append(f"Suggested angle: {signal_ctx['engagement_hook']}")
        if signal_ctx.get("signal_summary"):
            summary = signal_ctx["signal_summary"][:150]
            parts.append(f"Context: {summary}")
        if signal_ctx.get("signal_angle"):
            parts.append(f"Approach: {signal_ctx['signal_angle'].replace('_', ' ')}")
        if signal_ctx.get("DO_NOT"):
            parts.append(f"Important: {signal_ctx['DO_NOT']}")
    return "\n".join(parts) if parts else "No analysis available — use profile data only."


def _format_prior_conversation(conversation_history: list[dict] | None) -> str:
    """Format prior conversation history for prompt injection."""
    if not conversation_history:
        return ""
    lines = []
    for msg in conversation_history:
        role_label = "[YOU]" if msg.get("role") == "sdr" else "[PROSPECT]"
        lines.append(f"{role_label}: {msg.get('text', '')}")
    return "\n".join(lines)


async def generate_message(
    prospect: dict[str, Any],
    sender_profile: dict[str, Any],
    voice_signature: dict[str, Any],
    campaign_context: dict[str, Any],
    prospect_analysis: dict[str, Any] | None = None,
    campaign_ctx: dict[str, Any] | None = None,
    conversation_history: list[dict] | None = None,
    brief: Any = None,
    max_chars: int | None = None,
    action_timeline: str | None = None,
) -> dict[str, Any]:
    """Generate a personalized LinkedIn invitation message.

    Args:
        prospect: Prospect profile data (name, title, company, headline, etc.)
        sender_profile: User's own profile data
        voice_signature: Voice analysis results (tone, vocabulary, patterns)
        campaign_context: Campaign ICP and target description
        prospect_analysis: Optional prospect analysis results
        campaign_ctx: Optional campaign context (offerings, case_studies, social_proofs)
        conversation_history: Optional prior conversation messages from LinkedIn.
            When provided, the AI will acknowledge the existing relationship.
        max_chars: Connection-note cap (200 regular, 300 Sales Nav).

    Returns:
        Dict with "message" (str, within max_chars) and "reasoning" (str).
    """
    from ..constants import INVITATION_NOTE_MAX_CHARS

    if max_chars is None:
        max_chars = INVITATION_NOTE_MAX_CHARS
    # Route through backend if in backend mode and no local LLM key
    from ..config import has_local_llm_key, is_backend_mode
    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client
        client = get_linkedin_client()
        try:
            msg = await client.generate_message(
                sender_profile, prospect, voice_signature, campaign_context,
                prospect_analysis=prospect_analysis,
                conversation_history=conversation_history,
            )
            if isinstance(msg, str) and len(msg) > max_chars:
                msg = await shorten_to_limit(msg, max_chars)
            # Backend returns plain str — wrap in dict for compatibility
            return {"message": msg, "reasoning": ""}
        finally:
            await client.close()

    # ── Try v63 prompts ──
    outreach_prompt = select_outreach_prompt(campaign_context)
    if has_prompt(outreach_prompt) and has_prompt("outreach_system"):
        logger.debug("Using v63 prompts for invitation message (%s)", outreach_prompt)
        # A job-search message describes the sender, so it needs the stored
        # facts about them. Without these the model invents a career.
        expertise_map = await load_expertise_map()

        ctx = build_context_block(
            sender=sender_profile,
            prospect=prospect,
            campaign_config=campaign_context,
            voice=voice_signature,
            campaign_context=campaign_ctx,
            analysis=prospect_analysis,
            max_chars=max_chars,
            expertise_map=expertise_map,
            brief=brief,
        )
        # Optional: enrich trigger_info with recent news
        try:
            news = await get_prospect_news(prospect, sender_profile, campaign_context)
            if news:
                ctx["trigger_info"] = (ctx.get("trigger_info", "") + "\n\n" + news).strip()
        except Exception:
            pass  # Silently skip — news is optional for invitations

        from .intent import resolve_intent, select_prompt
        _intent = resolve_intent(campaign_context or {})
        system_name = select_prompt("outreach_system", _intent)
        system = render_prompt(system_name, ctx)
        prompt = render_prompt(outreach_prompt, ctx)

        # Inject prior conversation history if available
        prior_convo = _format_prior_conversation(conversation_history)
        if prior_convo:
            prompt += (
                "\n\n## PREVIOUS CONVERSATION\n"
                "You have previously exchanged messages with this person:\n"
                f"{prior_convo}\n\n"
                "IMPORTANT: Acknowledge the prior relationship naturally. "
                "Do NOT introduce yourself as if meeting for the first time. "
                "Reference your earlier exchange casually."
            )
        from ..services.action_timeline import append_action_timeline
        prompt = append_action_timeline(prompt, action_timeline or "")

        temp = get_prompt_temperature(outreach_prompt)

        client = LLMClient()
        raw = await client.generate(prompt, system=system, temperature=temp)

        # v63 returns plain text (not JSON) — wrap it
        message = raw.strip().strip('"').strip("'").strip()
        reasoning = f"v63 intent={_intent} prompts={system_name}+{outreach_prompt}"

    else:
        # ── Fallback to legacy prompts ──
        logger.debug("Using legacy prompts for invitation message (v63 not found)")

        voice_vocab = voice_signature.get("vocabulary_preferences", [])
        if isinstance(voice_vocab, list):
            voice_vocab = ", ".join(voice_vocab)

        prospect_name = first_name(prospect.get("name"), "there")
        prospect_intelligence = _build_intelligence_text(prospect_analysis)

        prompt = MESSAGE_PROMPT_LEGACY.format(
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
            prospect_location=prospect.get("location", ""),
            prospect_intelligence=prospect_intelligence,
            campaign_target=campaign_context.get("target_description", ""),
            relevance_hook=campaign_context.get("relevance_hook", ""),
        )

        # Inject prior conversation history if available
        prior_convo = _format_prior_conversation(conversation_history)
        if prior_convo:
            prompt += (
                "\n\n## PREVIOUS CONVERSATION\n"
                "You have previously exchanged messages with this person:\n"
                f"{prior_convo}\n\n"
                "IMPORTANT: Acknowledge the prior relationship naturally. "
                "Do NOT introduce yourself as if meeting for the first time. "
                "Reference your earlier exchange casually."
            )

        client = LLMClient()
        result = await client.generate_json(
            prompt, schemas.MESSAGE, system=MESSAGE_SYSTEM_LEGACY, temperature=0.7,
        )
        message = result["message"]
        reasoning = result.get("reasoning", "")

    # Clean up — strip quotes, whitespace, etc.
    message = message.strip().strip('"').strip("'").strip()

    # Strip name openers (LLM sometimes ignores the "no name opener" instruction)
    message = _strip_name_opener(message, prospect)

    # Enforce char limit — LLM shortening with truncation fallback
    if len(message) > max_chars:
        message = await shorten_to_limit(message, max_chars)

    return {"message": message, "reasoning": reasoning}


def _strip_name_opener(message: str, prospect: dict[str, Any]) -> str:
    """Remove prospect name from the start of the message if present."""
    import re

    prospect_name = prospect.get("name", "")
    if not prospect_name or not message:
        return message

    # first_name() rather than split()[0]: a whitespace-only name is truthy
    # and has no words, so the index raised.
    prospect_first = first_name(prospect_name, "")
    if not prospect_first or len(prospect_first) < 2:
        return message

    # Match: "Name," or "Name " at start (case insensitive)
    pattern = rf'^{re.escape(prospect_first)}[,!.\s]+\s*'
    cleaned = re.sub(pattern, '', message, flags=re.IGNORECASE).strip()

    # Capitalize first letter after stripping
    if cleaned and cleaned[0].islower():
        # Keep lowercase if it looks intentional (e.g., "your" → keep as-is is fine)
        pass

    return cleaned if cleaned else message
