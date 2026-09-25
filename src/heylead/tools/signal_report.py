"""Tool: signal_report — Signal analytics, conversion rates, and weight recommendations.

Provides deep signal intelligence analytics including:
- Per-signal-type conversion rates (signal → outreach → accept → reply → won)
- Signal ROI analysis (won deals per signal detection)
- Weight tuning recommendations based on actual conversion data
- Compound intent effectiveness tracking
- Signal health metrics (decay status, expiry, watchlist health)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def run_signal_report(
    days: int = 30,
    campaign_id: str = "",
) -> str:
    """Generate a comprehensive signal analytics report.

    Shows signal conversion analytics, best-performing signal types,
    weight recommendations, and compound intent effectiveness.

    Args:
        days: Lookback window in days (default 30).
        campaign_id: Optional campaign filter. Analyzes all if empty.

    Returns:
        Formatted signal analytics report.
    """
    # Hosted: signals are collected, scored and optimised in the cloud
    # (heylead-api#1211); the local signal tables are not the account's.
    from .. import config
    if config.is_backend_mode():
        from ..services import cloud_learning
        data = await cloud_learning.fetch_learning()
        return cloud_learning.format_history(data) if data else cloud_learning.UNREACHABLE

    from ..db.async_bridge import run_db
    from ..services.signal_analytics import generate_signal_report

    return await run_db(generate_signal_report, days=days, campaign_id=campaign_id)
