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

import re
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
    a "professional" style the product never measured. The record keys
    (RECORD_KEYS) are never printed.
    """
    voice = voice or {}
    # Not usable: never analysed, or the "Could not analyze" placeholder an
    # older client stored as the user's tone (api #3499).
    if not is_usable_voice(voice):
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


# ── The voice record (api #3499; heylead-api app/services/voice_build.py) ──
#
# A built voice carries these keys about itself. No renderer prints them:
# voice_prompt_block reads only _VOICE_PROMPT_FIELDS, so a prompt is
# byte-identical with and without them. Zero own posts store exactly {} and
# never carry them, because every reader decides "has a voice" by emptiness.

RECORD_KEYS = ("built_at", "post_count", "thin", "excerpts", "rule", "source")

#: Below this a voice is thin ("three posts give it enough to go on").
VOICE_MIN_POSTS = 3
#: 1 = the posts HeyLead published were left out of the build.
VOICE_RULE = 1
EXCERPTS_KEPT = 2
EXCERPT_MAX_CHARS = 280

#: The default analyze_voice used to store when the model's answer did not
#: parse. It is never stored now and never rendered if an old row holds it.
SENTINEL_PREFIX = "Could not analyze"


def is_sentinel_voice(voice: Any) -> bool:
    """The "Could not analyze — check your LLM API key" placeholder."""
    return isinstance(voice, dict) and str(voice.get("tone") or "").strip().startswith(
        SENTINEL_PREFIX
    )


def is_usable_voice(voice: Any) -> bool:
    """A voice a writer can use: a dict with a field to render, never the sentinel."""
    if not isinstance(voice, dict) or is_sentinel_voice(voice):
        return False
    for key, _label in _VOICE_PROMPT_FIELDS:
        value = voice.get(key)
        if isinstance(value, (list, tuple)):
            if any(str(v).strip() for v in value):
                return True
        elif str(value or "").strip():
            return True
    return False


def without_record(voice: dict[str, Any] | None) -> dict[str, Any]:
    """The voice's own fields, the record taken off."""
    return {k: v for k, v in (voice or {}).items() if k not in RECORD_KEYS}


def with_record(
    voice: dict[str, Any], *, post_count: int, excerpts: list[str], source: str,
    now: int | None = None,
) -> dict[str, Any]:
    """The voice with its record. Never called for an empty voice."""
    import time

    out = without_record(voice)
    count = int(post_count)
    out.update({
        "built_at": int(now if now is not None else time.time()),
        "post_count": count,
        "thin": 0 < count < VOICE_MIN_POSTS,
        "excerpts": [str(e) for e in (excerpts or [])[:EXCERPTS_KEPT]],
        "rule": VOICE_RULE,
        "source": source,
    })
    return out


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def post_opening(text: str, limit: int = EXCERPT_MAX_CHARS) -> str:
    """The whole sentences a post opens with, at most ``limit`` characters.

    "" when the first sentence alone is longer: a cut sentence teaches the
    writer to stop mid-thought.
    """
    flat = " ".join(str(text or "").split())
    out = ""
    for sentence in _SENTENCE_END.split(flat):
        candidate = f"{out} {sentence}" if out else sentence
        if len(candidate) > limit:
            break
        out = candidate
    return out


def voice_excerpts(own_posts: list[dict[str, Any]] | None) -> list[str]:
    """Openings of up to two own posts of 120+ characters, in the order given
    (newest first), each distinct."""
    from ..services.voice_examples import MIN_EXAMPLE_CHARS

    picked: list[str] = []
    seen: set[str] = set()
    for post in own_posts or []:
        if not isinstance(post, dict):
            continue
        text = str(post.get("text") or "").strip()
        if len(text) < MIN_EXAMPLE_CHARS:
            continue
        opening = post_opening(text)
        key = opening.lower()
        if opening and key not in seen:
            seen.add(key)
            picked.append(opening)
        if len(picked) >= EXCERPTS_KEPT:
            break
    return picked
