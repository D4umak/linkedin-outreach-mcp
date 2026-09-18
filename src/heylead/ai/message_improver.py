"""Message Refiner — polishes draft messages for natural human tone.

After the initial LLM generation, this separate pass takes the draft
and refines it to sound like a real person wrote it (not AI).
The Refiner preserves the original intent, personalization, and
voice match while making the prose more natural and crisp.

From the original AI in Charge `improve_message()` (message.py lines 237-281).
"""

from __future__ import annotations

import logging
from typing import Any

from .length_fixer import shorten_to_limit
from .llm import LLMClient
from .prompt_loader import get_prompt_temperature, has_prompt, load_fragment, render_prompt

logger = logging.getLogger(__name__)

IMPROVE_SYSTEM = """You are the Message Refiner for an AI LinkedIn SDR.
Your sole job is to take the draft outreach message and polish it so it reads like a crisp, natural human text.

Rules:
- PRESERVE the original intent and CTA direction
- PRESERVE the sender's voice — tone, formality, vocabulary
- FIX: Remove name openers (e.g., "Cole," at the start)
- FIX: Remove company/place/product names from prospect's content
- FIX: Replace buzzy adjectives (impressive, killing it, serious move, massive, cool, game-changing)
- FIX: Replace LinkedIn-SDR filler (hands-on, in the weeds, hits differently) and identity labels (as a builder, as an operator)
- FIX: Replace template openers (Spot on, Love this, Agreed, Nothing beats)
- FIX: Remove quotation marks around paraphrased content
- FIX: Replace AI/trend questions with daily-experience questions
- FIX: Remove generic flattery (cool to see someone who, impressive work at)
- FIX: Remove direct word mirroring from prospect's posts
- FIX: Remove cliche phrases (a lifesaver, running the whole show, keep that energy going)
- FIX: If connection request, remove any product/solution mentions
- REMOVE any AI-sounding phrases ("I noticed", "I came across", "I'd love to")
- REMOVE any salesy language ("leverage", "synergy", "exciting opportunity")
- Make sentences punchier, understated, and natural
- Keep the same character limit as the original
- Output ONLY the refined message text. No explanation, no quotes, no formatting."""

IMPROVE_PROMPT = """Polish this {message_type} message to sound more natural and human.

## DRAFT MESSAGE
{draft}

## SENDER'S VOICE
Tone: {voice_tone}
Sentence style: {voice_sentence}
No-go words: {voice_nogo}

## ANTI-PATTERN CHECKLIST (fix ALL of these if present in the draft):
1. DIRECT MIRRORING: If the message quotes or mirrors exact words from prospect's posts/profile, paraphrase loosely instead.
2. NAME OPENER: If message starts with prospect's name (e.g., "Natasha,", "Hi Cole,"), remove it. Start with substance.
3. COMPANY/PLACE NAMES: If message mentions any company names, place names, or product names from prospect's content, remove them.
4. BUZZY ADJECTIVES: Replace "serious move", "massive", "cool", "impressive", "killing it", "game-changing", "hands-on", "in the weeds", "hits differently" with plain words about the actual work.
5. TEMPLATE OPENERS: Replace "Spot on.", "Nothing beats...", "Agreed.", "Love this." with original observations.
6. GENERIC FLATTERY: Remove "cool to see someone who...", "impressive work at..."
7. QUOTATION MARKS: Remove any quotation marks used to reference prospect's content.
8. AI/TREND QUESTIONS: Replace "How do you see AI changing..." with questions about their daily experience.
9. CLICHE PHRASES: Replace "a lifesaver", "running the whole show", "keep that energy going"
10. PITCHING IN INVITES: If this is a connection request, remove any product/solution mentions.

## EXAMPLE:
BEFORE: "Natasha, the jump from design to running everything is a serious move. Where do you see brand-building heading?"
AFTER: "Going from design to running everything changes the week. Do you still get time for the creative side?"

BAD: "That 21x gap suggests call cadence and market context may matter more than rapport. How much hands-on work does it take to make those habits repeatable?"
WHY BAD: Cites a specific stat and uses sales-methodology jargon. Paraphrase the situation in plain English; ask about their daily work.
AFTER: "Keeping a big gap between the best and the rest on the phones is one thing. Do you still run that rhythm yourself, or does the team carry it?"

## CONSTRAINTS
- MAXIMUM {max_chars} characters (hard limit)
- MUST sound like the sender wrote it naturally
- MUST preserve the core intent and CTA direction
- Apply ALL anti-pattern fixes from the checklist above
- Keep it crisp, understated, and genuine

## OUTPUT
Write ONLY the refined message text. Nothing else."""


