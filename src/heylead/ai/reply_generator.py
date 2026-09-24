"""Reply generator — sentiment-aware LinkedIn DM replies.

Generates personalized replies to prospect messages based on sentiment:
- positive: Advance toward meeting/call (with optional booking link)
- question: Answer contextually, then soft CTA
- negative: Graceful close, leave door open
- neutral: Build rapport, ask clarifying question

Pipeline mirrors followup_generator.py:
1. Select sentiment-specific prompt
2. Generate reply via LLM (with voice + prospect intelligence)
3. Validate + Fix (caller handles via message_validator)

Backend routing: if in backend mode with no local LLM key,
proxies through the backend API (same pattern as followup_generator.py).
"""

from __future__ import annotations

from ..textutil import first_name

import logging
import re
from typing import Any

from . import schemas
from .length_fixer import shorten_to_limit
from .llm import LLMClient
from .prompt_loader import (
    load_expertise_map,
    build_context_block,
    format_transcript,
    get_prompt_temperature,
    has_prompt,
    render_prompt,
    unanswered_prospect_turns,
)
from .sentiment import detect_calendar_intent, detect_calendar_url
from .voice_block import voice_prompt_block

logger = logging.getLogger(__name__)

# Scheduling-loop keywords — detect when conversation is stuck on scheduling
_SCHEDULING_LOOP_KEYWORDS = re.compile(
    r"\b(?:does\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|next\s+week|tomorrow)"
    r"|how\s+(?:about|does)\s+\d{1,2}\s*(?:am|pm)"
    r"|what\s+(?:time|day)\s+works"
    r"|(?:tuesday|wednesday|thursday|friday|monday|saturday|sunday)\s+(?:at|afternoon|morning)"
    r"|(?:2|3|4|5|6|7|8|9|10|11|12)\s*(?:am|pm)\s+(?:on|work|sound)"
    r"|(?:does|how\s+about|what\s+about)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)"
    r")\b",
    re.IGNORECASE,
)


def _detect_scheduling_loop(conversation_history: list[dict[str, Any]]) -> bool:
    """Detect if the last 2+ turns have been back-and-forth scheduling talk."""
    recent = conversation_history[-6:] if len(conversation_history) >= 6 else conversation_history
    scheduling_turns = sum(
        1 for m in recent
        if _SCHEDULING_LOOP_KEYWORDS.search(m.get("text", ""))
    )
    return scheduling_turns >= 2


def _apply_scheduling_loop_override(
    reply_text: str,
    sentiment: str,
    conversation_history: list[dict[str, Any]],
    booking_link: str,
) -> tuple[str, str]:
    """Keep the prospect's words; route a loop to its own strategy, not calendar.

    The calendar prompt assumes they already shared a booking link. A loop is
    the opposite: we have been proposing times. Append a directive and switch
    to ``scheduling_loop`` so the model addresses what they said, then shares
    our link.
    """
    if sentiment in ("calendar", "negative", "opt_out"):
        return reply_text, sentiment
    if not _detect_scheduling_loop(conversation_history):
        return reply_text, sentiment
    if booking_link:
        note = (
            f"[Scheduling has gone back and forth — share your booking link: {booking_link}]"
        )
    else:
        note = (
            "[Scheduling has gone back and forth — stop proposing times; "
            "ask the prospect to share a calendar link]"
        )
    return f"{reply_text}\n{note}", "scheduling_loop"

REPLY_MAX_CHARS = 500
NEGATIVE_MAX_CHARS = 200  # Graceful closes should be very short

REPLY_SYSTEM = """You are an expert at writing LinkedIn DM replies. You craft genuine, personal responses that match the sender's voice and advance the relationship appropriately based on the prospect's sentiment.

Key principles:
- Sound like a real person, not a bot or salesperson
- DIRECTLY ADDRESS what the prospect said — never ignore their message
- Paraphrase what the prospect said — never mirror their exact words back at them
- Match the sender's voice and tone exactly
- Adapt your strategy based on the prospect's sentiment
- Keep replies concise (under 500 characters)
- Relate from experience when possible
- Keep tone understated — no buzzy adjectives, no generic flattery
- Never use salesy phrases like "leverage", "synergy", "exciting opportunity"
- Never try to overcome objections or be pushy
- Never use template openers: "Spot on.", "Love this.", "Agreed."
- Never use cliche phrases: "a lifesaver", "running the whole show"
- NEVER sign off with "- YourName", "Best, YourName", "Cheers", "Regards", "Sincerely", or ANY email-style signature. LinkedIn DMs don't sign off. Ending with a signature is a bot tell.
- LANGUAGE RULE: Reply in the SAME language the prospect used in their latest message. If they wrote in Russian, reply in Russian. If they wrote in French, reply in French. Never switch languages mid-conversation.

You output a JSON object with two fields: "reasoning" and "message".
No markdown, no code fences, no explanation outside the JSON."""

