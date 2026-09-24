"""Tool: campaign — Control campaign lifecycle (pause, resume, archive, delete, etc.).

Thin dispatcher that routes to existing run_* functions based on the action parameter.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


async def run_campaign(
    action: str,
    campaign_id: str = "",
    confirm: bool = False,
) -> str:
    """Control campaign lifecycle.

    Actions:
      launch         — Start outreach for a draft campaign
      monitor        — Show a campaign's live progress (read-only)
      plan           — What happens after launch, step by step (read-only)
      pause          — Pause an active campaign
      resume         — Resume a paused campaign
      archive        — Archive a completed campaign
      delete         — Permanently delete a campaign (requires confirm=True)
      emergency_stop — Immediately pause ALL active campaigns
      retry_failed   — Reset error outreaches to pending
      repair_queue   — Drop never-contacted rows below min_fit_score
      clear_coordinator_hold — Release a campaign-wide coordinator hold

    Args:
        action: What to do: 'launch', 'monitor', 'plan', 'pause', 'resume', 'archive',
            'delete', 'emergency_stop', 'retry_failed', 'repair_queue',
            'clear_coordinator_hold'.
        campaign_id: Which campaign to act on. Required for
            clear_coordinator_hold. Auto-selects if empty for other actions.
        confirm: Must be True for delete, and to archive a campaign that
            still has pending/connected/invited outreach.
    """
    action = action.lower().strip()

    if action in ("launch", "start"):
        from .campaign_control import run_launch_campaign
        return await run_launch_campaign(campaign_id)

    # Deliberately not aliased to 'observe': scheduler(action='observe') sets
    # the mode, and one word meaning two things across two tools is how the
    # wrong one gets called. A read: activating is launch.
    if action == "monitor":
        from .campaign_control import run_monitor_campaign
        return await run_monitor_campaign(campaign_id)

    if action == "plan":
        from .campaign_control import run_campaign_plan
        return await run_campaign_plan(campaign_id)

    if action == "pause":
        from .campaign_control import run_pause_campaign
        return await run_pause_campaign(campaign_id)

    if action == "resume":
        from .campaign_control import run_resume_campaign
        return await run_resume_campaign(campaign_id)

    if action == "archive":
        from .archive_campaign import run_archive_campaign
        # confirm is the MCP-visible force flag. Dropping it left a
        # pending campaign with no way to archive from campaign().
        return await run_archive_campaign(campaign_id, force=confirm)

    if action == "delete":
        from .delete_campaign import run_delete_campaign
        return await run_delete_campaign(campaign_id, confirm)

    if action == "emergency_stop":
        from .emergency_stop import run_emergency_stop
        return await run_emergency_stop()

    if action in ("retry_failed", "retry"):
        from .retry_failed import run_retry_failed
        return await run_retry_failed(campaign_id)

    if action in ("repair_queue", "repair"):
        return await _repair_sendable_queue(campaign_id)

    if action in ("status_history", "history", "audit"):
        return await _format_status_history(campaign_id)

    if action in ("clear_coordinator_hold", "clear_hold"):
        return await _clear_coordinator_hold(campaign_id)

    return (
        f"Unknown action: '{action}'. Available actions:\n"
        "  'launch'         — Start outreach for a draft campaign\n"
        "  'monitor'        — Show a campaign's live progress (read-only)\n"
        "  'plan'           — What happens after launch, step by step (read-only)\n"
        "  'pause'          — Pause an active campaign\n"
        "  'resume'         — Resume a paused campaign\n"
        "  'archive'        — Archive a completed campaign\n"
        "  'delete'         — Permanently delete (requires confirm=True)\n"
        "  'emergency_stop' — Pause ALL active campaigns\n"
        "  'retry_failed'   — Reset error outreaches to pending\n"
        "  'repair_queue'   — Drop never-contacted rows below min_fit_score\n"
        "  'status_history' — View campaign status change audit log\n"
        "  'clear_coordinator_hold' — Release a campaign-wide coordinator hold"
    )


async def _clear_coordinator_hold(campaign_id: str = "") -> str:
    """Release a coordinator hold on the host that owns sending."""
    from ..config import is_backend_mode
    from ..db.async_bridge import run_db
    from ..db.queries import get_campaign
    from ..services.agent_commons import clear_hold, get_hold
    from ..services.cloud_sync import BackendAuthError, post_hosted_json

    cid = (campaign_id or "").strip()
    if not cid:
        return (
            "clear_coordinator_hold needs campaign_id. "
            "It will not pick a campaign for you."
        )
    campaign = await run_db(get_campaign, cid)
    if not campaign and not is_backend_mode():
        return "No campaign found."
    name = str((campaign or {}).get("name") or cid[:8])

    if is_backend_mode():
        try:
            data = await post_hosted_json(
                f"/api/v1/campaigns/{cid}/clear-coordinator-hold",
            )
        except BackendAuthError:
            return (
                "Clearing a coordinator hold needs a valid HeyLead token. "
                "Paste the token message and call setup_profile(backend_jwt='...')."
            )
        except Exception as exc:
            return f"Clear coordinator hold failed: {exc}"
        if not data.get("cleared"):
            return f"No coordinator hold on {name}."
        reason = str(data.get("reason") or "")
        extra = f" ({reason})" if reason else ""
        return f"Cleared coordinator hold on {name}{extra}. Sending can resume."

    hold = await run_db(get_hold, cid)
    if not hold:
        return f"No coordinator hold on {name}."
    await run_db(clear_hold, cid)
    reason = str(hold.get("reason") or hold.get("body") or "")
    extra = f" ({reason})" if reason else ""
    return f"Cleared coordinator hold on {name}{extra}. Sending can resume."


async def _repair_sendable_queue(campaign_id: str = "") -> str:
    """Delete never-invited, never-messaged outreaches below the send gate."""
    from ..constants import MIN_FIT_SCORE_THRESHOLD
    from ..db import aio as adb
    from ..db.async_bridge import run_db
    from ..db.queries import delete_never_contacted_below_threshold

    campaign, err = await adb.find_active_campaign(campaign_id)
    if not campaign:
        return err
    try:
        cfg = json.loads(campaign.get("config_json") or "{}")
        min_fit = float(cfg.get("min_fit_score", MIN_FIT_SCORE_THRESHOLD))
    except (TypeError, ValueError, json.JSONDecodeError):
        min_fit = MIN_FIT_SCORE_THRESHOLD
    from ..services.fit_gate import (
        restore_fit_skipped_after_contact,
        restore_fit_skipped_if_eligible,
    )

    deleted = await run_db(
        delete_never_contacted_below_threshold, campaign["id"], min_fit,
    )
    restored_fit = await run_db(
        restore_fit_skipped_if_eligible, campaign["id"], min_fit,
    )
    restored_contact = await run_db(
        restore_fit_skipped_after_contact, campaign["id"],
    )
    return (
        f"Removed {deleted} never-contacted prospect"
        f"{'' if deleted == 1 else 's'} below fit {min_fit:.2f} "
        f"from '{campaign['name']}'. Restored {restored_fit} "
        f"rescored skip{'' if restored_fit == 1 else 's'} and "
        f"{restored_contact} fit-skip{'' if restored_contact == 1 else 's'} "
        "after a real send. Contact records stay so refill "
        "does not re-import them."
    )


async def _format_status_history(campaign_id: str = "") -> str:
    """Format campaign status change history as a readable table."""
    import time as _time
    from ..db.queries import get_campaign_status_history
    from ..db.async_bridge import run_db

    entries = await run_db(get_campaign_status_history, campaign_id, 30)
    if not entries:
        return "No campaign status changes recorded yet."

    lines = ["# Campaign Status History\n"]
    lines.append("| When | Campaign | Change | By | Reason |")
    lines.append("|------|----------|--------|----|--------|")

    for e in entries:
        ts = e.get("timestamp", 0)
        ago = _time_ago(ts)
        name = e.get("campaign_name", "?")[:30]
        old = e.get("old_status", "?")
        new = e.get("new_status", "?")
        by = e.get("changed_by", "?")
        reason = e.get("reason", "")
        lines.append(f"| {ago} | {name} | {old} → {new} | {by} | {reason} |")

    from ..services.dashboard_snapshot import status_footer

    # Without a campaign the history spans every campaign: link the list.
    if campaign_id:
        lines.extend(status_footer("campaign", campaign_id, snapshot=False))
    else:
        lines.extend(status_footer("campaigns", snapshot=False))
    return "\n".join(lines)


def _time_ago(ts: int) -> str:
    """Human-readable time ago from unix timestamp."""
    import time as _time

    diff = int(_time.time()) - ts
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    return f"{diff // 86400}d ago"
