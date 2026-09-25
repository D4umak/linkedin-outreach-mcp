"""LinkedIn parameter enrichment — resolve LLM-generated names to LinkedIn codes.

Calls the Unipile search/parameters API to map ICP field values
(e.g., "Information Technology") to LinkedIn's internal codes.
Primary validation: LLM-based filtering (filter_candidates prompt).
Fallback: heuristic Jaccard similarity when LLM is unavailable.

Ported from the original AI in Charge IcpParamRetriever.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from ..constants import LLM_TIER_FAST
from .icp_schemas import (
    EnrichedField,
    EnrichedIcpParams,
    EnrichedParam,
    SingleIcp,
)
from .llm import loads_json_object as parse_json
from .prompt_loader import get_prompt_temperature, has_prompt, render_prompt

logger = logging.getLogger(__name__)

# Unipile types only a Sales Navigator seat can answer. Same list as
# heylead-api network_router.SALES_NAV_ONLY_PARAMETER_TYPES.
SALES_NAV_ONLY_PARAMETER_TYPES = frozenset({
    "DEPARTMENT",
    "SALES_INDUSTRY",
    "PERSONA",
    "ACCOUNT_LISTS",
    "LEAD_LISTS",
    "TECHNOLOGIES",
    "SAVED_ACCOUNTS",
    "SAVED_SEARCHES",
    "RECENT_SEARCHES",
    "REGION",
    "POSTAL_CODE",
    "GROUPS",
})

# Field name → Unipile search parameter type
_FIELD_TO_TYPE: dict[str, str] = {
    "job_titles": "JOB_TITLE",
    "industries": "INDUSTRY",
    "locations": "LOCATION",
    "company_locations": "LOCATION",
    "departments": "DEPARTMENT",
}

# Minimum Jaccard similarity for fuzzy matching
_JACCARD_THRESHOLD = 0.5

# Tokens that carry seniority or function-shape but say nothing about the
# DOMAIN. "Head of Telematics" and "Head of Banking" share two of three tokens,
# so plain Jaccard scores them 0.5 and lets every "Head of X" in the catalogue
# through — that is how one requested title became 44 stored ones, including
# Head of School, Head of Laboratory and Head of Banking on a telematics ICP.
# A match has to agree on the domain word, not just the rank word.
_GENERIC_TITLE_TOKENS = frozenset({
    "head", "of", "the", "and", "chief", "officer", "director", "vp",
    "vice", "president", "lead", "leader", "manager", "management",
    "senior", "global", "group", "deputy", "assistant", "executive",
    "principal", "acting", "interim", "joint",
})


def _domain_tokens(tokens: set[str]) -> set[str]:
    """The part of a title that identifies the field rather than the rank."""
    return tokens - _GENERIC_TITLE_TOKENS

# Location aliases for common variants
_LOCATION_ALIASES: dict[str, set[str]] = {
    "united states": {"united states", "united states of america", "usa", "us", "u s a", "u s"},
    "united kingdom": {"united kingdom", "uk", "great britain", "gb"},
}

# Industry aliases — maps LLM-generated names to LinkedIn's canonical names.
# Keys are _norm()'d LinkedIn canonical names; values include common LLM variants.
_INDUSTRY_ALIASES: dict[str, set[str]] = {
    "hospital  health care": {
        "hospital  health care", "hospital and health care", "healthcare",
        "health care", "hospitals", "medical", "hospital health care",
    },
    "biotechnology": {"biotechnology", "biotech", "bio technology"},
    "pharmaceuticals": {"pharmaceuticals", "pharma", "pharmaceutical"},
    "financial services": {"financial services", "finserv", "finance"},
    "banking": {"banking", "banks", "bank"},
    "insurance": {"insurance", "insurtech"},
    "investment management": {
        "investment management", "investment", "asset management",
        "wealth management", "portfolio management",
    },
    "information technology and services": {
        "information technology and services", "information technology",
        "it services", "technology", "tech",
    },
    "computer software": {"computer software", "software", "saas"},
    "government administration": {
        "government administration", "government", "govt", "public sector",
        "public administration",
    },
    "defense  space": {
        "defense  space", "defense and space", "defense", "defence",
        "aerospace", "military", "defense space",
    },
    "consumer goods": {"consumer goods", "cpg", "fmcg", "consumer products"},
    "retail": {"retail", "ecommerce", "e commerce", "e-commerce"},
    "e-commerce": {"e-commerce", "ecommerce", "online retail", "retail"},
    "staffing and recruiting": {
        "staffing and recruiting", "staffing", "recruiting", "recruitment",
        "talent acquisition",
    },
    "management consulting": {
        "management consulting", "consulting", "consultancy",
        "professional services",
    },
    "marketing and advertising": {
        "marketing and advertising", "marketing", "advertising",
        "digital marketing",
    },
    "telecommunications": {"telecommunications", "telecom", "telco"},
    "oil  energy": {"oil  energy", "oil and energy", "oil", "energy", "oil energy"},
    "real estate": {"real estate", "property", "realty"},
    "education management": {
        "education management", "education", "edtech", "higher education",
    },
    "automotive": {"automotive", "auto", "cars", "vehicles"},
    "construction": {"construction", "building", "infrastructure"},
    "logistics and supply chain": {
        "logistics and supply chain", "logistics", "supply chain",
        "transportation",
    },
    "media production": {"media production", "media", "entertainment", "film"},
}


def _seat_cannot_lookup(field_name: str, search_seat_has_sales_nav: bool | None) -> bool:
    """True when this field needs Sales Navigator and the resolved seat has none.

    None means the seat is unknown (the hosted balancer picks). False is a
    resolved Premium-only seat: asking would 401.
    """
    if search_seat_has_sales_nav is not False:
        return False
    return _FIELD_TO_TYPE.get(field_name, "") in SALES_NAV_ONLY_PARAMETER_TYPES


async def enrich_icp_linkedin_params(
    icp: SingleIcp,
    get_params_fn: Any,
    *,
    search_seat_has_sales_nav: bool | None = None,
) -> EnrichedIcpParams:
    """Resolve ICP field values to LinkedIn search parameter codes.

    Args:
        icp: A SingleIcp with LLM-generated field values.
        get_params_fn: Async callable(type, keywords) -> list[{name, code}]
            Typically bound to BackendClient.get_search_params or
            UnipileClient equivalent.

    Returns:
        EnrichedIcpParams with validated LinkedIn codes. Only fields the ICP
        itself carries are attempted; the others stay None whether or not the
        incoming ICP had codes for them, exactly as before. Of the fields that
        ARE attempted, one whose parameter lookup was unavailable is named in
        ``failed_fields`` and is not overwritten with the degraded result — see
        _store_field for what it keeps instead.
    """
    result = EnrichedIcpParams()
    icp_name = icp.name or "Unknown ICP"
    previous = icp.linkedin_enriched_params

    # Industries
    if icp.industries and icp.industries.include:
        enriched, failed = await _enrich_field(
            "industries", icp.industries.include, icp.industries.exclude,
            get_params_fn, icp_name,
        )
        _store_field(result, previous, "industries", enriched, failed)

    # Job titles
    if icp.job_titles and icp.job_titles.include:
        enriched, failed = await _enrich_field(
            "job_titles", icp.job_titles.include, icp.job_titles.exclude,
            get_params_fn, icp_name,
        )
        _store_field(result, previous, "job_titles", enriched, failed)

    # Locations
    if icp.locations and icp.locations.include:
        enriched, failed = await _enrich_field(
            "locations", icp.locations.include, icp.locations.exclude,
            get_params_fn, icp_name,
        )
        _store_field(result, previous, "locations", enriched, failed)

    # Company locations
    if icp.company_locations and icp.company_locations.include:
        enriched, failed = await _enrich_field(
            "company_locations", icp.company_locations.include,
            icp.company_locations.exclude, get_params_fn, icp_name,
        )
        _store_field(result, previous, "company_locations", enriched, failed)

    # Departments. Sales Navigator only: a Premium seat is not asked.
    if icp.departments and icp.departments.include:
        if _seat_cannot_lookup("departments", search_seat_has_sales_nav):
            logger.warning(
                "Enrichment for '%s': departments skipped — the search seat "
                "has no Sales Navigator",
                icp_name,
            )
            _store_field(result, previous, "departments", EnrichedField(), True)
        else:
            enriched, failed = await _enrich_field(
                "departments", icp.departments.include, icp.departments.exclude,
                get_params_fn, icp_name,
            )
            _store_field(result, previous, "departments", enriched, failed)

    if result.failed_fields:
        logger.warning(
            f"Enrichment for '{icp_name}': parameter lookup unavailable for "
            f"{', '.join(result.failed_fields)} — those fields not refreshed"
        )

    return result


def _store_field(
    result: EnrichedIcpParams,
    previous: EnrichedIcpParams | None,
    field_name: str,
    enriched: EnrichedField,
    lookup_failed: bool,
) -> None:
    """Persist an enriched field, unless that would destroy a better one.

    An outage on /search/parameters yields a partial candidate list, and
    filtering against it produces a SMALLER field than the one already stored:
    a repair pass run during a DEPARTMENT outage turned 13 entries into 12 by
    deleting "Chief Executive Officer" and "Co-Founder" while keeping six
    near-useless variants. Because the count went DOWN, a guard checking
    cardinality reads the loss as an improvement, so the only usable signal is
    whether the catalogue was reachable at all.

    The guard therefore protects codes that exist and nothing else: it keeps
    whichever of the two actually carries any, preferring the stored one. With
    nothing stored, the partial result still beats None — blanking the field
    would cost create_campaign its structured targeting outright (it computes
    has_structured from these codes and falls back to keyword-only search),
    which is a bigger loss than the staleness this guards against. Either way
    the field is named in ``failed_fields``.
    """
    if not lookup_failed:
        setattr(result, field_name, enriched)
        return

    result.failed_fields.append(field_name)
    # No getattr default: every call site passes a literal, and a typo must
    # raise here rather than silently blank the field it meant to protect.
    stored = getattr(previous, field_name) if previous else None
    if stored is not None and stored.include:
        # Copied, not aliased: `previous` hangs off the incoming ICP and the
        # call sites assign this result straight back onto that same ICP, so a
        # shared list would make later edits to one show up in the other.
        setattr(result, field_name, EnrichedField(
            include=list(stored.include), exclude=list(stored.exclude),
        ))
    elif enriched.include:
        setattr(result, field_name, enriched)


async def _enrich_field(
    field_name: str,
    include_values: list[str],
    exclude_values: list[str],
    get_params_fn: Any,
    icp_name: str = "",
) -> tuple[EnrichedField, bool]:
    """Enrich a single include/exclude field.

    Returns (field, lookup_failed). ``lookup_failed`` is True when at least one
    call to get_params_fn found the catalogue unavailable, meaning the candidate
    list is incomplete in a way we cannot measure.
    """
    param_type = _FIELD_TO_TYPE.get(field_name)
    if not param_type:
        return EnrichedField(), False

    include_params, include_failed = await _resolve_values(
        param_type, include_values, field_name, get_params_fn, icp_name,
    )
    exclude_params, exclude_failed = await _resolve_values(
        param_type, exclude_values, field_name, get_params_fn, icp_name,
    )

    # Remove overlaps: exclude wins
    exclude_codes = {p.code for p in exclude_params}
    include_params = [p for p in include_params if p.code not in exclude_codes]

    # An unavailable EXCLUDE lookup counts too: the exclusion set comes back
    # short, so codes that should have been removed survive into include. It is
    # only ever a reason to prefer a stored value, never a reason to discard the
    # includes that did resolve — _store_field draws that line.
    return EnrichedField(include=include_params, exclude=exclude_params), (
        include_failed or exclude_failed
    )


# 4xx codes that still mean "ask again later" rather than "your keyword is wrong".
# 408/425/429 are transient by definition. 401/403/404 are here because they are
# systemic rather than per-keyword: an expired key or a moved endpoint answers
# the same way for every value in the field, so treating them as "that keyword
# is wrong" blanks the whole stored field and reports no failure at all.
_TRANSIENT_STATUSES = frozenset({401, 403, 404, 408, 425, 429})


def _lookup_unavailable(exc: BaseException) -> bool:
    """Did this failure mean the code catalogue was unreachable?

    A 4xx names the request, not the service: /search/parameters answered, and
    its answer for THAT keyword is no. Counting it as an outage pins the field
    to its stored value and reports a failure on every later run — and the
    remedy the tool output advertises, re-running generate_icp, can never clear
    a deterministic rejection. 5xx, timeouts, connect errors and rate limits are
    the opposite: the catalogue told us nothing, so whatever we build from the
    surviving candidates is missing rows we cannot see.

    Anything unrecognised counts as unavailable. The guard exists for the
    failures nobody anticipated, and only a status code we can actually read is
    grounds for waving one through.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status >= 500 or status in _TRANSIENT_STATUSES
    return True


