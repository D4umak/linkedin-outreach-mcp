"""ICP match scoring and composite lead scoring for campaign prioritization.

Replaces the old data-completeness heuristic with actual ICP attribute
matching across 6 dimensions: title, industry, seniority, company size,
location, and keyword overlap.

Also provides a unified composite lead score that combines ICP match,
revenue estimate, signal score, engagement history, and historical
segment performance into a single 0-1 priority score.

No LLM calls — all heuristic-based for speed and consistency.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..db.async_bridge import run_db

from ..textutil import contains_term
from .seniority import (
    DECISION_MAKER_LEVELS,
    infer_seniority_level,
    normalize_seniority_list,
    states_seniority,
)
from .revenue_estimator import (
    extract_headcount,
    extract_industry,
    parse_json_field,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# ICP Match Score Weights
# ──────────────────────────────────────────────

ICP_WEIGHT_TITLE = 0.30
ICP_WEIGHT_INDUSTRY = 0.20
ICP_WEIGHT_SENIORITY = 0.15
ICP_WEIGHT_COMPANY_SIZE = 0.15
ICP_WEIGHT_LOCATION = 0.10
ICP_WEIGHT_KEYWORDS = 0.10

# Neutral scores for unknown/missing data (don't penalize, don't reward)
UNKNOWN_SCORE = 0.3
LOCATION_UNKNOWN_SCORE = 0.5

# Cap when the ICP names a product (AISP, open banking, …) and the contact
# text has none of those terms. Title+seniority alone used to score ~0.3–0.6
# and clear both SIGNAL_ICP_MATCH_THRESHOLD and the send gate.
PRODUCT_KEYWORD_VETO_CAP = 0.20

_ROLE_KEYWORD_NOISE = {
    "chief executive officer",
    "founder",
    "co-founder",
    "co founder",
    "managing director",
    "ceo",
    "cto",
    "cfo",
    "coo",
    "cmo",
    "director",
    "head of business development",
    "chief commercial officer",
    "director of partnerships",
    "business development lead",
    "chief technology officer",
}
_NATIONALITY_KEYWORD_NOISE = {
    "ukraine",
    "ukrainian",
    "uk",
    "united kingdom",
    "britain",
    "british",
}

# ──────────────────────────────────────────────
# Composite Lead Score Weights
# ──────────────────────────────────────────────

COMPOSITE_WEIGHT_ICP_MATCH = 0.30
COMPOSITE_WEIGHT_REVENUE = 0.25
COMPOSITE_WEIGHT_SIGNAL = 0.20
COMPOSITE_WEIGHT_ENGAGEMENT = 0.15
COMPOSITE_WEIGHT_SEGMENT = 0.10

# Revenue normalization cap ($100K ACV = 1.0)
REVENUE_NORM_CAP = 100_000


# ──────────────────────────────────────────────
# ICP Match Scoring
# ──────────────────────────────────────────────

def compute_icp_match(
    contact: dict[str, Any],
    campaign_icp_json: str | dict | None = None,
) -> dict[str, Any]:
    """Score a contact against the campaign ICP (0.0-1.0).

    Args:
        contact: Contact dict with title, company, location, profile_json, analysis_json.
        campaign_icp_json: Campaign ICP as JSON string or parsed dict.

    Returns:
        {"icp_match_score": float, "breakdown": {dimension: score}}.
    """
    icp = _parse_first_icp(campaign_icp_json)
    if not icp:
        # No ICP data — return neutral score
        return {"icp_match_score": UNKNOWN_SCORE, "breakdown": {}}

    title_score = _score_title_match(contact.get("title", ""), icp)
    industry_score = _score_industry_match(contact, icp, campaign_icp_json)
    seniority_score = _score_seniority_match(contact.get("title", ""), icp)
    size_score = _score_company_size(contact, icp, campaign_icp_json)
    location_score = _score_location_match(contact, icp)
    keyword_score = _score_keyword_overlap(contact, icp)

    breakdown = {
        "title": title_score,
        "industry": industry_score,
        "seniority": seniority_score,
        "company_size": size_score,
        "location": location_score,
        "keywords": keyword_score,
    }

    # A stated level the ICP does not ask for is a hard miss for the whole
    # prospect, not 0.15 of lost weight — the same rule the backend applies in
    # `app/services/icp_match.py` and in `_score_discovery_prospect`. Two
    # definitions of one thing is how a Senior Engineer kept clearing the fit
    # gate on title and keyword overlap while the ICP named only CXOs
    # (9 Sep 2026). `_score_seniority_match` returns 0.0 only when the
    # title *states* a level: unknown titles score UNKNOWN_SCORE and pass.
    seniority_miss = (
        seniority_score == 0.0
        and _states_seniority(contact.get("title", "") or "")
    )

    raw = (
        ICP_WEIGHT_TITLE * title_score
        + ICP_WEIGHT_INDUSTRY * industry_score
        + ICP_WEIGHT_SENIORITY * seniority_score
        + ICP_WEIGHT_COMPANY_SIZE * size_score
        + ICP_WEIGHT_LOCATION * location_score
        + ICP_WEIGHT_KEYWORDS * keyword_score
    )

    icp_match = round(max(0.0, min(1.0, raw)), 4)
    vetoed = _product_keyword_veto_applies(contact, icp)
    if vetoed:
        icp_match = min(icp_match, PRODUCT_KEYWORD_VETO_CAP)
        breakdown["product_keywords"] = 0.0
    if seniority_miss:
        # `weighted_score` is the six-dimension average; `icp_match_score` is
        # the intake answer. Callers that decide whether to *enrol* read the
        # score and get a hard 0.0 — the same rule the backend applies in
        # `icp_match.py` and `_score_discovery_prospect`, because losing 0.15
        # of seniority weight was not enough to stop title and keyword overlap
        # carrying an individual contributor over the fit floor. Callers that
        # decide whether to *close* a conversation already under way read
        # `weighted_score` instead: an intake rule is not grounds to hang up on
        # someone (see `ai/targeting_recheck._heuristic_recheck`).
        return {
            "icp_match_score": 0.0,
            "weighted_score": icp_match,
            "breakdown": breakdown,
            "seniority_miss": True,
        }
    return {"icp_match_score": icp_match, "breakdown": breakdown}


# ──────────────────────────────────────────────
# Composite Lead Score
# ──────────────────────────────────────────────

def compute_composite_lead_score(
    contact: dict[str, Any],
    campaign_icp_json: str | dict | None = None,
    patterns: list[dict[str, Any]] | None = None,
) -> float:
    """Unified 0-1 composite score combining all available signals.

    Components (weights sum to 1.0):
    - ICP Match (0.30): How well prospect matches campaign ICP
    - Revenue Estimate (0.25): Normalized estimated_revenue
    - Signal Score (0.20): From signal_accounts composite_score
    - Engagement Score (0.15): Behavioral engagement history
    - Segment Performance (0.10): Historical conversion for matching segment

    Args:
        contact: Contact dict (must include id, title, company, linkedin_id, etc.)
        campaign_icp_json: Campaign ICP JSON.
        patterns: Strategy patterns for segment performance lookup.

    Returns:
        Composite score 0.0-1.0.
    """
    # 1. ICP Match
    icp_result = compute_icp_match(contact, campaign_icp_json)
    icp_score = icp_result["icp_match_score"]

    # 2. Revenue (normalized to 0-1)
    revenue_raw = contact.get("estimated_revenue", 0) or 0
    revenue_score = min(revenue_raw / REVENUE_NORM_CAP, 1.0) if revenue_raw > 0 else 0.0

    # 3. Signal Score
    signal_score = _get_signal_score(contact.get("linkedin_id", ""))

    # 4. Engagement Score
    engagement_score = _compute_engagement_score(
        contact.get("id", ""),
        contact.get("linkedin_id", ""),
    )

    # 5. Segment Performance
    segment_score = _get_segment_performance_score(contact, patterns)

    composite = (
        COMPOSITE_WEIGHT_ICP_MATCH * icp_score
        + COMPOSITE_WEIGHT_REVENUE * revenue_score
        + COMPOSITE_WEIGHT_SIGNAL * signal_score
        + COMPOSITE_WEIGHT_ENGAGEMENT * engagement_score
        + COMPOSITE_WEIGHT_SEGMENT * segment_score
    )

    return round(max(0.0, min(1.0, composite)), 4)


# ──────────────────────────────────────────────
# Backfill
# ──────────────────────────────────────────────

async def backfill_icp_match_scores(limit: int = 500) -> int:
    """Re-score contacts with ICP match instead of data completeness.

    Targets contacts whose fit_score was set by the old heuristic
    (typically rounded to 2 decimal places from the data-completeness formula).

    Returns:
        Number of contacts updated.
    """
    def _do_backfill():
        from ..db.schema import get_db

        db = get_db()
        rows = db.execute("""
            SELECT c.id, c.title, c.company, c.linkedin_id,
                   c.profile_json, c.analysis_json, c.fit_score,
                   c.estimated_revenue, c.campaign_id, camp.icp_json
            FROM contacts c
            LEFT JOIN campaigns camp ON c.campaign_id = camp.id
            WHERE camp.icp_json IS NOT NULL AND camp.icp_json != ''
            ORDER BY c.created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        db.close()

        if not rows:
            return 0

        updated = 0
        db = get_db()
        for row in rows:
            contact = dict(row)
            icp_json = contact.get("icp_json")
            if not icp_json:
                continue

            result = compute_icp_match(contact, icp_json)
            new_score = result["icp_match_score"]
            old_score = contact.get("fit_score", 0) or 0

            if abs(new_score - old_score) > 0.01:
                db.execute(
                    "UPDATE contacts SET fit_score = ? WHERE id = ?",
                    (new_score, contact["id"]),
                )
                updated += 1

        if updated:
            db.commit()
            logger.info("ICP match scorer: backfilled %d contacts", updated)
        db.close()
        if updated:
            from .fit_gate import restore_fit_skipped_for_campaign

            touched = {
                (dict(r).get("campaign_id") or "")
                for r in rows
            } - {""}
            for cid in touched:
                restore_fit_skipped_for_campaign(cid)
        return updated

    return await run_db(_do_backfill)


