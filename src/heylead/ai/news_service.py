"""News service — fetch recent company/industry news via SERPER API.

Provides contextual news for LinkedIn outreach messages. Two-tier search:
1. Company-specific news (pastMonth, 3 results)
2. Industry fallback (pastWeek, 3 results)

LLM generates search queries based on prospect/sender context (no hardcoded searches).
News is formatted for prompt injection into follow-up and invitation generators.

Ported from the original AI in Charge news_service.py with:
- httpx.AsyncClient instead of requests (already a dependency)
- LLMClient instead of LangChain
- Config-based API key instead of env var

Completely optional — silently returns None if SERPER key not configured.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .. import constants
from ..config import get_serper_api_key
from ..constants import LLM_TIER_FAST
from .llm import LLMClient

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(10.0, connect=5.0, read=15.0)


# ──────────────────────────────────────────────
# Search Query Generation (LLM)
# ──────────────────────────────────────────────

_COMPANY_QUERY_PROMPT = """TASK: Generate a search query to find recent news specifically about the prospect's company.
Extract the company name and relevant details from the contact information.
Focus on company announcements, developments, acquisitions, or major changes.
Query should be short, usable by SERPER API.
DO NOT include any year or date in the query.

CONTACT INFORMATION:
Name: {name}
Title: {title}
Company: {company}
Headline: {headline}

OUTPUT: Return ONLY the search query text with no additional explanation or formatting.
Make sure to include the company name in the query."""

_INDUSTRY_QUERY_PROMPT = """TASK: Generate a search query to find recent industry news relevant to both the prospect and the sender's company.
Focus on industry trends, market changes, or news that would connect the prospect's needs with the sender's offerings.
Query should be short, usable by SERPER API.
DO NOT include any year or date in the query.

PROSPECT:
Name: {name}
Title: {title}
Company: {company}
Headline: {headline}

SENDER COMPANY: {sender_company}

