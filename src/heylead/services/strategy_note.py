"""The strategy engine's note, kept apart from the operator's preferences.

``context_json.campaign_preferences`` is the operator's own text: tone, topics
to avoid, the rules the copywriter must follow. Until 28 Sep 2026 the strategy
engine appended " | Strategy engine: ..." to it on every cycle whose
recommendation text differed, with no cap and no replacement. One workspace
held 1,197,559 bytes of it across 42 campaigns, up to 589 notes in one value,
and every prompt carried the lot as "CAMPAIGN RULES ... follow them exactly"
(heylead-api#1663).

Now:

- the note lives under ``strategy_note``, replaced each time, capped at
  ``STRATEGY_NOTE_MAX_CHARS``;
- ``operator_preferences`` is the only way a reader gets the operator's text,
  with any legacy notes stripped;
- ``preferences_for_prompt`` is what a prompt reads: the operator's text, the
  note (capped) and an A/B variant instruction, each from its own key.

The hosted twin is heylead-api ``app/services/strategy_note.py``; keep the
marker and the cap the same in both.
"""

from __future__ import annotations

import re
from typing import Any

STRATEGY_NOTE_KEY = "strategy_note"
AB_VARIANT_KEY = "ab_variant_instruction"
STRATEGY_NOTE_MAX_CHARS = 400

# " | Strategy engine: " as the old writer appended it; tolerant of a value
# someone .strip()ped (no leading space) and of doubled whitespace.
_LEGACY_MARKER = re.compile(r"\s*\|\s*Strategy engine:\s*")


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def cap_note(text: Any, limit: int = STRATEGY_NOTE_MAX_CHARS) -> str:
    """One line, at most ``limit`` characters, cut at a word."""
    line = _clean(text)
    if len(line) <= limit:
        return line
    cut = line[: limit - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ;,.") + "…"


def split_legacy_notes(preferences: Any) -> tuple[str, list[str]]:
    """(the operator's text, the appended strategy notes in order)."""
    parts = _LEGACY_MARKER.split(str(preferences or ""))
    head = parts[0].strip()
    notes = [p.strip() for p in parts[1:] if p.strip()]
    return head, notes


def operator_preferences(ctx: dict[str, Any] | None) -> str:
    """The operator's own preferences, with any machine notes stripped."""
    if not isinstance(ctx, dict):
        return ""
    return split_legacy_notes(ctx.get("campaign_preferences"))[0]


def strategy_note(ctx: dict[str, Any] | None) -> str:
    """The current strategy note, capped: its own key, else the last legacy one."""
    if not isinstance(ctx, dict):
        return ""
    note = cap_note(ctx.get(STRATEGY_NOTE_KEY))
    if note:
        return note
    notes = split_legacy_notes(ctx.get("campaign_preferences"))[1]
    return cap_note(notes[-1]) if notes else ""


def normalise_context(ctx: dict[str, Any] | None) -> dict[str, Any]:
    """A copy with legacy notes moved out of the preferences and the note capped."""
    out = dict(ctx or {})
    prefs = out.get("campaign_preferences")
    if isinstance(prefs, str):
        head, notes = split_legacy_notes(prefs)
        if notes:
            out["campaign_preferences"] = head
            if not _clean(out.get(STRATEGY_NOTE_KEY)):
                out[STRATEGY_NOTE_KEY] = notes[-1]
    if STRATEGY_NOTE_KEY in out:
        out[STRATEGY_NOTE_KEY] = cap_note(out[STRATEGY_NOTE_KEY])
    return out


def with_strategy_note(ctx: dict[str, Any] | None, recommendations: list[str]) -> dict[str, Any]:
    """A copy whose note is these recommendations, replacing any earlier one."""
    out = normalise_context(ctx)
    out[STRATEGY_NOTE_KEY] = cap_note("; ".join(_clean(r) for r in recommendations if _clean(r)))
    return out


def preferences_for_prompt(ctx: dict[str, Any] | None) -> str:
    """What a copywriter prompt reads as the campaign's preferences."""
    if not isinstance(ctx, dict):
        return ""
    parts = [operator_preferences(ctx)]
    note = strategy_note(ctx)
    if note:
        parts.append(
            "Suggestion from this campaign's results (the operator's rules above win): "
            + note
        )
    variant = str(ctx.get(AB_VARIANT_KEY) or "").strip()
    if variant:
        parts.append(variant)
    return "\n".join(p for p in parts if p)