# ──────────────────────────────────────────────
# ICP Match — Dimension Scorers
# ──────────────────────────────────────────────

def _parse_first_icp(campaign_icp_json: str | dict | None) -> dict[str, Any] | None:
    """Parse the first ICP from campaign ICP JSON.

    Handles two formats:
    - Modern: {"icps": [{"job_titles": {"include": [...]}, ...}]}
    - Legacy: {"segments": [{"titles": [...], "keywords": "...", ...}]}
    """
    if not campaign_icp_json:
        return None

    if isinstance(campaign_icp_json, str):
        try:
            data = json.loads(campaign_icp_json)
        except (json.JSONDecodeError, TypeError):
            return None
    else:
        data = campaign_icp_json

    if isinstance(data, dict):
        # Modern format: icps[]
        icps = data.get("icps", [])
        if isinstance(icps, list) and icps:
            first = icps[0]
            return first if isinstance(first, dict) else None

        # Legacy format: segments[] — convert to ICP dict
        segments = data.get("segments", [])
        if isinstance(segments, list) and segments:
            seg = segments[0]
            if isinstance(seg, dict):
                return _segment_to_icp(seg)

    return None


def _segment_to_icp(seg: dict[str, Any]) -> dict[str, Any]:
    """Convert a legacy segment dict to ICP dict format.

    Segment format: {"titles": [...], "industries": [...], "seniority": [...],
                     "locations": [...], "keywords": "comma,separated",
                     "company_headcount": {"min": N, "max": N}}
    ICP format: {"job_titles": {"include": [...]}, "industries": {"include": [...]}, ...}
    """
    titles = seg.get("titles", [])
    industries = seg.get("industries", [])
    locations = seg.get("locations", [])
    seniority = seg.get("seniority", [])

    # Keywords: segment stores as comma-separated string, ICP as list
    keywords_raw = seg.get("keywords", "")
    if isinstance(keywords_raw, str):
        keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]
    elif isinstance(keywords_raw, list):
        keywords = keywords_raw
    else:
        keywords = []

    return {
        "job_titles": {"include": titles, "exclude": []},
        "industries": {"include": industries, "exclude": []},
        "seniority": {"include": seniority, "exclude": []},
        "locations": {"include": locations, "exclude": []},
        "company_headcount": seg.get("company_headcount") or {},
        "keywords": keywords,
    }


