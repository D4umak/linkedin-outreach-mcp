"""Persona confidence from the brief, the titles and the rewrite (api #7211).

The hosted score is heylead-api app/services/icp_grounding.py. These are the
same weights for a generation that runs in the client. A brief that names no
role, no region and no company size stays under 0.5.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..services.generic_titles import is_generic_title
from ..services.seniority import levels_named
from ..textutil import contains_term


PARTLY_GROUNDED = 0.5
WELL_GROUNDED = 0.75


def _include(persona: Mapping[str, Any], key: str) -> list[str]:
    raw = persona.get(key) if isinstance(persona, Mapping) else None
    if isinstance(raw, Mapping):
        values = raw.get("include") or []
    elif isinstance(raw, (list, tuple)):
        values = raw
    else:
        values = []
    return [str(item) for item in values if str(item or "").strip()]




#: A brief names a place when one of these is a whole word in it. Short
#: tokens that are also ordinary English ("us", "it") are absent on purpose.
_REGION_TERMS: tuple[str, ...] = (
    "uk", "united kingdom", "great britain", "britain", "england", "scotland", "wales",
    "usa", "united states", "america",
    "europe", "european", "dach", "emea", "apac", "latam", "mena", "nordics", "nordic",
    "benelux", "middle east", "asia", "africa",
    "germany", "france", "spain", "italy", "netherlands", "ireland", "canada", "australia",
    "india", "brazil", "mexico", "poland", "sweden", "norway", "denmark", "finland",
    "switzerland", "austria", "belgium", "portugal", "singapore", "japan", "uae",
    "dubai", "london", "berlin", "paris", "amsterdam", "new york", "toronto", "sydney",
)

_SIZE_RE = re.compile(
    r"(?i)(?:\bstartups?\b|\bsm[be]s?\b|\benterprises?\b|\bmid[- ]markets?\b|"
    r"\bscale[- ]ups?\b|\bsmall businesses\b|\bheadcount\b|"
    r"\d[\d,]*\s*(?:-|–|to)\s*\d[\d,]*|"
    r"\d[\d,]*\+?\s*(?:employees|staff|people|persons)\b)"
)

#: What a stored persona's confidence was computed with. Readers split the
#: spread before and after #7211 on this, because icp.generated's closed
#: shape cannot grow while another claim holds product_events.py.
CONFIDENCE_VERSION = 2

# Weights. A brief that names nothing stays under PARTLY_GROUNDED; offer and
# profile still move the number, with less weight than the brief.
_W_BRIEF = 0.34
_W_TITLE = 0.16
_W_REWRITE = 0.10
_W_HISTORY = 0.08
_W_OFFER = 0.20
_W_PROFILE = 0.10
_W_KB = 0.02


def brief_names_role(text: str | None) -> bool:
    """True when the brief names a job level ("bid managers", "CTOs", "founders")."""
    return bool(levels_named(text))


def brief_names_region(text: str | None) -> bool:
    """True when the brief names a country, a city or a region as a whole word."""
    return any(contains_term(text, term) for term in _REGION_TERMS)


def brief_names_size(text: str | None) -> bool:
    """True when the brief names a company size ("200-1000 staff", "startups")."""
    return bool(_SIZE_RE.search(str(text or "")))


def title_specificity(persona: Mapping[str, Any], *, investor: bool = False) -> float:
    """1 when a title names a function; lower when every title is only a rank.

    Founder, CEO, Owner, MD and President say how senior someone is (#5751).
    In an investor segment, Partner and Principal are the role.
    """
    titles = _include(persona, "job_titles")
    if not titles:
        return 0.2
    if any(not is_generic_title(title, investor=investor) for title in titles):
        return 1.0
    return 0.4


def rewrite_share(persona: Mapping[str, Any], prior: Mapping[str, Any] | None) -> float:
    """Share of the stored titles and seniority the model did not write.

    0 when nothing was rewritten, or when no snapshot was taken. Added titles
    and added levels count; a title the model already named does not.
    """
    if not isinstance(prior, Mapping) or not prior:
        return 0.0
    titles = _include(persona, "job_titles")
    levels = [item.lower() for item in _include(persona, "seniority")]
    prior_titles = {item.lower() for item in _include(prior, "job_titles")}
    prior_levels = {item.lower() for item in _include(prior, "seniority")}
    total = len(titles) + len(levels)
    if not total:
        return 0.0
    changed = sum(1 for title in titles if title.lower() not in prior_titles)
    changed += sum(1 for level in levels if level not in prior_levels)
    return changed / total


def grounded_confidence(
    persona: Mapping[str, Any], *, company_context: str, user_context: Mapping[str, Any] | None,
    kb_evidence: list[str] | None, target_description: str = "",
    prior: Mapping[str, Any] | None = None, saw_targeting_record: bool = False,
    investor: bool = False,
) -> tuple[float, float, float]:
    """(overall, completeness, evidence) from the brief and the persona, not the model.

    Completeness used to be the share of eight search fields the model fills
    every time, and evidence a near-constant of "an offer and a profile exist",
    so overall was 0.75 for 79% of personas (#7211). The number now moves when
    the brief names a role, a place or a company size, when a title names a
    function rather than only a rank, and when HeyLead rewrote the model's
    titles or seniority. A brief that names none of the three stays under
    PARTLY_GROUNDED. Offer, profile, a targeting record and knowledge-base
    lines still count, with less weight.
    """
    role = brief_names_role(target_description)
    region = brief_names_region(target_description)
    size = brief_names_size(target_description)
    brief_points = (int(role) + int(region) + int(size)) / 3
    titles = title_specificity(persona, investor=investor)
    kept = 1.0 - rewrite_share(persona, prior)
    offer_words = len(str(company_context or "").split())
    offer = 1.0 if offer_words >= 12 else 0.5 if offer_words else 0.0
    profile_map = user_context or {}
    profile = 1.0 if any(str(profile_map.get(key) or "").strip() for key in ("headline", "about", "core", "expertise")) else 0.0
    history = 1.0 if saw_targeting_record else 0.0
    kb = 1.0 if kb_evidence else 0.0
    overall = (
        _W_BRIEF * brief_points + _W_TITLE * titles + _W_REWRITE * kept
        + _W_HISTORY * history + _W_OFFER * offer + _W_PROFILE * profile + _W_KB * kb
    )
    if not (role or region or size):
        overall = min(overall, 0.49)
    elif role and region and size:
        overall = max(overall, PARTLY_GROUNDED)
    overall = max(0.1, min(0.9, overall))
    evidence = 0.15
    if offer_words >= 12:
        evidence += 0.25
    elif offer_words:
        evidence += 0.1
    if profile:
        evidence += 0.15
    if kb:
        evidence += 0.1
    if history:
        evidence += 0.1
    evidence = min(evidence, 0.85)
    completeness = (brief_points + titles + kept) / 3
    return round(overall, 2), round(completeness, 2), round(evidence, 2)



def priors_from_icps(icps) -> list[dict[str, Any]]:
    """Titles and seniority before the seniority policy rewrites them."""
    priors = []
    for icp in icps:
        titles = list(getattr(getattr(icp, "job_titles", None), "include", None) or [])
        levels = list(getattr(getattr(icp, "seniority", None), "include", None) or [])
        priors.append({
            "job_titles": {"include": [str(item) for item in titles]},
            "seniority": {"include": [str(item) for item in levels]},
        })
    return priors


def score_icps(
    icps, *, target_description: str, company_context: str, user_context, kb_evidence,
    priors: list[Mapping[str, Any]] | None, goal: str, saw_targeting_record: bool = False,
) -> None:
    """Write the grounded score onto each persona. The model's number is dropped."""
    from ..services.icp_vocabulary import is_investor_segment

    for index, icp in enumerate(icps):
        persona = {
            "job_titles": {"include": list(getattr(getattr(icp, "job_titles", None), "include", None) or [])},
            "seniority": {"include": list(getattr(getattr(icp, "seniority", None), "include", None) or [])},
            "name": getattr(icp, "name", ""),
            "description": getattr(icp, "description", ""),
            "keywords": list(getattr(icp, "keywords", None) or []),
        }
        prior = priors[index] if priors and index < len(priors) else None
        overall, completeness, evidence = grounded_confidence(
            persona, company_context=company_context or "", user_context=user_context,
            kb_evidence=kb_evidence, target_description=target_description, prior=prior,
            saw_targeting_record=saw_targeting_record,
            investor=is_investor_segment(persona, goal, target_description),
        )
        icp.overall_confidence = overall
        icp.data_completeness = completeness
        icp.evidence_strength = evidence
        icp.confidence_version = CONFIDENCE_VERSION
