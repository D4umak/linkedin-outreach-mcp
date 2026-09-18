"""Collector: company_follower_collector — Track new followers on company LinkedIn pages.

Polls GET /api/v1/users/followers to detect new followers over time.
Saves SIGNAL_COMPANY_FOLLOWER for each new follower.

Runs every 4 hours via the scheduler.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from ..constants import (
    SIGNAL_COMPANY_FOLLOWER,
    SIGNAL_TTL_COMPANY_FOLLOWER,
)
from ..linkedin.circuit_breaker import CircuitBreakerOpen, CollectorCircuitBreaker

logger = logging.getLogger(__name__)

_cb = CollectorCircuitBreaker("company_followers")

# Max concurrent API calls to avoid hammering Unipile
_MAX_CONCURRENT = 3


async def _fetch_followers_for_watchlist(
    client: object,
    account_id: str,
    wl: dict,
) -> tuple[str, str, str | None, list[dict]]:
    """Fetch followers for a single watchlist. Returns (wl_id, company_name, campaign_id, followers)."""
    wl_id = wl["id"]
    company_name = wl.get("name", "Unknown Company")
    campaign_id = wl.get("campaign_id")
    keywords_list = wl.get("keywords_list", [])
    company_id = keywords_list[0] if keywords_list else ""

    if company_id.isdigit():
        followers = await _cb.call(
            client.get_followers(account_id, company_id=company_id),
            label=f"get_followers({company_name})",
        )
    else:
        # Without a numeric company id there is no company-follower list to
        # fetch. Do not fall back to the personal follower list — those are
        # the wrong people.
        logger.info(
            "Skipping follower collection for '%s': company id %r is not numeric",
            company_name, company_id,
        )
        followers = []
    return wl_id, company_name, campaign_id, followers or []


async def collect_company_followers() -> str:
    """Collect new follower signals from company LinkedIn pages.

    For each company watchlist entry:
    1. Call get_followers() for the company account (concurrently, up to 3 at a time)
    2. Diff against previously seen followers (stored in watchlist keywords)
    3. Save new followers as SIGNAL_COMPANY_FOLLOWER signals

    Note: This requires the company page admin to have connected their
    LinkedIn account via Unipile. The followers endpoint may not be
    available for all company pages — gracefully skips on failure.

    Returns summary string.
    """
    from ..db.async_bridge import run_db
    from ..db.signal_queries import (
        batch_signal_exists,
        list_watchlists,
        save_signal,
        update_watchlist,
        upsert_signal_account,
    )
    from ..linkedin import get_account_id, get_linkedin_client

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected — skipping follower collection."

    client = get_linkedin_client()
    try:
        watchlists = await run_db(list_watchlists, is_active=True)
        company_watchlists = [w for w in watchlists if w.get("watch_type") == "company"]

        if not company_watchlists:
            return "No company watchlists configured."

        total_new = 0
        errors = 0
        now = int(time.time())

        # ── Fetch followers concurrently (bounded) ──
        sem = asyncio.Semaphore(_MAX_CONCURRENT)

        async def _guarded_fetch(wl: dict) -> tuple[str, str, str | None, list[dict]] | Exception:
            async with sem:
                try:
                    return await _fetch_followers_for_watchlist(client, account_id, wl)
                except CircuitBreakerOpen as e:
                    return e
                except Exception as e:
                    return e

        results = await asyncio.gather(
            *[_guarded_fetch(wl) for wl in company_watchlists],
        )

        circuit_broken = False
        fetched: list[tuple[str, str, str | None, list[dict]]] = []
        for i, result in enumerate(results):
            if isinstance(result, CircuitBreakerOpen):
                logger.warning("Skipping follower collection: %s", result)
                circuit_broken = True
                break
            if isinstance(result, Exception):
                wl_name = company_watchlists[i].get("name", "Unknown")
                logger.warning(
                    "Follower collection failed for '%s': %s "
                    "(endpoint may not be available for company pages)",
                    wl_name, result,
                )
                errors += 1
                continue
            fetched.append(result)

        # ── Process fetched followers ──
        for wl_id, company_name, campaign_id, followers in fetched:
            if not followers:
                await run_db(update_watchlist, wl_id, last_polled_at=now)
                continue

            # Batch dedup
            follower_dedup_keys = []
            for follower in followers:
                fid = follower.get("provider_id") or follower.get("id", "")
                if fid:
                    follower_dedup_keys.append(f"cf:{wl_id}:{fid}")
            existing_signals = await run_db(
                batch_signal_exists,
                SIGNAL_COMPANY_FOLLOWER, post_ids=follower_dedup_keys,
            )

            for follower in followers:
                follower_id = follower.get("provider_id") or follower.get("id", "")
                follower_name = follower.get("name", "") or follower.get("full_name", "")
                if not follower_id:
                    continue

                dedup_key = f"cf:{wl_id}:{follower_id}"
                if dedup_key in existing_signals:
                    continue

                metadata = {
                    "company_name": company_name,
                    "follower_name": follower_name,
                    "follower_id": follower_id,
                    "follower_headline": follower.get("headline", ""),
                    "follower_url": follower.get("profile_url", ""),
                }

                await run_db(
                    save_signal,
                    signal_type=SIGNAL_COMPANY_FOLLOWER,
                    source="company_page",
                    prospect_name=follower_name,
                    linkedin_id=follower_id,
                    campaign_id=campaign_id,
                    content=f"New follower of {company_name}: {follower_name}",
                    post_id=dedup_key,
                    metadata_json=json.dumps(metadata),
                    expires_at=now + SIGNAL_TTL_COMPANY_FOLLOWER,
                    confidence=0.85,
                )
                await run_db(
                    upsert_signal_account,
                    linkedin_id=follower_id,
                    prospect_name=follower_name,
                )
                total_new += 1

            await run_db(update_watchlist, wl_id, last_polled_at=now)

        summary = f"Checked {len(company_watchlists)} company pages, found {total_new} new followers"
        if errors:
            summary += f", {errors} errors"
        return summary
    finally:
        await client.close()
