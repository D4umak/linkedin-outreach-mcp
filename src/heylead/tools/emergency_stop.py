"""Tool: emergency_stop — Immediately pause all active campaigns.

Safety kill-switch. Iterates every active campaign and sets status='paused'.
Logs each pause for audit trail. Use resume_campaign() to re-enable individually.
"""

from __future__ import annotations

import json
import logging
import time

from ..db.queries import (
    get_setting,
    list_campaigns,
    log_action,
    skip_pending_outreaches,
    update_campaign,
)
from ..db import queries
from ..db.async_bridge import run_db

logger = logging.getLogger(__name__)


def _cancel_pending_jobs(campaign_id: str) -> int:
    """Cancel a campaign's queued scheduler jobs. Returns the count.

    Only 'pending' jobs are touched — a 'running' job is mid-flight and must not
    be rewritten underneath itself.
    """
    from ..db.schema import get_db

    db = get_db()
    cursor = db.execute(
        "UPDATE scheduler_jobs SET status = 'cancelled' "
        "WHERE campaign_id = ? AND status = 'pending'",
        (campaign_id,),
    )
    count = cursor.rowcount
    db.commit()
    db.close()
    return count


async def run_emergency_stop() -> str:
    """Pause all active campaigns immediately."""

    # ── Pre-check ──
    setup_done = await run_db(get_setting, "setup_complete", False)
    if not setup_done:
        return (
            "Setup required before managing campaigns.\n\n"
            "Please run setup_profile first."
        )

    # ── Find all active campaigns ──
    active_campaigns = await run_db(list_campaigns, status="active")

    if not active_campaigns:
        return (
            "No active campaigns to stop.\n\n"
            "All campaigns are already paused or completed.\n"
            "Use show_status() to see all campaigns."
        )

    # ── Pause each one ──
    paused_names = []
    paused_ids: list[str] = []
    total_skipped = 0
    now_ts = int(time.time())
    for campaign in active_campaigns:
        cid = campaign["id"]
        skipped = await run_db(skip_pending_outreaches, cid)
        total_skipped += skipped
        # Warm-up jobs (engage, follow, endorse, profile_view) sit in
        # scheduler_jobs, not in outreaches, and nothing on their execution path
        # re-checks campaign status — left pending they act after the stop.
        cancelled_jobs = await run_db(_cancel_pending_jobs, cid)
        # The two keys this stop sets, and the status, in one statement.
        await run_db(
            queries.merge_campaign_config, cid,
            {"pause_reason": "emergency_stop", "paused_at": now_ts},
            status="paused",
        )
        await run_db(log_action, "campaign_status_change",
            result="paused",
            details={
                "campaign_id": cid,
                "campaign_name": campaign["name"],
                "old_status": "active",
                "new_status": "paused",
                "changed_by": "user",
                "reason": "emergency_stop",
                "pending_skipped": skipped,
                "jobs_cancelled": cancelled_jobs,
            },)
        paused_names.append(campaign["name"])
        paused_ids.append(cid)
        logger.info(f"Emergency stop: paused campaign {cid}: {campaign['name']} ({skipped} pending skipped)")

    # ── Sync to backend so cloud scheduler also stops ──
    from ..services.cloud_sync import sync_emergency_stop, sync_campaign_status

    synced = await sync_emergency_stop()
    if synced:
        logger.info("Emergency stop synced to cloud backend")
    else:
        # Fallback: sync each campaign individually
        logger.warning("Bulk emergency stop sync failed, trying per-campaign sync")
        fallback_ok = 0
        for campaign in active_campaigns:
            ok = await sync_campaign_status(
                campaign["id"], "paused",
                caller="emergency_stop", reason="emergency_stop_fallback",
            )
            if ok:
                fallback_ok += 1
        synced = fallback_ok == len(active_campaigns)
        if synced:
            logger.info("Emergency stop synced via per-campaign fallback (%d)", fallback_ok)
        else:
            logger.warning("Emergency stop partial sync: %d/%d campaigns", fallback_ok, len(active_campaigns))

    # ── Format result ──
    count = len(paused_names)
    output = [
        f"\U0001f6d1 EMERGENCY STOP: {count} campaign{'s' if count != 1 else ''} paused.",
        "",
    ]

    for i, name in enumerate(paused_names):
        is_last = i == len(paused_names) - 1
        prefix = "\u2514\u2500\u2500" if is_last else "\u251c\u2500\u2500"
        output.append(f"{prefix} {name}")

    output.append("")
    output.append(f"All outreach is stopped. {total_skipped} pending outreaches skipped.")
    if not synced:
        # show_status retries no cloud call. Pausing an already-paused
        # campaign re-sends its pause on hosted accounts, so that is the retry.
        output.append("")
        output.append(
            "**Warning**: Could not sync to cloud scheduler, so the cloud may "
            "still send. Retry the cloud pause for each campaign:"
        )
        for cid in paused_ids:
            output.append(f"- campaign(action='pause', campaign_id='{cid}')")
    output.append("")
    output.append("To resume individual campaigns: resume_campaign(campaign_id='...')")
    output.append("To see all campaigns: show_status()")
    from ..services.dashboard_snapshot import status_footer

    output.extend(status_footer("overview", fresh=True))

    return "\n".join(output)