REPLY_PROMPT_POSITIVE = """Write a LinkedIn DM reply to a prospect who expressed INTEREST.

## SENDER (you are writing AS this person)
Name: {sender_name}
Title: {sender_title}
Company: {sender_company}
{voice_block}

## PROSPECT
Name: {prospect_name}
Title: {prospect_title}
Company: {prospect_company}
Headline: {prospect_headline}

## PROSPECT INTELLIGENCE (from analysis)
{prospect_intelligence}

## CAMPAIGN CONTEXT
Target: {campaign_target}
Relevance: {relevance_hook}

## CONVERSATION HISTORY (oldest first)
{conversation_history}

## THEIR LATEST MESSAGE (positive sentiment — they're interested)
"{reply_text}"

## BOOKING LINK
{booking_link_section}

## STRATEGY
The prospect expressed interest. Your goal:
1. Acknowledge their interest genuinely — show you're excited but not desperate
2. Propose a concrete next step (call, meeting, quick demo)
3. If a booking link is available, weave it in naturally
4. Keep it warm, personal, and action-oriented

## CONSTRAINTS
- Match the sender's voice exactly
- DIRECTLY reference what they said in their reply
- Keep under {max_chars} characters
- First name only for the prospect
- NO emojis unless the sender's voice uses them
- NO salesy language, no desperation
- Make scheduling easy — one clear CTA

## OUTPUT
Return ONLY valid JSON (no markdown):
{{
    "reasoning": {{
        "reply_hook": "what they said that you're responding to",
        "strategy": "how you're advancing toward a meeting",
        "cta_choice": "booking_link|suggest_time|ask_availability"
    }},
    "message": "the actual reply text"
}}"""

REPLY_PROMPT_QUESTION = """Write a LinkedIn DM reply to a prospect who asked a QUESTION.

## SENDER (you are writing AS this person)
Name: {sender_name}
Title: {sender_title}
Company: {sender_company}
{voice_block}

## PROSPECT
Name: {prospect_name}
Title: {prospect_title}
Company: {prospect_company}
Headline: {prospect_headline}

## PROSPECT INTELLIGENCE (from analysis)
{prospect_intelligence}

## CAMPAIGN CONTEXT
Target: {campaign_target}
Relevance: {relevance_hook}

## CONVERSATION HISTORY (oldest first)
{conversation_history}

## THEIR LATEST MESSAGE (question — they want more info)
"{reply_text}"

## STRATEGY
The prospect asked a question. Your goal:
1. Answer their specific question directly and honestly
2. Use your knowledge of the campaign context to give a relevant answer
3. End with a soft CTA — offer to elaborate, share a resource, or suggest a call
4. Do NOT dodge the question or redirect to "hop on a call" without answering first

## CONSTRAINTS
- Match the sender's voice exactly
- ANSWER their question — do not deflect
- Keep under {max_chars} characters
- First name only for the prospect
- NO emojis unless the sender's voice uses them
- NO salesy language
- Be helpful and straightforward

## OUTPUT
Return ONLY valid JSON (no markdown):
{{
    "reasoning": {{
        "question_summary": "what they're actually asking",
        "answer_approach": "how you're addressing it",
        "cta_choice": "offer_call|share_resource|ask_followup"
    }},
    "message": "the actual reply text"
}}"""

