"""One way to cut a tool result for a model: within the limit, and saying so (#1630)."""

from __future__ import annotations


def cut(text: str, limit: int) -> str:
    """``text`` within ``limit``, saying so when it was cut.

    A silent cut read as the whole answer: on 28 Sep 2026 the dashboard
    assistant counted the 20 archived campaigns it could see in show_status's
    first 3,500 of 6,274 characters and told Denys he had 19; he had 34 (#1630).
    """
    if len(text) <= limit:
        return text
    note = (f"\n[This answer was cut at {limit} of {len(text)} characters. Never count or total a list "
            "from it: what follows the cut is missing. Ask the tool for less, or say it was cut.]")
    if limit < 2 * len(note):
        return text[:limit]  # too small a window to spend on the note
    return text[: limit - len(note)] + note
