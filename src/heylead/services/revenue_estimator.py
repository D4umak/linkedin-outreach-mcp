"""Heuristic revenue estimation for contacts based on company data.

Estimates annual contract value (ACV) from:
- Company headcount (from profile_json, analysis_json, or campaign ICP)
- Industry (from profile/analysis or ICP)
- Prospect seniority (from title keyword matching)

No LLM calls — pure lookup-table heuristic for speed and consistency.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..constants import (
    HEADCOUNT_BASE_ACV,
    INDUSTRY_MULTIPLIERS,
    SENIORITY_KEYWORDS,
    SENIORITY_MULTIPLIERS,
)
from ..db.async_bridge import run_db
from ..db.strategy_queries import get_contacts_without_revenue, update_contact_revenue
from ..textutil import contains_term

logger = logging.getLogger(__name__)


def estimate_revenue(
    contact: dict[str, Any],
    campaign_icp: dict[str, Any] | None = None,
) -> float:
    """Estimate deal value for a contact.

    Args:
        contact: Contact dict with title, company, profile_json, analysis_json.
        campaign_icp: Optional ICP JSON from the campaign (fallback data source).

    Returns:
        Estimated annual contract value in USD.
    """
    headcount = extract_headcount(contact, campaign_icp)
    industry = extract_industry(contact, campaign_icp)
    seniority = infer_seniority(contact.get("title", ""))

    base_acv = _headcount_to_acv(headcount)
    industry_mult = _industry_multiplier(industry)
    seniority_mult = SENIORITY_MULTIPLIERS.get(seniority, 0.7)

    return round(base_acv * industry_mult * seniority_mult, 2)


async def backfill_revenue_estimates() -> int:
    """Estimate revenue for all contacts that don't have one yet.

    Returns:
        Number of contacts updated.
    """
    def _do_backfill():
        contacts = get_contacts_without_revenue(limit=500)
        if not contacts:
            return 0

        updated = 0
        for c in contacts:
            icp_json = c.get("icp_json")
            campaign_icp = None
            if icp_json:
                try:
                    campaign_icp = json.loads(icp_json)
                except (json.JSONDecodeError, TypeError):
                    pass

            rev = estimate_revenue(c, campaign_icp)
            if rev > 0:
                update_contact_revenue(c["id"], rev)
                updated += 1

        if updated:
            logger.info("Revenue estimator: backfilled %d contacts", updated)
        return updated

    return await run_db(_do_backfill)


# ──────────────────────────────────────────────
# Private Helpers
# ──────────────────────────────────────────────

def extract_headcount(
    contact: dict[str, Any],
    campaign_icp: dict[str, Any] | None,
) -> int | None:
    """Extract company headcount from available data sources."""
    # 1. Try analysis_json
    analysis = parse_json_field(contact.get("analysis_json"))
    if analysis:
        for key in ("company_size", "headcount", "employees", "company_headcount"):
            val = analysis.get(key)
            if val:
                parsed = _parse_headcount_value(val)
                if parsed:
                    return parsed

    # 2. Try profile_json
    profile = parse_json_field(contact.get("profile_json"))
    if profile:
        # LinkedIn profile may have company size in various fields
        for key in ("company_size", "headcount", "employees",
                     "company_headcount", "companySize"):
            val = profile.get(key)
            if val:
                parsed = _parse_headcount_value(val)
                if parsed:
                    return parsed
        # Try headline for patterns like "200+ employees"
        headline = profile.get("headline", "") or ""
        parsed = _parse_headcount_from_text(headline)
        if parsed:
            return parsed

    # 3. Fallback to campaign ICP headcount range midpoint
    if campaign_icp:
        icps = campaign_icp.get("icps", [])
        if isinstance(icps, list) and icps:
            first_icp = icps[0] if isinstance(icps[0], dict) else {}
            hc = first_icp.get("company_headcount", {})
            if isinstance(hc, dict):
                min_hc = hc.get("min", 0) or 0
                max_hc = hc.get("max", 0) or 0
                if min_hc or max_hc:
                    return (min_hc + (max_hc or min_hc * 3)) // 2

    return None


def extract_industry(
    contact: dict[str, Any],
    campaign_icp: dict[str, Any] | None,
) -> str:
    """Extract industry from available data sources."""
    # 1. Try analysis_json
    analysis = parse_json_field(contact.get("analysis_json"))
    if analysis:
        industry = analysis.get("industry", "")
        if industry:
            return str(industry).lower().strip()

    # 2. Try profile_json
    profile = parse_json_field(contact.get("profile_json"))
    if profile:
        for key in ("industry", "company_industry"):
            val = profile.get(key)
            if val:
                return str(val).lower().strip()

    # 3. Fallback to ICP industries
    if campaign_icp:
        icps = campaign_icp.get("icps", [])
        if isinstance(icps, list) and icps:
            first_icp = icps[0] if isinstance(icps[0], dict) else {}
            industries = first_icp.get("industries", [])
            if isinstance(industries, list) and industries:
                return str(industries[0]).lower().strip()

    return ""


def infer_seniority(title: str) -> str:
    """Infer seniority level from job title using keyword matching."""
    if not title:
        return "entry"
    title_lower = title.lower()

    # Check each seniority level in priority order (highest first).
    # Whole-word matching, not substring: "cto" is inside "director" and
    # "successfactors", "vp" inside "vpn", "lead" inside "leadership".
    for level in ("owner", "cxo", "vp", "director", "manager", "senior", "entry"):
        keywords = SENIORITY_KEYWORDS.get(level, [])
        for kw in keywords:
            if contains_term(title_lower, kw):
                return level

    return "manager"  # Default for unrecognized titles


def _headcount_to_acv(headcount: int | None) -> int:
    """Map headcount to base ACV using lookup table."""
    if headcount is None:
        return 10_000  # Default middle estimate

    for (low, high), acv in HEADCOUNT_BASE_ACV.items():
        if high is None:
            if headcount >= low:
                return acv
        elif low <= headcount <= high:
            return acv

    return 10_000  # Default


def _industry_multiplier(industry: str) -> float:
    """Get industry multiplier, with fuzzy matching."""
    if not industry:
        return 1.0

    # Exact match
    if industry in INDUSTRY_MULTIPLIERS:
        return INDUSTRY_MULTIPLIERS[industry]

    # Partial match (e.g., "financial services and banking" → "financial
    # services") — on word boundaries, or "tech" prices as fin(tech) 1.4x
    # and "paralegal" as (legal) 1.2x.
    for key, mult in INDUSTRY_MULTIPLIERS.items():
        if contains_term(industry, key) or contains_term(key, industry):
            return mult

    return 1.0  # Unknown industry


def parse_json_field(value: Any) -> dict[str, Any] | None:
    """Parse a JSON string field, returning None on failure."""
    if not value:
        return None
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _parse_headcount_value(val: Any) -> int | None:
    """Parse a headcount value from various formats."""
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        return _parse_headcount_from_text(val)
    return None


def _parse_headcount_from_text(text: str) -> int | None:
    """Extract headcount from text like '51-200', '200+', '500 employees'."""
    if not text:
        return None

    # "51-200" range format → midpoint
    m = re.search(r"(\d{1,6})\s*[-–]\s*(\d{1,6})", text)
    if m:
        return (int(m.group(1)) + int(m.group(2))) // 2

    # "200+" or "200+ employees"
    m = re.search(r"(\d{1,6})\+", text)
    if m:
        return int(m.group(1))

    # Plain number followed by "employees"
    m = re.search(r"(\d{1,6})\s*employees", text, re.IGNORECASE)
    if m:
        return int(m.group(1))

    return None
