"""Tool: scheduler — View scheduler status or toggle on/off.

Thin dispatcher that routes to existing run_* functions based on the action parameter.
"""

from __future__ import annotations

import logging

from ..dashboard_links import dashboard_url

logger = logging.getLogger(__name__)


_LOOKBACK_DEFAULT = 24  # hours, for the log/activity actions


async def run_scheduler(
    action: str = "status",
    enabled: bool = True,
    cloud: bool = False,
    hours: int | None = None,
    event_type: str = "",
    campaign_id: str = "",
    host: str = "",
) -> str:
    """Manage the autonomous scheduler.

    Actions:
      status         — Show scheduler status, pending jobs, and recent activity
      toggle         — Enable or disable the scheduler
      observe        — Collect and classify signals, but send nothing
      always_on      — Enable/disable always-on mode (auto-re-enable + immediate alerts)
      logs           — Event log with metrics and recent failures
      activity       — Real action results from DB: what happened, what didn't, and why
      diagnostics    — Full system diagnostics: rate limits, timers, blockers
      report         — Configure periodic email campaign reports
      backfill_cloud — One-shot push of ALL local history (every campaign, any
                       mode/status) to the hosted dashboard (backend mode only)
      send_from      — Move all campaign outbound to the cloud or this machine

    Args:
        action: What to do: 'status', 'toggle', 'observe', 'logs',
            'diagnostics', 'report', or 'backfill_cloud'.
        enabled: True to enable, False to disable (for 'toggle').
            For 'report': True to enable email reports, False to disable.
        cloud: If True, toggle the cloud scheduler for 24/7 operation (for 'toggle').
            Launching or resuming a campaign already switches it on for hosted
            accounts; pass cloud=True, enabled=False to stop the backend sending
            while leaving this machine's scheduler alone.
        hours: Lookback window in hours for 'logs' (default 24).
            For 'report': report interval in hours (1, 2, 4, 8, or 24).
            Omitted, 'report' leaves the stored interval alone.
        event_type: Filter events by type for 'logs'.
            For 'report': recipient email (empty = use login email).
        campaign_id: Filter by campaign for 'logs' and 'diagnostics'.
    """
    action = action.lower().strip()

    if action == "status":
        from .scheduler_status import run_scheduler_status
        return await run_scheduler_status()

    if action == "toggle":
        from .scheduler_status import run_toggle_scheduler
        return await run_toggle_scheduler(enabled=enabled, cloud=cloud)

    if action == "send_from":
        from .scheduler_status import run_send_from
        return await run_send_from(host)

    if action == "observe":
        from .scheduler_status import run_set_observe_mode
        return await run_set_observe_mode()

    if action == "logs":
        from .scheduler_status import run_scheduler_logs
        return await run_scheduler_logs(
            hours=_LOOKBACK_DEFAULT if hours is None else hours,
            event_type=event_type, campaign_id=campaign_id,
        )

    if action == "diagnostics":
        from .scheduler_status import run_scheduler_diagnostics
        return await run_scheduler_diagnostics(campaign_id=campaign_id)

    if action == "activity":
        from .scheduler_status import run_scheduler_activity
        return await run_scheduler_activity(
            hours=_LOOKBACK_DEFAULT if hours is None else hours,
            campaign_id=campaign_id,
        )

    if action == "always_on":
        from .scheduler_status import run_toggle_always_on
        return await run_toggle_always_on(enabled=enabled)

    if action == "report":
        return await _run_report_settings(
            enabled=enabled, interval_hours=hours, report_email=event_type,
        )

    if action == "backfill_cloud":
        return await _run_backfill_cloud()

    return (
        f"Unknown action: '{action}'. "
        f"Use 'status', 'toggle', 'observe', 'always_on', 'logs', 'activity', "
        f"'diagnostics', 'report', 'backfill_cloud', or 'send_from'."
    )


async def _run_backfill_cloud() -> str:
    """One-shot push of full local history to the hosted dashboard.

    Unlike periodic sync (active/paused autopilot only), this includes
    completed and manual campaigns so the dashboard shows everything.

    Except live autopilot campaigns in observe mode: handing the backend one of
    those is the instruction to send from it every 5 minutes. sync_to_cloud
    withholds them and reports how many, which is said out loud here rather
    than left as a short count.
    """
    from .. import config

    if not config.is_backend_mode():
        return (
            "Backend account not linked — sign in at "
            "https://heylead.dev/auth/login-url and run setup_profile "
            "with your token first."
        )

    from ..services import cloud_sync

    result = await cloud_sync.sync_to_cloud(include_all=True)
    if "error" in result:
        return f"Backfill failed: {result['error']}"

    campaigns = result.get("campaigns", 0)
    outreaches = result.get("outreaches", 0)
    withheld = result.get("observe_withheld", 0)
    note = ""
    if withheld:
        note = (
            f"\n\n{withheld} active autopilot campaign(s) were held back: the "
            "scheduler is in observe mode, and handing the backend a live "
            "autopilot campaign is the instruction to send from it every 5 "
            "minutes. Everything else was pushed, active copilot campaigns "
            "included — the backend only schedules autopilot. Run this again "
            "after `scheduler(action='toggle', enabled=True)` to push them."
        )
    return (
        f"Pushed {campaigns} campaigns, {outreaches} outreaches "
        f"to your hosted workspace — {dashboard_url()}{note}"
    )


async def _run_report_settings(
    enabled: bool = True,
    interval_hours: int | None = None,
    report_email: str = "",
) -> str:
    """Configure periodic email campaign report via backend API."""
    from ..config import is_backend_mode

    if not is_backend_mode():
        return (
            "Email reports require backend mode (cloud scheduler).\n"
            "Run `setup_profile` to connect to the backend first."
        )

    import httpx
    from ..services.cloud_sync import _base_url, _headers

    base = _base_url()

    # Build update payload — only include explicitly set values
    body: dict = {"report_enabled": enabled}
    # Only when an interval was actually asked for. This used to read
    # `if interval_hours != 24`, borrowing the 'hours' lookback default as a
    # sentinel — but 24 is itself a legal interval (the endpoint takes
    # 1, 2, 4, 8 or 24), so asking for a daily report was the one request the
    # tool silently dropped while printing the unchanged interval back.
    if interval_hours is not None:
        body["report_interval_hours"] = interval_hours
    if report_email:
        body["report_email"] = report_email

    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        try:
            resp = await client.post(
                f"{base}/api/v1/scheduler/report-settings",
                json=body,
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            return f"Failed to update report settings: {e.response.status_code}"
        except httpx.HTTPError as e:
            return f"Failed to reach backend: {e}"

    status = "enabled" if data.get("report_enabled") else "disabled"
    interval = data.get("report_interval_hours", 1)
    email = data.get("report_email", "") or "(login email)"

    lines = [
        f"# Email Report {status.upper()}",
        "",
        f"- Status: **{status}**",
        f"- Interval: every **{interval}h**",
        f"- Recipient: **{email}**",
        "",
    ]

    if data.get("report_enabled"):
        lines.append(
            "The next report will be sent on the next scheduler tick "
            "(within ~5 minutes)."
        )
        lines.append("")
        lines.append("The report includes:")
        lines.append("- Campaign metrics (invitations, engagements, follow-ups, replies)")
        lines.append("- Issue detection (why campaigns may be idle)")
        lines.append("- Copy-pasteable fix commands for Claude Code")
    else:
        lines.append("No more reports will be sent until re-enabled.")

    return "\n".join(lines)
