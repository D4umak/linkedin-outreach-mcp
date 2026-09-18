"""Memory formatter — format outreach memory for prompt injection.

Converts the structured memory list (stored in outreaches.memory_json)
into a human-readable "PREVIOUSLY USED" block that gets injected into
the v63 followup_reasoning prompt via the {previously_used} variable.

This gives the LLM explicit anti-repetition context:
- What CTAs were already used (don't repeat)
- What pain angles were covered (rotate to new ones)
- What structure/length was used (vary it)
- What greetings/openings were used (use fresh ones)
"""

from __future__ import annotations

from typing import Any


def format_memory_for_prompt(memory: list[dict[str, Any]]) -> str:
    """Format outreach memory into a 'PREVIOUSLY USED' prompt block.

    Args:
        memory: List of per-followup decision records from memory_json.
                Each record has: followup_number, cta_used, pain_used,
                structure, greeting, signoff, had_bullets, had_link,
                first_sentence, hook_angle.

    Returns:
        Formatted text block for prompt injection.
        Returns empty string if no memory entries.
    """
    if not memory:
        return ""

    lines = ["PREVIOUSLY USED (do NOT repeat these — use fresh alternatives):"]

    for entry in memory:
        num = entry.get("followup_number", "?")
        lines.append(f"\nFollow-up #{num}:")

        cta = entry.get("cta_used", "")
        if cta:
            lines.append(f'  CTA: "{cta}"')

        pain = entry.get("pain_used", "")
        if pain:
            lines.append(f"  Pain angle: {pain}")

        structure = entry.get("structure", "")
        bullets = entry.get("had_bullets", False)
        structure_desc = structure
        if bullets:
            structure_desc += " (with bullets)"
        if structure_desc:
            lines.append(f"  Structure: {structure_desc}")

        greeting = entry.get("greeting", "")
        if greeting:
            lines.append(f'  Greeting: "{greeting}"')

        first_sentence = entry.get("first_sentence", "")
        if first_sentence:
            # Truncate long first sentences
            display = first_sentence[:60] + "..." if len(first_sentence) > 60 else first_sentence
            lines.append(f'  Opening: "{display}"')

        hook = entry.get("hook_angle", "")
        if hook:
            lines.append(f"  Hook: {hook}")

        had_link = entry.get("had_link", False)
        if had_link:
            lines.append("  Included link: yes")

    return "\n".join(lines)
