"""Luca keeps only the filters a brief asked for (D4umak/heylead-api#7114).

The ICP prompt once showed a tenure of min 1, max 5 and a
departments include as plain example values, and the model copied them: on
19 Nov 2026, 171 of 171 non-staff personas generated in 60 days carried a
tenure while 0 of their briefs named years, and 14 of 18 founder personas
carried a department they do not work in. The prompt no longer shows them;
this guard makes the answer obey whatever the model writes.

- **tenure** is always dropped. No brief asked for it, no hosted search reads
  it, and the code cannot say whether LinkedIn's tenure means time in role,
  at the company or in total. A user can still set it by editing the ICP.
- **departments** are dropped on a founder persona (every job title is a
  founder, CEO, owner, managing director or president title) unless the brief
  names that department as a function. "marketing agency owners" names an
  industry, not a department. A persona with any other title keeps its
  departments: a Head of Sales works in Sales.

The founder test reads job titles, never seniority: ``apply_seniority_policy``
adds ``owner`` to every decision-maker persona. The api carries the same
rule in heylead-api ``app/services/icp_example_fields.py``; keep the two alike.
"""

from __future__ import annotations

import re
from typing import Any

from ..textutil import contains_term

FOUNDER_TITLE_TERMS = (
    "founder", "co-founder", "cofounder", "ceo", "chief executive", "owner",
    "managing director", "president",
)
# Titles that carry a founder word but name someone inside a function.
NOT_FOUNDER_TITLE_TERMS = ("vice president", "product owner", "process owner", "business owner")

# A department word followed by one of these names a kind of company.
ORG_NOUNS = (
    "agency", "agencies", "consultancy", "consultancies", "firm", "firms", "studio", "studios",
    "company", "companies", "business", "businesses", "shop", "shops",
)
_ORG_NOUN = "|".join(ORG_NOUNS)


def _founder_title(title: Any) -> bool:
    # The one title matcher (it knows "CEO" is the chief executive officer).
    from ..services.profile_signals import role_hit

    text = str(title or "")
    if role_hit(text, NOT_FOUNDER_TITLE_TERMS):
        return False
    return bool(role_hit(text, FOUNDER_TITLE_TERMS))


def is_founder_persona(persona: Any) -> bool:
    """True when the persona has job titles and every one is a founder-level title."""
    if not isinstance(persona, dict):
        return False
    titles = persona.get("job_titles")
    include = titles.get("include") if isinstance(titles, dict) else None
    if not isinstance(include, list):
        return False
    named = [t for t in include if str(t or "").strip()]
    return bool(named) and all(_founder_title(t) for t in named)


def brief_names_department(brief: str | None, department: Any) -> bool:
    """True when *brief* names *department* as a function, not as an industry."""
    text, term = str(brief or ""), str(department or "").strip()
    if not contains_term(text, term):
        return False
    words = r"\s+".join(re.escape(w) for w in term.split())
    pattern = re.compile(
        rf"(?<![0-9a-z]){words}(?![0-9a-z])(?!\s+(?:{_ORG_NOUN})(?![0-9a-z]))", re.IGNORECASE,
    )
    return bool(pattern.search(text))


def _guard_persona(persona: dict, brief: str) -> dict:
    out = dict(persona)
    if "tenure" in out:
        out["tenure"] = None
    if is_founder_persona(out):
        departments = out.get("departments")
        include = departments.get("include") if isinstance(departments, dict) else None
        kept = [d for d in include or [] if brief_names_department(brief, d)]
        out["departments"] = {"include": kept, "exclude": []} if kept else None
    return out


def drop_unasked_fields(data: Any, brief: str | None) -> Any:
    """The ICP answer with copied tenure and unasked founder departments removed.

    Returns a new dict; anything that is not a persona dict is left as it is.
    """
    if not isinstance(data, dict) or not isinstance(data.get("icps"), list):
        return data
    text = str(brief or "")
    out = dict(data)
    out["icps"] = [_guard_persona(p, text) if isinstance(p, dict) else p for p in data["icps"]]
    return out