async def _resolve_values(
    param_type: str,
    values: list[str],
    field_name: str,
    get_params_fn: Any,
    icp_name: str = "",
) -> tuple[list[EnrichedParam], bool]:
    """Resolve a list of string values to EnrichedParam objects.

    Returns (params, lookup_failed). Only get_params_fn — the source of truth
    for which codes exist — can set lookup_failed, and only when it was
    UNAVAILABLE (see _lookup_unavailable). A rate-limited LLM *filter* is
    deliberately not tracked here: it only means ambiguous candidates fell
    through to the heuristic, which is sound, and treating it as a failure
    would block every enrichment for as long as a free-tier quota is exhausted,
    i.e. permanently.
    """
    all_params: list[EnrichedParam] = []
    seen_codes: set[str] = set()
    lookup_failed = False

    for value in values:
        try:
            candidates = await get_params_fn(type=param_type, keywords=value)
        except Exception as e:
            if _lookup_unavailable(e):
                logger.warning(
                    f"Search params lookup unavailable for {param_type}/{value}: {e}"
                )
                lookup_failed = True
            else:
                logger.info(f"Search params rejected {param_type}/{value}: {e}")
            continue

        # If primary query returned nothing, retry with each alias (collect all)
        if not candidates:
            aliases = _get_aliases(value, field_name)
            all_alias_candidates: list[dict[str, str]] = []
            for alias in aliases:
                if alias.lower() == value.lower():
                    continue  # Skip the original value we already tried
                try:
                    alias_results = await get_params_fn(type=param_type, keywords=alias)
                    if alias_results:
                        logger.info(
                            f"Alias retry: {param_type}/{value} → "
                            f"'{alias}' returned {len(alias_results)} candidates"
                        )
                        all_alias_candidates.extend(alias_results)
                except Exception as e:
                    # The retries run only because the primary query came back
                    # empty, so an outage here leaves us with no candidate
                    # source at all for this value — same blind spot as an
                    # unavailable primary lookup.
                    if _lookup_unavailable(e):
                        lookup_failed = True
                    continue
            if all_alias_candidates:
                candidates = all_alias_candidates

        if not candidates:
            # Last resort: try individual words/parts from the value
            # e.g., "Marketing and Advertising" → try "Marketing", "Advertising" separately
            # Collect ALL results across parts (don't break on first hit)
            parts = [w.strip() for w in value.replace(" and ", ",").replace(" & ", ",").split(",") if w.strip()]
            if len(parts) == 1:
                # Single part — try splitting by space for multi-word queries
                parts = [w for w in value.split() if len(w) > 3 and w.lower() not in ("and", "the", "for", "with")]
            all_part_candidates: list[dict[str, str]] = []
            for part in parts:
                if part.lower() == value.lower():
                    continue
                try:
                    part_results = await get_params_fn(type=param_type, keywords=part)
                    if part_results:
                        logger.info(
                            f"Word-split retry: {param_type}/{value} → "
                            f"'{part}' returned {len(part_results)} candidates"
                        )
                        all_part_candidates.extend(part_results)
                except Exception as e:
                    if _lookup_unavailable(e):
                        lookup_failed = True
                    continue
            if all_part_candidates:
                candidates = all_part_candidates

        if not candidates:
            logger.debug(f"No candidates found for {param_type}/{value} (including alias retries)")
            continue

        # Filter candidates: LLM primary, heuristic fallback
        filtered = await _filter_candidates_with_llm(
            value, candidates, field_name, icp_name,
        )

        for p in filtered:
            code = p.get("code", "")
            if code and code not in seen_codes:
                seen_codes.add(code)
                all_params.append(EnrichedParam(name=p.get("name", ""), code=code))

    return all_params, lookup_failed


