"""Evidence citation and confidence scoring for ICP fields.

Matches ICP field values to supporting evidence snippets and computes
field-level and overall confidence scores.

Ported from the original AI in Charge _generate_citations_and_evidence.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .context_summarizer import CompressedEvidence
from .icp_schemas import Citation, FieldEvidence, SingleIcp
from .vector_search import SearchResult

logger = logging.getLogger(__name__)

# Fields to generate citations for
_CITATION_FIELDS = [
    "industries",
    "job_titles",
    "locations",
    "departments",
    "company_types",
    "keywords",
    "pain_points",
    "fears",
    "barriers",
]


def generate_field_citations(
    icp: SingleIcp,
    evidence_snippets: list[CompressedEvidence],
    kb_results: list[SearchResult] | None = None,
) -> list[FieldEvidence]:
    """Match ICP field values to supporting evidence snippets.

    For each major ICP field, searches the evidence for mentions
    of the field values. Returns FieldEvidence objects with citations.

    Args:
        icp: A SingleIcp to generate citations for.
        evidence_snippets: Compressed evidence from company data.
        kb_results: Optional knowledge base search results.

    Returns:
        List of FieldEvidence objects.
    """
    all_evidence: list[FieldEvidence] = []

    for field_name in _CITATION_FIELDS:
        values = _extract_field_values(icp, field_name)
        if not values:
            continue

        citations = _find_citations(values, evidence_snippets, kb_results)
        if not citations:
            # No evidence found — low confidence
            all_evidence.append(FieldEvidence(
                field_name=field_name,
                value=values,
                citations=[],
                confidence=0.3,
                source_count=0,
            ))
            continue

        # Compute field confidence from citation scores
        avg_confidence = sum(c.confidence for c in citations) / len(citations)
        field_confidence = min(avg_confidence, 0.9)  # Cap at 0.9

        all_evidence.append(FieldEvidence(
            field_name=field_name,
            value=values,
            citations=citations[:3],  # Max 3 citations per field
            confidence=field_confidence,
            source_count=len(set(c.source_url for c in citations)),
        ))

    return all_evidence


def compute_confidence_scores(
    icp: SingleIcp,
    field_evidence: list[FieldEvidence],
    client_chunk_count: int = 0,
    kb_chunk_count: int = 0,
) -> tuple[float, float, float]:
    """Compute overall, completeness, and evidence_strength scores.

    Args:
        icp: The ICP to score.
        field_evidence: Evidence for each field.
        client_chunk_count: Number of client data chunks available.
        kb_chunk_count: Number of KB chunks available.

    Returns:
        Tuple of (overall_confidence, data_completeness, evidence_strength).
    """
    # Data completeness: what % of key fields are meaningfully populated
    key_fields = [
        bool(icp.industries.include),
        bool(icp.job_titles.include),
        bool(icp.locations.include),
        bool(icp.pain_points),
        bool(icp.seniority.include),
        bool(icp.company_headcount.min or icp.company_headcount.max),
        bool(icp.keywords),
        bool(icp.description),
    ]
    data_completeness = sum(key_fields) / len(key_fields)

    # Evidence strength: average confidence of cited fields
    if field_evidence:
        cited = [fe for fe in field_evidence if fe.citations]
        if cited:
            evidence_strength = sum(fe.confidence for fe in cited) / len(cited)
        else:
            evidence_strength = 0.2  # No evidence found
    else:
        evidence_strength = 0.2  # No evidence processing

    # Boost evidence_strength slightly if we have more data
    if client_chunk_count > 20:
        evidence_strength = min(evidence_strength + 0.05, 0.95)
    if kb_chunk_count > 0:
        evidence_strength = min(evidence_strength + 0.05, 0.95)

    # Overall confidence: weighted blend
    overall_confidence = (
        data_completeness * 0.4
        + evidence_strength * 0.6
    )

    # Clamp
    overall_confidence = max(0.1, min(0.95, overall_confidence))
    data_completeness = max(0.1, min(1.0, data_completeness))
    evidence_strength = max(0.1, min(0.95, evidence_strength))

    return overall_confidence, data_completeness, evidence_strength


def apply_evidence_to_icp(
    icp: SingleIcp,
    evidence_snippets: list[CompressedEvidence],
    kb_results: list[SearchResult] | None = None,
    client_chunk_count: int = 0,
    kb_chunk_count: int = 0,
) -> SingleIcp:
    """Apply evidence citations and confidence scores to an ICP.

    Modifies the ICP in-place and returns it.
    """
    field_evidence = generate_field_citations(icp, evidence_snippets, kb_results)
    icp.field_evidence = field_evidence

    overall, completeness, strength = compute_confidence_scores(
        icp, field_evidence, client_chunk_count, kb_chunk_count,
    )
    icp.overall_confidence = overall
    icp.data_completeness = completeness
    icp.evidence_strength = strength

    return icp


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _extract_field_values(icp: SingleIcp, field_name: str) -> list[str]:
    """Extract string values from an ICP field."""
    if field_name in ("industries", "job_titles", "locations", "departments", "seniority"):
        param = getattr(icp, field_name, None)
        if param is None:
            return []
        return getattr(param, "include", []) or []

    if field_name == "company_types":
        return icp.company_types or []

    if field_name == "keywords":
        return icp.keywords or []

    if field_name in ("pain_points", "fears", "barriers"):
        return getattr(icp, field_name, []) or []

    return []


def _find_citations(
    values: list[str],
    evidence: list[CompressedEvidence],
    kb_results: list[SearchResult] | None = None,
) -> list[Citation]:
    """Find evidence citations matching given field values."""
    citations: list[Citation] = []

    for value in values:
        value_lower = value.lower()
        # Split multi-word values into tokens for flexible matching
        tokens = [t for t in value_lower.split() if len(t) > 2]

        for snippet in evidence:
            text_lower = snippet.evidence_snippet.lower()
            # Check if any token appears in the evidence
            if any(token in text_lower for token in tokens) or value_lower in text_lower:
                citations.append(Citation(
                    source_url=snippet.source_uri,
                    header_path=snippet.header_path,
                    snippet=snippet.evidence_snippet[:300],
                    confidence=snippet.confidence,
                ))
                break  # One citation per value from evidence

        # Also check KB results
        if kb_results:
            for result in kb_results:
                text_lower = result.text.lower()
                if any(token in text_lower for token in tokens) or value_lower in text_lower:
                    citations.append(Citation(
                        source_url=result.source_uri,
                        header_path=result.header_path,
                        snippet=result.text[:300],
                        confidence=min(result.similarity_score, 0.8),
                    ))
                    break  # One citation per value from KB

    return citations
