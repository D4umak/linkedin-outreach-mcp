"""Shared LinkedIn query construction for ICP-driven prospecting.

``create_campaign`` built its Unipile request inline, so the only way to see
which profiles an ICP matches was to create a campaign — and one outreach record
per prospect with it. The query builder and the page sizes now live here, and
``create_campaign`` and ``icp(action='preview')`` both call them, so a preview
sends the same keywords and the same filter dict the campaign would send.

Nothing in this module writes to the database or calls LinkedIn.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Page sizes and page budgets create_campaign searches with. Shared so a
# preview's single page is the page create_campaign's first request would fetch.
CLASSIC_PAGE_SIZE = 50
SALES_NAV_PAGE_SIZE = 100
CLASSIC_MAX_PAGES = 15
# 100 x 25 = 2,500 — Sales Navigator's real result-depth allowance; classic
# stays at 15 x 50. tier.caps_for reads these, so they must not be copied.
SALES_NAV_MAX_PAGES = 25

# Filters that only reach LinkedIn through Sales Navigator. On Classic search
# create_campaign never puts them in the request; some are applied locally after
# the search instead. A preview has to say which of the two happened.
# Same token rules as heylead-api discovery search. Copied so MCP does not
# import the API package.
_PLACE_OR_NATIONALITY_TOKENS = frozenset({
    "ukraine", "ukrainian", "kyiv", "kiev", "lviv", "odesa", "odessa",
    "kharkiv", "dnipro",
    "uk", "united kingdom", "britain", "british", "england", "scotland",
    "united states", "usa", "us", "america", "american",
    "canada", "canadian", "germany", "german", "poland", "polish",
    "france", "french", "india", "indian",
})
_HIRING_INTENT_TOKENS = frozenset({
    "hiring",
    "we're hiring",
    "we are hiring",
    "open role",
    "open roles",
    "now hiring",
    "looking for",
})


_HIRING_INTENT_RE = re.compile(
    r"(?<!\w)("
    + "|".join(
        re.escape(p) for p in sorted(_HIRING_INTENT_TOKENS, key=len, reverse=True)
    )
    + r")(?!\w)",
    re.I,
)


def _is_hiring_intent_token(tok: str) -> bool:
    return bool(_HIRING_INTENT_RE.search(tok))


def _diaspora_identity_token(segment: dict[str, Any]) -> str | None:
    blob = f"{segment.get('name') or ''} {segment.get('keywords') or ''}".lower()
    if "ukrainian" not in blob and "ukraine" not in blob:
        return None
    tokens = [
        t.strip()
        for t in str(segment.get("keywords") or "").replace(";", ",").split(",")
        if t.strip()
    ]
    by_low = {t.lower(): t for t in tokens}
    if "ukrainian" in by_low:
        return by_low["ukrainian"]
    if "ukraine" in by_low:
        return by_low["ukraine"]
    return "Ukrainian"


def _product_stems_for_search(segment: dict[str, Any]) -> str:
    """1–2 product stems; drop titles, places, hiring-intent.

    Diaspora ICPs keep one identity token (Ukrainian preferred) after stems.
    """
    raw = segment.get("keywords") or ""
    tokens = [t.strip() for t in str(raw).replace(";", ",").split(",") if t.strip()]
    titles = {str(t).lower() for t in (segment.get("titles") or [])}
    location_names = {str(x).lower() for x in (segment.get("locations") or [])}
    kept: list[str] = []
    for tok in tokens:
        low = tok.lower()
        if low in titles or low in location_names or low in _PLACE_OR_NATIONALITY_TOKENS:
            continue
        if _is_hiring_intent_token(tok):
            continue
        kept.append(tok)
        if len(kept) >= 2:
            break
    spec = segment.get("profile_signals")
    skip_identity = isinstance(spec, dict) and spec.get("kind") in ("country_tie", "interest")
    identity = None if skip_identity else _diaspora_identity_token(segment)
    if identity and identity.lower() not in {k.lower() for k in kept}:
        kept.append(identity)
    return ", ".join(kept)


SALES_NAV_ONLY_FILTERS = (
    "role_codes",
    "seniority",
    "company_headcount",
    "company_types",
    "department_codes",
    "tenure",
    "spotlight",
    "annual_revenue",
    "company_headcount_growth",
)


def page_size(use_sales_nav: bool) -> int:
    """Results requested per search page."""
    return SALES_NAV_PAGE_SIZE if use_sales_nav else CLASSIC_PAGE_SIZE


def max_pages(use_sales_nav: bool) -> int:
    """How many pages create_campaign will paginate through."""
    return SALES_NAV_MAX_PAGES if use_sales_nav else CLASSIC_MAX_PAGES


def build_segment_query(
    segment: dict[str, Any],
    use_sales_nav: bool,
    abm_company_name: str = "",
) -> tuple[str, dict[str, Any]]:
    """Build the (keywords, structured filters) pair for one ICP segment.

    Extracted verbatim from create_campaign's search loop. The returned filter
    dict is passed straight to ``client.search_people`` as keyword arguments.
    """
    keywords = segment.get("keywords", "")
    titles = segment.get("titles", [])
    has_structured = segment.get("has_structured", False)

    # Build search keywords as fallback
    search_keywords = keywords
    if not has_structured and titles and titles[0].lower() not in keywords.lower():
        search_keywords = f"{titles[0]} {keywords}"

    # ABM: prepend company name to keywords for company-targeted search
    if abm_company_name:
        title_part = titles[0] if titles else ""
        search_keywords = f"{title_part} {abm_company_name}".strip()

    # Extract structured filters (enriched LinkedIn codes)
    search_filters: dict[str, Any] = {}
    if segment.get("location_codes"):
        search_filters["location_codes"] = segment["location_codes"]

    product_kw = _product_keyword_clause(segment)
    structured = bool(has_structured or segment.get("industry_codes"))

    if structured:
        if segment.get("industry_codes"):
            search_filters["industry_codes"] = segment["industry_codes"]

        if use_sales_nav:
            if segment.get("title_codes"):
                search_filters["role_codes"] = segment["title_codes"]
            if segment.get("seniority"):
                search_filters["seniority"] = segment["seniority"]
            if segment.get("company_headcount"):
                search_filters["company_headcount"] = segment["company_headcount"]
            if segment.get("company_types"):
                search_filters["company_types"] = segment["company_types"]
            if segment.get("department_codes"):
                search_filters["department_codes"] = segment["department_codes"]
            if segment.get("tenure"):
                search_filters["tenure"] = segment["tenure"]
            if segment.get("spotlight"):
                search_filters["spotlight"] = segment["spotlight"]
            if segment.get("annual_revenue"):
                search_filters["annual_revenue"] = segment["annual_revenue"]
            if segment.get("company_headcount_growth"):
                search_filters["company_headcount_growth"] = segment["company_headcount_growth"]
            if segment.get("boolean_keywords"):
                search_keywords = segment["boolean_keywords"]
            elif search_filters.get("role_codes"):
                search_keywords = product_kw
        else:
            title_kw = " OR ".join(titles[:3]) if titles else ""
            # Classic has no title filter — keywords ARE the query.
            # OR-ing titles with product terms returns a page of CEOs
            # and the fit veto then empties the campaign. AND keeps
            # LinkedIn on people who mention the product.
            if title_kw and product_kw:
                search_keywords = f"({title_kw}) AND ({product_kw})"
            elif product_kw:
                search_keywords = product_kw
            elif title_kw:
                search_keywords = title_kw
            elif search_filters.get("industry_codes"):
                search_keywords = ""

        logger.info(
            "Searching with %d structured filters for '%s'",
            len(search_filters), segment.get("name"),
        )
    else:
        if titles:
            search_filters["title_keywords"] = titles[:3]
        if not abm_company_name:
            search_keywords = _product_stems_for_search(segment)

    return search_keywords, search_filters


def _product_keyword_clause(segment: dict[str, Any]) -> str:
    """Keep license/product tokens that title/role filters must not wipe."""
    from .icp_match_scorer import precision_stems_from_text

    blob = " ".join(
        str(segment.get(key) or "")
        for key in ("keywords", "boolean_keywords", "description")
    )
    stems = precision_stems_from_text(blob)
    if not stems:
        return ""
    parts = [f'"{stem}"' if " " in stem else stem for stem in stems]
    return " OR ".join(parts)


def resolve_icp_record(icp_id: str) -> dict[str, Any] | None:
    """Load a saved ICP by full id, falling back to an id prefix.

    Read-only. Callers pass a truncated id (``a1b2c3d4...``) constantly because
    that is what generate_icp prints, so the prefix fallback is the normal path.
    """
    from ..db.queries import get_icp, list_icps

    if not icp_id:
        return None

    record = get_icp(icp_id)
    if record:
        return record

    prefix = icp_id.rstrip(".")
    if not prefix:
        return None
    for candidate in list_icps():
        if candidate["id"].startswith(prefix):
            return candidate
    return None
