"""Evidence citation and confidence scoring for ICP fields.

Matches ICP field values to supporting evidence snippets and computes
field-level and overall confidence scores.

Ported from the original AI in Charge _generate_citations_and_evidence.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..textutil import contains_term, is_evidence_term
from .context_summarizer import CompressedEvidence
from .icp_schemas import Citation, FieldEvidence, SingleIcp
from .vector_search import SearchResult

logger = logging.getLogger(__name__)

# Words that say nothing about a field value. Until heylead-api #1570 any 3+
# letter word of a value counted as a citation by substring, so "and" or
# "the" in any sentence cited every pain point, and "bid" inside "forbidden"
# cited "Bid Director".
# The one term filter (heylead-api#1809): textutil.STOPWORDS holds this list's
# words, and is_evidence_term the three-character floor this used to write out.

_VALUE_WORD = re.compile(r"[a-z0-9][a-z0-9+&'.-]*")


def _value_words(value: str) -> list[str]:
    return [w for w in _VALUE_WORD.findall(value.lower()) if is_evidence_term(w)]


def _cites(value: str, text: str) -> bool:
    """Whether *text* names *value*: the whole value, or one of its words, as whole words.

    A plural counts either way ("CTOs" cites "CTO", "bids" cites "bid").
    Matching goes through contains_term, never substring containment.
    """
    if not value or not text:
        return False
    if contains_term(text, value):
        return True
    for word in _value_words(value):
        forms = {word, f"{word}s", word[:-1] if word.endswith("s") and len(word) > 3 else word}
        if any(contains_term(text, form) for form in forms):
            return True
    return False


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
    target_description: str = "",
) -> tuple[float, float, float]:
    """Overall, completeness and evidence from the brief, not from filled fields.

    Field-fill times citation confidence was one number for almost every
    persona (api #7211). A persona the generator already scored
    (confidence_version) is left as it is.
    """
    from .icp_grounding import grounded_confidence

    persona = {
        "job_titles": {"include": list(icp.job_titles.include or [])},
        "seniority": {"include": list(icp.seniority.include or [])},
        "name": icp.name,
        "description": icp.description,
        "keywords": list(icp.keywords or []),
    }
    kb = ["cited"] if field_evidence or kb_chunk_count or client_chunk_count > 20 else None
    return grounded_confidence(
        persona, company_context="", user_context=None, kb_evidence=kb,
        target_description=target_description,
    )


def apply_evidence_to_icp(
    icp: SingleIcp,
    evidence_snippets: list[CompressedEvidence],
    kb_results: list[SearchResult] | None = None,
    client_chunk_count: int = 0,
    kb_chunk_count: int = 0,
    target_description: str = "",
) -> SingleIcp:
    """Apply evidence citations and confidence scores to an ICP.

    Modifies the ICP in-place and returns it.
    """
    field_evidence = generate_field_citations(icp, evidence_snippets, kb_results)
    icp.field_evidence = field_evidence

    if icp.confidence_version >= 2:
        return icp
    overall, completeness, strength = compute_confidence_scores(
        icp, field_evidence, client_chunk_count, kb_chunk_count,
        target_description=target_description,
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
        for snippet in evidence:
            # Whole words, never substrings (D4umak/heylead-api#1570).
            if _cites(str(value), snippet.evidence_snippet):
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
                if _cites(str(value), result.text):
                    citations.append(Citation(
                        source_url=result.source_uri,
                        header_path=result.header_path,
                        snippet=result.text[:300],
                        confidence=min(result.similarity_score, 0.8),
                    ))
                    break  # One citation per value from KB

    return citations
