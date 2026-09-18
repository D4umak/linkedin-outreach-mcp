"""Voice signature → Hume AI voice config mapping.

Maps the HeyLead voice signature (tone, formality, vocabulary, communication
style) to Hume AI's Voice Control slider parameters (10 dimensions, each
-100 to 100) plus acting instructions and speed.

The resulting config is stored in settings as 'hume_voice_config' and used
by the TTS generator to produce personalized voice memos.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Slider dimension names (Hume AI Voice Control)
SLIDER_ASSERTIVENESS = "assertiveness"
SLIDER_BUOYANCY = "buoyancy"
SLIDER_CONFIDENCE = "confidence"
SLIDER_ENTHUSIASM = "enthusiasm"
SLIDER_NASALITY = "nasality"
SLIDER_RELAXEDNESS = "relaxedness"
SLIDER_SMOOTHNESS = "smoothness"
SLIDER_TEPIDITY = "tepidity"
SLIDER_TIGHTNESS = "tightness"

ALL_SLIDERS = [
    SLIDER_ASSERTIVENESS,
    SLIDER_BUOYANCY,
    SLIDER_CONFIDENCE,
    SLIDER_ENTHUSIASM,
    SLIDER_NASALITY,
    SLIDER_RELAXEDNESS,
    SLIDER_SMOOTHNESS,
    SLIDER_TEPIDITY,
    SLIDER_TIGHTNESS,
]


def _clamp(value: int, lo: int = -100, hi: int = 100) -> int:
    """Clamp an integer value to [lo, hi]."""
    return max(lo, min(hi, value))


def _map_formality_to_sliders(formality_level: int) -> dict[str, int]:
    """Map formality_level (1-10) to base slider values.

    Low formality  (1-3): relaxed, less assertive, less smooth
    Mid formality  (4-6): balanced
    High formality (7-10): less relaxed, more assertive, smoother
    """
    level = max(1, min(10, formality_level))

    if level <= 3:
        # Casual
        t = (level - 1) / 2  # 0.0 → 1.0
        return {
            SLIDER_ASSERTIVENESS: int(20 + 20 * t),     # 20 → 40
            SLIDER_BUOYANCY: int(30 + 20 * t),          # 30 → 50
            SLIDER_CONFIDENCE: int(20 + 20 * t),        # 20 → 40
            SLIDER_ENTHUSIASM: int(20 + 30 * t),        # 20 → 50
            SLIDER_NASALITY: 0,
            SLIDER_RELAXEDNESS: int(90 - 20 * t),       # 90 → 70
            SLIDER_SMOOTHNESS: int(30 + 20 * t),        # 30 → 50
            SLIDER_TEPIDITY: 0,
            SLIDER_TIGHTNESS: int(-20 + 10 * t),        # -20 → -10
        }
    elif level <= 6:
        # Balanced
        t = (level - 4) / 2  # 0.0 → 1.0
        return {
            SLIDER_ASSERTIVENESS: int(40 + 20 * t),     # 40 → 60
            SLIDER_BUOYANCY: int(20 + 10 * t),          # 20 → 30
            SLIDER_CONFIDENCE: int(40 + 20 * t),        # 40 → 60
            SLIDER_ENTHUSIASM: int(20 + 10 * t),        # 20 → 30
            SLIDER_NASALITY: 0,
            SLIDER_RELAXEDNESS: int(60 - 20 * t),       # 60 → 40
            SLIDER_SMOOTHNESS: int(50 + 20 * t),        # 50 → 70
            SLIDER_TEPIDITY: 0,
            SLIDER_TIGHTNESS: 0,
        }
    else:
        # Formal
        t = (level - 7) / 3  # 0.0 → 1.0
        return {
            SLIDER_ASSERTIVENESS: int(60 + 20 * t),     # 60 → 80
            SLIDER_BUOYANCY: int(10 - 10 * t),          # 10 → 0
            SLIDER_CONFIDENCE: int(60 + 20 * t),        # 60 → 80
            SLIDER_ENTHUSIASM: int(10 - 10 * t),        # 10 → 0
            SLIDER_NASALITY: 0,
            SLIDER_RELAXEDNESS: int(40 - 20 * t),       # 40 → 20
            SLIDER_SMOOTHNESS: int(70 + 20 * t),        # 70 → 90
            SLIDER_TEPIDITY: int(-10 - 10 * t),         # -10 → -20
            SLIDER_TIGHTNESS: int(10 + 10 * t),         # 10 → 20
        }


# Keyword → slider delta pairs
_TONE_KEYWORDS: dict[str, dict[str, int]] = {
    "warm": {SLIDER_BUOYANCY: 30, SLIDER_ENTHUSIASM: 20, SLIDER_RELAXEDNESS: 10},
    "friendly": {SLIDER_BUOYANCY: 25, SLIDER_ENTHUSIASM: 15, SLIDER_RELAXEDNESS: 15},
    "direct": {SLIDER_ASSERTIVENESS: 20, SLIDER_TEPIDITY: -20},
    "confident": {SLIDER_CONFIDENCE: 30, SLIDER_ASSERTIVENESS: 10},
    "casual": {SLIDER_RELAXEDNESS: 30, SLIDER_SMOOTHNESS: -10, SLIDER_TIGHTNESS: -15},
    "informal": {SLIDER_RELAXEDNESS: 20, SLIDER_TIGHTNESS: -10},
    "formal": {SLIDER_SMOOTHNESS: 20, SLIDER_ASSERTIVENESS: 10, SLIDER_RELAXEDNESS: -15},
    "professional": {SLIDER_SMOOTHNESS: 15, SLIDER_CONFIDENCE: 10},
    "technical": {SLIDER_ASSERTIVENESS: 10, SLIDER_SMOOTHNESS: 10},
    "analytical": {SLIDER_ASSERTIVENESS: 10, SLIDER_TEPIDITY: -10},
    "enthusiastic": {SLIDER_ENTHUSIASM: 40, SLIDER_BUOYANCY: 30},
    "energetic": {SLIDER_ENTHUSIASM: 30, SLIDER_BUOYANCY: 25},
    "calm": {SLIDER_RELAXEDNESS: 30, SLIDER_ENTHUSIASM: -15, SLIDER_TIGHTNESS: -20},
    "authoritative": {SLIDER_ASSERTIVENESS: 30, SLIDER_CONFIDENCE: 20, SLIDER_SMOOTHNESS: 15},
    "approachable": {SLIDER_RELAXEDNESS: 20, SLIDER_BUOYANCY: 15, SLIDER_TIGHTNESS: -10},
    "concise": {SLIDER_ASSERTIVENESS: 10},
    "conversational": {SLIDER_RELAXEDNESS: 25, SLIDER_BUOYANCY: 10},
    "humble": {SLIDER_ASSERTIVENESS: -15, SLIDER_RELAXEDNESS: 15},
    "bold": {SLIDER_ASSERTIVENESS: 25, SLIDER_CONFIDENCE: 20},
}


def _map_tone_keywords(tone: str) -> dict[str, int]:
    """Parse tone string for keywords and return slider deltas.

    Examples:
        "Direct, technical, slightly informal" → combined deltas
        "Warm and confident" → combined deltas
    """
    deltas: dict[str, int] = {}
    tone_lower = tone.lower()

    for keyword, adjustments in _TONE_KEYWORDS.items():
        if keyword in tone_lower:
            for slider, delta in adjustments.items():
                deltas[slider] = deltas.get(slider, 0) + delta

    return deltas


def _map_style_keywords(communication_style: list[str]) -> dict[str, int]:
    """Parse communication_style list for keywords and return slider deltas."""
    deltas: dict[str, int] = {}

    for style in communication_style:
        style_lower = style.lower()
        for keyword, adjustments in _TONE_KEYWORDS.items():
            if keyword in style_lower:
                # Smaller weight than tone (style is secondary)
                for slider, delta in adjustments.items():
                    deltas[slider] = deltas.get(slider, 0) + delta // 2

    return deltas


def _map_style_to_speed(
    communication_style: list[str], sentence_length: str
) -> float:
    """Determine speech speed from writing style indicators.

    Returns 0.8 - 1.2 range.
    """
    speed = 1.0
    sl = sentence_length.lower()

    # Sentence length affects speed
    if "short" in sl:
        speed += 0.05  # Slightly faster for punchy speakers
    elif "long" in sl or "compound" in sl:
        speed -= 0.05  # Slightly slower for complex speakers

    # Style adjustments
    style_text = " ".join(s.lower() for s in communication_style)
    if "concise" in style_text or "direct" in style_text:
        speed += 0.05
    if "analytical" in style_text or "deliberate" in style_text:
        speed -= 0.05
    if "energetic" in style_text or "enthusiastic" in style_text:
        speed += 0.1

    return max(0.8, min(1.2, round(speed, 2)))


def _build_acting_instructions(voice_signature: dict[str, Any]) -> str:
    """Generate Hume AI acting instructions from voice signature.

    Creates a natural language acting direction (kept under ~150 chars for
    best results with Hume's description field).

    Example output:
        "Confident, direct sales professional. Warm but efficient.
         Short sentences. No scripted or salesy language."
    """
    parts: list[str] = []

    # Tone is the primary driver
    tone = voice_signature.get("tone", "")
    if tone:
        parts.append(tone.rstrip("."))

    # Communication style adds color
    comm_style = voice_signature.get("communication_style", [])
    if comm_style:
        # Pick top 2-3 style descriptors
        style_text = ", ".join(comm_style[:3])
        parts.append(style_text)

    # Sentence length → pacing direction
    sl = voice_signature.get("sentence_length", "")
    if sl:
        if "short" in sl.lower():
            parts.append("Keep pacing brisk")
        elif "long" in sl.lower():
            parts.append("Measured, unhurried pace")

    # No-go → negative constraints
    no_go = voice_signature.get("no_go", "")
    if no_go:
        parts.append(f"Avoid: {no_go[:60]}")

    instruction = ". ".join(parts)

    # Truncate to ~200 chars if needed (Hume recommends concise instructions)
    if len(instruction) > 200:
        instruction = instruction[:197] + "..."

    return instruction


def create_voice_config(voice_signature: dict[str, Any]) -> dict[str, Any]:
    """Map a HeyLead voice signature to Hume AI voice parameters.

    Args:
        voice_signature: Dict with tone, sentence_length, signature_pattern,
            vocabulary_preferences, no_go, formality_level, communication_style.

    Returns:
        Dict with sliders, speed, acting_instructions, output_format.
    """
    voice = voice_signature.get("voice", voice_signature)

    formality = voice.get("formality_level", 5)
    tone = voice.get("tone", "")
    comm_style = voice.get("communication_style", [])
    sentence_length = voice.get("sentence_length", "Medium")

    # Step 1: Base sliders from formality
    sliders = _map_formality_to_sliders(formality)

    # Step 2: Apply tone keyword deltas
    tone_deltas = _map_tone_keywords(tone)
    for slider, delta in tone_deltas.items():
        sliders[slider] = sliders.get(slider, 0) + delta

    # Step 3: Apply communication style deltas (half weight)
    style_deltas = _map_style_keywords(comm_style)
    for slider, delta in style_deltas.items():
        sliders[slider] = sliders.get(slider, 0) + delta

    # Step 4: Clamp all sliders to [-100, 100]
    sliders = {k: _clamp(v) for k, v in sliders.items()}

    # Step 5: Speed from style + sentence length
    speed = _map_style_to_speed(comm_style, sentence_length)

    # Step 6: Acting instructions
    acting_instructions = _build_acting_instructions(voice)

    config = {
        "sliders": sliders,
        "speed": speed,
        "acting_instructions": acting_instructions,
        "output_format": "mp3",
        "sample_rate": 48000,
    }

    logger.info(
        "Hume voice config generated: formality=%d speed=%.2f sliders=%d dims",
        formality,
        speed,
        len(sliders),
    )

    return config


def validate_voice_config(config: dict[str, Any]) -> tuple[bool, str]:
    """Validate a Hume voice config dict.

    Returns:
        (is_valid, error_message)
    """
    if not isinstance(config, dict):
        return False, "Config must be a dict"

    sliders = config.get("sliders")
    if not isinstance(sliders, dict):
        return False, "Missing 'sliders' dict"

    for name in ALL_SLIDERS:
        val = sliders.get(name)
        if val is None:
            return False, f"Missing slider: {name}"
        if not isinstance(val, (int, float)):
            return False, f"Slider {name} must be numeric, got {type(val).__name__}"
        if val < -100 or val > 100:
            return False, f"Slider {name} out of range: {val}"

    speed = config.get("speed", 1.0)
    if not isinstance(speed, (int, float)) or speed < 0.5 or speed > 2.0:
        return False, f"Speed out of range: {speed}"

    return True, ""
