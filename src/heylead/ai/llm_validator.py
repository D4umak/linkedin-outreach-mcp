"""LLM-based message validation using v63 production prompts.

Complements the rule-based validator (message_validator.py) with context-sensitive
checks that regexes can't do:

1. Bullet reuse across last 2 messages
2. Greeting/signoff reuse vs previous
3. Length variation (±15% of previous)
4. CTA variation (identical check)
5. First-sentence overlap (stopword removal, >30%)
6. Guardrails (company names, roles, buzzwords)
7. Link-gating (link present + SDR count < 2)

Usage:
    from .llm_validator import llm_validate
    result = await llm_validate(message, history, company)
    if not result.is_valid:
        # merge with rule-based issues
"""

from __future__ import annotations

import logging
from typing import Any

from ..constants import LLM_TIER_FAST
from . import schemas
from .llm import LLMClient
from .message_validator import ValidationResult
from .prompt_loader import get_prompt_temperature, has_prompt, load_fragment, render_prompt

logger = logging.getLogger(__name__)


def _format_history_for_validation(messages: list[dict[str, Any]]) -> str:
    """Format conversation history for the validation prompt."""
    if not messages:
        return "No previous messages."
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        text = msg.get("text", "")
        label = "SDR" if role == "sdr" else "PROSPECT"
        lines.append(f"[{label}]: {text}")
    return "\n".join(lines)


async def llm_validate(
    message: str,
    history: list[dict[str, Any]],
    company: str,
    calendar_link: str = "",
    message_type: str = "followup",
    prospect_company: str = "",
    max_chars: int = 500,
    intent: str = "sell",
) -> ValidationResult:
    """Run LLM-based validation using v63's validate prompt.

    Args:
        message: The generated message to validate
        history: Conversation history (list of {role, text} dicts)
        company: Sender's company name (for name-leak detection)
        calendar_link: Calendar link (for link-gating check)
        message_type: "invitation" or "followup"
        prospect_company: Prospect's company name (for invitation validation)
        max_chars: Character limit for validation

    Returns:
        ValidationResult with is_valid, issues, and warnings
    """
    result = ValidationResult()

    # Pick the right prompt based on message type
    from .intent import select_prompt
    if message_type == "invitation":
        prompt_name = select_prompt("outreach_validate", intent)
    else:
        prompt_name = select_prompt("followup_validate", intent)

    if not has_prompt(prompt_name):
        result.pass_stage("LLM")
        return result

    try:
        if message_type == "invitation":
            ctx = {
                "message": message,
                "company": company,
                "prospect_company": prospect_company,
                "max_chars": str(max_chars),
            }
        else:
            ctx = {
                "message": message,
                "history": _format_history_for_validation(history),
                "company": company,
                "calendar_link": calendar_link or "Not configured",
                "voice_rules": load_fragment("voice_rules"),
            }

        prompt = render_prompt(prompt_name, ctx)
        temp = get_prompt_temperature(prompt_name)

        llm_client = LLMClient()
        parsed = await llm_client.generate_json(
            prompt, schemas.VALIDATION, temperature=temp,
            max_tokens=500, tier=LLM_TIER_FAST,
        )

        # generate_json returns a dict matching the schema, or raises.
        # valid=false is a deny even when issues is empty or not strings —
        # the old `if not is_valid and issues` treated that as a pass.
        is_valid = parsed.get("valid", True)
        raw_issues = parsed.get("issues", [])
        if not isinstance(raw_issues, list):
            raw_issues = []

        if is_valid:
            result.pass_stage("LLM-Validate")
        else:
            strings = [i for i in raw_issues if isinstance(i, str) and i.strip()]
            if not strings:
                reason = parsed.get("reasoning") or parsed.get("analysis") or ""
                if isinstance(reason, str) and reason.strip():
                    strings = [reason.strip()]
                else:
                    strings = ["LLM marked the message invalid"]
            for issue in strings:
                result.fail("LLM-Validate", issue)

    except Exception as e:
        # LLM validation is optional — don't block on errors
        logger.warning("LLM validation failed (non-blocking): %s", e)
        result.pass_stage("LLM-Validate")
        result.warn("LLM-Validate", f"LLM validation unavailable: {e}")

    return result
