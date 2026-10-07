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

The rule: a segment whose titles name a distinct investor's title (any
goal), or a goal-`partner` segment whose brief or own text names investors
(INVESTOR_BRIEF_TERMS, matched as whole words through heylead.textutil.contains_term),
takes the vocabulary below into its keywords (and into its industries when it
names industries), without duplicates and after the model's own words.
Enriching twice adds nothing.

Goal `partner` is "Find partners or investors": affiliates, resellers,
agencies and tutors are partners too. Until 6 Oct 2026 the goal alone made a
segment an investor's, so Scholify's "Preferred Partners" campaign
(6b9f51d6, org 69df1ecc), which recruits ACCA tutors and academies as
affiliates, took Fund, Investor, Angel, Capital, Ventures, Investment and the
industries Venture Capital, Investment Management and Private Equity on all
three segments. The api's repair-investor-vocabulary route takes that tail off stored rows.
"""

from __future__ import annotations

from typing import Any

from .. import goals
from ..textutil import contains_term
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
#
# Nor does "Investor" (#2348, 7 Oct 2026): role_hit finds it inside "Real
# Estate Investor", "Hotel Investor" and "Property Investor", buyers of an
# asset and not of a startup. Campaign 6caa0f6a (org 24c148bb, goal sell)
# sold an owner's-rep service to new hotel owners and investors; its segment
# took Fund, Angel and Ventures and invited venture capitalists. The venture
# investors who call themselves "... Investor" are named in full instead.
INVESTOR_TITLES: tuple[str, ...] = (
    "General Partner", "Managing Partner", "Partner", "Angel Investor",
    "Investment Director", "Principal", "Venture Partner", "Limited Partner",
    "Fund Manager", "Investor", "Venture Investor", "Seed Investor",
    "Startup Investor", "Venture Capitalist",
)
DISTINCT_INVESTOR_TITLES: tuple[str, ...] = (
    "General Partner", "Angel Investor", "Investment Director", "Venture Partner",
    "Limited Partner", "Fund Manager", "Venture Investor", "Seed Investor",
    "Startup Investor", "Venture Capitalist",
)

# The words that make a partner brief an investor's, matched as whole words
# (plurals are spelled out: "investor" does not match "investors"). Not
# "Capital", "Ventures", "Investment" or "Portfolio": those are an
# affiliate's or an agency's words too ("working capital", "a portfolio of
# clients", "return on investment").
INVESTOR_BRIEF_TERMS: tuple[str, ...] = (
    "investor", "investors", "angel", "angels", "angel investor", "angel investors",
    "business angel", "business angels", "venture capital", "venture capitalist",
    "venture capitalists", "VC", "VCs", "venture fund", "venture funds",
    "fund", "funds", "fund manager", "fund managers", "fundraise", "fundraising",
    "seed round", "seed funding", "pre-seed", "series A", "series B",
    "family office", "family offices", "private equity", "limited partner",
    "limited partners", "general partner", "general partners", "investment fund",
    "investment funds", "investment firm", "investment firms", "raise capital",
    "raising capital", "raise funding", "raising funding",
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


def _segment_text(segment: dict[str, Any]) -> list[str]:
    """The model's own words about a segment: its name, description, titles,
    keywords and industries, one field per entry."""
    fields = [str(segment.get("name") or ""), str(segment.get("description") or "")]
    fields += segment_titles(segment)
    fields += _clean(segment.get("keywords"))
    industries = segment.get("industries")
    fields += _clean(industries.get("include") if isinstance(industries, dict) else industries)
    return [f for f in fields if f]


def names_investors(*texts: str | None) -> bool:
    """True when any of *texts* names investors (INVESTOR_BRIEF_TERMS), as
    whole words."""
    return any(contains_term(text, term) for text in texts if text for term in INVESTOR_BRIEF_TERMS)


def is_investor_segment(
    segment: dict[str, Any], goal: str | None = None, brief: str | None = None,
) -> bool:
    """True for a segment whose titles name a distinct investor's title,
    under any goal; and under goal `partner`, for a segment whose *brief*
    (the campaign's target description) or own text names investors.

    The goal alone never decides: "Find partners or investors" also recruits
    affiliates, resellers and tutors (6 Oct 2026, #2156).
    """
    titles = segment_titles(segment)
    if titles and role_hit(titles, DISTINCT_INVESTOR_TITLES):
        return True
    if (goals.normalize_goal(goal) or "") != goals.PARTNER:
        return False
    return names_investors(brief, *_segment_text(segment))


def enrich_investor_segment(
    segment: dict[str, Any], goal: str | None = None, brief: str | None = None,
) -> dict[str, Any]:
    """The segment with INVESTOR_VOCABULARY merged into its keywords, and
    into its industries when it names industries. A new dict; the model's
    own words stay first; a segment that is not an investor's is returned
    as it came.

    Keywords are a list on a generated persona and a comma-separated string
    on a stored segment; each keeps its shape.
    """
    if not isinstance(segment, dict) or not is_investor_segment(segment, goal, brief):
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


def icp_brief(icp: dict[str, Any], brief: str | None = None) -> str:
    """The brief an ICP was generated from: *brief* when given, else the
    target description it stores, with the model's summary beside it."""
    parts = [brief or icp.get("target_description") or "", icp.get("summary") or ""]
    return " | ".join(str(p) for p in parts if p)


def enrich_investor_icp(icp: Any, goal: str | None = None, brief: str | None = None) -> Any:
    """Every persona (`icps`) or stored segment (`segments`) of an ICP through
    enrich_investor_segment, judged against *brief* (or the ICP's own target
    description and summary). Anything that is not a dict is returned as it came."""
    if not isinstance(icp, dict):
        return icp
    out = dict(icp)
    text = icp_brief(icp, brief)
    for key in ("icps", "segments"):
        personas = icp.get(key)
        if isinstance(personas, list):
            out[key] = [enrich_investor_segment(p, goal, text) for p in personas]
    return out


def enrich_investor_result(result: Any, goal: str | None = None, brief: str | None = None) -> Any:
    """The client's generator holds SingleIcp dataclasses, not dicts: each
    persona's titles, keywords and industries go through
    enrich_investor_segment and the keywords and industries come back onto
    it, in place (the shape apply_seniority_policy uses). Each persona is
    judged against *brief* (the target description) and the result's summary.
    Returns *result*."""
    text = " | ".join(str(t) for t in (brief, getattr(result, "summary", None)) if t)
    for icp in getattr(result, "icps", None) or []:
        titles = getattr(icp, "job_titles", None)
        industries = getattr(icp, "industries", None)
        segment = {
            "name": getattr(icp, "name", None) or "",
            "description": getattr(icp, "description", None) or "",
            "job_titles": {"include": list(getattr(titles, "include", None) or [])},
            "keywords": getattr(icp, "keywords", None),
            "industries": {"include": list(getattr(industries, "include", None) or [])},
        }
        enriched = enrich_investor_segment(segment, goal, text)
        if enriched is segment:
            continue
        icp.keywords = enriched["keywords"]
        if industries is not None:
            industries.include = list(enriched["industries"]["include"])
    return result
