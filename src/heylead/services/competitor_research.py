"""Web-research the client's competitors after ICP generation.

Serper for "{company} competitors", then an LLM extract. Failures are empty,
never blocking. Hosted accounts go through the backend Serper proxy.
"""

from __future__ import annotations

import logging
from typing import Any

from .competitors import normalize_company, parse_competitor_names

logger = logging.getLogger(__name__)

_EXTRACT_SYSTEM = (
    "You extract competitor company names from web search snippets. "
    "Return strict JSON only."
)

_EXTRACT_PROMPT = """The client company is: {company}
Context: {context}

Web search results:
{snippets}

List companies that compete with this client — same product category, sold to
the same buyers. Do NOT include the client itself, investors, customers,
job boards, or generic industry terms.

Return ONLY JSON:
{{"competitors": ["Company A", "Company B"]}}
"""


def names_from_extract(raw: Any, *, self_names: list[str]) -> list[str]:
    if isinstance(raw, dict):
        raw = raw.get("competitors") or raw.get("companies") or []
    names = parse_competitor_names(raw)
    self_keys = {normalize_company(n) for n in self_names if n}
    return [n for n in names if normalize_company(n) not in self_keys]


def _snippet_lines(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in items:
        title = str(item.get("title") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        if title or snippet:
            lines.append(f"- {title}: {snippet}"[:400])
    return "\n".join(lines[:12])


async def research_competitors(
    company_name: str,
    company_context: str = "",
    target_description: str = "",
) -> list[str]:
    """Search the web and extract competitor company names. Empty on failure."""
    company = (company_name or "").strip()
    if not company and not (company_context or "").strip():
        return []
    query_name = company or (target_description or "").strip()[:80]
    if not query_name:
        return []

    items: list[dict[str, Any]] = []
    try:
        from ..ai.news_service import _fetch_serper_search
        for query in (f"{query_name} competitors", f"{query_name} alternatives"):
            try:
                items.extend(await _fetch_serper_search(query, num_results=5))
            except Exception:
                logger.info("Competitor search failed for %r", query, exc_info=True)
    except Exception:
        logger.info("Competitor search unavailable", exc_info=True)
        return []

    snippets = _snippet_lines(items)
    if not snippets:
        return []

    context = " ".join(
        part for part in (company_context, target_description) if part
    ).strip()[:1500]
    try:
        from ..ai.llm import LLMClient
        from ..constants import LLM_TIER_FAST
        client = LLMClient()
        parsed = await client.generate_json(
            _EXTRACT_PROMPT.format(
                company=query_name,
                context=context or "(none)",
                snippets=snippets,
            ),
            schema={
                "type": "object",
                "properties": {
                    "competitors": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["competitors"],
            },
            system=_EXTRACT_SYSTEM,
            temperature=0.1,
            max_tokens=512,
            tier=LLM_TIER_FAST,
        )
    except Exception:
        logger.info("Competitor extract failed for %s", query_name, exc_info=True)
        return []
    return names_from_extract(parsed, self_names=[company, query_name])


async def attach_competitors_to_icp(
    result: Any,
    *,
    company_name: str = "",
    company_context: str = "",
    target_description: str = "",
) -> list[str]:
    """Research and set ``result.competitors``. Returns the names. Never raises."""
    existing = parse_competitor_names(getattr(result, "competitors", None))
    if existing:
        return existing
    try:
        names = await research_competitors(
            company_name,
            company_context=company_context,
            target_description=target_description,
        )
    except Exception:
        logger.info("Competitor research skipped", exc_info=True)
        return []
    if names and hasattr(result, "competitors"):
        result.competitors = names
    return names
