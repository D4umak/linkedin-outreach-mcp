"""LLM-based message length fixer — shortens messages intelligently.

Ported from the original AI in Charge fix_symbols_count() (helpers.py).
Uses an LLM at temperature=0.0 with up to 5 decreasing-limit retries
to shorten messages while preserving meaning and calendar links.

Fallback: simple string truncation at word boundary if LLM fails.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"[.!?]")

# Retry config (from original AI in Charge)
MAX_SHORTEN_RETRIES = 5
DECREASE_COEF = 0.95  # Each retry targets 5% fewer chars

_SHORTEN_PROMPT = (
    "INPUT MESSAGE: {message}\n\n"
    "TASK: The message has {current_len} characters. "
    "Rewrite it to have less than {target_limit} characters. "
    "You can drop filler text but preserve the core meaning. "
    "If a calendar link is present, keep it unchanged. "
    "Respond ONLY with the new shortened message."
)


def _truncate(message: str, max_chars: int) -> str:
    """Fit max_chars by dropping an incomplete trailing clause — never ellipsis.

    Connection notes that end in 'Open to 20...' look broken. Keep the last
    complete sentence that fits; if none, cut on a word boundary with no '...'.
    """
    if len(message) <= max_chars:
        return message
    window = message[:max_chars].rstrip()
    last_end = -1
    for match in _SENTENCE_END.finditer(window):
        last_end = match.end()
    if last_end >= 40:
        return window[:last_end].rstrip()
    last_space = window.rfind(" ")
    if last_space >= 40:
        return window[:last_space].rstrip(" ,;:-")
    return window


async def shorten_to_limit(
    message: str,
    max_chars: int,
    *,
    use_llm: bool = True,
) -> str:
    """Shorten a message to fit within max_chars using LLM retries.

    Strategy (from original AI in Charge):
    1. If already within limit, return as-is
    2. Try LLM shortening up to 5 times with decreasing target limits
    3. Fallback to string truncation if LLM fails

    Args:
        message: The message to shorten.
        max_chars: Maximum allowed characters.
        use_llm: If True, try LLM-based shortening first.

    Returns:
        Shortened message within max_chars.
    """
    if len(message) <= max_chars:
        return message

    # Skip LLM for very small overlaps (< 10 chars over) — truncation is fine
    if not use_llm or (len(message) - max_chars) < 10:
        return _truncate(message, max_chars)

    try:
        from .llm import LLMClient
        client = LLMClient()

        target_limit = max_chars
        current = message

        for attempt in range(MAX_SHORTEN_RETRIES):
            prompt = _SHORTEN_PROMPT.format(
                message=current,
                current_len=len(current),
                target_limit=target_limit,
            )
            shortened = await client.generate(
                prompt,
                system="You shorten messages. Output ONLY the shortened text.",
                temperature=0.0,
                max_tokens=max_chars + 50,
            )

            shortened = shortened.strip().strip('"').strip("'").strip()

            if shortened and len(shortened) <= max_chars:
                logger.info(
                    "LLM shortened message: %d → %d chars (attempt %d)",
                    len(message), len(shortened), attempt + 1,
                )
                return shortened

            # Decrease target for next attempt
            target_limit = int(target_limit * DECREASE_COEF)
            if shortened and len(shortened) < len(current):
                current = shortened  # Use improved version for next attempt

        logger.warning(
            "LLM shortening failed after %d attempts (%d chars), truncating",
            MAX_SHORTEN_RETRIES, len(current),
        )
    except Exception as e:
        logger.warning("LLM shortening error, truncating: %s", e)

    return _truncate(message, max_chars)