REPLY_PROMPT_WRONG_PERSON = """Write a short LinkedIn DM apology — we messaged the wrong person.

## SENDER (you are writing AS this person)
Name: {sender_name}
Title: {sender_title}
Company: {sender_company}
{voice_block}

## PROSPECT
Name: {prospect_name}

## CONVERSATION HISTORY (oldest first)
{conversation_history}

## THEIR LATEST MESSAGE (they asked if this was meant for them)
"{reply_text}"

## STRATEGY
We checked: they are not the buyer we were looking for. Your ONLY goal:
1. Acknowledge the mix-up briefly and apologize
2. Do not pitch, explain the product, or ask who we should talk to
3. Close the conversation — one or two sentences
4. Reply in the SAME language they used

## CONSTRAINTS
- Match the sender's voice exactly
- MAXIMUM {max_chars} characters
- First name only for the prospect
- NO emojis
- NO pitch, NO CTA, NO "who handles this on your team?"

## OUTPUT
Return ONLY valid JSON (no markdown):
{{
    "reasoning": {{
        "tone": "apologetic and brief",
        "close_style": "wrong_contact"
    }},
    "message": "the actual reply text"
}}"""

REPLY_PROMPT_CONFIRM_FIT = """Write a LinkedIn DM confirming we meant to write to this person.

## SENDER (you are writing AS this person)
Name: {sender_name}
Title: {sender_title}
Company: {sender_company}
{voice_block}

## PROSPECT
Name: {prospect_name}
Title: {prospect_title}
Company: {prospect_company}
Headline: {prospect_headline}

## CAMPAIGN CONTEXT
Target: {campaign_target}

## CONVERSATION HISTORY (oldest first)
{conversation_history}

## THEIR LATEST MESSAGE (they asked if this was meant for them)
"{reply_text}"

## STRATEGY
We checked: they ARE the right person. Your goal:
1. Confirm you wrote to them specifically — not a mass blast
2. In one sentence, say who you look for using the campaign target
3. Do not hard-pitch. Invite a short reply if they want the why.
4. Reply in the SAME language they used

## CONSTRAINTS
- Match the sender's voice exactly
- Keep under {max_chars} characters
- First name only for the prospect
- NO emojis unless the sender's voice uses them
- NO booking link, NO product dump

## OUTPUT
Return ONLY valid JSON (no markdown):
{{
    "reasoning": {{
        "question_summary": "they asked if the message was for them",
        "answer_approach": "confirm fit and name who we look for"
    }},
    "message": "the actual reply text"
}}"""

REPLY_PROMPT_NEGATIVE = """Write a LinkedIn DM reply to a prospect who said they're NOT INTERESTED.

## SENDER (you are writing AS this person)
Name: {sender_name}
Title: {sender_title}
Company: {sender_company}
{voice_block}

## PROSPECT
Name: {prospect_name}

## CONVERSATION HISTORY (oldest first)
{conversation_history}

## THEIR LATEST MESSAGE (negative sentiment — not interested)
"{reply_text}"

## STRATEGY
The prospect is not interested. Your ONLY goal:
1. Acknowledge their position with genuine respect
2. Close gracefully — no pushback, no objection handling, no "but actually..."
3. Leave the door open with ONE brief line: "Happy to reconnect if things change"
4. Keep it VERY SHORT — 1-2 sentences maximum

## CONSTRAINTS
- Match the sender's voice exactly
- MAXIMUM {max_chars} characters (keep it very short!)
- First name only for the prospect
- NO emojis
- ABSOLUTELY NO attempt to sell, persuade, or overcome objections
- NO "I understand, but..." or "What if..."
- Be genuinely respectful — they said no, respect it

## OUTPUT
Return ONLY valid JSON (no markdown):
{{
    "reasoning": {{
        "tone": "respectful and brief",
        "close_style": "graceful_exit"
    }},
    "message": "the actual reply text"
}}"""

REPLY_PROMPT_ENGAGED = """Write a LinkedIn DM reply to a prospect who is ENGAGED in conversation — they answered your question or shared their business context, but have NOT expressed interest in your product.

## SENDER (you are writing AS this person)
Name: {sender_name}
Title: {sender_title}
Company: {sender_company}
{voice_block}

## PROSPECT
Name: {prospect_name}
Title: {prospect_title}
Company: {prospect_company}
Headline: {prospect_headline}

## PROSPECT INTELLIGENCE (from analysis)
{prospect_intelligence}

## CAMPAIGN CONTEXT
Target: {campaign_target}
Relevance: {relevance_hook}

## CONVERSATION HISTORY (oldest first)
{conversation_history}

## THEIR LATEST MESSAGE (engaged — answering your question or sharing context)
"{reply_text}"

## STRATEGY
{engaged_strategy}

## CRITICAL RULES
{engaged_rules}
- DO reference specific details from their message (names, numbers, challenges they mentioned)
- Keep it short — 2-3 sentences. A question, not a speech.

## CONSTRAINTS
- Match the sender's voice exactly
- Keep under {max_chars} characters
- First name only for the prospect
- NO emojis unless the sender's voice uses them
- NO salesy language whatsoever
- Sound genuinely curious, not strategically curious

## OUTPUT
Return ONLY valid JSON (no markdown):
{{
    "reasoning": {{
        "what_they_shared": "key details from their reply",
        "empathy_angle": "how you're validating what they said",
        "followup_question": "what you're asking next and why",
        "cta_choice": "deepen_conversation"
    }},
    "message": "the actual reply text"
}}"""

