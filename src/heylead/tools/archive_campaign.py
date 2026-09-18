"""Tool: archive_campaign — Mark a campaign as completed/archived.

Archived campaigns stop receiving outreach and move to completed status.
They remain viewable via show_status(campaign_id=...) for historical reference.
"""

from __future__ import annotations

import logging

from ..db.queries import (
    count_open_outreaches,
    get_campaign,
    get_setting,
    list_campaigns,
    log_action,
    skip_pending_outreaches,
    update_campaign,
)
from ..db.async_bridge import run_db
from ..services.cloud_sync import sync_campaign_archive

logger = logging.getLogger(__name__)


async def run_archive_campaign(campaign_id: str = "", force: bool = False) -> str:
    """Archive a campaign, setting its status to 'completed'.

    If no campaign_id is provided, lists the campaigns — it does not
    guess. Auto-picking the newest non-completed campaign terminally
    skipped its pending queue with no confirmation and no undo.
    If pending/connected/invited prospects remain, returns a warning unless
    force=True is passed.
    """

    setup_done = await run_db(get_setting, "setup_complete", False)
    if not setup_done:
        return (
            "Setup required before managing campaigns.\n\n"
            "Please run setup_profile first."
        )

    # Find the campaign to archive
    if campaign_id:
        campaign = await run_db(get_campaign, campaign_id)
        if not campaign:
            return f"Campaign not found: {campaign_id}"
    else:
        campaigns = await run_db(list_campaigns)
        candidates = [c for c in campaigns if c.get("status") != "completed"]
        if not candidates:
            return (
                "No campaigns available to archive.\n\n"
                "All campaigns are already archived, or no campaigns exist.\n"
                "Use show_status() to see all campaigns."
            )
        # Same shape as delete: name the campaigns, do not pick one.
        lines = ["Which campaign do you want to archive?\n"]
        for c in candidates:
            lines.append(
                f"  `{c['id']}` — {c['name']} ({c.get('status', '?')})"
            )
        lines.append("")
        lines.append(
            "Call campaign(action='archive', campaign_id='...') with the id above."
        )
        return "\n".join(lines)

    # Check current status
    status = campaign.get("status", "")
    if status == "completed":
        return (
            f"Campaign '{campaign['name']}' is already archived.\n\n"
            f"View it with: show_status(campaign_id=\"{campaign_id}\")"
        )

    # Warn about un-messaged prospects unless force=True
    open_counts = await run_db(count_open_outreaches, campaign_id)
    total_open = sum(open_counts.values())
    if total_open > 0 and not force:
        parts = [f"{cnt} {status}" for status, cnt in sorted(open_counts.items())]
        warning = " + ".join(parts)
        return (
            f"Cannot archive '{campaign['name']}' — {warning} prospects "
            f"still have pending work.\n\n"
            f"These prospects still have unsent outreach. Archiving will "
            f"terminally skip them — there is no undo.\n\n"
            f"Options:\n"
            f"- Keep the campaign active so the scheduler finishes outreach\n"
            f"- campaign(action='archive', campaign_id='{campaign_id}', "
            f"confirm=True) to archive anyway\n"
        )

    # Archive it and skip pending outreaches
    skipped = await run_db(skip_pending_outreaches, campaign_id)
    await run_db(update_campaign, campaign_id, status="completed")

    log_details: dict = {
        "campaign_id": campaign_id,
        "campaign_name": campaign["name"],
        "previous_status": status,
        "pending_skipped": skipped,
    }
    if total_open > 0:
        log_details["open_outreaches_abandoned"] = open_counts
        await run_db(log_action,
            "campaign_archived_with_open_work",
            details=log_details,
        )
    else:
        await run_db(log_action, "campaign_archived", details=log_details)
    # Sync to backend so cloud scheduler also stops
    synced = await sync_campaign_archive(campaign_id)
    logger.info(
        "Archived campaign %s: %s (cloud_synced=%s)",
        campaign_id, campaign["name"], synced,
    )

    cloud_note = ""
    if not synced:
        cloud_note = "\n**Warning**: Could not sync archive to cloud scheduler."

    result = (
        f"Campaign '{campaign['name']}' has been archived.\n\n"
        f"{skipped} pending outreaches marked as skipped.\n"
    )
    if total_open > 0:
        parts = [f"{cnt} {status}" for status, cnt in sorted(open_counts.items())]
        result += f"Warning: {' + '.join(parts)} prospects were abandoned without messaging.\n"
    result += (
        "No further outreach will be sent for this campaign.\n\n"
        f"To view archived campaign details: show_status(campaign_id=\"{campaign_id[:8]}...\")\n"
        f"To create a new campaign: create_campaign(\"your target description\"){cloud_note}"
    )
    return result