def _get_include_exclude(icp: dict, field: str) -> tuple[list[str], list[str]]:
    """Extract include/exclude lists from an ICP field (LinkedinSearchParam format)."""
    val = icp.get(field)
    if isinstance(val, dict):
        include = [s.lower().strip() for s in val.get("include", []) if s]
        exclude = [s.lower().strip() for s in val.get("exclude", []) if s]
        return include, exclude
    if isinstance(val, list):
        return [s.lower().strip() for s in val if s], []
    return [], []


_PRODUCT_LEADER_CANONICAL = (
    "head of product", "product director", "chief product officer", "cpo",
)
_PRODUCT_LEADER_SYNONYMS = (
    "head of product",
    "product director",
    "vp of product",
    "vice president of product",
    "ai product leader",
    "chief product officer",
    "cpo",
)


def _expand_product_leader_terms(include: list[str]) -> list[str]:
    """VP / AI Product Leader match a Head of Product ICP. Marketing does not."""
    joined = " ".join(include)
    if any(canon in joined for canon in _PRODUCT_LEADER_CANONICAL):
        return list(dict.fromkeys([*include, *_PRODUCT_LEADER_SYNONYMS]))
    return include


def _score_title_match(title: str, icp: dict) -> float:
    """Score title match against ICP job_titles (0.0-1.0)."""
    if not title:
        return UNKNOWN_SCORE

    title_lower = title.lower().strip()
    include, exclude = _get_include_exclude(icp, "job_titles")
    marketingish = (
        "marketing" in title_lower or "business development" in title_lower
    )
    if marketingish:
        blocked = set(_PRODUCT_LEADER_CANONICAL) | set(_PRODUCT_LEADER_SYNONYMS)
        include = [t for t in include if t not in blocked]
        if not include:
            return 0.1
    else:
        include = _expand_product_leader_terms(include)
        if "product leader" in title_lower:
            title_lower = title_lower.replace("product leader", "head of product")

    # Exclude check first — disqualify immediately. Whole-word, or an
    # exclude of "cto" would disqualify every Director on LinkedIn.
    for term in exclude:
        if contains_term(title_lower, term):
            return 0.0

    if not include:
        return UNKNOWN_SCORE

    # Exact match (full title contains a full include term).
    # Substring matching scored "SAP SuccessFactors Employee Central" and
    # "Executive Director for Children and Education" as perfect CTO matches
    # — both contain the letters "cto" — and both cleared the send gate.
    for term in include:
        if contains_term(title_lower, term):
            return 1.0

    # Partial word overlap: check individual words from include terms
    title_words = set(title_lower.split())
    best_overlap = 0.0
    for term in include:
        term_words = set(term.split())
        if not term_words:
            continue
        overlap = len(title_words & term_words) / len(term_words)
        best_overlap = max(best_overlap, overlap)

    if best_overlap >= 0.5:
        return 0.3 + (best_overlap * 0.4)  # 0.5 overlap → 0.5, 1.0 overlap → 0.7

    return 0.1  # Title exists but no match


