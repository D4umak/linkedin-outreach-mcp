"""Context summarization for ICP RAG pipeline.

Compresses retrieved evidence chunks into structured summaries
suitable for ICP synthesis. Ported from the original AI in Charge
ICPContextSummarizer.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..constants import LLM_TIER_FAST
from .llm import LLMClient
from .vector_search import SearchResult

logger = logging.getLogger(__name__)


@dataclass
class CompressedEvidence:
    """A compressed evidence snippet with source metadata."""

    chunk_id: str = ""
    evidence_snippet: str = ""
    source_uri: str = ""
    source_title: str = ""
    header_path: str = ""
    confidence: float = 0.5     # From similarity score


_COMPRESS_SYSTEM = """\
You are an expert business analyst. You extract the most relevant \
evidence from text snippets to support ICP (Ideal Customer Profile) generation.

Output structured JSON only. No markdown."""

_COMPRESS_PROMPT = """\
Extract the most relevant evidence from these text snippets for generating \
an ICP targeting: "{query}"

## TEXT SNIPPETS
{snippets}

## TASK
For each snippet that contains useful information for ICP generation, extract:
- The key evidence (industries, company characteristics, personas, geography, tech stack)
- A concise summary (1-2 sentences)

Return ONLY valid JSON:
{{
    "evidence": [
        {{
            "chunk_index": 0,
            "summary": "Key finding from this snippet",
            "relevance": "high" | "medium" | "low"
        }}
    ],
    "business_summary": "2-3 sentence summary of the overall business context"
}}"""


_SUMMARIZE_SYSTEM = """\
You are a business strategist. You synthesize evidence into concise \
business context summaries for sales targeting."""

_SUMMARIZE_PROMPT = """\
Synthesize these evidence snippets into a concise business context summary.

## EVIDENCE
{evidence}

## TASK
Create a business summary (max 300 words) covering:
1. What the company does and who they serve
2. Key products/services and value proposition
3. Target market characteristics
4. Notable strengths or differentiators
5. Industry context and market position

Return ONLY the summary text (no JSON, no markdown headers)."""


async def compress_evidence(
    query: str,
    search_results: list[SearchResult],
    max_snippets: int = 10,
) -> list[CompressedEvidence]:
    """LLM-powered evidence compression.

    Takes search results, asks the LLM to extract the most relevant
    evidence for ICP generation.

    Args:
        query: The ICP target description / search query.
        search_results: Raw vector search results.
        max_snippets: Maximum snippets to process.

    Returns:
        List of CompressedEvidence objects.
    """
    if not search_results:
        return []

    # Take top-N results
    results = search_results[:max_snippets]

    # Format snippets for the LLM
    snippet_parts = []
    for i, result in enumerate(results):
        header = f" ({result.header_path})" if result.header_path else ""
        snippet_parts.append(
            f"[{i}] Source: {result.source_title}{header}\n"
            f"    {result.ctx_text[:500]}"
        )

    snippets_text = "\n\n".join(snippet_parts)
    prompt = _COMPRESS_PROMPT.format(query=query, snippets=snippets_text)

    # Route through backend or local LLM
    from ..config import has_local_llm_key, is_backend_mode
    if is_backend_mode() and not has_local_llm_key():
        # In backend mode, skip compression — just use raw results
        return _raw_to_evidence(results)

    client = LLMClient()
    raw = await client.generate(prompt, system=_COMPRESS_SYSTEM, temperature=0.3, tier=LLM_TIER_FAST)

    try:
        data = _parse_json(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Failed to parse evidence compression, using raw results")
        return _raw_to_evidence(results)

    # Build compressed evidence from LLM output
    evidence_list = []
    for item in data.get("evidence", []):
        idx = item.get("chunk_index", 0)
        if idx < 0 or idx >= len(results):
            continue
        relevance = item.get("relevance", "medium")
        if relevance == "low":
            continue

        result = results[idx]
        evidence_list.append(CompressedEvidence(
            chunk_id=result.chunk_id,
            evidence_snippet=item.get("summary", result.text[:200]),
            source_uri=result.source_uri,
            source_title=result.source_title,
            header_path=result.header_path,
            confidence=_relevance_to_confidence(relevance, result.similarity_score),
        ))

    return evidence_list


async def summarize_context(
    evidence: list[CompressedEvidence],
) -> str:
    """Create a business summary from compressed evidence.

    Args:
        evidence: Compressed evidence snippets.

    Returns:
        Business summary string (max ~300 words).
    """
    if not evidence:
        return ""

    evidence_parts = []
    for i, e in enumerate(evidence):
        source = e.source_title or e.source_uri or "Unknown"
        evidence_parts.append(f"- [{source}] {e.evidence_snippet}")

    evidence_text = "\n".join(evidence_parts)
    prompt = _SUMMARIZE_PROMPT.format(evidence=evidence_text)

    from ..config import has_local_llm_key, is_backend_mode
    if is_backend_mode() and not has_local_llm_key():
        # In backend mode, build a simple summary from evidence
        return "\n".join(e.evidence_snippet for e in evidence[:5])

    client = LLMClient()
    raw = await client.generate(prompt, system=_SUMMARIZE_SYSTEM, temperature=0.3, tier=LLM_TIER_FAST)
    return raw.strip()


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _raw_to_evidence(results: list[SearchResult]) -> list[CompressedEvidence]:
    """Convert raw search results to CompressedEvidence without LLM."""
    return [
        CompressedEvidence(
            chunk_id=r.chunk_id,
            evidence_snippet=r.text[:300],
            source_uri=r.source_uri,
            source_title=r.source_title,
            header_path=r.header_path,
            confidence=min(r.similarity_score, 0.9),
        )
        for r in results
    ]


def _relevance_to_confidence(relevance: str, similarity: float) -> float:
    """Convert relevance label + similarity score to a confidence value."""
    base = {"high": 0.8, "medium": 0.6, "low": 0.3}.get(relevance, 0.5)
    # Blend with similarity score (50/50)
    return min((base + similarity) / 2, 0.9)


def _parse_json(raw: str) -> dict:
    """Parse JSON from LLM response."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(
            line for line in lines if not line.strip().startswith("```")
        )
    return json.loads(cleaned)
