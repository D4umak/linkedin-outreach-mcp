"""Tool: retry_failed — Reset error outreaches to pending for retry.

Finds outreaches stuck in 'error' status and resets them so they
can be picked up by generate_and_send() again.
"""

from __future__ import annotations

import logging

from ..db.queries import (
    get_campaign,
    get_error_outreaches,
    get_setting,
    log_action,
    update_outreach,
)
from ..db.async_bridge import run_db
from ..services.cloud_sync import sync_to_cloud

logger = logging.getLogger(__name__)


async def run_retry_failed(campaign_id: str = "") -> str:
    """Reset error outreaches to pending for retry."""

    setup_done = await run_db(get_setting, "setup_complete", False)
    if not setup_done:
        return (
            "Setup required before retrying.\n\n"
            "Please run setup_profile first."
        )

    from ..services.cloud_sync import hosted_queue_refusal

    refusal = hosted_queue_refusal("retry_failed")
    if refusal:
        return refusal

    # Validate campaign if specified
    if campaign_id:
        campaign = await run_db(get_campaign, campaign_id)
        if not campaign:
            return f"Campaign not found: {campaign_id}"

    # Find error outreaches
    errors = await run_db(get_error_outreaches, campaign_id)

    if not errors:
        scope = f"in campaign '{campaign['name']}'" if campaign_id else "across all campaigns"
        return (
            f"No failed outreaches {scope}.\n\n"
            "All outreaches are in healthy states.\n"
            "Use show_status() to see your dashboard."
        )

    # Reset each error outreach to pending (skip those that exceeded max attempts)
    from ..constants import MAX_INVITE_ATTEMPTS

    reset_count = 0
    skipped_count = 0
    names: list[str] = []
    for err in errors:
        oid = err["outreach_id"]
        attempts = err.get("invite_attempts") or 0

        if attempts >= MAX_INVITE_ATTEMPTS:
            await run_db(update_outreach, oid, status="skipped",
                            last_attempt_error=f"Exceeded {MAX_INVITE_ATTEMPTS} attempts")
            skipped_count += 1
            continue

        await run_db(update_outreach, oid, status="pending", next_action=None)
        await run_db(log_action, "outreach_retried",
            outreach_id=oid,
            result="reset",
            details={
                "prospect": err.get("name", "Unknown"),
                "campaign_id": err.get("campaign_id", ""),
            },)
        reset_count += 1
        names.append(err.get("name", "Unknown"))

    logger.info(f"Retried {reset_count} failed outreaches, skipped {skipped_count} (max attempts)")

    # Sync reset statuses to backend so cloud scheduler picks them up
    if reset_count > 0:
        sync_result = await sync_to_cloud()
        if "error" in sync_result:
            logger.warning("Could not sync retried outreaches to backend: %s", sync_result["error"])

    # Format output
    names_preview = ", ".join(names[:5])
    if len(names) > 5:
        names_preview += f", ... and {len(names) - 5} more"

    parts = [f"Reset {reset_count} failed outreach{'es' if reset_count != 1 else ''} to pending."]
    if skipped_count:
        parts.append(f"Permanently skipped {skipped_count} (exceeded {MAX_INVITE_ATTEMPTS} attempts).")
    if names_preview:
        parts.append(f"\nProspects: {names_preview}")
    parts.append("\nThese prospects are back in the queue.\nUse generate_and_send() to process them.")

    from ..services.dashboard_snapshot import status_footer

    # Without a campaign the retry spans every campaign: link the list.
    if campaign_id:
        parts.extend(status_footer("campaign", campaign_id, snapshot=False))
    else:
        parts.extend(status_footer("campaigns", snapshot=False))
    return "\n".join(parts)
