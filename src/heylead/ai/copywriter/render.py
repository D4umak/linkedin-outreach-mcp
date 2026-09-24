"""The rules table as the block a prompt carries.

One entry point, `rules_for(channel)`, called from `llm._call_llm` so that a
prompt gets its channel's rules without its generator doing anything. Rendered
BEFORE the operator's own rules, which say in their own words that they win
when they conflict.
"""

from __future__ import annotations

from .rules import CHANNELS, RULES

HEADING = "## HOUSE STYLE"

_INTRO = (
    "How this sender writes, whatever the task above asks for. A line that "
    "breaks one of these reads as a machine wrote it."
)

_FAMILY_ORDER = ("voice", "openers", "vocabulary", "grounding", "names", "typography")

_CACHE: dict[str, str] = {}


def rules_for(channel: str) -> str:
    """The house rules that bind *channel*, as prompt text.

    "" renders nothing: that is the judge path, a call that writes no words a
    person will read. An unknown channel raises rather than rendering nothing,
    because a typo that silently unruled a channel is the defect this table
    exists to end.
    """
    if not channel:
        return ""
    if channel not in CHANNELS:
        raise ValueError(
            f"unknown copy channel {channel!r}; add it to copywriter.rules.CHANNELS "
            f"or pass rules=False if this prompt writes nothing a person reads"
        )
    if channel in _CACHE:
        return _CACHE[channel]

    bound = [rule for rule in RULES if rule.binds(channel)]
    by_family: dict[str, list[str]] = {}
    for rule in bound:
        by_family.setdefault(rule.family, []).append(rule.wording(channel))

    lines = [HEADING, _INTRO, ""]
    for family in _FAMILY_ORDER:
        for text in by_family.get(family, []):
            lines.append(f"- {text}")
    block = "\n".join(lines).strip()
    _CACHE[channel] = block
    return block
