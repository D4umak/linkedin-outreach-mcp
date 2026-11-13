"""Split a LinkedIn headline into (title, company).

A headline reads "<title> at <company>", often with a self-description after
the employer: "Lead Software Engineer | AI/ML | Strategy at Northwestern
Mutual | Python/LangChain". Until 5 Oct 2026 fourteen sites here took
everything after the last " at " as the company, so that contact's Company
read "Northwestern Mutual | Python/LangChain/LangGraph/Pydantic/RAG" (outcome
D4umak/heylead-api#2128). The hosted api had the right cut in its discovery
parser (``app.services.headline_split``, the same code) and nowhere else.

The employer is in the pipe- or bullet-delimited segment that holds the
separator, and stops at the next "|", "•", "·", "—" or "–". The title is
everything before the separator, as it always was.
"""

from __future__ import annotations

import re

# LinkedIn headlines separate role from employer in several ways. Only " at "
# and " @ " are canonical separators that split into title/company for a
# clean headline.
CLEAN_SEPARATORS = (" at ", " @ ")
SEPARATORS = (*CLEAN_SEPARATORS, " — ", " – ")

_SEGMENT_RE = re.compile(r"[^|•·]+")
_COMPANY_TAIL_RE = re.compile(r"\s[—–]\s|[|•·]")


def split_headline(headline: object) -> tuple[str, str]:
    """(title, company) of a headline; ("<headline>", "") when it names none."""
    return _split_with(headline, SEPARATORS)


def split_clean_headline(headline: object) -> tuple[str, str]:
    """(title, company) of a headline, using only canonical separators.

    Separators like " — " and " – " are common in the tail of a headline but
    rarely separate role from employer in a way that produces a clean company
    name.
    """
    title, company = _split_with(headline, CLEAN_SEPARATORS)
    if not title or not company:
        return ("", "")

    # Reject non-role lead-ins before a pipe/bullet: "Helping B2B founders scale | Advisor at Y"
    parts = _COMPANY_TAIL_RE.split(title, 1)
    if len(parts) > 1 and parts[0].strip().lower().startswith(("helping ", "building ", "i help ", "i am ", "passionate ")):
        return ("", "")

    return title, company


def _split_with(headline: object, separators: tuple[str, ...]) -> tuple[str, str]:
    text = str(headline or "").strip()
    for sep in separators:
        if sep not in text:
            continue
        for segment in _SEGMENT_RE.finditer(text):
            at = segment.group().rfind(sep)
            if at < 0:
                continue
            cut = segment.span()[0] + at
            company_tail = text[cut + len(sep):]
            company = _COMPANY_TAIL_RE.split(company_tail, 1)[0]
            return text[:cut].strip(), company.strip()
    return text, ""


def company_from_headline(headline: object) -> str:
    """The employer a headline names, or "" when it names none."""
    return split_headline(headline)[1]
