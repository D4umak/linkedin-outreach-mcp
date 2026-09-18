"""ICP (Ideal Customer Profile) generator.

Takes a natural language target description like "fintech CTOs" and
generates a structured ICP with LinkedIn search parameters.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .llm import LLMClient

logger = logging.getLogger(__name__)

ICP_SYSTEM = """You are an expert B2B sales strategist. You convert natural language descriptions of target customers into structured Ideal Customer Profiles (ICPs) with LinkedIn search parameters.

You are precise and output structured JSON only."""

ICP_PROMPT = """Convert this target description into a structured ICP with LinkedIn search parameters.

## TARGET DESCRIPTION
"{target_description}"

## USER CONTEXT (their profile)
Name: {user_name}
Title: {user_title}
Company: {user_company}
Expertise: {user_expertise}

## TASK
Generate a structured ICP that can be used to search LinkedIn. Include:

1. **ICP Summary**: One-line description of the ideal customer
2. **Search Parameters**: LinkedIn search filters
3. **Segments**: 1-3 target segments (from most to least specific)
4. **Relevance Hook**: Why the user (above) would be credible reaching out to this audience
5. **Campaign Name**: Auto-generate a short campaign name if not provided

Each segment should have:
- `keywords`: LinkedIn search keywords (comma-separated)
- `titles`: Job titles to target (list)
- `seniority`: Seniority levels (list from: owner, partner, cxo, vp, director, manager, senior, entry)
- `industries`: LinkedIn industries (list)
- `company_size`: Company sizes (list from: 1-10, 11-50, 51-200, 201-500, 501-1000, 1001-5000, 5001-10000, 10001+)
- `geography`: Regions/countries (list, or empty for global)
- `estimated_results`: Rough estimate of how many LinkedIn results this would yield

## RESPONSE FORMAT
Return ONLY valid JSON (no markdown, no explanation):
{{
    "summary": "...",
    "campaign_name": "...",
    "relevance_hook": "...",
    "segments": [
        {{
            "name": "Primary",
            "keywords": "...",
            "titles": ["CTO", "VP Engineering"],
            "seniority": ["cxo", "vp", "director"],
            "industries": ["Financial Services", "Fintech"],
            "company_size": ["11-50", "51-200"],
            "geography": [],
            "estimated_results": "200-500"
        }}
    ]
}}"""


async def generate_icp(
    target_description: str,
    user_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate an ICP from a natural language target description.

    Args:
        target_description: e.g., "CTOs at fintech startups", "yoga studio owners in California"
        user_profile: The user's own profile data (for relevance hook)

    Returns:
        Structured ICP dict with search parameters.
    """
    # Route through backend if in backend mode and no local LLM key
    from ..config import has_local_llm_key, is_backend_mode
    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client
        client = get_linkedin_client()
        try:
            return await client.generate_icp(target_description, user_profile or {})
        finally:
            await client.close()

    user_name = ""
    user_title = ""
    user_company = ""
    user_expertise = ""

    if user_profile:
        user_name = user_profile.get("name", "")
        user_title = user_profile.get("title", "")
        user_company = user_profile.get("company", "")
        user_expertise = user_profile.get("core", user_profile.get("headline", ""))

    prompt = ICP_PROMPT.format(
        target_description=target_description,
        user_name=user_name or "Unknown",
        user_title=user_title or "Founder",
        user_company=user_company or "Unknown",
        user_expertise=user_expertise or "Not specified",
    )

    client = LLMClient()
    raw = await client.generate(prompt, system=ICP_SYSTEM, temperature=0.4)

    # Parse JSON
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(
            line for line in lines if not line.strip().startswith("```")
        )

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Failed to parse ICP JSON, building minimal structure")
        result = {
            "summary": f"Target: {target_description}",
            "campaign_name": target_description[:30],
            "relevance_hook": "",
            "segments": [
                {
                    "name": "Primary",
                    "keywords": target_description,
                    "titles": [],
                    "seniority": [],
                    "industries": [],
                    "company_size": [],
                    "geography": [],
                    "estimated_results": "Unknown",
                }
            ],
        }

    return result