OUTPUT: Return ONLY the search query text with no additional explanation or formatting.
Focus on industry terms and trends rather than specific company names."""


async def _generate_search_query(
    prospect: dict[str, Any],
    query_type: str,
    sender: dict[str, Any] | None = None,
) -> str:
    """Use LLM to generate a contextual search query.

    Args:
        prospect: Prospect profile data
        query_type: "company" or "industry"
        sender: Sender profile (needed for industry queries)

    Returns:
        Short search query string
    """
    name = prospect.get("name", "")
    title = prospect.get("title", "")
    company = prospect.get("company", "")
    headline = prospect.get("headline", f"{title} at {company}")

    if query_type == "company":
        prompt = _COMPANY_QUERY_PROMPT.format(
            name=name, title=title, company=company, headline=headline,
        )
    else:
        sender_company = (sender or {}).get("company", "")
        prompt = _INDUSTRY_QUERY_PROMPT.format(
            name=name, title=title, company=company,
            headline=headline, sender_company=sender_company,
        )

    llm = LLMClient()
    query = await llm.generate(prompt, temperature=0.3, max_tokens=100, tier=LLM_TIER_FAST)
    query = query.strip().strip('"').strip("'").strip()
    logger.info("Generated %s search query: %s", query_type, query)
    return query


# ──────────────────────────────────────────────
# SERPER API
# ──────────────────────────────────────────────

async def _fetch_serper_news(
    query: str,
    num_results: int = 3,
    time_range: str = "pastMonth",
) -> list[dict[str, Any]]:
    """Fetch news articles from SERPER API.

    Routes through backend proxy when in backend mode and no local SERPER key.

    Args:
        query: Search query
        num_results: Number of results to return
        time_range: "pastWeek", "pastMonth", "pastYear"

    Returns:
        List of news items (title, link, snippet, date, source)
    """
    api_key = get_serper_api_key()

    # If no local SERPER key, try routing through backend proxy
    if not api_key:
        return await _fetch_news_via_backend(query, num_results, time_range)

    # Direct SERPER API call (local key available)
    payload = {
        "q": query,
        "num": num_results,
        "timeRange": time_range,
    }
    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                constants.SERPER_API_URL,
                json=payload,
                headers=headers,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

        news_items = []
        for item in data.get("news", [])[:num_results]:
            news_items.append({
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "snippet": item.get("snippet", ""),
                "date": item.get("date", ""),
                "source": item.get("source", ""),
            })

        logger.info("SERPER returned %d news items for: %s", len(news_items), query)
        return news_items

    except Exception as e:
        logger.warning("SERPER API error: %s — trying Google News RSS fallback", e)
        return await _fetch_google_news_rss(query, num_results)


async def _fetch_serper_search(
    query: str,
    num_results: int = 5,
    time_range: str = "pastMonth",
) -> list[dict[str, Any]]:
    """Fetch organic web results from SERPER search (not the news endpoint).

    ``site:`` queries (Reddit, HN, G2, ATS boards) do not come back from
    ``/news``. Local key hits ``/search`` and reads ``organic``. No local
    key: degrade to the backend news proxy with the same query — hosted
    still returns *something*, just not site-restricted threads.

    No Google News RSS fallback: RSS will not return Reddit threads or
    Greenhouse boards.
    """
    api_key = get_serper_api_key()

    if not api_key:
        return await _fetch_search_via_backend(query, num_results, time_range)

    payload = {
        "q": query,
        "num": num_results,
        "timeRange": time_range,
    }
    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                constants.SERPER_SEARCH_API_URL,
                json=payload,
                headers=headers,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

        items = []
        for item in data.get("organic", [])[:num_results]:
            items.append({
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "snippet": item.get("snippet", ""),
                "date": item.get("date", ""),
                "source": item.get("source", "") or item.get("sitename", ""),
            })

        logger.info("SERPER search returned %d items for: %s", len(items), query)
        return items

    except Exception as e:
        logger.warning("SERPER search error: %s", e)
        return []


async def _fetch_search_via_backend(
    query: str,
    num_results: int = 5,
    time_range: str = "pastMonth",
) -> list[dict[str, Any]]:
    """Degrade to the backend news proxy when no local SERPER key exists.

    There is no hosted web-search endpoint in this cut. News results for
    a ``site:`` query are a degraded substitute, not an empty hard-fail.
    """
    try:
        from ..linkedin import get_linkedin_client
        from ..linkedin.backend_client import BackendClient

        client = get_linkedin_client()
        if not isinstance(client, BackendClient):
            return []

        try:
            results = await client.search_news(
                query=query,
                num_results=num_results,
                time_range=time_range,
            )
            return results or []
        finally:
            await client.close()

    except Exception as e:
        logger.debug("Backend search fallback unavailable: %s", e)
        return []


async def _fetch_news_via_backend(
    query: str,
    num_results: int = 3,
    time_range: str = "pastMonth",
) -> list[dict[str, Any]]:
    """Fetch news via the backend SERPER proxy (backend mode only).

    Falls back to Google News RSS if SERPER proxy fails.
    Falls back silently to empty list if backend mode is not active
    or backend doesn't have SERPER configured.
    """
    try:
        from ..linkedin import get_linkedin_client
        from ..linkedin.backend_client import BackendClient

        client = get_linkedin_client()
        if not isinstance(client, BackendClient):
            # Not in backend mode — try Google News RSS directly
            return await _fetch_google_news_rss(query, num_results)

        try:
            results = await client.search_news(
                query=query,
                num_results=num_results,
                time_range=time_range,
            )
            if results:
                return results
        finally:
            await client.close()

    except Exception as e:
        logger.debug("Backend news search unavailable: %s — trying RSS fallback", e)

    # Fallback: Google News RSS (free, no API key needed)
    return await _fetch_google_news_rss(query, num_results)


# ──────────────────────────────────────────────
# Google News RSS Fallback
# ──────────────────────────────────────────────

async def _fetch_google_news_rss(
    query: str,
    num_results: int = 5,
) -> list[dict[str, Any]]:
    """Fetch news via Google News RSS feed (free, no API key needed).

    This is the fallback when SERPER API is unavailable (502, key issues, etc.).
    Parses the RSS XML feed from Google News search.

    Args:
        query: Search query string.
        num_results: Max number of results to return.

    Returns:
        List of news items with title, link, snippet, date, source.
    """
    import xml.etree.ElementTree as ET
    from urllib.parse import quote

    encoded_query = quote(query)
    rss_url = (
        f"https://news.google.com/rss/search"
        f"?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
    )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(rss_url, timeout=_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            xml_text = resp.text

        root = ET.fromstring(xml_text)
        channel = root.find("channel")
        if channel is None:
            return []

        news_items: list[dict[str, Any]] = []
        for item in channel.findall("item"):
            if len(news_items) >= num_results:
                break

            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            description = (item.findtext("description") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()

            # Google News RSS includes source in the title: "Title - Source"
            source = ""
            if " - " in title:
                parts = title.rsplit(" - ", 1)
                if len(parts) == 2:
                    title = parts[0].strip()
                    source = parts[1].strip()

            # Strip HTML from description (Google News wraps in <a> tags)
            import re
            snippet = re.sub(r"<[^>]+>", "", description).strip()

            if title:
                news_items.append({
                    "title": title,
                    "link": link,
                    "snippet": snippet[:500],
                    "date": pub_date,
                    "source": source or (item.findtext("source") or ""),
                })

        logger.info(
            "Google News RSS returned %d items for: %s",
            len(news_items), query,
        )
        return news_items

    except Exception as e:
        logger.warning("Google News RSS fallback failed: %s", e)
        return []


# ──────────────────────────────────────────────
# Formatting
# ──────────────────────────────────────────────

def _format_news_for_prompt(
    news_items: list[dict[str, Any]],
    is_company_news: bool = False,
) -> str:
    """Format news items into a text block for prompt injection.

    Args:
        news_items: List of news articles
        is_company_news: True for company news, False for industry

    Returns:
        Formatted news string
    """
    if not news_items:
        return ""

    header = "RELEVANT COMPANY NEWS:" if is_company_news else "RELEVANT INDUSTRY NEWS:"
    lines = [header]

    for i, item in enumerate(news_items, 1):
        lines.append(f"{i}. {item['title']}")
        lines.append(f"   Source: {item['source']}")
        lines.append(f"   Date: {item['date']}")
        lines.append(f"   Summary: {item['snippet']}")
        lines.append("")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# Main Entry Point
# ──────────────────────────────────────────────

async def get_prospect_news(
    prospect: dict[str, Any],
    sender: dict[str, Any],
    campaign_context: dict[str, Any],
) -> str | None:
    """Fetch and format news relevant to the prospect.

    Two-tier search:
    1. Company-specific news (past month, 3 results)
    2. Industry fallback (past week, 3 results)

    Args:
        prospect: Prospect profile data (name, title, company, headline)
        sender: Sender profile data (name, company)
        campaign_context: Campaign config (target_description, etc.)

    Returns:
        Formatted news string for prompt injection, or None if no news found
        or SERPER key not configured.
    """
    # No key gate here: _fetch_serper_news routes a keyless call to the backend
    # proxy, and from there to the free Google News RSS feed. Returning early
    # made that fallback unreachable for every self-hosted user without a key.

    # Need at least a company name for meaningful search
    company = prospect.get("company", "")
    if not company and not prospect.get("title"):
        logger.debug("No company or title for prospect — skipping news")
        return None

    try:
        # Tier 1: Company-specific news
        company_query = await _generate_search_query(prospect, "company")
        company_news = await _fetch_serper_news(
            company_query, num_results=3, time_range="pastMonth",
        )

        if company_news:
            logger.info("Found company-specific news for %s", company)
            return _format_news_for_prompt(company_news, is_company_news=True)

        # Tier 2: Industry fallback
        logger.info("No company news found, trying industry news")
        industry_query = await _generate_search_query(prospect, "industry", sender)
        industry_news = await _fetch_serper_news(
            industry_query, num_results=3, time_range="pastWeek",
        )

        if industry_news:
            logger.info("Found industry news for %s", company)
            return _format_news_for_prompt(industry_news, is_company_news=False)

        logger.info("No news found (company or industry) for %s", company)
        return None

    except Exception as e:
        logger.warning("News retrieval failed: %s", e)
        return None
