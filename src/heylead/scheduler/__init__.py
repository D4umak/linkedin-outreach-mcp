"""HeyLead scheduler — asyncio-based cron for outreach timing.

The scheduler runs as a background asyncio task alongside the MCP server
(via FastMCP's lifespan hook). It autonomously handles:

- Invitations: 20-40 min randomized delays between sends
- Follow-ups: day 1, 3, 7, 14 schedule
- Reply checks: every 5 minutes
- Engagement warm-ups: every 30 minutes

Only active for autopilot campaigns. Enable via toggle_scheduler(enabled=True).
"""

from .engine import SchedulerEngine

__all__ = ["SchedulerEngine"]
