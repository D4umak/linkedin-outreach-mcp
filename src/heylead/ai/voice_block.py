"""The user's voice signature, rendered for a prompt, in one place.

analyze_voice returns seven fields. create_post was made to render them all on
14 Sep 2026; the outreach, follow-up, comment, reply, fixer and improver
prompts kept picking two to four keys by hand, the strategist and the
comment-thread reply asked for voice["style"] (a key no analysis writes), and
the brand-audit prompts rendered the tone alone. The hosted backend had the
same shape (heylead-api app/services/voice_prompt.py is the mirror of this
module), and Denys read the cloud drafts on 21 Sep 2026 as generic model prose
under his name.

A leaf module on purpose: prompt_loader renders every JSON prompt and
voice_analyzer imports prompt_loader, so the renderer cannot live there.
tests/test_voice_reaches_every_generator.py fails the suite on a hand-picked
read or a leftover per-field placeholder in a template.
"""

from __future__ import annotations

from typing import Any

# Named against the keys analyze_voice actually returns.
_VOICE_PROMPT_FIELDS: tuple[tuple[str, str], ...] = (
    ("tone", "Tone"),
    ("sentence_length", "Sentence length"),
    ("signature_pattern", "Opens and closes"),
    ("vocabulary_preferences", "Words they reach for"),
    ("communication_style", "How they communicate"),
    ("no_go", "Never writes"),
)

_FORMALITY = (
    (3, "very casual"),
    (5, "casual, peer to peer"),
    (7, "professional but relaxed"),
    (10, "formal"),
)


def _formality(value: Any) -> str:
    try:
        level = int(value)
    except (TypeError, ValueError):
        return ""
    for ceiling, label in _FORMALITY:
        if level <= ceiling:
            return f"{label} ({level}/10)"
    return ""


def voice_prompt_block(voice: dict[str, Any] | None) -> str:
    """The voice signature as prompt lines, skipping fields we do not have.

    "" for a user never analysed, so a caller can say so rather than invent
    a "professional" style the product never measured.
    """
    voice = voice or {}
    if not isinstance(voice, dict):
        return ""
    lines: list[str] = []
    for key, label in _VOICE_PROMPT_FIELDS:
        value = voice.get(key)
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v).strip() for v in value if str(v).strip())
        value = str(value or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    formality = _formality(voice.get("formality_level"))
    if formality:
        lines.append(f"Formality: {formality}")
    return "\n".join(lines)