async def _filter_candidates_with_llm(
    original: str,
    candidates: list[dict[str, str]],
    field_name: str,
    icp_name: str = "",
) -> list[dict[str, str]]:
    """Filter candidates using LLM validation, falling back to heuristics.

    Primary: Uses the filter_candidates prompt for precise LLM-based validation.
    Routes through backend proxy when no local LLM key is available.
    Fallback: Jaccard + alias heuristic when LLM is unavailable or fails.
    """
    if not candidates:
        return []

    # Validate structure first
    valid = _validate_candidates(candidates)
    if not valid:
        return []

    # An exact or alias match needs no model. "locations/Spain" has a candidate
    # literally named Spain; asking an LLM to confirm string equality is what
    # pushed enrichment into Gemini's per-minute rate limit — dozens of trivial
    # calls per ICP — and each 429 then degraded that field to the Jaccard
    # fallback, which is where junk like a "Head of" keyword expanding into
    # forty unrelated titles comes from. The LLM's job is the ambiguous
    # remainder, not the lookups string comparison already answers.
    o_norm = _norm(original)
    aliases = _get_aliases(o_norm, field_name)
    exact = [c for c in valid if _norm(c["name"]) in aliases]
    if exact:
        return _dedupe_by_code(exact)

    # Try LLM-based filtering if prompt exists
    if has_prompt("filter_candidates"):
        try:
            candidates_str = "\n".join(
                f"- name: {c['name']} | code: {c['code']}" for c in valid
            )
            prompt = render_prompt("filter_candidates", {
                "icp_name": icp_name or "Unknown ICP",
                "field": field_name,
                "original": original,
                "candidates": candidates_str,
            })
            temperature = get_prompt_temperature("filter_candidates")

            # Try local LLM first; if no key, route through backend
            raw = await _llm_generate(
                prompt,
                system="You validate LinkedIn search parameters. Output ONLY JSON.",
                temperature=temperature,
                max_tokens=200,
            )

            if raw:
                result = parse_json(raw, fallback=None)
                keep_codes = result.get("param_codes", [])
                if isinstance(keep_codes, list) and all(isinstance(c, str) for c in keep_codes):
                    kept = [c for c in valid if c["code"] in set(keep_codes)]
                    if kept:
                        logger.info(
                            f"LLM filtered {field_name}/{original}: "
                            f"{len(valid)} → {len(kept)} candidates"
                        )
                        return _dedupe_by_code(kept)
                    # LLM returned empty — fall through to heuristic
                    logger.debug(f"LLM returned no matches for {field_name}/{original}")
        except Exception as e:
            # Swallowed on purpose, and NOT reported as a lookup failure: the
            # candidate list is intact, only the tie-breaker is gone, and the
            # heuristic below is a sound answer for it. Blocking the write here
            # would stop all enrichment for as long as a free-tier LLM quota is
            # exhausted.
            logger.warning(f"LLM filtering failed for {field_name}/{original}: {e}")

    # Fallback to heuristic matching
    return _filter_candidates_heuristic(original, valid, field_name)