def _phrases_overlap(a: str, b: str) -> bool:
    """Either phrase contains the other as whole words.

    Industry and location values are compared in both directions ("software"
    vs "software development", "berlin" vs "berlin, germany"), but a raw
    substring test in either direction is the 6 Sep 2026 title bug again:
    "it" inside "hospitality", "uk" inside "Ukraine", "us" inside "Russia".
    """
    return contains_term(a, b) or contains_term(b, a)


def _score_industry_match(
    contact: dict[str, Any],
    icp: dict,
    campaign_icp_json: str | dict | None = None,
) -> float:
    """Score industry match against ICP industries (0.0-1.0)."""
    parsed_icp = None
    if isinstance(campaign_icp_json, str):
        try:
            parsed_icp = json.loads(campaign_icp_json)
        except (json.JSONDecodeError, TypeError):
            pass
    elif isinstance(campaign_icp_json, dict):
        parsed_icp = campaign_icp_json

    industry = extract_industry(contact, parsed_icp)
    if not industry:
        return UNKNOWN_SCORE

    include, exclude = _get_include_exclude(icp, "industries")

    # Exclude check
    for term in exclude:
        if _phrases_overlap(industry, term):
            return 0.0

    if not include:
        return UNKNOWN_SCORE

    # Exact match
    for term in include:
        if term == industry or _phrases_overlap(industry, term):
            return 1.0

    # No match
    return 0.1