def _recent_turns(
    conversation_history: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """The last two turns, normalised to {role, text, timestamp}."""
    out: list[dict[str, Any]] = []
    for msg in (conversation_history or [])[-2:]:
        text = str(msg.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "role": "sdr" if msg.get("role") == "sdr" else "prospect",
            "text": text,
            "timestamp": int(msg.get("timestamp") or 0),
        })
    return out


async def improve_message(
    draft: str,
    voice_signature: dict[str, Any],
    message_type: str = "invitation",
    max_chars: int = 200,
    intent: str = "sell",
    brief: Any = None,
    conversation_history: list[dict[str, Any]] | None = None,
) -> str:
    """Polish a draft message to sound more natural and human.

    This is the "Improve" stage in the Generate → Improve → Validate → Fix pipeline.

    Args:
        draft: The raw generated message to improve.
        voice_signature: User's voice analysis (tone, vocabulary, patterns).
        message_type: Type of message: "invitation", "followup", or "comment".
        max_chars: Character limit for the message.
        intent: Campaign intent ("sell", "job_search", …).
        brief: MessageBrief whose ``must_say`` the refiner must preserve.
        conversation_history: Thread turns; the last two are sent to the
            backend so a hosted refiner does not polish away a point that
            only makes sense against what was said before it.

    Returns:
        Improved message string. Returns original draft if improvement fails.
    """
    if not draft or not draft.strip():
        return draft

    # Route through backend if in backend mode and no local LLM key
    from ..config import has_local_llm_key, is_backend_mode
    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client
        client = get_linkedin_client()
        try:
            # intent/brief/thread were dropped on this route until 9 Sep, so
            # a hosted user's refiner saw the draft with no idea what it had
            # to keep saying or what it was answering. Extra fields are
            # ignored by a backend that has not yet shipped Task 04.
            return await client.improve_message(
                draft=draft,
                voice=voice_signature,
                message_type=message_type,
                max_chars=max_chars,
                intent=intent,
                must_keep=str(getattr(brief, "must_say", "") or ""),
                role_frame=str(getattr(brief, "role_frame", "") or ""),
                conversation_history=_recent_turns(conversation_history),
            )
        except Exception as e:
            logger.warning(f"Backend improve_message failed, returning draft: {e}")
            return draft
        finally:
            await client.close()

    tpl_vars = {
        "message_type": message_type,
        "draft": draft,
        "voice_tone": voice_signature.get("tone", "Professional, direct"),
        "voice_sentence": voice_signature.get("sentence_length", "Medium"),
        "voice_nogo": voice_signature.get("no_go", "Generic sales phrases"),
        "max_chars": str(max_chars),
        "voice_rules": load_fragment("voice_rules"),
    }

    # v63 path: use JSON prompt template
    if has_prompt("message_improve"):
        logger.debug("Using v63 message_improve prompt")
        prompt = render_prompt("message_improve", tpl_vars)
        from .intent import intent_frame
        _frame = intent_frame(intent)
        if brief is not None and getattr(brief, "must_say", ""):
            _keep = (f"PRESERVE THE POINT: the message must keep saying: "
                     f"{brief.must_say}. {getattr(brief, 'role_frame', '')}")
            _frame = f"{_frame}\n{_keep}".strip()
        if _frame:
            prompt = f"{_frame}\n\n{prompt}"
        temperature = get_prompt_temperature("message_improve")
    else:
        logger.debug("Using legacy message_improve prompt")
        prompt = IMPROVE_PROMPT.format(**tpl_vars)
        temperature = 0.7

    try:
        client = LLMClient()
        improved = await client.generate(
            prompt, system=IMPROVE_SYSTEM, temperature=temperature,
        )

        # Clean up
        improved = improved.strip().strip('"').strip("'").strip()

        # Sanity checks — if the LLM returned garbage, keep the original
        if not improved or len(improved) < 10:
            logger.warning("Improve stage returned empty/short result, keeping draft")
            return draft

        # Enforce char limit (LLM-based shortening with retry)
        improved = await shorten_to_limit(improved, max_chars)

        return improved

    except Exception as e:
        logger.warning(f"Message improve failed, returning draft: {e}")
        return draft
