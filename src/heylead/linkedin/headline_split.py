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
# was handled once, so "VP Engineering @ Resident" parsed no company at all.
SEPARATORS = (" at ", " @ ", " — ", " – ")

_SEGMENT_RE = re.compile(r"[^|•·]+")
_COMPANY_TAIL_RE = re.compile(r"\s[—–]\s|[|•·]")


def split_headline(headline: object) -> tuple[str, str]:
    """(title, company) of a headline; ("<headline>", "") when it names none."""
    text = str(headline or "").strip()
    for sep in SEPARATORS:
        if sep not in text:
            continue
        for segment in _SEGMENT_RE.finditer(text):
            at = segment.group().rfind(sep)
            if at < 0:
                continue
            cut = segment.start() + at
            company = _COMPANY_TAIL_RE.split(text[cut + len(sep):], 1)[0]
            return text[:cut].strip(), company.strip()
    return text, ""


def company_from_headline(headline: object) -> str:
    """The employer a headline names, or "" when it names none."""
    return split_headline(headline)[1]
