"""Tool: analytics — Campaign reports, comparisons, and exports.

Thin dispatcher that routes to existing run_* functions based on the action parameter.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def run_analytics(
    action: str = "report",
    campaign_id: str = "",
    campaign_ids: str = "",
    format: str = "table",
) -> str:
    """Campaign analytics and reporting.

    Actions:
      report  — Detailed analytics with outcomes, conversion rates, stale lead warnings
      compare — Compare 2+ campaigns side by side
      export  — Export campaign results as table, CSV, or JSON

    Args:
        action: What to do: 'report', 'compare', 'export'.
        campaign_id: Which campaign. Uses active campaign if empty.
        campaign_ids: Comma-separated campaign IDs (for 'compare'). Compares all if empty.
        format: Output format for 'export': 'table', 'csv', or 'json'.
    """
    action = action.lower().strip()

    if action == "report":
        from .campaign_report import run_campaign_report
        return await run_campaign_report(campaign_id)

    if action == "compare":
        from .compare_campaigns import run_compare_campaigns
        return await run_compare_campaigns(campaign_ids)

    if action == "export":
        from .export_campaign import run_export_campaign
        return await run_export_campaign(campaign_id, format)

    return f"Unknown action: '{action}'. Use 'report', 'compare', or 'export'."
