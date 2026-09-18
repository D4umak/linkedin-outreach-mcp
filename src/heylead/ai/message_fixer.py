"""Targeted Message Fixer — surgically fixes validation issues.

When the validation pipeline detects issues in a message, instead of
regenerating from scratch (which wastes tokens and often produces worse
output), this module sends the specific issues to the LLM for surgical
fixes while preserving the good parts of the message.

From the original AI in Charge validate+fix stages (message.py lines 139-188).
"""

from __future__ import annotations

import logging
from typing import Any

from .length_fixer import shorten_to_limit
from .llm import LLMClient
from .prompt_loader import get_prompt_temperature, has_prompt, load_fragment, render_prompt

logger = logging.getLogger(__name__)

FIX_SYSTEM = """You are the Message Fixer for an AI LinkedIn SDR.
Your job is to fix SPECIFIC issues in a message while preserving everything else.

Rules:
- Fix ONLY the listed issues — do NOT rewrite the entire message
- PRESERVE the original intent, personalization, CTA, and tone
- PRESERVE parts of the message that are NOT flagged
- Keep the same character limit
- If fixing a "too long" issue, shorten smartly (remove filler, not substance)
- If fixing salesy language, replace with natural alternatives
- If fixing AI-tells, rephrase to sound more human
- Output ONLY the fixed message text. No explanation, no quotes, no formatting."""

FIX_PROMPT = """Fix the specific issues in this {message_type} message.

## ORIGINAL MESSAGE
{message}

## ISSUES TO FIX
{issues_text}

## SENDER'S VOICE
Tone: {voice_tone}
Sentence style: {voice_sentence}
No-go words: {voice_nogo}

## CONSTRAINTS
- MAXIMUM {max_chars} characters (hard limit)
- Fix ONLY the listed issues
- Preserve everything else — same prospect details, same CTA, same tone
- Must still sound like the sender wrote it

## OUTPUT
Write ONLY the fixed message text. Nothing else."""


async def fix_message(
    message: str,
    issues: list[str],
    voice_signature: dict[str, Any],
    message_type: str = "invitation",
    max_chars: int = 200,
    intent: str = "sell",
    brief: Any = None,
) -> str:
    """Fix specific validation issues in a message while preserving intent.

    This is the "Fix" stage in the Generate → Improve → Validate → Fix pipeline.

    Args:
        message: The message that failed validation.
        issues: List of validation issue strings from ValidationResult.issues.
        voice_signature: User's voice analysis (tone, vocabulary, patterns).
        message_type: Type of message: "invitation", "followup", or "comment".
        max_chars: Character limit for the message.

    Returns:
        Fixed message string. Returns original message if fix fails.
    """
    if not message or not message.strip():
        return message

    if not issues:
        return message

    # Route through backend if in backend mode and no local LLM key
    from ..config import has_local_llm_key, is_backend_mode
    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client
        client = get_linkedin_client()
        try:
            return await client.fix_message(
                message=message,
                issues=issues,
                voice=voice_signature,
                message_type=message_type,
                max_chars=max_chars,
            )
        except Exception as e:
            logger.warning(f"Backend fix_message failed, returning original: {e}")
            return message
        finally:
            await client.close()

    # Format issues for the prompt
    issues_text = "\n".join(f"- {issue}" for issue in issues)

    tpl_vars = {
        "message_type": message_type,
        "message": message,
        "issues_text": issues_text,
        "voice_tone": voice_signature.get("tone", "Professional, direct"),
        "voice_sentence": voice_signature.get("sentence_length", "Medium"),
        "voice_nogo": voice_signature.get("no_go", "Generic sales phrases"),
        "max_chars": str(max_chars),
        "voice_rules": load_fragment("voice_rules"),
    }

    # v63 path: use JSON prompt template
    if has_prompt("message_fix"):
        logger.debug("Using v63 message_fix prompt")
        prompt = render_prompt("message_fix", tpl_vars)
        from .intent import intent_frame
        _frame = intent_frame(intent)
        if brief is not None and getattr(brief, "must_say", ""):
            _keep = (f"PRESERVE THE POINT: the message must keep saying: "
                     f"{brief.must_say}. {getattr(brief, 'role_frame', '')}")
            _frame = f"{_frame}\n{_keep}".strip()
        if _frame:
            prompt = f"{_frame}\n\n{prompt}"
        temperature = get_prompt_temperature("message_fix")
    else:
        logger.debug("Using legacy message_fix prompt")
        prompt = FIX_PROMPT.format(**tpl_vars)
        temperature = 0.3

    try:
        client = LLMClient()
        fixed = await client.generate(
            prompt, system=FIX_SYSTEM, temperature=temperature,
        )

        # Clean up
        fixed = fixed.strip().strip('"').strip("'").strip()

        # Sanity checks — if the LLM returned garbage, keep the original
        if not fixed or len(fixed) < 10:
            logger.warning("Fix stage returned empty/short result, keeping original")
            return message

        # Enforce char limit (LLM-based shortening with retry)
        fixed = await shorten_to_limit(fixed, max_chars)

        return fixed

    except Exception as e:
        logger.warning(f"Message fix failed, returning original: {e}")
        return message
