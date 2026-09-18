"""Memory extractor — extract structured decisions from follow-up reasoning + message.

After each follow-up is generated, this module extracts a structured record of
what was used (CTA, pain angle, greeting, structure, etc.) so future follow-ups
can explicitly avoid repeating the same patterns.

Two-tier extraction:
1. Heuristic: regex/length-based extraction from the final message (fast, free)
2. LLM: structured extraction from reasoning text (richer, optional)

The heuristic tier is always used. The LLM tier enriches it if available.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from ..constants import LLM_TIER_FAST
from . import schemas
from .llm import LLMClient

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# LLM extraction prompt
# ──────────────────────────────────────────────

_EXTRACT_PROMPT = """Extract structured decisions from this follow-up reasoning and message.

REASONING (from planning stage):
{reasoning}

FINAL MESSAGE:
{message}

Extract these fields as JSON:
{{
    "cta_used": "the CTA question used (exact text)",
    "pain_used": "the pain point or symptom addressed (brief)",
    "hook_angle": "the opening angle/approach (brief, 5-10 words)"
}}

Return ONLY valid JSON. If a field is unclear, use "" (empty string)."""


# ──────────────────────────────────────────────
# Heuristic extraction (always runs)
# ──────────────────────────────────────────────

def _extract_cta(message: str) -> str:
    """Extract the CTA question from the message (last sentence ending with ?)."""
    # Find all sentences ending with ?
    questions = re.findall(r'[^.!?\n]+\?', message)
    if questions:
        return questions[-1].strip()
    return ""


def _extract_greeting(message: str) -> str:
    """Extract the greeting from the first line/token."""
    lines = message.strip().split("\n")
    first_line = lines[0].strip() if lines else ""
    # Common greeting patterns: "Hey Name,", "Name,", "Hi Name,"
    match = re.match(r'^(Hey\s+\w+[,.]?|Hi\s+\w+[,.]?|\w+[,])', first_line)
    if match:
        return match.group(0).strip()
    # If first line is short (< 30 chars), it's likely a greeting
    if len(first_line) < 30 and first_line.endswith(","):
        return first_line
    return ""


def _extract_signoff(message: str) -> str:
    """Extract the signoff from the last line/token."""
    lines = [l.strip() for l in message.strip().split("\n") if l.strip()]
    if not lines:
        return ""
    last_line = lines[-1]
    # Common signoff patterns
    signoff_patterns = [
        r'^(Cheers|Thanks|Best|Take care|Talk soon|Looking forward)',
        r'^[—\-]\s*\w+',  # "— Dan"
    ]
    for pattern in signoff_patterns:
        if re.match(pattern, last_line, re.IGNORECASE):
            return last_line
    return ""


def _extract_first_sentence(message: str) -> str:
    """Extract the first sentence of the message (after greeting)."""
    lines = message.strip().split("\n")
    text = message.strip()

    # Skip greeting line if present
    if lines and (lines[0].strip().endswith(",") or re.match(r'^(Hey|Hi|Hello)\s', lines[0])):
        text = "\n".join(lines[1:]).strip()

    # Get first sentence
    match = re.match(r'([^.!?]+[.!?])', text)
    if match:
        return match.group(1).strip()
    # If no sentence boundary, take first 80 chars
    return text[:80].strip() if text else ""


def _classify_structure(message: str) -> str:
    """Classify message structure: one_liner / short / medium."""
    length = len(message)
    if length <= 120:
        return "one_liner"
    elif length <= 220:
        return "short"
    else:
        return "medium"


def _has_bullets(message: str) -> bool:
    """Check if message contains bullet points."""
    return bool(re.search(r'^[\s]*[-•]\s', message, re.MULTILINE))


def _has_link(message: str) -> bool:
    """Check if message contains a link."""
    return bool(re.search(r'https?://|www\.', message))


def extract_memory_heuristic(
    message: str,
    followup_number: int,
) -> dict[str, Any]:
    """Extract structured memory from message text using heuristics only.

    This is the lightweight fallback that always works — no LLM needed.
    Used when LLM extraction fails or for copilot approval re-extraction.
    """
    return {
        "followup_number": followup_number,
        "cta_used": _extract_cta(message),
        "pain_used": "",  # Can't reliably extract without reasoning
        "structure": _classify_structure(message),
        "greeting": _extract_greeting(message),
        "signoff": _extract_signoff(message),
        "had_bullets": _has_bullets(message),
        "had_link": _has_link(message),
        "first_sentence": _extract_first_sentence(message),
        "hook_angle": "",  # Can't reliably extract without reasoning
        "timestamp": int(time.time()),
    }


# ──────────────────────────────────────────────
# LLM-enriched extraction (optional)
# ──────────────────────────────────────────────

async def extract_memory_from_reasoning(
    reasoning_text: str,
    message_text: str,
    followup_number: int,
) -> dict[str, Any]:
    """Extract structured follow-up memory from reasoning + message.

    Combines heuristic extraction (always works) with LLM extraction
    (enriches pain_used and hook_angle from reasoning text).

    Args:
        reasoning_text: Raw reasoning output from Stage 1
        message_text: Final generated message text
        followup_number: Which follow-up this is (1, 2, 3...)

    Returns:
        Structured memory dict ready for save_outreach_memory()
    """
    # Start with heuristic extraction (guaranteed to work)
    memory = extract_memory_heuristic(message_text, followup_number)

    # Enrich with LLM extraction from reasoning text
    if reasoning_text and reasoning_text.strip():
        try:
            prompt = _EXTRACT_PROMPT.format(
                reasoning=reasoning_text[:500],  # Cap to avoid token bloat
                message=message_text,
            )
            llm = LLMClient()
            extracted = await llm.generate_json(
                prompt, schemas.OUTREACH_MEMORY, temperature=0.3,
                max_tokens=300, tier=LLM_TIER_FAST,
            )

            # Merge LLM fields into heuristic results (LLM wins for richer fields)
            if extracted.get("pain_used"):
                memory["pain_used"] = extracted["pain_used"]
            if extracted.get("hook_angle"):
                memory["hook_angle"] = extracted["hook_angle"]
            # LLM CTA extraction only overwrites if heuristic found nothing
            if not memory["cta_used"] and extracted.get("cta_used"):
                memory["cta_used"] = extracted["cta_used"]

        except Exception as e:
            logger.debug("LLM memory extraction failed, using heuristic only: %s", e)

    return memory
