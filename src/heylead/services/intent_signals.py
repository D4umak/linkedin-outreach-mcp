"""Intent signal detection — job postings reveal buying intent.

Companies hiring for specific roles signal demand for related tools/services.
E.g., a company hiring "data engineers" likely needs data infrastructure tools.

This service:
1. Searches LinkedIn job postings matching relevant keywords
2. Extracts unique hiring companies with context
3. Returns intent-enriched company data for campaign targeting + message personalization
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def search_job_intent(
    client: Any,
    account_id: str,
    keywords: str,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Search for companies with buying intent based on job postings.

    Args:
        client: UnipileClient or BackendClient instance.
        account_id: The Unipile account ID.
        keywords: Job-related keywords (e.g., "data engineer", "DevOps").
        limit: Max jobs to search.

    Returns:
        List of intent-enriched company dicts with job context, deduplicated
        by company name. Each entry contains:
        - company_name, company_id, company_url, location
        - jobs: list of job titles + descriptions at that company
        - job_count: number of matching open positions
        - intent_signal: human-readable intent description
    """
    try:
        jobs, _ = await client.search_jobs(account_id, keywords, limit=limit)
    except Exception as e:
        logger.warning("Job search failed: %s", e)
        return []

    if not jobs:
        return []

    # Deduplicate by company name and aggregate jobs
    companies: dict[str, dict[str, Any]] = {}
    for job in jobs:
        name = job.get("company_name") or ""
        if not name:
            continue
        key = name.lower().strip()
        if key not in companies:
            companies[key] = {
                "company_name": name,
                "company_id": job.get("company_id", ""),
                "company_url": job.get("company_url", ""),
                "location": job.get("location", ""),
                "jobs": [],
                "job_count": 0,
            }
        companies[key]["jobs"].append({
            "title": job.get("title", ""),
            "description": job.get("description", "")[:200],
            "posted_at": job.get("posted_at", ""),
        })
        companies[key]["job_count"] += 1
        # Keep the first non-empty company_id/url
        if not companies[key]["company_id"] and job.get("company_id"):
            companies[key]["company_id"] = job["company_id"]
        if not companies[key]["company_url"] and job.get("company_url"):
            companies[key]["company_url"] = job["company_url"]

    # Build intent signals
    result: list[dict[str, Any]] = []
    for entry in companies.values():
        job_titles = [j["title"] for j in entry["jobs"] if j["title"]]
        titles_str = ", ".join(job_titles[:3])
        if len(job_titles) > 3:
            titles_str += f" (+{len(job_titles) - 3} more)"

        entry["intent_signal"] = (
            f"Hiring {entry['job_count']} role(s) matching '{keywords}': {titles_str}"
        )
        result.append(entry)

    # Sort by job_count descending (more open positions = stronger intent)
    result.sort(key=lambda x: x["job_count"], reverse=True)
    return result


def format_intent_companies(companies: list[dict[str, Any]], max_show: int = 10) -> str:
    """Format intent-enriched companies for display."""
    if not companies:
        return "No companies with matching job postings found."

    lines = [f"Found {len(companies)} companies with hiring intent:\n"]
    for i, c in enumerate(companies[:max_show]):
        lines.append(
            f"   {i + 1}. {c['company_name']} — {c['job_count']} open role(s)"
        )
        if c.get("location"):
            lines[-1] += f" ({c['location']})"
        # Show first 2 job titles
        for j in c["jobs"][:2]:
            lines.append(f"      • {j['title']}")

    if len(companies) > max_show:
        lines.append(f"\n   ... and {len(companies) - max_show} more companies")

    return "\n".join(lines)


def build_intent_context(companies: list[dict[str, Any]]) -> str:
    """Build an intent context string for message generation prompts.

    Returns a brief description of hiring intent that can be injected into
    the message generation prompt for better personalization.
    """
    if not companies:
        return ""

    parts: list[str] = []
    for c in companies[:5]:
        job_titles = [j["title"] for j in c["jobs"][:2]]
        parts.append(
            f"{c['company_name']} is hiring: {', '.join(job_titles)}"
        )

    return "HIRING INTENT SIGNALS:\n" + "\n".join(f"- {p}" for p in parts)
