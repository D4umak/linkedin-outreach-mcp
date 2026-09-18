"""News & funding event signal collector — monitors company news for buying triggers.

Uses the existing SERPER API integration (via `_fetch_serper_news`) to search
for funding events, M&A, product launches, and leadership changes at target
companies. Rule-based classification detects signal sub-types without LLM calls.

Runs every 4 hours via the scheduler.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


async def collect_news_signals() -> str:
    """Collect news/funding signals for companies in active campaigns.

    For each active campaign:
    1. Extract target company names from contacts + ICP
    2. Search news via SERPER API for each company
    3. Classify news as funding_event or news_event
    4. Dedup against existing signals
    5. Save new signals with metadata

    Returns summary string.
    """
    from ..constants import (
        SIGNAL_FUNDING_EVENT,
        SIGNAL_NEWS_EVENT,
        SIGNAL_NEWS_ACQUISITION,
        SIGNAL_NEWS_EXEC_HIRE,
        SIGNAL_NEWS_EXPANSION,
        SIGNAL_NEWS_FUNDING,
        SIGNAL_NEWS_LAYOFFS,
        SIGNAL_NEWS_PRODUCT_LAUNCH,
        SIGNAL_TTL_FUNDING_EVENT,
        SIGNAL_TTL_NEWS_ACQUISITION,
        SIGNAL_TTL_NEWS_EVENT,
        SIGNAL_TTL_NEWS_EXEC_HIRE,
        SIGNAL_TTL_NEWS_EXPANSION,
        SIGNAL_TTL_NEWS_FUNDING,
        SIGNAL_TTL_NEWS_LAYOFFS,
        SIGNAL_TTL_NEWS_PRODUCT_LAUNCH,
        STATUS_ACTIVE,
    )
    from ..db.async_bridge import run_db
    from ..db.queries import list_campaigns
    from ..db.signal_queries import (
        batch_signal_exists,
        save_signal,
        upsert_signal_account,
    )

    # Get active campaigns
    campaigns = await run_db(list_campaigns, status=STATUS_ACTIVE)
    if not campaigns:
        return "No active campaigns."

    # Extract company names to search
    companies = await run_db(_extract_target_companies, campaigns)
    if not companies:
        return "No target companies found in campaigns."

    # Cap at 30 news searches per run
    max_searches = 30
    total_searched = 0
    total_new = 0
    errors = 0
    now = int(time.time())

    for company_name, campaign_id in companies:
        if total_searched >= max_searches:
            break

        try:
            news_items = await _search_company_news(company_name)
            total_searched += 1

            if not news_items:
                continue

            # Pre-classify all items and batch dedup per signal type
            classified_items: list[tuple[dict, str, str, int]] = []  # (item, news_type, signal_type, ttl)
            dedup_keys_by_type: dict[str, list[str]] = {}
            dedup_key_map: dict[str, str] = {}  # dedup_key -> signal_type
            for item in news_items:
                title = item.get("title", "")
                if not title:
                    continue
                snippet = item.get("snippet", "")
                news_type = _classify_news(title, snippet)
                signal_type, ttl = _news_type_to_signal(
                    news_type,
                    SIGNAL_NEWS_FUNDING, SIGNAL_TTL_NEWS_FUNDING,
                    SIGNAL_NEWS_ACQUISITION, SIGNAL_TTL_NEWS_ACQUISITION,
                    SIGNAL_NEWS_EXEC_HIRE, SIGNAL_TTL_NEWS_EXEC_HIRE,
                    SIGNAL_NEWS_EXPANSION, SIGNAL_TTL_NEWS_EXPANSION,
                    SIGNAL_NEWS_PRODUCT_LAUNCH, SIGNAL_TTL_NEWS_PRODUCT_LAUNCH,
                    SIGNAL_NEWS_LAYOFFS, SIGNAL_TTL_NEWS_LAYOFFS,
                    SIGNAL_NEWS_EVENT, SIGNAL_TTL_NEWS_EVENT,
                )
                dedup_key = _news_dedup_key(title, company_name)
                classified_items.append((item, news_type, signal_type, ttl))
                dedup_keys_by_type.setdefault(signal_type, []).append(dedup_key)
                dedup_key_map[dedup_key] = signal_type

            # Batch check existence per signal type
            existing_dedup_keys: set[str] = set()
            for sig_type, keys in dedup_keys_by_type.items():
                existing_dedup_keys.update(await run_db(batch_signal_exists, sig_type, post_ids=keys))

            for item, news_type, signal_type, ttl in classified_items:
                title = item.get("title", "")
                snippet = item.get("snippet", "")
                link = item.get("link", "")
                dedup_key = _news_dedup_key(title, company_name)
                if dedup_key in existing_dedup_keys:
                    continue

                metadata = {
                    "company_name": company_name,
                    "news_type": news_type,
                    "title": title,
                    "source": item.get("source", ""),
                    "date": item.get("date", ""),
                    "link": link,
                }

                content = f"{title}\n{snippet}"[:1000]

                await run_db(
                    save_signal,
                    signal_type=signal_type,
                    source="news_search",
                    prospect_name=company_name,
                    campaign_id=campaign_id,
                    content=content,
                    post_id=dedup_key,
                    metadata_json=json.dumps(metadata),
                    expires_at=now + ttl,
                )
                total_new += 1

                # Aggregate under company in signal accounts
                await run_db(
                    upsert_signal_account,
                    linkedin_id=f"company:{company_name.lower().replace(' ', '_')}",
                    prospect_name=company_name,
                    company=company_name,
                )

        except Exception as e:
            logger.warning(
                "News search failed for '%s': %s", company_name, e
            )
            errors += 1

    summary = (
        f"Searched news for {total_searched} companies, "
        f"found {total_new} new signals"
    )
    if errors:
        summary += f", {errors} errors"
    return summary


async def _search_company_news(
    company_name: str,
) -> list[dict[str, Any]]:
    """Search for recent news about a company.

    Uses the existing SERPER integration (local key or backend proxy).
    Returns list of news items.
    """
    from ..ai.news_service import _fetch_serper_news

    # Search for company news in the past week
    return await _fetch_serper_news(
        query=f'"{company_name}" news',
        num_results=5,
        time_range="pastWeek",
    )


def _news_type_to_signal(
    news_type: str,
    sig_funding: str, ttl_funding: int,
    sig_acquisition: str, ttl_acquisition: int,
    sig_exec_hire: str, ttl_exec_hire: int,
    sig_expansion: str, ttl_expansion: int,
    sig_product: str, ttl_product: int,
    sig_layoffs: str, ttl_layoffs: int,
    sig_default: str, ttl_default: int,
) -> tuple[str, int]:
    """Map news classification to granular signal type and TTL."""
    mapping = {
        "funding": (sig_funding, ttl_funding),
        "acquisition": (sig_acquisition, ttl_acquisition),
        "leadership": (sig_exec_hire, ttl_exec_hire),
        "expansion": (sig_expansion, ttl_expansion),
        "product": (sig_product, ttl_product),
        "layoffs": (sig_layoffs, ttl_layoffs),
    }
    return mapping.get(news_type, (sig_default, ttl_default))


def _classify_news(title: str, snippet: str) -> str:
    """Classify a news article as a signal sub-type.

    Uses rule-based keyword matching (no LLM needed for classification).

    Returns:
        'funding' — Series A/B/C, raised capital, IPO
        'acquisition' — M&A, acquired, merger
        'leadership' — New CEO/CTO/VP, leadership change
        'layoffs' — Layoffs, downsizing, restructuring
        'expansion' — New office, market entry, global expansion
        'product' — Product launch, new feature, major update
        'general' — Other news
    """
    text = f"{title} {snippet}".lower()

    # Funding events (highest value)
    funding_patterns = [
        "series a", "series b", "series c", "series d", "series e",
        "seed round", "pre-seed",
        "raised $", "raises $", "funding round",
        "million in funding", "billion in funding",
        "venture capital", "investment round",
        "ipo", "initial public offering", "goes public", "went public",
        "valuation of $", "valued at $",
    ]
    if any(p in text for p in funding_patterns):
        return "funding"

    # Acquisitions
    acquisition_patterns = [
        "acquired", "acquires", "acquisition",
        "merger", "merges with", "merged with",
        "buys ", "bought ", "purchased",
        "takeover", "taken over",
    ]
    if any(p in text for p in acquisition_patterns):
        return "acquisition"

    # Layoffs / downsizing (check before leadership — "restructuring" is layoff context)
    from ..constants import NEWS_LAYOFF_PATTERNS

    if any(p in text for p in NEWS_LAYOFF_PATTERNS):
        return "layoffs"

    # Leadership changes
    leadership_patterns = [
        "new ceo", "new cto", "new cfo", "new coo", "new cro",
        "appoints", "appointed", "names", "named",
        "hires", "hired as",
        "joins as", "joined as",
        "steps down", "resigned", "departure",
        "leadership change", "executive team",
    ]
    if any(p in text for p in leadership_patterns):
        return "leadership"

    # Expansion
    expansion_patterns = [
        "new office", "opens office", "headquarter",
        "expands to", "expanding to", "expansion",
        "enters market", "entering market",
        "launches in", "launched in",
        "global", "international",
        "new employees", "hiring spree", "growing team",
    ]
    if any(p in text for p in expansion_patterns):
        return "expansion"

    # Product launches
    product_patterns = [
        "launches", "launched", "launch of",
        "announces", "announced", "introducing",
        "new product", "new feature", "new platform",
        "release", "released", "version",
        "partnership with", "partners with", "partnered",
        "integration with", "integrates with",
    ]
    if any(p in text for p in product_patterns):
        return "product"

    return "general"


def _news_dedup_key(title: str, company_name: str) -> str:
    """Generate a dedup key for a news article.

    Combines normalized title + company to catch duplicate articles
    from different sources.
    """
    import hashlib

    normalized = f"{company_name.lower().strip()}:{title.lower().strip()}"
    return hashlib.md5(normalized.encode()).hexdigest()[:16]


def _extract_target_companies(
    campaigns: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Extract company names to search from campaigns.

    Sources:
    1. Contact companies (from contacts in active campaigns)
    2. ICP-defined company characteristics (if available)

    Returns list of (company_name, campaign_id) tuples, deduplicated.
    """
    import json as _json

    from ..db.queries import get_contacts_for_campaign

    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    # Source 0: Company watchlists (most reliable — auto-created during setup/ABM)
    try:
        from ..db.signal_queries import list_watchlists

        company_watchlists = list_watchlists(is_active=True)
        for wl in company_watchlists:
            if wl.get("watch_type") != "company":
                continue
            company_name = wl.get("name", "")
            # Strip common suffixes from auto-created watchlists
            for suffix in (" (Your Company)", " — Company Page"):
                company_name = company_name.replace(suffix, "")
            company_name = company_name.strip()
            if not company_name or len(company_name) <= 2:
                continue
            company_lower = company_name.lower()
            if company_lower not in seen:
                seen.add(company_lower)
                # Use watchlist campaign_id if linked, else first active campaign
                wl_campaign_id = wl.get("campaign_id") or (
                    campaigns[0].get("id", "") if campaigns else ""
                )
                results.append((company_name, wl_campaign_id))
    except Exception as e:
        logger.debug("Failed to load company watchlists for news: %s", e)

    for campaign in campaigns:
        campaign_id = campaign.get("id", "")

        # Source 1: Companies from contacts
        try:
            contacts = get_contacts_for_campaign(campaign_id)
            for contact in contacts[:20]:  # Cap per campaign
                company = _extract_company_from_contact(contact)
                if company:
                    company_lower = company.lower().strip()
                    if company_lower not in seen and len(company_lower) > 2:
                        seen.add(company_lower)
                        results.append((company, campaign_id))
        except Exception as e:
            logger.debug("Failed to get contacts for campaign %s: %s", campaign_id, e)

        # Source 2: ICP company names/industries (for broader coverage)
        icp_json = campaign.get("icp_json", "")
        if icp_json:
            try:
                icp_data = _json.loads(icp_json)
                icps = icp_data.get("icps", [icp_data])
                for icp in icps:
                    if not isinstance(icp, dict):
                        continue
                    # Extract industry keywords for targeted searches
                    industries = icp.get("industries", {})
                    if isinstance(industries, dict):
                        for ind in industries.get("include", [])[:3]:
                            ind_lower = ind.lower().strip()
                            if ind_lower not in seen:
                                seen.add(ind_lower)
                                # Use industry as a broader news search
                                results.append((ind, campaign_id))
            except (ValueError, TypeError):
                pass

    return results


def _extract_company_from_contact(contact: dict[str, Any]) -> str | None:
    """Extract company name from a contact record.

    Checks analysis_json first, then profile_json, then headline parsing.
    """
    import json as _json

    # Try analysis_json
    analysis_json = contact.get("analysis_json", "")
    if analysis_json:
        try:
            analysis = _json.loads(analysis_json)
            company = analysis.get("company", "")
            if company:
                return company
        except (ValueError, TypeError):
            pass

    # Try profile_json
    profile_json = contact.get("profile_json", "")
    if profile_json:
        try:
            profile = _json.loads(profile_json)
            # Check experience entries
            experiences = profile.get("experiences", [])
            if experiences and isinstance(experiences, list):
                current = experiences[0]
                if isinstance(current, dict):
                    company = current.get("company_name", "") or current.get("company", "")
                    if company:
                        return company
        except (ValueError, TypeError):
            pass

    # Try headline parsing (e.g., "VP Sales at Acme Corp")
    name = contact.get("name", "")
    headline = contact.get("headline", "")
    if not headline:
        return None

    for sep in [" at ", " @ ", " | "]:
        if sep in headline:
            parts = headline.split(sep, 1)
            if len(parts) == 2:
                return parts[1].strip()

    return None
