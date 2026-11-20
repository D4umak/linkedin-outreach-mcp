"""What a job title in an ICP may be, and how a list of them is read from text.

A leaf module: the ICP generator, the campaign segments and the repair import
it, so it imports nothing of the app.

Two rules, both from "000 Staff" (heylead-api#2483). A brief that said
"companies with 51-1,000 staff" was split on its commas, so the thousands
separator cut "1,000" in two, and "000 staff" was kept as a title because
"Staff" alone reads as a seniority level. So:

* a list is split on a comma only where it is not a thousands separator
  (``split_list_text``);
* a title names a role, so its first word is not a number: "1,000+
  employees" and "51-200 people" are headcounts (``is_job_title``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

# A comma or semicolon that separates items: not one between two digits ("1,000").
_ITEM_SEPARATOR = re.compile(r"\s*(?:;|,(?!\d{3}\b)|(?<!\d),)\s*")

# A first word made only of digits, separators and "+": "000", "51-1,000", "1,000+", "12".
_NUMBER_WORD = re.compile(r"^[\d][\d,.+\-–—]*$")
_LETTER = re.compile(r"[A-Za-z]")


def split_list_text(text: str | None) -> list[str]:
    """Items of a comma- or semicolon-separated list, keeping "1,000" whole."""
    return [part.strip() for part in _ITEM_SEPARATOR.split(text or "") if part and part.strip()]


def is_job_title(text: Any) -> bool:
    """Whether ``text`` can be a job title: it has letters and its first word is not a number.

    A title may contain a digit ("3D Artist", "Tier 2 Support Lead"), but it
    does not start with a number: "000 Staff", "51-200 employees", "1000 FTE"
    and "51 - 200 Employees" are headcounts.
    """
    value = str(text or "").strip()
    if not _LETTER.search(value):
        return False
    return not _NUMBER_WORD.match(value.split()[0])


def clean_titles(titles: Iterable[Any] | None) -> list[str]:
    """The entries of ``titles`` that are job titles, in order."""
    return [str(t).strip() for t in (titles or []) if is_job_title(t)]


def strip_non_titles(icp: dict[str, Any]) -> list[str]:
    """Remove non-titles from every title list in a stored or generated ICP. Returns what went.

    The lists: ``icps[].job_titles.include`` and the ``job_titles`` entry of
    ``icps[].field_evidence`` (generated ICPs), and ``segments[].titles``
    (campaigns). Edits ``icp`` in place.
    """
    removed: list[str] = []

    def _clean(values: Any) -> Any:
        if not isinstance(values, list):
            return values
        for v in values:
            if not is_job_title(v):
                text = str(v)
                if text not in removed:
                    removed.append(text)
        return [v for v in values if is_job_title(v)]

    for persona in icp.get("icps") or []:
        if not isinstance(persona, dict):
            continue
        titles = persona.get("job_titles")
        if isinstance(titles, dict) and isinstance(titles.get("include"), list):
            titles["include"] = _clean(titles["include"])
        elif isinstance(titles, list):
            persona["job_titles"] = _clean(titles)
        for evidence in persona.get("field_evidence") or []:
            if isinstance(evidence, dict) and evidence.get("field_name") == "job_titles":
                if isinstance(evidence.get("value"), list):
                    evidence["value"] = _clean(evidence["value"])
    for segment in icp.get("segments") or []:
        if isinstance(segment, dict) and isinstance(segment.get("titles"), list):
            segment["titles"] = _clean(segment["titles"])
    return removed
