"""Web-research the competitors of what the client sells, after ICP generation.

For a sale only: Serper for "{offer} competitors" and "{offer} alternatives",
then an LLM extract. Failures are empty, never blocking. Hosted accounts go
through the backend Serper proxy.

The subject is the offer (the campaign's company context, else its project
brief), never the sender's company. That value is the tail of the seat's
LinkedIn headline, the employer: on 25 Sep 2026 it had a person selling AI
outreach researched as "Tver State University" and handed "GoIT, Postnauka"
as competitors (D4umak/heylead-api#1428). The sender's company is only ever a
name to drop from the answer. A job search, a hire, a partnership, a vendor
search or research interviews have no competitors, so they are not researched.
The hosted twin is heylead-api ``app/services/competitor_research.py``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .. import goals
from ..textutil import contains_term
from .competitors import normalize_company, parse_competitor_names

logger = logging.getLogger(__name__)

_EXTRACT_SYSTEM = (
    "You extract competitor company names from web search snippets. "
    "Return strict JSON only."
)

_EXTRACT_PROMPT = """The client sells: {offer}
Context: {context}

Web search results:
{snippets}

List companies that sell the same kind of product or service to the same
buyers. Do NOT include the client itself, investors, customers, job boards,
schools or universities, or generic industry terms.

Return ONLY JSON:
{{"competitors": ["Company A", "Company B"]}}
"""

_OFFER_QUERY_CHARS = 80
_URL_SCHEME = re.compile(r"^https?://(www\.)?", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def researches_competitors(goal: Any) -> bool:
    """Only a sale has competitors to keep out of the audience."""
    return goals.normalize_goal(goal) == goals.SELL


def offer_text(company_context: str = "", project_brief: str = "") -> str:
    """What the campaign sells: its company context, else its project brief."""
    for text in (company_context, project_brief):
        flat = " ".join(str(text or "").split())
        if flat:
            return flat
    return ""


def offer_query(offer: str) -> str:
    """The search phrase for an offer: its first sentence, cut at a word."""
    text = _URL_SCHEME.sub("", " ".join(str(offer or "").split()))
    first = _SENTENCE_END.split(text, maxsplit=1)[0].rstrip(".!? ")
    if len(first) > _OFFER_QUERY_CHARS:
        first = first[:_OFFER_QUERY_CHARS].rsplit(" ", 1)[0]
    return first.strip(" ,;:-")


def _is_self(name: str, self_keys: list[str]) -> bool:
    key = normalize_company(name)
    return any(
        contains_term(key, own) or contains_term(own, key)
        for own in self_keys
    )


def names_from_extract(raw: Any, *, self_names: list[str]) -> list[str]:
    """Accept a list or {competitors: [...]} and drop the client's own names.

    Own is a whole-term match on the normalised names, either way round:
    "HeyLead AI" is HeyLead, and "Northwind" is "Northwind Group Ltd". Exact
    equality let the sender's employer through under any other spelling.
    """
    if isinstance(raw, dict):
        raw = raw.get("competitors") or raw.get("companies") or []
    names = parse_competitor_names(raw)
    self_keys = [k for k in (normalize_company(n) for n in self_names if n) if len(k) >= 2]
    return [n for n in names if not _is_self(n, self_keys)]


def _snippet_lines(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in items:
        title = str(item.get("title") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        if title or snippet:
            lines.append(f"- {title}: {snippet}"[:400])
    return "\n".join(lines[:12])


async def research_competitors(
    offer: str,
    *,
    goal: str,
    sender_company: str = "",
    target_description: str = "",
) -> list[str]:
    """Search the web for what competes with *offer*. Empty on failure.

    ``sender_company`` is never searched; it is dropped from the answer.
    """
    if not researches_competitors(goal):
        return []
    query_name = offer_query(offer)
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
        part for part in (offer, target_description) if part
    ).strip()[:1500]
    try:
        from ..ai.llm import LLMClient
        from ..constants import LLM_TIER_FAST
        client = LLMClient()
        parsed = await client.generate_json(
            _EXTRACT_PROMPT.format(
                offer=query_name,
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
    return names_from_extract(parsed, self_names=[sender_company])


async def attach_competitors_to_icp(
    result: Any,
    *,
    goal: str,
    offer: str,
    sender_company: str = "",
    target_description: str = "",
) -> list[str]:
    """Research and set ``result.competitors``. Returns the names. Never raises."""
    existing = parse_competitor_names(getattr(result, "competitors", None))
    if existing:
        return existing
    if not researches_competitors(goal):
        return []
    try:
        names = await research_competitors(
            offer,
            goal=goal,
            sender_company=sender_company,
            target_description=target_description,
        )
    except Exception:
        logger.info("Competitor research skipped", exc_info=True)
        return []
    if names and hasattr(result, "competitors"):
        result.competitors = names
    return names