def _states_seniority(title: str) -> bool:
    """True when the title actually names a level, rather than being guessed."""
    return states_seniority(title)


def _score_seniority_match(title: str, icp: dict) -> float:
    """Score seniority match against ICP seniority (0.0-1.0).

    A stated level that the ICP excludes — or that sits outside a non-empty
    include list — is a hard miss (0.0), not a distant rung on the ladder.
    9 Sep 2026 asked for decision makers; an SDR scoring 0.2 for
    seniority still cleared the fit gate on title and keywords alone.
    """
    if not title:
        return UNKNOWN_SCORE

    # infer_seniority falls back to "manager" for a title that states no level
    # at all, and that guess used to score a perfect 1.0 against any ICP asking
    # for managers — 0.15 of free weight for every unreadable title, which is
    # what carried "SAP SuccessFactors Employee Central" over the fit gate.
    # Ahead of the exclude checks: neither can read a level that is not there.
    seniority = infer_seniority_level(title)
    if seniority is None:
        return UNKNOWN_SCORE

    # The ICP is written by an LLM told to emit LinkedIn's vocabulary
    # ("vice_president", "entry_level"); the ladder below is keyed on the
    # canonical one. Without this normalisation a VP-targeted ICP matched
    # nobody at all.
    raw_include, raw_exclude = _get_include_exclude(icp, "seniority")
    include = normalize_seniority_list(raw_include)
    exclude = normalize_seniority_list(raw_exclude)

    if seniority in exclude:
        return 0.0

    if not include:
        return UNKNOWN_SCORE

    if seniority in include:
        return 1.0

    # Stated, and not one of the levels this ICP asks for. That is a miss, and
    # the caller records it as `seniority_miss` — being two rungs from a CXO
    # does not make a Senior Engineer a decision maker.
    return 0.0


def seniority_verdict(title: str, icp: dict) -> dict[str, Any]:
    """Explain the seniority judgement for one title. Used by `icp preview`."""
    level = infer_seniority_level(title)
    include = normalize_seniority_list(_get_include_exclude(icp, "seniority")[0])
    exclude = normalize_seniority_list(_get_include_exclude(icp, "seniority")[1])
    score = _score_seniority_match(title or "", icp)
    if not include and not exclude:
        explain = "persona sets no seniority"
    elif level is None:
        explain = "title states no level — passes as unknown"
    elif level in exclude:
        explain = f"{level} is excluded"
    elif include and level not in include:
        explain = f"{level} is not in {', '.join(include)}"
    else:
        explain = f"{level} matches"
    return {
        "level": level,
        "score": score,
        "keep": score > 0.0,
        "decision_maker": level in DECISION_MAKER_LEVELS if level else False,
        "explain": explain,
    }