async def _llm_generate(
    prompt: str,
    system: str = "",
    temperature: float = 0.0,
    max_tokens: int = 200,
) -> str | None:
    """Generate LLM response via local key or backend proxy.

    Returns None if neither is available.
    """
    from ..config import has_local_llm_key, is_backend_mode

    # 1. Try local LLM key. A local failure falls THROUGH to the backend proxy
    # rather than propagating: the proxy is exactly the second provider the
    # "All LLM providers failed" error claims not to have, and it was
    # unreachable for anyone with a key configured — a rate-limited Gemini key
    # made enrichment strictly worse than no key at all.
    if has_local_llm_key():
        from .llm import LLMClient
        client = LLMClient()
        try:
            return await client.generate(
                prompt, system=system, temperature=temperature,
                max_tokens=max_tokens, tier=LLM_TIER_FAST,
            )
        except Exception as e:
            logger.warning(
                f"Local LLM failed ({e}); falling back to backend proxy"
            )

    # 2. Route through backend proxy
    if is_backend_mode():
        try:
            from ..linkedin import get_linkedin_client
            from ..linkedin.backend_client import BackendClient

            client = get_linkedin_client()
            if isinstance(client, BackendClient):
                try:
                    result = await client.filter_candidates(prompt)
                    return result
                finally:
                    await client.close()
        except Exception as e:
            logger.debug(f"Backend LLM proxy for filter_candidates failed: {e}")
            return None

    return None


