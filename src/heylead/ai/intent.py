"""Campaign intent — the stance a campaign's messages take.

The v63 prompt stack was written for one job: selling. The system prompt
says "qualify the prospect and secure a meeting", the invitation template
demands a vague curiosity opener with no company names, and the polish
and validation stages strip or fail anything that names a product. That
stance is wrong for campaigns where the sender is the BUYER (vendor
scouting), a partner, or a recruiter — on 18 Aug 2026 three buyer
campaigns shipped meaningless founder small talk because no layer knew
the campaign's purpose.

Intent is declared per campaign in config_json.campaign_intent
(sell | buy | partner | recruit, default sell) and selects prompt
variants by suffix: select_prompt("outreach_invitation", "buy") →
"outreach_invitation_buy" when that template exists, falling back to the
base template otherwise, so sell campaigns are byte-identical to before
and unauthored intents degrade gracefully.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .prompt_loader import has_prompt

logger = logging.getLogger(__name__)

VALID_INTENTS = ("sell", "buy", "partner", "recruit")
DEFAULT_INTENT = "sell"


def resolve_intent(campaign_config: Any) -> str:
    """Read campaign_intent from a config dict or raw config_json string.

    Anything absent, malformed, or unrecognised resolves to "sell" — the
    historical behavior — never an exception.
    """
    if isinstance(campaign_config, str):
        try:
            campaign_config = json.loads(campaign_config or "{}")
        except (ValueError, TypeError):
            campaign_config = {}
    if not isinstance(campaign_config, dict):
        campaign_config = {}

    raw = str(campaign_config.get("campaign_intent") or "").strip().lower()
    if not raw:
        return DEFAULT_INTENT
    if raw not in VALID_INTENTS:
        logger.warning("Unknown campaign_intent %r — falling back to %s", raw, DEFAULT_INTENT)
        return DEFAULT_INTENT
    return raw


def select_prompt(base: str, intent: str) -> str:
    """Return the intent-specific prompt name, or the base when unauthored.

    The fallback is deliberate but must stay visible: the resolved name is
    logged by callers into the message audit trail, so a missing variant
    shows up as sell-prompt routing rather than passing silently forever.
    """
    if intent and intent != DEFAULT_INTENT:
        candidate = f"{base}_{intent}"
        if has_prompt(candidate):
            return candidate
        logger.debug("No %s template — using %s", candidate, base)
    return base


def intent_frame(intent: str) -> str:
    """One stance paragraph for SHARED prompts (improve/fix) that have no
    per-intent variant. Empty for sell so the historical prompts are
    untouched."""
    if intent == "buy":
        return (
            "STANCE: The sender is the BUYER, evaluating this vendor in order "
            "to purchase what the campaign describes. Mentions of the "
            "prospect's product or company are intentional buying signals — "
            "keep them. Do not reframe the message as selling anything."
        )
    if intent == "partner":
        return (
            "STANCE: The sender is proposing a mutual partnership, not selling. "
            "Keep concrete mentions of both sides' work."
        )
    if intent == "recruit":
        return (
            "STANCE: The sender is recruiting the prospect. Keep mentions of "
            "the role and the prospect's experience."
        )
    return ""