def _score_company_size(
    contact: dict[str, Any],
    icp: dict,
    campaign_icp_json: str | dict | None = None,
) -> float:
    """Score company size match against ICP headcount range (0.0-1.0)."""
    parsed_icp = None
    if isinstance(campaign_icp_json, str):
        try:
            parsed_icp = json.loads(campaign_icp_json)
        except (json.JSONDecodeError, TypeError):
            pass
    elif isinstance(campaign_icp_json, dict):
        parsed_icp = campaign_icp_json

    # Don't use ICP fallback for headcount — we're scoring against ICP, not filling gaps
    headcount = extract_headcount(contact, None)

    hc_range = icp.get("company_headcount", {})
    if not isinstance(hc_range, dict):
        return UNKNOWN_SCORE

    icp_min = hc_range.get("min") or 0
    icp_max = hc_range.get("max") or 0

    if not icp_min and not icp_max:
        return UNKNOWN_SCORE  # ICP doesn't specify headcount

    if headcount is None:
        return UNKNOWN_SCORE  # Can't determine headcount

    # Within range → perfect match
    effective_max = icp_max or (icp_min * 10)  # No max means open-ended
    if icp_min <= headcount <= effective_max:
        return 1.0

    # Within 2x range → partial match
    extended_min = max(1, icp_min // 2)
    extended_max = effective_max * 2
    if extended_min <= headcount <= extended_max:
        return 0.5

    # Way out of range
    return 0.1


def _score_location_match(contact: dict[str, Any], icp: dict) -> float:
    """Score location match against ICP locations (0.0-1.0)."""
    location = (contact.get("location") or "").lower().strip()
    if not location:
        # Try profile_json
        profile = parse_json_field(contact.get("profile_json"))
        if profile:
            location = (profile.get("location", "") or "").lower().strip()

    include, exclude = _get_include_exclude(icp, "locations")

    if not location:
        return LOCATION_UNKNOWN_SCORE

    # Exclude check
    for term in exclude:
        if _phrases_overlap(location, term):
            return 0.0

    if not include:
        return LOCATION_UNKNOWN_SCORE

    # Match
    for term in include:
        if _phrases_overlap(location, term):
            return 1.0

    return 0.2  # Has location but doesn't match


def _contact_keyword_text(contact: dict[str, Any]) -> str:
    """Title, company, headline, about, and researched analysis — one haystack."""
    parts = [
        contact.get("title", ""),
        contact.get("company", ""),
        contact.get("headline", ""),
        contact.get("research_summary", ""),
    ]
    profile = parse_json_field(contact.get("profile_json"))
    if profile:
        parts.append(profile.get("headline", "") or "")
        parts.append(profile.get("summary", "") or "")
    analysis = parse_json_field(contact.get("analysis_json"))
    if analysis:
        parts.append(analysis.get("summary", "") or "")
        pains = analysis.get("pain_points") or []
        if isinstance(pains, list):
            parts.extend(str(p) for p in pains)
        tone = analysis.get("tone") or {}
        if isinstance(tone, dict):
            jargon = tone.get("industry_jargon") or []
            if isinstance(jargon, list):
                parts.extend(str(j) for j in jargon)
    return " ".join(p for p in parts if p).lower()


_PRECISION_PRODUCT_STEMS = (
    "open banking",
    "aisp",
    "pisp",
    "psd2",
    "psd agent",
    "fca agent",
    "agent model",
    "payment initiation",
    "account information",
    "idvt",
    "diatf",
    "idsp",
    "right to work",
    "evisa",
)


def _is_precision_product_keyword(kw: str) -> bool:
    """True for license/product tokens, not LinkedIn search-topic words.

    'fintech' / 'payments' / 'platform engineering' are search bait and
    will not appear on every matching VP. AISP / open banking will.
    """
    return any(stem in kw for stem in _PRECISION_PRODUCT_STEMS)


def _distinctive_keywords(icp: dict) -> list[str]:
    """High-precision product keywords only — leftover search terms are ignored."""
    keywords = icp.get("keywords") or []
    if not isinstance(keywords, list):
        return []
    titles, _ = _get_include_exclude(icp, "job_titles")
    industries, _ = _get_include_exclude(icp, "industries")
    noise = set(titles) | set(industries) | _ROLE_KEYWORD_NOISE | _NATIONALITY_KEYWORD_NOISE
    out: list[str] = []
    for raw in keywords:
        kw = str(raw or "").lower().strip()
        if (
            kw
            and kw not in noise
            and len(kw) >= 3
            and _is_precision_product_keyword(kw)
        ):
            out.append(kw)
    return out


def _has_product_context(contact: dict[str, Any]) -> bool:
    """Enough text to judge product fit — not a title-only stub."""
    if contact.get("company"):
        return True
    if contact.get("headline"):
        return True
    if contact.get("research_summary"):
        return True
    profile = parse_json_field(contact.get("profile_json"))
    if profile and (profile.get("headline") or profile.get("summary")):
        return True
    analysis = parse_json_field(contact.get("analysis_json"))
    if analysis and (analysis.get("summary") or analysis.get("tone")):
        return True
    title = str(contact.get("title") or "")
    return len(title) > 40


def _product_keyword_veto_applies(contact: dict[str, Any], icp: dict) -> bool:
    product = _distinctive_keywords(icp)
    if not product:
        return False
    text = _contact_keyword_text(contact)
    return not any(kw in text for kw in product)


def precision_stems_from_text(text: str) -> list[str]:
    """Precision product stems mentioned in a keywords/description blob."""
    blob = (text or "").lower()
    return [stem for stem in _PRECISION_PRODUCT_STEMS if stem in blob]


def prospect_mentions_precision_stems(
    prospect: dict[str, Any], stems: list[str],
) -> bool:
    haystack = " ".join(
        str(prospect.get(key) or "")
        for key in ("title", "company", "headline")
    ).lower()
    return any(stem in haystack for stem in stems)


def _score_keyword_overlap(contact: dict[str, Any], icp: dict) -> float:
    """Score keyword overlap between contact text and ICP keywords (0.0-1.0)."""
    keywords = icp.get("keywords", [])
    if not keywords or not isinstance(keywords, list):
        return UNKNOWN_SCORE

    text = _contact_keyword_text(contact)
    if not text:
        return UNKNOWN_SCORE

    # Whole-word, or "ai" matches inside "retail" and "email" and every
    # contact with those words in their haystack scores keyword overlap.
    matches = sum(1 for kw in keywords if contains_term(text, kw.lower()))
    if not matches:
        return 0.0

    return min(1.0, matches / max(len(keywords), 1))


# ──────────────────────────────────────────────
# Composite Score — Sub-Components
# ──────────────────────────────────────────────

def _get_signal_score(linkedin_id: str) -> float:
    """Look up composite signal score for a prospect."""
    if not linkedin_id:
        return 0.0

    try:
        from ..db.signal_queries import get_signal_account
        account = get_signal_account(linkedin_id)
        if account:
            return account.get("composite_score", 0.0) or 0.0
    except Exception:
        logger.debug("Could not look up signal score for %s", linkedin_id)
    return 0.0


def _compute_engagement_score(contact_id: str, linkedin_id: str) -> float:
    """Compute engagement score from behavioral history (0.0-1.0).

    Considers:
    - Profile views from prospect (from signals table)
    - Comments on our posts (from signals table)
    - Post reactions on our content (from signals table)
    - Our engagement with their content (from engagements table)

    Uses freshness decay and best-signal-as-baseline pattern.
    """
    if not contact_id and not linkedin_id:
        return 0.0

    now = int(time.time())
    scored_types: dict[str, float] = {}

    # Check signals table for prospect-initiated engagement
    if linkedin_id:
        try:
            from ..db.signal_queries import list_signals
            signals = list_signals(linkedin_id=linkedin_id, limit=20)
            for sig in signals:
                sig_type = sig.get("signal_type", "")
                detected_at = sig.get("detected_at", 0) or sig.get("created_at", 0) or 0

                # Apply freshness decay
                decay = _simple_freshness_decay(detected_at, now)

                # Score by signal type
                type_score = 0.0
                if sig_type == "profile_view":
                    type_score = 0.30 * decay
                elif sig_type == "commenter_match":
                    type_score = 0.30 * decay
                elif sig_type == "post_engagement":
                    type_score = 0.20 * decay

                if type_score > scored_types.get(sig_type, 0):
                    scored_types[sig_type] = type_score
        except Exception:
            logger.debug("Could not fetch signals for engagement score")

    # Check engagements table for our engagement with their content
    if contact_id:
        try:
            from ..db.schema import get_db
            db = get_db()
            rows = db.execute("""
                SELECT e.action_type, e.created_at
                FROM engagements e
                JOIN outreaches o ON e.outreach_id = o.id
                WHERE o.contact_id = ? AND e.status = 'sent'
                ORDER BY e.created_at DESC
                LIMIT 10
            """, (contact_id,)).fetchall()
            db.close()

            if rows:
                # Our engagement counts for a smaller amount (reciprocity signal)
                best_our_engagement = 0.0
                for row in rows:
                    detected_at = row["created_at"] or 0
                    decay = _simple_freshness_decay(detected_at, now)
                    best_our_engagement = max(best_our_engagement, 0.10 * decay)
                if best_our_engagement > scored_types.get("our_engagement", 0):
                    scored_types["our_engagement"] = best_our_engagement
        except Exception:
            logger.debug("Could not fetch engagements for engagement score")

    if not scored_types:
        return 0.0

    # Best-signal-as-baseline with diminishing stacking (same pattern as signal_scorer)
    sorted_scores = sorted(scored_types.values(), reverse=True)
    baseline = sorted_scores[0] / 0.30 * 0.80  # Normalize baseline to ~0.80
    baseline = min(baseline, 0.80)

    additional = sum(s * 0.5 for s in sorted_scores[1:])
    return min(1.0, round(baseline + additional, 4))


def _get_segment_performance_score(
    contact: dict[str, Any],
    patterns: list[dict[str, Any]] | None,
) -> float:
    """Score based on historical segment performance (0.0-1.0).

    Looks up matching patterns by title+company against strategy_patterns
    to find historical acceptance/conversion rates for similar prospects.
    """
    if not patterns:
        return 0.5  # Neutral on cold start

    title = (contact.get("title", "") or "").lower()
    company = (contact.get("company", "") or "").lower()

    if not title and not company:
        return 0.5

    best_score = 0.0
    for pattern in patterns:
        pattern_key = (pattern.get("pattern_key", "") or "").lower()
        if not pattern_key:
            continue

        # Check if contact matches pattern key (title+company format)
        parts = pattern_key.split("+")
        matched = any(
            (contains_term(title, part.strip())
             or contains_term(company, part.strip()))
            for part in parts
            if part.strip()
        )
        if not matched:
            continue

        # Score = acceptance_rate × confidence × sample_size_factor
        confidence = pattern.get("confidence", 0.5) or 0.5
        sample_size = pattern.get("sample_size", 0) or 0
        sample_factor = min(1.0, sample_size / 50)

        # Try to extract acceptance rate from details
        details = pattern.get("details_json")
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except (json.JSONDecodeError, TypeError):
                details = {}
        elif not isinstance(details, dict):
            details = {}

        acceptance = details.get("acceptance_rate", 0) or 0
        if isinstance(acceptance, str):
            try:
                acceptance = float(acceptance.rstrip("%")) / 100
            except (ValueError, TypeError):
                acceptance = 0

        score = acceptance * confidence * sample_factor
        best_score = max(best_score, score)

    return best_score if best_score > 0 else 0.5


def _simple_freshness_decay(detected_at: int, now: int) -> float:
    """Simple freshness decay matching signal_scorer's curve."""
    if not detected_at:
        return 0.1

    age_days = max(0, (now - detected_at) / 86400)

    decay_curve = [
        (1, 1.0),
        (3, 0.85),
        (7, 0.65),
        (14, 0.40),
        (30, 0.20),
    ]

    for threshold_days, multiplier in decay_curve:
        if age_days <= threshold_days:
            return multiplier

    return 0.1
