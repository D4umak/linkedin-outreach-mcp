"""Hiring surge signal collector — detects companies posting many related jobs.

Uses `search_jobs()` with ICP-derived role keywords to find active hiring.
Groups results by company and flags "surges" when a company has 3+ related
job postings — a strong budget/growth signal indicating buying intent.

Runs every 4 hours via the scheduler, against its own slice of the daily search
budget (SIGNAL_DAILY_HIRING_SEARCHES) and its own counter. It used to read the
keyword collector's counter and record nothing, so its job searches were
invisible to every cap while it was stopped by searches it never made (#76).

The share is smaller than a typical ICP's role list and the first run of the
day takes all of it, so the role list is walked with a persisted cursor —
without one the same head is searched every day and the tail never at all.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)

# Where the next run starts in the ICP role list. SIGNAL_DAILY_HIRING_SEARCHES
# is smaller than a typical ICP's role list, and the first run of the day takes
# the whole remaining share, so without a cursor the same head is searched
# every day and the tail is never searched at all. Mirrors the competitor
# collector's _TERM_CURSOR_KEY.
_ROLE_CURSOR_KEY = "hiring_search_role_cursor"


def _rotate(
    keywords: list[tuple[str, str]], cursor: int
) -> list[tuple[str, str]]:
    """Start the run at `cursor`, wrapping — a permutation, not a truncation."""
    if not keywords:
        return []
    start = cursor % len(keywords)
    return keywords[start:] + keywords[:start]


async def collect_hiring_signals() -> str:
    """Collect hiring surge signals from LinkedIn job searches.

    For each active campaign with an ICP:
    1. Extract role-related keywords from ICP job_titles + keywords
    2. Search LinkedIn jobs using those keywords
    3. Group results by company
    4. Flag companies with 3+ related postings as hiring surges
    5. Cross-reference against campaign contacts

    Returns summary string.
    """
    from ..constants import (
        SIGNAL_DAILY_HIRING_SEARCHES,
        SIGNAL_HIRING_SURGE,
        SIGNAL_SEARCH_TYPE_HIRING,
        SIGNAL_TTL_HIRING_SURGE,
        STATUS_ACTIVE,
    )
    from ..db.async_bridge import run_db
    from ..db.queries import get_setting, list_campaigns, save_setting
    from ..db.signal_queries import (
        get_daily_signal_search_count,
        record_signal_search,
        save_signal,
        signal_exists,
        upsert_signal_account,
    )
    from ..linkedin import UnipileError, get_account_id, get_linkedin_client
    from ..linkedin.search_traffic import search_scope

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected."

    # This collector's own slice of the day, and its own counter — the two have
    # to be the same key or the cap governs somebody else's searches (#76).
    daily_count = await run_db(get_daily_signal_search_count, SIGNAL_SEARCH_TYPE_HIRING)
    remaining = SIGNAL_DAILY_HIRING_SEARCHES - daily_count
    if remaining <= 0:
        return (
            f"Daily hiring search limit reached "
            f"({daily_count}/{SIGNAL_DAILY_HIRING_SEARCHES})."
        )

    # A surge is detected by grouping one run's results, so the first run of the
    # day takes the whole remaining share rather than a sixth of it.
    max_searches = remaining

    # Get active campaigns with ICPs
    campaigns = await run_db(list_campaigns, status=STATUS_ACTIVE)
    if not campaigns:
        return "No active campaigns."

    # Extract search keywords from campaign ICPs
    all_keywords = _extract_hiring_keywords(campaigns)
    if not all_keywords:
        return "No ICP keywords available for hiring search."

    # Where the next run starts in the role list. The daily share is smaller
    # than a typical ICP's role list, and without a cursor every run restarts
    # at role 0, so the tail is never searched on any day — the same defect
    # the competitor collector carried, and the reason distinct-role coverage
    # was capped at SIGNAL_DAILY_HIRING_SEARCHES no matter how many days ran.
    cursor = await run_db(get_setting, _ROLE_CURSOR_KEY, 0)
    try:
        cursor = max(0, int(cursor))
    except (TypeError, ValueError):
        cursor = 0
    search_keywords = _rotate(all_keywords, cursor)

    try:
        client = get_linkedin_client()
    except UnipileError as e:
        return f"LinkedIn client error: {e}"

    total_searched = 0
    total_new = 0
    attempted = 0
    unissued = 0
    errors = 0
    now = int(time.time())
    surge_threshold = 3  # 3+ related postings = hiring surge

    # Aggregate jobs by company across all keyword searches
    company_jobs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    company_campaigns: dict[str, str] = {}  # company_id → campaign_id

    try:
        for keyword, campaign_id in search_keywords:
            if total_searched >= max_searches:
                break

            attempted += 1
            try:
                # One unit for one request that reached LinkedIn — no sooner,
                # so a local outage cannot spend the day's allowance, and no
                # later, so a 429 storm is not free to retry at full speed.
                # The client counts what it puts on the wire because it is the
                # only layer that can: search_jobs swallows transport errors
                # and returns ([], None). The scope keeps that count to THIS
                # call — in backend mode one client is shared, and the
                # scheduler runs five jobs at once. See search_traffic.py.
                search_error: Exception | None = None
                jobs: list[dict[str, Any]] = []
                with search_scope(client) as scope:
                    try:
                        jobs, _ = await client.search_jobs(
                            account_id, keyword, limit=25
                        )
                    except Exception as e:
                        search_error = e
                if scope.request_was_issued(search_error):
                    total_searched += 1
                    await run_db(record_signal_search, SIGNAL_SEARCH_TYPE_HIRING, 1)
                else:
                    unissued += 1
                    logger.debug(
                        "Job search for '%s' never left this machine (%s) — "
                        "no budget spent",
                        keyword, search_error or "no request issued",
                    )
                if search_error is not None:
                    raise search_error

                for job in jobs:
                    company_id = job.get("company_id", "")
                    company_name = job.get("company_name", "")
                    if not company_id or not company_name:
                        continue

                    job_id = job.get("job_id") or job.get("id") or ""
                    if job_id:
                        seen = {j.get("job_id") or j.get("id") for j in company_jobs[company_id]}
                        if job_id in seen:
                            continue
                    company_jobs[company_id].append(job)
                    if campaign_id and company_id not in company_campaigns:
                        company_campaigns[company_id] = campaign_id

            except Exception as e:
                logger.warning(
                    "Job search failed for '%s': %s", keyword, e
                )
                errors += 1

        # Detect surges: companies with 3+ job postings
        for company_id, jobs in company_jobs.items():
            if len(jobs) < surge_threshold:
                continue

            company_name = jobs[0].get("company_name", "")

            # Dedup: skip if we already have a recent hiring surge for this company
            if await run_db(
                signal_exists,
                SIGNAL_HIRING_SURGE,
                linkedin_id=company_id,
                lookback_seconds=7 * 86400,  # 1 week cooldown
            ):
                continue

            # Build job summary
            job_titles = [j.get("title", "") for j in jobs[:10]]
            locations = list({j.get("location", "") for j in jobs if j.get("location")})
            campaign_id = company_campaigns.get(company_id)

            metadata = {
                "company_id": company_id,
                "company_name": company_name,
                "company_url": jobs[0].get("company_url", ""),
                "job_count": len(jobs),
                "job_titles": job_titles,
                "locations": locations[:5],
                "surge_threshold": surge_threshold,
                "search_keywords": [
                    kw for kw, _ in search_keywords[:5]
                ],
            }

            # Build descriptive content
            content = (
                f"{company_name} is hiring for {len(jobs)} roles: "
                f"{', '.join(job_titles[:5])}"
            )
            if len(job_titles) > 5:
                content += f" and {len(job_titles) - 5} more"

            await run_db(
                save_signal,
                signal_type=SIGNAL_HIRING_SURGE,
                source="job_search",
                prospect_name=company_name,
                linkedin_id=company_id,
                campaign_id=campaign_id,
                content=content[:1000],
                metadata_json=json.dumps(metadata),
                expires_at=now + SIGNAL_TTL_HIRING_SURGE,
            )
            total_new += 1

            # Update signal account for company
            await run_db(
                upsert_signal_account,
                linkedin_id=company_id,
                prospect_name=company_name,
                company=company_name,
            )

    finally:
        await client.close()

    # Resume at the first role this run did not get to, so a share smaller
    # than the role list walks the whole list over a few days instead of
    # re-searching its head. Advanced by roles ATTEMPTED, not by searches
    # booked, so it never names a resume point ahead of roles already walked.
    next_cursor: int | None = None
    if attempted:
        candidate = (cursor + attempted) % len(all_keywords)
        try:
            await run_db(save_setting, _ROLE_CURSOR_KEY, candidate)
            next_cursor = candidate
        except Exception as e:  # a lost cursor costs fairness, not correctness
            logger.warning("Could not persist the hiring role cursor: %s", e)

    # "Searched" counts requests that reached LinkedIn, so a run whose calls
    # all died locally reports 0 rather than the length of the list it walked.
    summary = (
        f"Searched {total_searched} job keywords, "
        f"found {total_new} hiring surges "
        f"({len(company_jobs)} companies analyzed)"
    )
    if errors:
        summary += f", {errors} errors"
    if unissued:
        summary += (
            f", {unissued} searches never left this machine"
            f" (no request reached LinkedIn, so they cost no budget)"
        )

    # Not "budget exhausted": the count includes roles the run walked past
    # without a request leaving, which cost no budget at all. The used/total
    # figure beside it says whether the budget was in fact the binding limit.
    unsearched = len(all_keywords) - total_searched
    if unsearched > 0:
        summary += (
            f". {unsearched} job keywords unsearched"
            f" ({daily_count + total_searched}/{SIGNAL_DAILY_HIRING_SEARCHES}"
            f" hiring searches used today)"
        )
        # Only claimed when the cursor was actually written — a run that could
        # not persist it will re-search this same head.
        if next_cursor is not None:
            summary += (
                f"; the next run resumes at role {next_cursor + 1}"
                f" of {len(all_keywords)}"
            )
    return summary


def _extract_hiring_keywords(
    campaigns: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Extract job-search keywords from campaign ICPs.

    Pulls from ICP job_titles and keywords fields.
    Returns list of (keyword, campaign_id) tuples.
    """
    import json as _json

    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    for campaign in campaigns:
        campaign_id = campaign.get("id", "")
        icp_json = campaign.get("icp_json", "")
        if not icp_json:
            continue

        try:
            icp_data = _json.loads(icp_json)
        except (ValueError, TypeError):
            continue

        # Handle both direct ICP and IcpResult wrapper
        icps = icp_data.get("icps", [icp_data])

        for icp in icps:
            if not isinstance(icp, dict):
                continue

            # Extract from job_titles — handle dict, list, and str formats
            job_titles = icp.get("job_titles", {})
            titles_list: list[str] = []
            if isinstance(job_titles, dict):
                titles_list = job_titles.get("include", [])
            elif isinstance(job_titles, list):
                titles_list = job_titles
            elif isinstance(job_titles, str) and job_titles.strip():
                titles_list = [job_titles]

            for title in titles_list:
                if not isinstance(title, str):
                    continue
                title_clean = title.strip().lower()
                if title_clean and title_clean not in seen:
                    seen.add(title_clean)
                    results.append((title, campaign_id))

            # Extract from keywords — handle dict, list, and str formats
            keywords = icp.get("keywords", [])
            keywords_list: list[str] = []
            if isinstance(keywords, dict):
                keywords_list = keywords.get("include", [])
            elif isinstance(keywords, list):
                keywords_list = keywords
            elif isinstance(keywords, str) and keywords.strip():
                keywords_list = [keywords]

            for kw in keywords_list[:5]:
                if not isinstance(kw, str):
                    continue
                kw_clean = kw.strip().lower()
                if kw_clean and kw_clean not in seen:
                    seen.add(kw_clean)
                    results.append((kw, campaign_id))

    return results