REPLY_PROMPT_NEUTRAL = """Write a LinkedIn DM reply to a prospect who gave a NEUTRAL response.

## SENDER (you are writing AS this person)
Name: {sender_name}
Title: {sender_title}
Company: {sender_company}
{voice_block}

## PROSPECT
Name: {prospect_name}
Title: {prospect_title}
Company: {prospect_company}
Headline: {prospect_headline}

## PROSPECT INTELLIGENCE (from analysis)
{prospect_intelligence}

## CAMPAIGN CONTEXT
Target: {campaign_target}
Relevance: {relevance_hook}

## CONVERSATION HISTORY (oldest first)
{conversation_history}

## THEIR LATEST MESSAGE (neutral — acknowledgment without clear intent)
"{reply_text}"

## STRATEGY
The prospect gave a neutral response (e.g., "thanks", "cool", "got it").
Your goal:
1. Build on the small signal — they did respond, which is positive
2. Reference something from the conversation or their profile
3. Ask a clarifying question to gauge their interest level
4. Keep it light and genuine — do NOT assume they're interested or disinterested

## CONSTRAINTS
- Match the sender's voice exactly
- DIRECTLY reference what they said
- Keep under {max_chars} characters
- First name only for the prospect
- NO emojis unless the sender's voice uses them
- NO salesy language
- Keep it conversational and low-pressure

## OUTPUT
Return ONLY valid JSON (no markdown):
{{
    "reasoning": {{
        "reply_hook": "what they said that you're building on",
        "angle": "how you're gauging interest without pressure",
        "cta_choice": "clarifying_question|shared_interest|light_value"
    }},
    "message": "the actual reply text"
}}"""

REPLY_PROMPT_SCHEDULING_LOOP = """Write a LinkedIn DM reply that ends a scheduling back-and-forth.

## SENDER (you are writing AS this person)
Name: {sender_name}
{voice_block}

## CONVERSATION HISTORY (oldest first)
{conversation_history}

## THEIR LATEST MESSAGE
"{reply_text}"

## YOUR BOOKING LINK
{booking_link}

## STRATEGY
Scheduling has gone back and forth. Stop proposing times. Share your booking link.
Do NOT claim the prospect shared a calendar — they offered a time or stalled.
Engage with what they actually said, then send the link so they can book.
- Keep it to 2-3 sentences
- Do NOT propose another day or time
- Do NOT ask "does Tuesday work?"

## CONSTRAINTS
- Match the sender's voice exactly
- MAXIMUM {max_chars} characters
- NO emojis

## OUTPUT
Return ONLY valid JSON (no markdown):
{{
    "reasoning": {{
        "strategy": "end_scheduling_loop",
        "cta_choice": "share_your_booking_link"
    }},
    "message": "the actual reply text"
}}"""

REPLY_PROMPT_CALENDAR = """Write a LinkedIn DM reply to a prospect who shared their CALENDAR/BOOKING LINK.

## SENDER (you are writing AS this person)
Name: {sender_name}
{voice_block}

## CONVERSATION HISTORY (oldest first)
{conversation_history}

## THEIR LATEST MESSAGE (shared a booking link)
"{reply_text}"

## STRATEGY
The prospect shared their calendar or booking link. This means they WANT to meet.
Your ONLY goal: acknowledge the link and confirm you'll book a time.
- Keep it to 1-2 sentences max
- Do NOT ask about their availability — they already shared their calendar
- Do NOT suggest alternative times — just use their link
- Do NOT ask scheduling questions like "Does Tuesday work?"
- Simply thank them and say you'll book

## CONSTRAINTS
- Match the sender's voice exactly
- MAXIMUM {max_chars} characters (keep it very short!)
- NO emojis
- NO scheduling questions — they already solved scheduling by sharing their link

## OUTPUT
Return ONLY valid JSON (no markdown):
{{
    "reasoning": {{
        "strategy": "acknowledge_calendar_link",
        "cta_choice": "book_on_their_calendar"
    }},
    "message": "the actual reply text"
}}"""

