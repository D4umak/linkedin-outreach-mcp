"""Structured follow-up reasoning schemas — Pydantic models.

Ported from the original AI in Charge FollowUpReasoning models
(core/models.py lines 40-78). These models enforce structured output
from the reasoning LLM call, ensuring Stage 2 receives consistent,
well-typed decisions instead of raw text.

Used in followup_generator.py's v63 2-stage pipeline:
- Stage 1 (Reasoning) → FollowUpReasoning (FollowUpDecisions + MessageSkeleton)
- Stage 2 (Generate) → plain text message using flattened decisions
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class FollowUpDecisions(BaseModel):
    """Strategic decisions from the reasoning stage.

    Each field maps to a specific guideline in the followup_reasoning prompt:
    link-gating, bullets policy, CTA rotation, length style, pain-first pattern,
    fresh opener selection, and anti-repetition controls.
    """

    structure_change: str = Field(
        description="What structure will change vs last SDR message"
    )
    fresh_hook: str = Field(
        description="Chosen opening idea, 6-12 words"
    )
    phrase_rewrites: list[str] = Field(
        default_factory=list,
        description="Key phrases to avoid repeating, 3-6 items",
    )
    new_context: str = Field(
        description="New benefit/challenge not used before, 1 sentence"
    )
    pain_to_use: str = Field(
        description="One concrete symptom phrased plainly"
    )
    solve_clause: str = Field(
        description="One short how-we-solve clause, plain English"
    )
    micro_proof: Optional[str] = Field(
        default=None,
        description="Optional, anonymized outcome or mechanism",
    )
    cta_choice: str = Field(
        description="One from CTA LIBRARY not used recently"
    )
    greeting_variant: str = Field(
        default="",
        description="Greeting choice (or empty for no greeting)",
    )
    signoff_variant: str = Field(
        default="",
        description="Signoff choice",
    )
    length_style: Literal["one_liner", "short", "medium"] = Field(
        default="short",
        description='Length style: "one_liner" (≤120), "short" (≤220), "medium" (≤350)',
    )
    allow_bullets: bool = Field(
        default=False,
        description="Whether bullets are allowed (false if last 2 SDR msgs had bullets)",
    )
    allow_link: bool = Field(
        default=False,
        description="Whether link is allowed (true only if SDR msg count ≥ 2)",
    )
    human_tell: str = Field(
        default="",
        description="Human signal to use (e.g., weekday touch, permission phrasing)",
    )
    first_sentence_words_to_avoid: list[str] = Field(
        default_factory=list,
        description="Words to avoid from last first sentence",
    )
    banned_word_scan: list[str] = Field(
        default_factory=list,
        description="Risky words found in draft ideas",
    )


class MessageSkeleton(BaseModel):
    """Message structure skeleton from reasoning stage.

    Provides a pre-planned sentence-level structure that the
    generation stage fills in with natural language.
    """

    opening: str = Field(
        description="One sentence opening based on fresh_hook"
    )
    symptom: str = Field(
        description="One sentence with the chosen pain"
    )
    solve: str = Field(
        description="One clause with the solve_clause"
    )
    proof: Optional[str] = Field(
        default=None,
        description="Optional one short metric or mechanism",
    )
    cta: str = Field(
        description="CTA choice as a question"
    )


class FollowUpReasoning(BaseModel):
    """Complete follow-up reasoning output from Stage 1.

    Contains both strategic decisions and a message skeleton
    that Stage 2 uses to generate the final message text.
    """

    decisions: FollowUpDecisions
    message_skeleton: MessageSkeleton


# ── JSON schema for LLM prompt injection ──

FOLLOWUP_REASONING_SCHEMA = FollowUpReasoning.model_json_schema()


def get_schema_hint() -> str:
    """Return a compact JSON schema description for prompt injection.

    Used when the LLM doesn't support native structured output —
    we inject the schema into the prompt so the LLM knows what
    JSON structure to produce.
    """
    return """{
  "decisions": {
    "structure_change": "string — what changes vs last SDR message",
    "fresh_hook": "string — opening idea, 6-12 words",
    "phrase_rewrites": ["string — phrases to avoid, 3-6 items"],
    "new_context": "string — new benefit/challenge, 1 sentence",
    "pain_to_use": "string — concrete symptom, plain English",
    "solve_clause": "string — how-we-solve clause",
    "micro_proof": "string|null — anonymized outcome",
    "cta_choice": "string — from CTA LIBRARY",
    "greeting_variant": "string — greeting or empty",
    "signoff_variant": "string — signoff",
    "length_style": "one_liner|short|medium",
    "allow_bullets": true/false,
    "allow_link": true/false,
    "human_tell": "string — human signal",
    "first_sentence_words_to_avoid": ["words from last first sentence"],
    "banned_word_scan": ["risky words found"]
  },
  "message_skeleton": {
    "opening": "string — one sentence from fresh_hook",
    "symptom": "string — one sentence with chosen pain",
    "solve": "string — one clause with solve_clause",
    "proof": "string|null — short metric or mechanism",
    "cta": "string — CTA as a question"
  }
}"""
