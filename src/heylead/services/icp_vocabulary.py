"""An investor ICP carries investor vocabulary (Denys, 5 Oct 2026, heylead-api#2156).

One table, stated in heylead-api's app/services/icp_vocabulary.py and
mirrored here word for word (tests/test_an_investor_icp_carries_investor_vocabulary.py
compares the two when an api checkout is found). It is merged into a generated
segment after the model answers, so the words a venture firm uses for itself
are the product's knowledge and not something each ICP call has to rediscover.

Why: with "a title alone is not a fit" (heylead-api#2149, icp_match_scorer's
apply_title_only_cap), a row needs an industry or keyword hit besides its
title to clear the floor. Mousum Gogoi's investor ICP (campaign 53298d40, org
eafcb9cf) had the keywords "General Partner, Managing Partner, Venture
Capital, Capital Markets, Deep Tech, Hardware, Aerospace" and the industries
Venture Capital and Capital Markets: nothing said Fund, Investor or Angel, so
"Co-founder, General Partner, Bharat Innovation Fund" and "Early-growth
equity Investor | Cactus Partners (General Partner)" were held as title-only,
while 3one4 Capital and Elev8 Venture Partners passed only because "Capital"
and "Venture" happen to be in their names.

The rule: a segment generated for goal `partner`, or any segment whose titles
name an investor's title, takes the vocabulary below into its keywords (and
into its industries when it names industries), without duplicates and after
the model's own words. Enriching twice adds nothing. A stored campaign is not
rewritten by this module: it runs on generation only, and the rescore route
shows the effect on an existing campaign on request.
"""

from __future__ import annotations

from typing import Any

from .. import goals
from .profile_signals import role_hit

INVESTOR_VOCABULARY: dict[str, list[str]] = {
    "keywords": [
        "Fund", "Investor", "Angel", "Capital", "Ventures", "Venture",
        "Investment", "Portfolio",
    ],
    "industries": ["Venture Capital", "Investment Management", "Private Equity"],
}

# The titles an investor holds (the issue's list). Under goal `partner` any
# segment takes the vocabulary; under another goal a segment takes it when its
# titles name one of the DISTINCT ones. "Managing Partner", "Partner" and
# "Principal" are also a law firm's, an accountancy's and an engineer's
# ("Principal Engineer" is a role_hit on "Principal"), so on their own they do
# not turn a sell or hire segment into an investor segment.
INVESTOR_TITLES: tuple[str, ...] = (
    "General Partner", "Managing Partner", "Partner", "Angel Investor",
    "Investment Director", "Principal", "Venture Partner", "Limited Partner",
    "Fund Manager", "Investor",
)
DISTINCT_INVESTOR_TITLES: tuple[str, ...] = (
    "General Partner", "Angel Investor", "Investment Director", "Venture Partner",
    "Limited Partner", "Fund Manager", "Investor",
)


def _clean(values: Any) -> list[str]:
    if isinstance(values, str):
        values = values.split(",")
    if not isinstance(values, (list, tuple)):
        return []
    return [str(v).strip() for v in values if str(v or "").strip()]


def _union(own: list[str], extra: list[str]) -> list[str]:
    """*own* first, then each of *extra* that is not already there (case-insensitively)."""
    out: list[str] = []
    seen: set[str] = set()
    for word in [*own, *extra]:
        key = word.lower()
        if key not in seen:
            seen.add(key)
            out.append(word)
    return out


def segment_titles(segment: dict[str, Any]) -> list[str]:
    """The titles of a generated persona (`job_titles.include`) or of a stored
    segment (`titles`)."""
    titles = segment.get("job_titles")
    if isinstance(titles, dict):
        return _clean(titles.get("include"))
    return _clean(segment.get("titles"))


def is_investor_segment(segment: dict[str, Any], goal: str | None = None) -> bool:
    """True for goal `partner`, and for a segment whose titles name an
    investor's title under any goal."""
    if (goals.normalize_goal(goal) or "") == goals.PARTNER:
        return True
    titles = segment_titles(segment)
    return bool(titles) and bool(role_hit(titles, DISTINCT_INVESTOR_TITLES))


def enrich_investor_segment(segment: dict[str, Any], goal: str | None = None) -> dict[str, Any]:
    """The segment with INVESTOR_VOCABULARY merged into its keywords, and
    into its industries when it names industries. A new dict; the model's
    own words stay first; a segment that is not an investor's is returned
    as it came.

    Keywords are a list on a generated persona and a comma-separated string
    on a stored segment; each keeps its shape.
    """
    if not isinstance(segment, dict) or not is_investor_segment(segment, goal):
        return segment
    out = dict(segment)
    raw_keywords = segment.get("keywords")
    merged = _union(_clean(raw_keywords), INVESTOR_VOCABULARY["keywords"])
    out["keywords"] = ", ".join(merged) if isinstance(raw_keywords, str) else merged
    industries = segment.get("industries")
    if isinstance(industries, dict):
        include = _clean(industries.get("include"))
        if include:
            out["industries"] = {
                **industries, "include": _union(include, INVESTOR_VOCABULARY["industries"]),
            }
    elif _clean(industries):
        out["industries"] = _union(_clean(industries), INVESTOR_VOCABULARY["industries"])
    return out


def enrich_investor_icp(icp: Any, goal: str | None = None) -> Any:
    """Every persona (`icps`) or stored segment (`segments`) of an ICP through
    enrich_investor_segment. Anything that is not a dict is returned as it came."""
    if not isinstance(icp, dict):
        return icp
    out = dict(icp)
    for key in ("icps", "segments"):
        personas = icp.get(key)
        if isinstance(personas, list):
            out[key] = [enrich_investor_segment(p, goal) for p in personas]
    return out


def enrich_investor_result(result: Any, goal: str | None = None) -> Any:
    """The client's generator holds SingleIcp dataclasses, not dicts: each
    persona's titles, keywords and industries go through
    enrich_investor_segment and the keywords and industries come back onto
    it, in place (the shape apply_seniority_policy uses). Returns *result*."""
    for icp in getattr(result, "icps", None) or []:
        titles = getattr(icp, "job_titles", None)
        industries = getattr(icp, "industries", None)
        segment = {
            "job_titles": {"include": list(getattr(titles, "include", None) or [])},
            "keywords": getattr(icp, "keywords", None),
            "industries": {"include": list(getattr(industries, "include", None) or [])},
        }
        enriched = enrich_investor_segment(segment, goal)
        if enriched is segment:
            continue
        icp.keywords = enriched["keywords"]
        if industries is not None:
            industries.include = list(enriched["industries"]["include"])
    return result
