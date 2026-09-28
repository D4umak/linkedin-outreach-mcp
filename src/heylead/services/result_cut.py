"""One way to cut a tool result for a model: within the limit, between lines, and saying so (#1630)."""

from __future__ import annotations


def cut(text: str, limit: int) -> str:
    """``text`` within ``limit``, cut at a line end and saying so when it was cut.

    A silent cut read as the whole answer: on 28 Sep 2026 the dashboard
    assistant counted the 20 archived campaigns it could see in show_status's
    first 3,500 of 6,274 characters and told Denys he had 19; he had 34 (#1630).

    The note is also shown to the person, in the tool card, so it speaks to
    both; and the cut falls at the last line end that fits, since a cut inside
    a Markdown link rendered a live half URL ("https://www.linkedi", UI QA of
    #1631).
    """
    if len(text) <= limit:
        return text
    note = (f"\n[Showing the first part of this result ({{shown}} of {len(text)} characters): it is not complete, "
            "so a list here is not the whole list and must not be counted as one.]")
    room = limit - len(note) - 8
    if room < len(note):
        return text[:limit]  # too small a window to spend on the note
    head = text[:room]
    line_end = head.rfind("\n")
    if line_end >= room // 2:
        head = head[:line_end]
    return head + note.replace("{shown}", str(len(head)))