# Map sentiment to prompt template
_PROMPT_MAP = {
    "positive": REPLY_PROMPT_POSITIVE,
    "question": REPLY_PROMPT_QUESTION,
    "negative": REPLY_PROMPT_NEGATIVE,
    "neutral": REPLY_PROMPT_NEUTRAL,
    "engaged": REPLY_PROMPT_ENGAGED,
    "calendar": REPLY_PROMPT_CALENDAR,
    "scheduling_loop": REPLY_PROMPT_SCHEDULING_LOOP,
    "wrong_person": REPLY_PROMPT_WRONG_PERSON,
    "confirm_fit": REPLY_PROMPT_CONFIRM_FIT,
}


def _format_conversation_history(messages: list[dict]) -> str:
    """Format conversation history for the prompt.

    Delegates to the shared transcript renderer so the legacy sentiment
    prompts, the v63 prompt and the backend reply prompts all show the same
    thread shape (9 Sep reply-context incident).
    """
    return format_transcript(messages)


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


async def generate_reply(
    prospect: dict[str, Any],
    sender_profile: dict[str, Any],
    voice_signature: dict[str, Any],
    campaign_context: dict[str, Any],
    conversation_history: list[dict[str, Any]],
    reply_text: str,
    sentiment: str,
    prospect_analysis: dict[str, Any] | None = None,
    booking_link: str = "",
    prospect_calendar_url: str = "",
) -> dict[str, Any]:
    """Generate a sentiment-aware reply to a prospect's message.

    Args:
        prospect: Prospect profile data (name, title, company, headline)
        sender_profile: User's own profile data
        voice_signature: Voice analysis results (tone, vocabulary, patterns)
        campaign_context: Campaign ICP and target description
        conversation_history: Previous messages in the thread (role + text)
        reply_text: The prospect's latest message text
        sentiment: Classified sentiment (positive, question, negative, neutral)
        prospect_analysis: Cached prospect intelligence (tone + pain points)
        booking_link: The USER's outbound calendar/booking URL (from campaign config,
            set via edit_campaign). Used by the LLM to weave into positive replies.
            NOT the prospect's calendar URL — that is detected separately via
            detect_calendar_url() and handled as prospect_calendar_url.

    Returns:
        Dict with "message" (str) and "reasoning" (dict).
    """
    # Force calendar sentiment if we have a stored prospect calendar URL
    # (detected by check_replies and stored in outreach.next_action)
    if prospect_calendar_url:
        sentiment = "calendar"
        if prospect_calendar_url not in reply_text:
            reply_text = f"{reply_text}\n[Prospect's calendar: {prospect_calendar_url}]"
        logger.info("Using stored prospect calendar URL: %s", prospect_calendar_url)

    # Route through backend if in backend mode and no local LLM key
    from ..config import has_local_llm_key, is_backend_mode
    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client
        client = get_linkedin_client()
        try:
            return await client.generate_reply(
                sender=sender_profile,
                prospect=prospect,
                voice=voice_signature,
                campaign_context=campaign_context,
                conversation_history=conversation_history,
                reply_text=reply_text,
                sentiment=sentiment,
                booking_link=booking_link,
                prospect_calendar_url=prospect_calendar_url,
            )
        finally:
            await client.close()

    # Detect PROSPECT's inbound calendar link (distinct from the user's
    # outbound booking_link parameter above) → use short calendar-specific reply
    prospect_calendar = detect_calendar_url(reply_text)
    if prospect_calendar:
        sentiment = "calendar"
        logger.info("Prospect shared calendar link: %s — using calendar reply template", prospect_calendar)

    # Fallback: detect calendar intent without URL (e.g., "check my calendar")
    # If prospect expressed calendar intent AND we have a stored calendar URL, force calendar sentiment
    if sentiment != "calendar" and detect_calendar_intent(reply_text) and prospect_calendar_url:
        sentiment = "calendar"
        logger.info("Calendar intent detected ('%s') with stored URL: %s", reply_text[:50], prospect_calendar_url)

    # Detect scheduling loop — append a directive; do not overwrite their words
    # or reuse the calendar prompt (that one assumes they already shared a link).
    reply_text, sentiment = _apply_scheduling_loop_override(
        reply_text, sentiment, conversation_history, booking_link,
    )
    if sentiment == "scheduling_loop":
        logger.info("Scheduling loop detected — keeping prospect text, using loop template")

    # Determine max chars (negative/calendar replies should be short)
    max_chars = NEGATIVE_MAX_CHARS if sentiment in (
        "negative", "calendar", "wrong_person",
    ) else REPLY_MAX_CHARS

    # ── Try v63 response prompt for positive/question/neutral ──
    # v63 has sophisticated 12-principle response logic with WARM-ENOUGH SIGNAL
    # Keep HeyLead's negative-specific prompt (v63 lacks sentiment-specific negative handling)
    use_v63 = (
        sentiment not in (
            "negative", "calendar", "scheduling_loop",
            "wrong_person", "confirm_fit",
        )
        and has_prompt("outreach_response")
        and has_prompt("outreach_system")
    )

    from .brief_builder import build_message_brief, render_brief_block
    from .intent import intent_frame, resolve_intent, select_prompt
    _intent = resolve_intent(campaign_context)
    message_brief = build_message_brief(
        intent=_intent,
        campaign_config=campaign_context,
        campaign_ctx=campaign_context,
        prospect=prospect,
        analysis=prospect_analysis,
        touch="reply",
    )
    ctx = build_context_block(
        channel="reply",
        sender=sender_profile,
        prospect=prospect,
        campaign_config={
            **campaign_context,
            "booking_link": booking_link,
        },
        voice=voice_signature,
        campaign_context=campaign_context,
        history=conversation_history,
        analysis=prospect_analysis,
        max_chars=max_chars,
        expertise_map=await load_expertise_map(),
        brief=message_brief,
        reply_text=reply_text,
    )
    if use_v63:
        logger.debug("Using v63 response prompt for %s reply", sentiment)
        system_name = select_prompt("outreach_system", _intent)
        system = render_prompt(system_name, ctx)
        response_name = select_prompt("outreach_response", _intent)
        prompt = render_prompt(response_name, ctx)
        temp = get_prompt_temperature(response_name)

        llm_client = LLMClient()
        raw = await llm_client.generate(prompt, system=system, temperature=temp)

        # v63 returns plain text, not JSON.
        message = raw.strip().strip('"').strip("'").strip()
        reasoning = ""

    else:
        # ── Fallback to legacy sentiment-specific prompts ──
        logger.debug("Using legacy %s reply prompt", sentiment)

        # Extract voice details

        prospect_name = first_name(prospect.get("name"), "there")
        prospect_intelligence = _build_intelligence_text(prospect_analysis)
        prompt_template = _PROMPT_MAP.get(sentiment, REPLY_PROMPT_NEUTRAL)

        # Build booking link section for positive replies
        booking_link_section = ""
        if booking_link and sentiment == "positive":
            booking_link_section = f"Available: {booking_link} — weave this naturally into the reply."
        elif sentiment == "positive":
            booking_link_section = "No booking link configured — suggest a time or ask their availability."

        format_kwargs: dict[str, Any] = {
            "sender_name": sender_profile.get("name", ""),
            "sender_title": sender_profile.get("title", ""),
            "sender_company": sender_profile.get("company", ""),
            "voice_block": voice_prompt_block(voice_signature),
            "prospect_name": prospect_name,
            "reply_text": reply_text,
            "max_chars": max_chars,
        }

        # Every template gets the thread. Before 9 Sep the negative and
        # wrong-person prompts had no history slot at all, so a decline was
        # answered with no idea what had been said before it.
        format_kwargs["conversation_history"] = _format_conversation_history(
            conversation_history,
        )

        if sentiment not in ("negative", "wrong_person"):
            format_kwargs.update({
                "prospect_title": prospect.get("title", ""),
                "prospect_company": prospect.get("company", ""),
                "prospect_headline": prospect.get("headline", ""),
                "prospect_intelligence": prospect_intelligence,
                "campaign_target": campaign_context.get("target_description", ""),
                "relevance_hook": campaign_context.get("relevance_hook", ""),
            })

        if sentiment == "positive":
            format_kwargs["booking_link_section"] = booking_link_section

        if sentiment == "scheduling_loop":
            format_kwargs["booking_link"] = booking_link or "(none configured)"

        if sentiment == "engaged":
            prospect_replies = sum(
                1 for m in conversation_history if m.get("role") == "prospect"
            )
            offerings = campaign_context.get("offerings", "")
            has_offerings = offerings and offerings != "Not specified"
            if prospect_replies >= 3 and has_offerings:
                format_kwargs["engaged_strategy"] = (
                    "The prospect has given 3+ engaged replies — you have enough rapport.\n"
                    "It's time to naturally bridge to our value prop:\n"
                    f"1. Reference what they just shared\n"
                    f"2. Briefly connect it to what we do: {offerings}\n"
                    "3. Ask if that's relevant for them\n"
                    "4. Keep it natural — connect THEIR words to YOUR product"
                )
                format_kwargs["engaged_rules"] = (
                    "- Bridge to our product naturally — do NOT keep asking discovery questions\n"
                    "- NEVER hard-pitch or be salesy — just relate their situation to what we do\n"
                    "- NEVER insert a booking link yet — just gauge interest"
                )
            else:
                format_kwargs["engaged_strategy"] = (
                    "The prospect answered your question or shared their business context. "
                    "They're having a real conversation — this is valuable. Your goal:\n"
                    "1. Show you actually READ and understood what they said — reference specific details\n"
                    "2. Validate or empathize with what they shared\n"
                    "3. Ask a thoughtful follow-up question that digs deeper — NOT a pivot to your product\n"
                    "4. Keep the conversation going naturally. DO NOT pitch or mention your product yet\n"
                    "5. If they shared a challenge, relate briefly from experience before asking your follow-up"
                )
                format_kwargs["engaged_rules"] = (
                    "- This is a CONVERSATION, not a sales interaction yet\n"
                    "- NEVER pivot to \"we can help with that\" — they didn't ask\n"
                    "- NEVER propose a call or meeting — it's too early\n"
                    "- NEVER insert a booking link"
                )

        prompt = prompt_template.format(**format_kwargs)
        prefix = "\n\n".join(
            part for part in (intent_frame(_intent), render_brief_block(message_brief))
            if part
        )
        if prefix:
            prompt = f"{prefix}\n\n{prompt}"

        system_name = select_prompt("outreach_system", _intent)
        system = render_prompt(system_name, ctx) if has_prompt(system_name) else REPLY_SYSTEM

        llm_client = LLMClient()
        result = await llm_client.generate_json(
            prompt, schemas.MESSAGE, system=system, temperature=0.7,
        )
        message = result["message"]
        reasoning = result.get("reasoning", "")

    # Clean up
    message = message.strip().strip('"').strip("'").strip()

    # Enforce character limit (LLM-based shortening with retry)
    message = await shorten_to_limit(message, max_chars)

    return {
        "message": message,
        "reasoning": reasoning,
        "brief": message_brief,
        "intent": _intent,
    }


def reply_prompt_shape(
    conversation_history: list[dict[str, Any]],
    sentiment: str,
) -> dict[str, Any]:
    """Describe the thread that went into a reply prompt, for the action log.

    Motivated by the 9 Sep incident: an AI reply ignored the
    prospect's earlier message and nothing in ``reply_sent`` recorded how much
    of the thread the prompt had actually seen — the log stored the output
    only. These five fields make that answerable after the fact.
    """
    import hashlib

    transcript = format_transcript(conversation_history)
    outstanding = unanswered_prospect_turns(conversation_history)
    use_v63 = (
        sentiment not in (
            "negative", "calendar", "scheduling_loop",
            "wrong_person", "confirm_fit",
        )
        and has_prompt("outreach_response")
        and has_prompt("outreach_system")
    )
    return {
        "history_turns": len(conversation_history or []),
        "history_chars": len(transcript),
        "last_prospect_turn_index": (
            len(conversation_history) - 1 if outstanding else -1
        ),
        "unanswered_prospect_turns": len(outstanding),
        "template": "outreach_response" if use_v63 else f"legacy_{sentiment}",
        "prompt_sha256": hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
    }
