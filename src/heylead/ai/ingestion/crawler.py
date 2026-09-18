"""Website crawler for ICP data ingestion.

Primary: Firecrawl API (high quality, handles JS-rendered content).
Fallback: Simple httpx fetch + basic HTML-to-text extraction.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

import httpx

from ...constants import FIRECRAWL_API_URL

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0, read=60.0)


async def crawl_website(
    url: str,
    max_pages: int = 10,
    firecrawl_api_key: str = "",
) -> list[dict[str, Any]]:
    """Crawl a website and return page data.

    Each item: {"url": str, "title": str, "markdown": str, "content_hash": str}

    Args:
        url: Website URL to crawl.
        max_pages: Maximum pages to crawl.
        firecrawl_api_key: Firecrawl API key. If empty, falls back to simple fetch.

    Returns:
        List of page dicts with markdown content.
    """
    if firecrawl_api_key:
        return await _crawl_firecrawl(url, max_pages, firecrawl_api_key)
    return await _crawl_simple(url)


# ──────────────────────────────────────────────
# Firecrawl
# ──────────────────────────────────────────────

async def _crawl_firecrawl(
    url: str, max_pages: int, api_key: str,
) -> list[dict[str, Any]]:
    """Crawl using Firecrawl API."""
    scrape_url = f"{FIRECRAWL_API_URL}/scrape"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # For single page, use /scrape endpoint (faster, no polling)
    if max_pages <= 1:
        return await _firecrawl_scrape(scrape_url, url, headers)

    # For multi-page, use /crawl endpoint with polling
    crawl_url = f"{FIRECRAWL_API_URL}/crawl"
    body = {
        "url": url,
        "limit": max_pages,
        "scrapeOptions": {"formats": ["markdown"]},
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        # Start crawl job
        resp = await client.post(crawl_url, json=body, headers=headers)
        if resp.status_code != 200:
            logger.warning(
                f"Firecrawl crawl failed ({resp.status_code}), falling back to scrape"
            )
            return await _firecrawl_scrape(scrape_url, url, headers)

        data = resp.json()
        job_id = data.get("id")
        if not job_id:
            logger.warning("Firecrawl returned no job ID, falling back to scrape")
            return await _firecrawl_scrape(scrape_url, url, headers)

        # Poll for results
        check_url = f"{FIRECRAWL_API_URL}/crawl/{job_id}"
        import asyncio
        pages = []
        for _ in range(60):  # Max 5 minutes polling
            await asyncio.sleep(5)
            check_resp = await client.get(check_url, headers=headers)
            if check_resp.status_code != 200:
                continue
            check_data = check_resp.json()
            status = check_data.get("status")

            if status == "completed":
                for item in check_data.get("data", []):
                    md = item.get("markdown", "")
                    if md.strip():
                        pages.append({
                            "url": item.get("metadata", {}).get("sourceURL", url),
                            "title": item.get("metadata", {}).get("title", ""),
                            "markdown": md,
                            "content_hash": hashlib.md5(md.encode()).hexdigest(),
                        })
                return pages
            elif status in ("failed", "cancelled"):
                logger.warning(f"Firecrawl job {status}, falling back to scrape")
                return await _firecrawl_scrape(scrape_url, url, headers)

        logger.warning("Firecrawl polling timed out, falling back to scrape")
        return await _firecrawl_scrape(scrape_url, url, headers)


async def _firecrawl_scrape(
    scrape_url: str, url: str, headers: dict,
) -> list[dict[str, Any]]:
    """Scrape a single page via Firecrawl /scrape endpoint."""
    body = {"url": url, "formats": ["markdown"]}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(scrape_url, json=body, headers=headers)
        if resp.status_code != 200:
            logger.error(f"Firecrawl scrape failed: {resp.status_code}")
            return await _crawl_simple(url)

        data = resp.json().get("data", {})
        md = data.get("markdown", "")
        if not md.strip():
            return await _crawl_simple(url)

        return [{
            "url": data.get("metadata", {}).get("sourceURL", url),
            "title": data.get("metadata", {}).get("title", ""),
            "markdown": md,
            "content_hash": hashlib.md5(md.encode()).hexdigest(),
        }]


# ──────────────────────────────────────────────
# Simple fallback
# ──────────────────────────────────────────────

async def _crawl_simple(url: str) -> list[dict[str, Any]]:
    """Simple single-page fetch + HTML-to-text extraction."""
    async with httpx.AsyncClient(
        timeout=_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "HeyLead/1.0"},
    ) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Simple crawl failed for {url}: {e}")
            return []

    html = resp.text
    text = _html_to_text(html)
    title = _extract_title(html)

    if not text.strip():
        return []

    return [{
        "url": str(resp.url),
        "title": title,
        "markdown": text,
        "content_hash": hashlib.md5(text.encode()).hexdigest(),
    }]


def _html_to_text(html: str) -> str:
    """Basic HTML to text conversion (no external deps)."""
    # Remove script and style blocks
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Convert common block elements to newlines
    text = re.sub(r"<(?:br|p|div|h[1-6]|li|tr)[^>]*>", "\n", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode common entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&nbsp;", " ").replace("&quot;", '"')
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n", "\n\n", text)
    return text.strip()


def _extract_title(html: str) -> str:
    """Extract <title> from HTML."""
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else ""
