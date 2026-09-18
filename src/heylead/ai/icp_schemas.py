"""Data structures for the ICP (Ideal Customer Profile) system.

Ported from the original AI in Charge ICP Agent schemas, adapted for
HeyLead's local-first MCP architecture. Uses dataclasses instead of
Pydantic to avoid adding dependencies.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

# ──────────────────────────────────────────────
# Evidence & Citations
# ──────────────────────────────────────────────

@dataclass
class Citation:
    """Evidence citation with source information."""

    source_url: str = ""
    header_path: str = ""       # e.g. "About > Team"
    snippet: str = ""           # max ~500 chars
    confidence: float = 0.5     # 0.0–1.0


@dataclass
class FieldEvidence:
    """Evidence supporting a specific ICP field value."""

    field_name: str = ""
    value: Any = None
    citations: list[Citation] = field(default_factory=list)
    confidence: float = 0.5     # 0.0–1.0
    source_count: int = 0


# ──────────────────────────────────────────────
# LinkedIn Search Parameters
# ──────────────────────────────────────────────

@dataclass
class LinkedinSearchParam:
    """Include/exclude pattern for LinkedIn filters."""

    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


@dataclass
class HeadcountParam:
    """Company headcount range."""

    min: int | None = None      # 1, 11, 51, 201, 501, 1001, 5001, 10001
    max: int | None = None      # 10, 50, 200, 500, 1000, 5000, 10000


@dataclass
class TenureParam:
    """Years of tenure range."""

    min: int | None = None      # 0, 1, 3, 6, 10
    max: int | None = None      # 1, 2, 5, 10


# ──────────────────────────────────────────────
# LinkedIn Enrichment (populated in Phase 6)
# ──────────────────────────────────────────────

@dataclass
class EnrichedParam:
    """A LinkedIn parameter with its provider-specific code."""

    name: str = ""
    code: str = ""


@dataclass
class EnrichedField:
    """Validated LinkedIn parameters with provider codes."""

    include: list[EnrichedParam] = field(default_factory=list)
    exclude: list[EnrichedParam] = field(default_factory=list)


@dataclass
class EnrichedIcpParams:
    """Container for all enriched LinkedIn fields."""

    industries: EnrichedField | None = None
    job_titles: EnrichedField | None = None
    locations: EnrichedField | None = None
    company_locations: EnrichedField | None = None
    departments: EnrichedField | None = None

    # Fields whose /search/parameters lookup was unavailable, so their codes
    # were NOT refreshed. Without this the caller cannot tell a field that
    # genuinely has no LinkedIn codes from one whose lookup 502'd — and a
    # degraded field looks like a clean, slightly smaller result.
    failed_fields: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────
# Core ICP Structure
# ──────────────────────────────────────────────

@dataclass
class SingleIcp:
    """A single ICP targeting a specific market segment."""

    # Identity
    name: str = ""
    description: str = ""

    # Psychology
    pain_points: list[str] = field(default_factory=list)
    fears: list[str] = field(default_factory=list)
    barriers: list[str] = field(default_factory=list)

    # LinkedIn search parameters (include/exclude)
    industries: LinkedinSearchParam = field(default_factory=LinkedinSearchParam)
    job_titles: LinkedinSearchParam = field(default_factory=LinkedinSearchParam)
    locations: LinkedinSearchParam = field(default_factory=LinkedinSearchParam)
    departments: LinkedinSearchParam | None = None
    company_headcount: HeadcountParam = field(default_factory=HeadcountParam)
    company_types: list[str] = field(default_factory=list)
    company_locations: LinkedinSearchParam | None = None
    tenure: TenureParam = field(default_factory=TenureParam)
    seniority: LinkedinSearchParam = field(default_factory=LinkedinSearchParam)
    keywords: list[str] = field(default_factory=list)
    profile_signals: dict[str, Any] | None = None

    # Sales Navigator advanced filters
    spotlight_filters: dict[str, bool] | None = None  # {"changed_jobs": True, "posted_recently": True}
    boolean_keywords: str = ""  # '("VP Sales" OR "Head of Sales") AND fintech NOT "intern"'
    annual_revenue: dict[str, int] | None = None  # {"min": 1000000, "max": 50000000}
    company_headcount_growth: str = ""  # "negative", "neutral", "positive", "rapid"

    # Confidence metrics (0.0–1.0)
    overall_confidence: float = 0.5
    data_completeness: float = 0.5
    evidence_strength: float = 0.5

    # Evidence (populated in Phase 5)
    field_evidence: list[FieldEvidence] = field(default_factory=list)

    # LinkedIn enrichment (populated in Phase 6)
    linkedin_enriched_params: EnrichedIcpParams | None = None


@dataclass
class IcpResult:
    """Complete ICP generation output with multiple personas/segments."""

    icps: list[SingleIcp] = field(default_factory=list)
    summary: str = ""
    campaign_name: str = ""
    relevance_hook: str = ""
    processing_time: float = 0.0
    source_info: dict[str, Any] = field(default_factory=dict)
    # Companies that compete with the sender. Researched after generation
    # and used to keep those employers out of the campaign (a customer, 11 Sep 2026).
    competitors: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────
# Serialization helpers
# ──────────────────────────────────────────────

def icp_result_to_dict(result: IcpResult) -> dict[str, Any]:
    """Convert an IcpResult to a JSON-serializable dict."""
    return asdict(result)


def icp_result_to_json(result: IcpResult) -> str:
    """Serialize an IcpResult to a JSON string."""
    return json.dumps(asdict(result), default=str)


def icp_result_from_dict(data: dict[str, Any]) -> IcpResult:
    """Reconstruct an IcpResult from a dict (best-effort)."""
    icps = []
    for icp_data in data.get("icps", []):
        icp = SingleIcp(
            name=icp_data.get("name", ""),
            description=icp_data.get("description", ""),
            pain_points=icp_data.get("pain_points", []),
            fears=icp_data.get("fears", []),
            barriers=icp_data.get("barriers", []),
            industries=_parse_search_param(icp_data.get("industries")),
            job_titles=_parse_search_param(icp_data.get("job_titles")),
            locations=_parse_search_param(icp_data.get("locations")),
            departments=_parse_search_param(icp_data.get("departments")),
            company_headcount=_parse_headcount(icp_data.get("company_headcount")),
            company_types=icp_data.get("company_types", []),
            company_locations=_parse_search_param(icp_data.get("company_locations")),
            tenure=_parse_tenure(icp_data.get("tenure")),
            seniority=_parse_search_param(icp_data.get("seniority")),
            keywords=icp_data.get("keywords", []),
            profile_signals=icp_data.get("profile_signals"),
            spotlight_filters=icp_data.get("spotlight_filters"),
            boolean_keywords=icp_data.get("boolean_keywords", ""),
            annual_revenue=icp_data.get("annual_revenue"),
            company_headcount_growth=icp_data.get("company_headcount_growth", ""),
            overall_confidence=icp_data.get("overall_confidence", 0.5),
            data_completeness=icp_data.get("data_completeness", 0.5),
            evidence_strength=icp_data.get("evidence_strength", 0.5),
            field_evidence=_parse_field_evidence_list(icp_data.get("field_evidence")),
            linkedin_enriched_params=_parse_enriched_icp_params(icp_data.get("linkedin_enriched_params")),
        )
        icps.append(icp)

    return IcpResult(
        icps=icps,
        summary=data.get("summary", ""),
        campaign_name=data.get("campaign_name", ""),
        relevance_hook=data.get("relevance_hook", ""),
        processing_time=data.get("processing_time", 0.0),
        source_info=data.get("source_info", {}),
        competitors=[
            str(n).strip() for n in (data.get("competitors") or [])
            if str(n).strip()
        ],
    )


def _parse_search_param(data: Any) -> LinkedinSearchParam:
    """Parse a LinkedinSearchParam from dict or None."""
    if data is None:
        return LinkedinSearchParam()
    if isinstance(data, dict):
        return LinkedinSearchParam(
            include=data.get("include", []),
            exclude=data.get("exclude", []),
        )
    return LinkedinSearchParam()


def _parse_headcount(data: Any) -> HeadcountParam:
    if data is None:
        return HeadcountParam()
    if isinstance(data, dict):
        return HeadcountParam(
            min=data.get("min"),
            max=data.get("max"),
        )
    return HeadcountParam()


def _parse_tenure(data: Any) -> TenureParam:
    if data is None:
        return TenureParam()
    if isinstance(data, dict):
        return TenureParam(
            min=data.get("min"),
            max=data.get("max"),
        )
    return TenureParam()


def _parse_citation(data: Any) -> Citation:
    if not isinstance(data, dict):
        return Citation()
    return Citation(
        source_url=data.get("source_url", data.get("source_uri", "")),
        header_path=data.get("header_path", ""),
        snippet=data.get("snippet", ""),
        confidence=float(data.get("confidence", 0.5)),
    )


def _parse_field_evidence_list(data: Any) -> list[FieldEvidence]:
    if not isinstance(data, list):
        return []
    result = []
    for item in data:
        if not isinstance(item, dict):
            continue
        citations = [
            _parse_citation(c) for c in item.get("citations", [])
            if isinstance(c, dict)
        ]
        result.append(FieldEvidence(
            field_name=item.get("field_name", ""),
            value=item.get("value"),
            citations=citations,
            confidence=float(item.get("confidence", 0.5)),
            source_count=int(item.get("source_count", 0)),
        ))
    return result


def _parse_enriched_param(data: Any) -> EnrichedParam:
    if not isinstance(data, dict):
        return EnrichedParam()
    return EnrichedParam(
        name=data.get("name", ""),
        code=data.get("code", ""),
    )


def _parse_enriched_field(data: Any) -> EnrichedField:
    if not isinstance(data, dict):
        return EnrichedField()
    include = data.get("include") or []
    exclude = data.get("exclude") or []
    return EnrichedField(
        include=[_parse_enriched_param(p) for p in include if isinstance(p, dict)],
        exclude=[_parse_enriched_param(p) for p in exclude if isinstance(p, dict)],
    )


def _parse_enriched_icp_params(data: Any) -> EnrichedIcpParams | None:
    if not isinstance(data, dict):
        return None
    return EnrichedIcpParams(
        industries=_parse_enriched_field(data.get("industries")) if data.get("industries") else None,
        job_titles=_parse_enriched_field(data.get("job_titles")) if data.get("job_titles") else None,
        locations=_parse_enriched_field(data.get("locations")) if data.get("locations") else None,
        company_locations=_parse_enriched_field(data.get("company_locations")) if data.get("company_locations") else None,
        departments=_parse_enriched_field(data.get("departments")) if data.get("departments") else None,
        failed_fields=[f for f in (data.get("failed_fields") or []) if isinstance(f, str)],
    )