def _filter_candidates_heuristic(
    original: str,
    candidates: list[dict[str, str]],
    field_name: str,
) -> list[dict[str, str]]:
    """Filter Unipile search parameter candidates using heuristic matching.

    Uses exact match → alias match → Jaccard similarity fallback.
    Candidates are expected to be pre-validated.
    """
    if not candidates:
        return []

    o_norm = _norm(original)

    # 1. Exact match (including location + industry aliases)
    aliases = _get_aliases(o_norm, field_name)
    exact = [c for c in candidates if _norm(c["name"]) in aliases]
    if exact:
        return _dedupe_by_code(exact)

    # 2. Reverse alias check: does any candidate's name alias-match our original?
    if field_name == "industries":
        for c in candidates:
            c_norm = _norm(c["name"])
            c_aliases = _get_aliases(c_norm, field_name)
            if o_norm in c_aliases:
                return _dedupe_by_code([c])

    # 3. For locations, only accept exact matches (already checked via aliases)
    if field_name in ("locations", "company_locations"):
        return _dedupe_by_code(
            [c for c in candidates if _norm(c["name"]) == o_norm]
        )

    # 4. Jaccard similarity for other fields
    if field_name in ("job_titles", "industries", "departments"):
        o_tokens = _tokens(o_norm)
        o_domain = _domain_tokens(o_tokens)
        # A query made only of rank words ("Head of", "Director") names no
        # domain, so every expansion of it is noise. An exact match would have
        # returned at step 1; reaching here with nothing to discriminate on
        # means the honest answer is no match, not all of them.
        if field_name == "job_titles" and not o_domain:
            return []
        scored: list[tuple[float, dict[str, str]]] = []
        for c in candidates:
            c_tokens = _tokens(_norm(c["name"]))
            jacc = _jaccard(o_tokens, c_tokens)
            if jacc < _JACCARD_THRESHOLD:
                continue
            # Token overlap alone is not similarity when the shared tokens are
            # all rank words. Where the query names a domain, the candidate has
            # to name one of the same domain words to qualify.
            if o_domain and not (o_domain & _domain_tokens(c_tokens)):
                continue
            scored.append((jacc, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        return _dedupe_by_code([c for _, c in scored])

    return []


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _validate_candidates(candidates: list[dict]) -> list[dict[str, str]]:
    """Validate candidate structure, returning only well-formed entries."""
    valid: list[dict[str, str]] = []
    seen: set[str] = set()
    for c in candidates:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        code = c.get("code")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(code, str) or not code.strip():
            continue
        code = code.strip()
        if code in seen:
            continue
        seen.add(code)
        valid.append({"name": name.strip(), "code": code})
    return valid


def _dedupe_by_code(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Deduplicate by code, preserving order."""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for c in items:
        if c["code"] in seen:
            continue
        seen.add(c["code"])
        out.append(c)
    return out


def _norm(s: str) -> str:
    """Normalize text for comparison."""
    return re.sub(r"[^a-z0-9 ]+", "", s.lower()).strip()


def _tokens(s: str) -> set[str]:
    """Split normalized text into token set."""
    return {t for t in s.split() if t}


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity between two token sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _get_aliases(norm: str, field_name: str = "") -> set[str]:
    """Get known aliases for a normalized string.

    Checks location aliases for location fields, industry aliases
    for industry fields, or both if field_name is empty.
    Case-insensitive matching against alias sets.
    """
    norm_lower = norm.lower()

    # Location aliases
    if not field_name or field_name in ("locations", "company_locations"):
        for canonical, alias_set in _LOCATION_ALIASES.items():
            if norm_lower in {a.lower() for a in alias_set}:
                return alias_set

    # Industry aliases
    if not field_name or field_name == "industries":
        for canonical, alias_set in _INDUSTRY_ALIASES.items():
            if norm_lower in {a.lower() for a in alias_set}:
                return alias_set

    return {norm}
