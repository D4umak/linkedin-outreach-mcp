"""Tools 26-27: scheduler_status + toggle_scheduler.

Provides visibility into and control over the autonomous scheduler.
Supports both local (in-process) and cloud (backend) scheduling modes.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

from ..config import (
    get_scheduler_mode,
    is_backend_mode,
    set_scheduler_mode,
)
from ..db import aio as db

logger = logging.getLogger(__name__)


async def run_scheduler_status() -> str:
    """Show scheduler status, pending jobs, and recent activity.

    If in backend mode, also shows cloud scheduler status.
    """
    from ..config import is_scheduler_always_on

    mode = get_scheduler_mode()
    enabled = mode == "full"
    observing = mode == "observe"
    always_on = is_scheduler_always_on()
    stats = await db.get_scheduler_stats()
    now = int(time.time())

    # ── Header ──
    # observe used to render as "Disabled" and return early, hiding the queue of
    # collection jobs that the mode exists to run.
    if enabled:
        status_icon, status_word = "\u2601\ufe0f", "Enabled"
    elif observing:
        status_icon, status_word = "\U0001f440", "Observing"
    else:
        status_icon, status_word = "\u23f8\ufe0f", "Disabled"
    lines = [
        f"# {status_icon} Scheduler {status_word}",
        "",
    ]

    if always_on:
        lines.append("**Always-On**: Active (auto-re-enables if disabled, sends immediate alerts)")
        lines.append("")

    if is_backend_mode():
        from ..config import get_sending_host
        host = get_sending_host()
        if host == "local":
            lines.append("**Sending from this machine** — the cloud scheduler is stood down.")
        else:
            lines.append(
                "**Sending from the cloud** — this machine stands down for "
                "campaign outbound. Move it here with "
                "`scheduler(action='send_from', host='local')`."
            )
        lines.append("")

    if observing:
        lines.append(
            "The scheduler is in **observe** mode: it reads, and this machine "
            "sends nothing.\n\n"
            "Running:\n"
            "- Prospect and network post scans\n"
            "- Watchlist collectors (keywords, competitors, hiring, news)\n"
            "- Signal classification, scoring, decay and re-matching\n"
            "- Reply checks\n\n"
            "Not running: invitations, follow-up DMs, engagements, and signal "
            "activation (which enrols people into campaigns). It hands the "
            "backend no new go-signal either: active autopilot campaigns are "
            "withheld from the cloud push, and nothing issues the /resume that "
            "would commission cloud sending — not launch, monitor or resume, "
            "and not the scheduler's own auto-resume of limit-paused "
            "campaigns.\n\n"
            "- `scheduler(action='toggle', enabled=True)` — leave observe and start sending\n"
            "- `scheduler(action='toggle', enabled=False)` — stop collecting too"
            + await _cloud_sending_caveat()
        )
        lines.append("")

    # How current the hosted dashboard is. Before every branch below, including
    # the early return for 'off': the observe copy above states withholding as a
    # rule, and this is the reading — which campaigns heylead.dev is actually
    # frozen on, and since when.
    freshness = await _dashboard_freshness()
    if freshness:
        lines.extend(freshness)
        lines.append("")

    if not enabled and not observing:
        lines.append(
            "The autonomous scheduler is currently **off**.\n"
            "Enable it with: `scheduler(action='toggle', enabled=True)`\n\n"
            "When enabled, the scheduler will automatically:\n"
            "- Send invitations to pending prospects (autopilot campaigns)\n"
            "- Send follow-ups on schedule (1, 3, 7, 14 days)\n"
            "- Check for replies every 5 minutes\n"
            "- Engage with prospect posts every 30 minutes\n"
            "- Respect rate limits, working hours, and daily caps\n\n"
            "To collect and classify signals without sending anything, use: "
            "`scheduler(action='observe')`"
        )
        # Still show cloud status if available
        cloud_section = await _cloud_status_section()
        if cloud_section:
            lines.append("")
            lines.append(cloud_section)
        return "\n".join(lines)

    # ── Job Counts ──
    counts = stats.get("counts", [])
    if counts:
        lines.append("## Job Queue")
        lines.append("")
        lines.append("| Status | Type | Count |")
        lines.append("|--------|------|-------|")
        for row in counts:
            status = row["status"]
            job_type = row["job_type"]
            cnt = row["cnt"]
            icon = {"pending": "\u23f3", "running": "\U0001f504", "completed": "\u2705", "failed": "\u274c"}.get(status, "")
            lines.append(f"| {icon} {status} | {job_type} | {cnt} |")
        lines.append("")
    else:
        lines.append("\U0001f4ed No jobs in queue\n")

    # ── Next Scheduled ──
    # A queued row is not a plan. runs_in_mode() refuses outbound job types in
    # observe, and the stale-pending sweeper reaps them 30 min later, so listing
    # one as "next brand_post in 5 min" promised work that could not happen —
    # directly above the section saying observe does not send. Rows already in
    # the queue when the mode changed are exactly the ones this hits: the
    # planner stopped creating new ones.
    #
    # Filtered rather than re-queried: get_scheduler_stats already narrowed this
    # to the five soonest pending rows, so a queue full of refused work can
    # empty this list. Saying so under "Queued, Not Scheduled" is the honest
    # reading of that state; silently listing the refused rows was not.
    next_jobs = stats.get("next_jobs", [])
    refused_jobs: list[dict[str, Any]] = []
    if observing:
        from ..scheduler.engine import runs_in_mode
        refused_jobs = [
            j for j in next_jobs if not runs_in_mode(j.get("job_type", ""), mode)
        ]
        next_jobs = [
            j for j in next_jobs if runs_in_mode(j.get("job_type", ""), mode)
        ]
    if next_jobs:
        lines.append("## Next Scheduled")
        lines.append("")
        for job in next_jobs:
            scheduled = job["scheduled_at"]
            delta = scheduled - now
            if delta > 0:
                mins = delta // 60
                when = f"in {mins} min" if mins > 0 else "now"
            else:
                when = "overdue"

            job_type = job["job_type"]
            outreach = job.get("outreach_id", "")
            target = f" (outreach: {outreach[:8]}...)" if outreach else ""
            lines.append(f"- **{job_type}** {when}{target}")
        lines.append("")

    if refused_jobs:
        held = sorted({j.get("job_type", "") for j in refused_jobs})
        lines.append("## Queued, Not Scheduled")
        lines.append("")
        lines.append(
            f"{len(refused_jobs)} queued job(s) the observe gate refuses "
            f"({', '.join(held)}). They are outbound, so they will not run "
            "until you leave observe with "
            "`scheduler(action='toggle', enabled=True)`."
        )
        lines.append("")

    # ── Recent Activity ──
    recent = stats.get("recent", [])
    if recent:
        lines.append("## Recent Activity")
        lines.append("")
        for job in recent[:5]:
            completed = job.get("completed_at", 0)
            ago = (now - completed) // 60 if completed else 0
            status = job["status"]
            icon = "\u2705" if status == "completed" else "\u274c"
            error = job.get("error", "")
            error_text = f" \u2014 {error[:50]}" if error else ""
            lines.append(
                f"- {icon} **{job['job_type']}** {ago} min ago{error_text}"
            )
        lines.append("")

    # ── 24h Metrics Summary ──
    metrics = await db.get_job_metrics(24)
    if metrics:
        total_jobs = sum(m["total"] for m in metrics.values())
        total_ok = sum(m["success"] for m in metrics.values())
        total_skip = sum(m.get("skipped", 0) + m.get("deferred", 0) + m.get("permanent_failure", 0) for m in metrics.values())
        total_fail = sum(m["failed"] for m in metrics.values())
        attempted = total_jobs - total_skip
        overall_rate = round(total_ok / attempted * 100, 1) if attempted > 0 else 0
        lines.append("## 24h Metrics")
        lines.append("")
        lines.append(
            f"Jobs: **{total_jobs}** | Success: **{total_ok}** | "
            f"Skipped: **{total_skip}** | Failed: **{total_fail}** | "
            f"Rate: **{overall_rate}%**"
        )
        if total_fail > 0:
            lines.append(
                f"\nUse `scheduler(action='logs')` to see failure details."
            )
        lines.append("")

    # ── Brand Automation ──
    brand_section = await _brand_automation_section(stats, mode=mode)
    if brand_section:
        lines.append(brand_section)
        lines.append("")

    # ── Cloud Status ──
    cloud_section = await _cloud_status_section()
    if cloud_section:
        lines.append(cloud_section)

    # ── Quick Actions ──
    lines.append("## Quick Actions")
    if observing:
        # The cloud toggle is deliberately absent here: it commissions backend
        # sending, which is the opposite of what this mode is for.
        lines.append("- `scheduler(action='toggle', enabled=True)` \u2014 leave observe and start sending")
        lines.append("- `scheduler(action='toggle', enabled=False)` \u2014 stop collecting too")
    else:
        lines.append("- `scheduler(action='observe')` \u2014 collect signals without sending")
        lines.append("- `scheduler(action='toggle', enabled=False)` \u2014 pause the scheduler")
        lines.append("- `scheduler(action='toggle', enabled=True, cloud=True)` \u2014 enable cloud 24/7 scheduling")
    lines.append("- `scheduler(action='logs')` \u2014 detailed event log and job metrics")
    lines.append("- `scheduler(action='diagnostics')` \u2014 full system diagnostics")
    lines.append("- `show_status()` \u2014 view campaign dashboard")
    lines.append("- `campaign(action='emergency_stop')` \u2014 pause all campaigns immediately")

    from ..services.dashboard_snapshot import status_footer

    lines.extend(status_footer("scheduler", snapshot=False))
    return "\n".join(lines)


async def _brand_automation_section(
    stats: dict[str, Any], *, mode: str = "full",
) -> str:
    """Build brand automation status section if brand plan exists.

    Args:
        stats: Scheduler stats, used for the next scheduled brand jobs.
        mode: The current scheduler mode. Brand posts and engagements are
            outbound, so the tick returns before scheduling them in observe and
            runs_in_mode refuses any row already queued — the section must not
            claim they run. Taking the mode rather than a bool keeps that
            decision on runs_in_mode, the same rule the executor applies,
            instead of a second hand-maintained list of what observe allows.
    """
    from ..formatter import progress_bar

    from ..db.async_bridge import run_db
    from ..services.brand_service import load_brand_plan
    brand_plan = await run_db(load_brand_plan)
    if not brand_plan or not isinstance(brand_plan, dict) or not brand_plan.get("weeks"):
        return ""

    total = sum(len(w.get("actions", [])) for w in brand_plan.get("weeks", []))
    done = sum(
        1 for w in brand_plan.get("weeks", [])
        for a in w.get("actions", []) if a.get("status") == "completed"
    )

    lines = [
        "## \U0001f3af Brand Automation",
        "",
        f"**Progress**: {done}/{total} actions {progress_bar(done, total, 15)}",
    ]

    # Show next scheduled brand jobs from stats. In observe mode a queued
    # brand_* row is not a plan: runs_in_mode refuses it, so listing "next
    # brand_post in 5 min" would promise a post that cannot happen.
    from ..scheduler.engine import runs_in_mode

    observing = mode == "observe"
    next_jobs = stats.get("next_jobs", [])
    brand_jobs = [
        j for j in next_jobs
        if j.get("job_type", "").startswith("brand_")
        and runs_in_mode(j.get("job_type", ""), mode)
    ]
    if brand_jobs:
        now = int(time.time())
        for job in brand_jobs[:3]:
            delta = job["scheduled_at"] - now
            if delta > 0:
                mins = delta // 60
                when = f"in {mins} min" if mins > 0 else "now"
            else:
                when = "overdue"
            lines.append(f"- Next **{job['job_type']}** {when}")
    elif observing:
        lines.append(
            "- Paused: posts and engagements are outbound, and observe mode "
            "does not send"
        )
    else:
        if done < total:
            lines.append("- Posts + engagements run automatically via scheduler")
        else:
            lines.append("- All actions complete! Re-analysis scheduled at 28-day mark")

    # Show automation status
    lines.append("")
    if observing:
        lines.append(
            "Automation resumes when you leave observe: "
            "`scheduler(action='toggle', enabled=True)`"
        )
    else:
        from ..services.brand_service import daily_comment_target, weekly_post_target

        weekly = weekly_post_target(brand_plan)
        daily = daily_comment_target(brand_plan)
        lines.append(
            f"Automation runs: posts ({weekly}/week), engagements ({daily}/day), "
            "re-analyze (every 28d)"
        )

    return "\n".join(lines)


async def _cloud_sending_caveat() -> str:
    """What observe mode cannot stop, stated plainly.

    Observe is a LOCAL mode. If the cloud scheduler is enabled, the backend
    already holds whatever campaigns were pushed to it and keeps sending from
    them every 5 minutes; withholding a NEW push does not retract an OLD one.
    Saying "nothing goes out" while that is running is the exact class of claim
    that this codebase has been rejected for repeatedly, so it is said here
    instead, next to the command that actually stops it.

    Returns "" when there is nothing to warn about — direct mode, cloud
    scheduler off, or the backend unreachable (in which case we do not know,
    and inventing reassurance would be worse than silence).
    """
    if not is_backend_mode():
        return ""
    try:
        from ..services.cloud_sync import get_cloud_scheduler_status

        cloud = await get_cloud_scheduler_status()
    except Exception:
        return ""
    if "error" in cloud or not cloud.get("enabled", False):
        return ""
    # Host already stood down (observe, stale mode, disabled gate). Warning
    # only when it is still allowed to send what observe cannot retract.
    if cloud.get("sending_allowed") is False:
        return ""

    held = [
        c for c in (cloud.get("campaigns") or [])
        if str(c.get("status", "")).lower() == "active"
    ]
    count = f"{len(held)} active campaign(s)" if held else "any campaign"
    return (
        "\n\n\u26a0\ufe0f **The cloud scheduler is still on, and observe does not "
        "reach it.** The backend already holds " + count + " and keeps sending "
        "from them roughly every 5 minutes. Observe stops this machine from "
        "sending; it does not retract what the backend was already given.\n"
        "  - `scheduler(action='toggle', enabled=False, cloud=True)` \u2014 stop cloud sending too"
    )


async def _dashboard_freshness() -> list[str]:
    """Freshness of the hosted dashboard, or nothing if it cannot be read.

    Purely local reads, so unlike _cloud_status_section() it still reports when
    the backend is unreachable — which is one of the ways the dashboard goes
    stale in the first place.
    """
    try:
        from ..services.cloud_sync import dashboard_freshness_lines

        return await dashboard_freshness_lines()
    except Exception as e:  # noqa: BLE001 — never break status over a status line
        logger.debug("Dashboard freshness read failed: %s", e)
        return []


async def _cloud_status_section() -> str:
    """Build cloud scheduler status section if in backend mode."""
    if not is_backend_mode():
        return ""

    try:
        from ..services.cloud_sync import get_cloud_scheduler_status

        cloud = await get_cloud_scheduler_status()
        if "error" in cloud:
            return f"## \u2601\ufe0f Cloud Scheduler\n\n\u26a0\ufe0f {cloud['error']}"

        cloud_enabled = cloud.get("enabled", False)
        pending = cloud.get("pending_jobs", 0)
        campaigns = cloud.get("campaigns", [])

        icon = "\U0001f7e2" if cloud_enabled else "\u23f8\ufe0f"
        lines = [
            f"## \u2601\ufe0f Cloud Scheduler {icon} {'Enabled' if cloud_enabled else 'Disabled'}",
            "",
        ]

        if cloud_enabled:
            lines.append(f"**Pending jobs**: {pending}")
            if campaigns:
                lines.append(f"**Cloud campaigns**: {len(campaigns)}")
                for c in campaigns[:5]:
                    lines.append(f"  - {c.get('name', 'Unknown')} ({c.get('status', '?')})")
            lines.append("")
            lines.append("The backend processes outreach every 5 min, 24/7.")

            # Recent cloud activity
            recent = cloud.get("recent_activity", [])
            if recent:
                lines.append("")
                lines.append("**Recent cloud activity:**")
                for job in recent[:3]:
                    jtype = job.get("job_type", "?")
                    status = job.get("status", "?")
                    icon = "\u2705" if status == "completed" else "\u274c"
                    lines.append(f"  {icon} {jtype} ({status})")
        else:
            lines.append(
                "Cloud scheduling is off. Enable with:\n"
                "`toggle_scheduler(enabled=True, cloud=True)`"
            )

        return "\n".join(lines)
    except Exception as e:
        logger.debug("Cloud status check failed: %s", e)
        return ""


async def run_toggle_scheduler(enabled: bool, cloud: bool = False) -> str:
    """Enable or disable the autonomous scheduler.

    Args:
        enabled: Whether to enable (True) or disable (False).
        cloud: If True, toggle the cloud (backend) scheduler for 24/7 operation.
               If False, toggle the local (in-process) scheduler.
    """
    if cloud:
        from ..config import set_sending_host
        from ..services.cloud_sync import toggle_cloud_scheduler

        if enabled:
            set_sending_host(
                "cloud", caller="mcp_tool",
                reason="scheduler(action='toggle', cloud=True)",
            )
        return await toggle_cloud_scheduler(enabled)

    # Local scheduler toggle. Compared against the MODE, not against
    # is_scheduler_enabled(): that reads False in observe as well as in off, so
    # a user watching signals who ran toggle(enabled=False) to stop collecting
    # was told "already disabled" and nothing changed.
    was_mode = get_scheduler_mode()
    target = "full" if enabled else "off"

    if was_mode == target:
        state = "enabled" if enabled else "disabled"
        return f"Scheduler is already {state}. No change made."

    action_label = "enabled" if enabled else "disabled"
    set_scheduler_mode(
        target,
        caller="mcp_tool",
        reason=f"User {action_label} via scheduler(action='toggle')",
    )
    left_observe = was_mode == "observe"

    # Resume first. Since heylead-api #328 only POST /campaigns/{id}/resume
    # moves a stopped cloud campaign to active, and resume/launch/monitor in
    # observe skipped that call on purpose. The catch-up push's status refresh
    # must see the cloud after those resumes, not before: a stale "paused"
    # would otherwise look like a dashboard pause. Each campaign's outcome is
    # printed, so a refusal never reads as sending.
    cloud_resumes = ""
    if enabled and left_observe:
        try:
            from .campaign_control import (
                format_cloud_resume_report,
                resume_observed_campaigns_in_cloud,
            )

            outcomes = await resume_observed_campaigns_in_cloud(
                caller="mcp_tool", reason="left_observe",
            )
            cloud_resumes = format_cloud_resume_report(outcomes)
        except Exception as e:  # noqa: BLE001 - never block the toggle
            logger.warning("Cloud resume after leaving observe failed: %s", e)
            cloud_resumes = (
                "\u26a0\ufe0f Active campaigns could not be resumed in the cloud "
                f"({e}), so they will not send from there. Retry each with "
                "campaign(action='resume', campaign_id='...').\n\n"
            )

    # Observe withheld this machine's active autopilot campaigns from the cloud
    # push, so the dashboard has been frozen on whatever it last received. The
    # periodic push would only correct them whenever it next ran; catch it up
    # now, while the user is here to be told. Best effort — a sync failure must
    # not make it look like the scheduler failed to start.
    caught_up = ""
    if enabled and left_observe:
        try:
            from ..services.cloud_sync import sync_to_cloud

            result = await sync_to_cloud()
            pushed = result.get("campaigns")
            if pushed:
                caught_up = (
                    f"Dashboard caught up: {pushed} campaign(s) pushed after being "
                    "withheld in observe.\n\n"
                )
            elif result.get("error"):
                caught_up = (
                    f"⚠️ Dashboard not caught up yet ({result['error']}). Its "
                    "figures stay stale until the next push; local state is "
                    "correct meanwhile.\n\n"
                )
        except Exception as e:  # noqa: BLE001 — never block the toggle
            logger.warning("Catch-up push after leaving observe failed: %s", e)
            caught_up = (
                "⚠️ Dashboard not caught up yet — the push failed. Its figures "
                "stay stale until the next one; local state is correct.\n\n"
            )

    if enabled:
        return (
            "\U0001f7e2 **Scheduler enabled!**\n\n"
            + (
                "Observe mode is off \u2014 the scheduler now sends as well as "
                "collects.\n\n" if left_observe else ""
            ) + caught_up + cloud_resumes +
            "The scheduler will now automatically process outreach for **autopilot** campaigns:\n"
            "- Send invitations (20-40 min randomized delays)\n"
            "- Send follow-ups (on schedule: day 1, 3, 7, 14)\n"
            "- Check for replies (every 5 min)\n"
            "- Engage with prospect posts (every 30 min)\n\n"
            "All actions respect working hours, rate limits, and daily caps.\n"
            "Copilot campaigns still require manual approval.\n\n"
            "Use `scheduler(action='status')` to monitor progress.\n\n"
            "\U0001f4a1 **Tip**: For 24/7 scheduling (even when laptop is off), use:\n"
            "`scheduler(action='toggle', enabled=True, cloud=True)`"
        )
    else:
        return (
            "\u23f8\ufe0f **Scheduler disabled.**\n\n"
            + (
                "Signal collection has stopped too \u2014 observe mode is "
                "off.\n\n" if left_observe else ""
            ) +
            "Autonomous outreach has been paused. No new jobs will be scheduled.\n"
            "Already-running jobs will complete, but no new ones will start.\n\n"
            "Your campaigns are still active \u2014 you can manually send with:\n"
            "- `generate_and_send()` \u2014 send an invitation\n"
            "- `send_message(action='followup')` \u2014 send a follow-up\n"
            "- `check_replies()` \u2014 check for replies\n\n"
            "Use `scheduler(action='toggle', enabled=True)` to re-enable."
        )


def _cloud_scheduler_reports_disabled(msg: str) -> bool:
    """True only for toggle_cloud_scheduler(False)'s single success string.

    Every failure path in cloud_sync.toggle_cloud_scheduler returns a
    "... failed: ..." sentence rather than raising, so the only safe test
    for "the cloud really is off" is its success wording.
    """
    return "**disabled**" in (msg or "")


async def run_send_from(host: str) -> str:
    """Move all campaign outbound to the cloud or to this machine."""
    from .. import config
    from ..config import set_sending_host, set_scheduler_mode
    from ..services.cloud_sync import toggle_cloud_scheduler

    host = str(host or "").strip().lower()
    if host not in ("cloud", "local"):
        return "host must be 'cloud' or 'local'."

    if not config.is_backend_mode():
        if host == "cloud":
            return (
                "This install has no hosted account. Sending stays on this "
                "machine."
            )
        set_sending_host("local", caller="mcp_tool", reason="send_from local")
        set_scheduler_mode(
            "full", caller="mcp_tool", reason="send_from local",
        )
        return "Sending is on this machine. There is no cloud scheduler here."

    if host == "local":
        # Switch the cloud off FIRST. Until 9 Sep 2026 this set
        # sending_host=local and scheduler_mode=full and only then called
        # toggle_cloud_scheduler(False), which returns a plain
        # "... failed: ..." string on any HTTP error and rolls nothing back.
        # A backend blip therefore left a laptop that sends AND a cloud that
        # still sends -- the delivery-level duplicate, by configuration
        # (duplicate-intro review).
        cloud_msg = await toggle_cloud_scheduler(False)
        if not _cloud_scheduler_reports_disabled(cloud_msg):
            return (
                "Sending was NOT moved. The cloud scheduler could not be "
                "switched off, so it is still the sender and this machine "
                "stays on standby \u2014 moving first would have left both "
                "sending.\n\n"
                f"{cloud_msg}\n\n"
                "Retry `scheduler(action='send_from', host='local')` once the "
                "backend answers."
            )
        set_sending_host("local", caller="mcp_tool", reason="send_from local")
        set_scheduler_mode(
            "full", caller="mcp_tool", reason="send_from local",
        )
        return (
            "Sending moved to this machine.\n\n"
            f"{cloud_msg}\n\n"
            "Every active campaign now sends from here. Move it back with "
            "`scheduler(action='send_from', host='cloud')`."
        )

    set_sending_host("cloud", caller="mcp_tool", reason="send_from cloud")
    cloud_msg = await toggle_cloud_scheduler(True)
    return (
        "Sending moved to the cloud.\n\n"
        f"{cloud_msg}\n\n"
        "This machine stands down for campaign outbound. Move it back with "
        "`scheduler(action='send_from', host='local')`."
    )


async def run_set_observe_mode() -> str:
    """Switch the scheduler to observe: collect and classify, never send.

    'observe' existed in config.get_scheduler_mode() and in every gate that
    reads it, but nothing in src/ ever wrote the key, so the only way to reach
    it was to hand-edit config.json. This is the way in.
    """
    was_mode = get_scheduler_mode()
    if was_mode == "observe":
        return (
            "Scheduler is already in observe mode. No change made.\n\n"
            "- `scheduler(action='status')` \u2014 what is being collected\n"
            "- `scheduler(action='toggle', enabled=True)` \u2014 leave observe and start sending\n"
            "- `scheduler(action='toggle', enabled=False)` \u2014 stop collecting too"
        )

    set_scheduler_mode(
        "observe",
        caller="mcp_tool",
        reason="User switched to observe via scheduler(action='observe')",
    )

    body = (
        f"\U0001f440 **Scheduler set to observe.** (was: {was_mode})\n\n"
        "It keeps reading and stops acting:\n"
        "- Prospect and network post scans\n"
        "- Watchlist collectors (keywords, competitors, hiring, news)\n"
        "- Signal classification, scoring, decay and re-matching\n"
        "- Reply checks\n\n"
        "This machine sends nothing: no invitations, follow-up DMs or "
        "engagements, and no signal activation, which is what enrols people "
        "into campaigns.\n\n"
        "It also hands the backend no new go-signal. Active autopilot campaigns "
        "are withheld from the cloud push, and nothing issues the /resume that "
        "would commission cloud sending — not launch, monitor or resume, "
        "and not the scheduler's own auto-resume of limit-paused campaigns. "
        "(Active copilot campaigns still sync: the backend only schedules "
        "autopilot, so they are history rather than an instruction.)\n\n"
        "\u26a0\ufe0f **The dashboard goes stale while this is on.** Withholding "
        "those campaigns means heylead.dev stops receiving their updates, so "
        "their numbers freeze at whatever was last pushed and keep being shown "
        "as if current. Local state stays correct \u2014 `show_status()` and "
        "`analytics()` read it directly. Leaving observe pushes them again.\n\n"
        "Inbound messages are still received and replies still checked \u2014 "
        "observation is the point of this mode.\n\n"
        "- `campaign(action='launch')` \u2014 activate a draft campaign; in observe it only collects\n"
        "- `scheduler(action='status')` \u2014 what is being collected\n"
        "- `scheduler(action='toggle', enabled=True)` \u2014 leave observe and start sending\n"
        "- `scheduler(action='toggle', enabled=False)` \u2014 stop collecting too"
    )
    return body + await _cloud_sending_caveat()


async def run_toggle_always_on(enabled: bool) -> str:
    """Enable or disable scheduler always-on mode.

    When enabled, the scheduler automatically re-enables itself if anything
    turns it off, and sends an immediate email alert with attribution.
    """
    from ..config import (
        is_scheduler_always_on,
        set_scheduler_always_on,
    )

    was_on = is_scheduler_always_on()
    if enabled == was_on:
        state = "enabled" if enabled else "disabled"
        return f"Scheduler always-on mode is already {state}. No change made."

    set_scheduler_always_on(enabled)

    if enabled:
        # Also ensure scheduler is enabled when turning on always-on.
        # Not from observe: that mode is an explicit "collect, do not send", and
        # always-on exists to undo accidental stops, not deliberate ones.
        mode = get_scheduler_mode()
        if mode == "off":
            set_scheduler_mode(
                "full",
                caller="always_on_activation",
                reason="Auto-enabled when always-on mode was activated",
            )

        # Sync always_on to cloud if in backend mode
        await _sync_always_on_to_cloud(True)

        closing = (
            "The scheduler stays in observe mode: always-on re-enables from "
            "'off', and leaves a deliberate observe alone. Nothing sends until "
            "`scheduler(action='toggle', enabled=True)`."
            if mode == "observe"
            else "This ensures your campaigns never silently stop running."
        )

        return (
            "**Scheduler always-on mode ENABLED.**\n\n"
            "The scheduler will now:\n"
            "- Automatically re-enable itself if anything turns it off\n"
            "- Send an immediate email alert when a disable event is detected\n"
            "- Include full attribution (who/what/why) in the alert\n\n"
            f"{closing}\n\n"
            "To disable: `scheduler(action='always_on', enabled=False)`"
        )
    else:
        # Sync always_on to cloud
        await _sync_always_on_to_cloud(False)

        return (
            "**Scheduler always-on mode DISABLED.**\n\n"
            "The scheduler can now be freely toggled on/off without auto-re-enable.\n"
            "No alerts will be sent when the scheduler is disabled."
        )


async def _sync_always_on_to_cloud(enabled: bool) -> None:
    """Sync the always_on preference to the backend (if in backend mode)."""
    from ..config import is_backend_mode

    if not is_backend_mode():
        return

    try:
        import httpx
        from ..services.cloud_sync import _base_url, _headers

        base = _base_url()
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                f"{base}/api/v1/scheduler/always-on",
                json={"always_on": enabled},
                headers=_headers(),
            )
    except Exception as e:
        logger.debug("Failed to sync always_on to cloud: %s", e)


async def run_scheduler_logs(
    hours: int = 24,
    event_type: str = "",
    campaign_id: str = "",
    cloud: bool = False,
) -> str:
    """Show scheduler event log with metrics and recent failures.

    Args:
        hours: Lookback window in hours (default 24).
        event_type: Filter by event type (e.g. 'job_failed').
        campaign_id: Filter by campaign ID.
        cloud: Read the hosted scheduler's log even from a local workspace.

    A hosted workspace's events are written by the cloud, not this machine:
    until 24 Sep 2026 this read the local SQLite log only and answered "No
    events recorded" against 20,000 hosted events, while its siblings
    (activity, diagnostics) already asked the backend. Same rule here.
    """
    from ..config import is_backend_mode
    if cloud or is_backend_mode():
        try:
            return await _run_logs_from_backend(hours, event_type, campaign_id)
        except Exception as e:
            if cloud:
                return f"Could not read the cloud scheduler's log: {e}"
            logger.debug("Backend event log failed, falling back to local: %s", e)

    summary = await db.get_scheduler_event_summary(hours)
    metrics = await db.get_job_metrics(hours)
    failures = await db.get_recent_scheduler_events(
        hours=hours, event_type="job_failed",
        campaign_id=campaign_id, limit=10,
    )

    lines = [f"# Scheduler Events ({hours}h)", ""]

    # ── Event Summary ──
    if summary:
        parts = [f"{etype}: **{cnt}**" for etype, cnt in summary.items()]
        lines.append(" | ".join(parts))
        lines.append("")
    else:
        lines.append("No events recorded in this window.")
        lines.append("")
        return "\n".join(lines)

    # ── Job Metrics Table ──
    if metrics:
        lines.append("## Job Metrics")
        lines.append("")
        lines.append("| Type | Total | OK | Skip | Fail | Rate | Avg Duration |")
        lines.append("|------|-------|----|------|------|------|-------------|")
        for jtype, m in sorted(metrics.items(), key=lambda x: x[1]["total"], reverse=True):
            avg_dur = m["avg_duration_ms"]
            dur_str = f"{avg_dur / 1000:.1f}s" if avg_dur else "—"
            # Combine skipped + deferred + permanent_failure into "Skip" column
            skip_count = m.get("skipped", 0) + m.get("deferred", 0) + m.get("permanent_failure", 0)
            lines.append(
                f"| {jtype} | {m['total']} | {m['success']} | {skip_count} | {m['failed']} "
                f"| {m['success_rate']}% | {dur_str} |"
            )
        lines.append("")

    # ── Recent Failures ──
    if failures:
        lines.append("## Recent Failures")
        lines.append("")
        for evt in failures[:10]:
            ts = evt.get("created_at", 0)
            t = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "?"
            ctx = evt.get("context", {})
            jtype = ctx.get("job_type", "?")
            cat = ctx.get("error_category", "")
            err = ctx.get("error", "")[:80]
            cat_tag = f" [{cat}]" if cat else ""
            job_id = (evt.get("job_id") or "")[:8]
            lines.append(f"- [{t}] {jtype} {job_id}{cat_tag}: {err}")
        lines.append("")

    # ── API & LLM Call Summary (from execution traces) ──
    trace_events = await db.get_recent_scheduler_events(
        hours=hours, event_type="job_trace", campaign_id=campaign_id, limit=50,
    )
    trace_error_events = await db.get_recent_scheduler_events(
        hours=hours, event_type="job_trace_error", campaign_id=campaign_id, limit=50,
    )
    all_traces = trace_events + trace_error_events
    if all_traces:
        api_total = 0
        api_errors = 0
        slow_calls = []
        llm_total = 0
        llm_errors = 0
        for evt in all_traces:
            ctx = evt.get("context", {})
            if isinstance(ctx, str):
                try:
                    ctx = json.loads(ctx)
                except Exception:
                    ctx = {}
            for call in ctx.get("api_calls", []):
                api_total += 1
                sc = call.get("status", 200)
                if isinstance(sc, int) and sc >= 400:
                    api_errors += 1
                if call.get("ms", 0) > 3000:
                    slow_calls.append(call)
            for call in ctx.get("llm_calls", []):
                llm_total += 1
                if not call.get("ok", True):
                    llm_errors += 1

        lines.append("")
        lines.append("## API Calls")
        lines.append("")
        lines.append(f"Total: **{api_total}** | Errors: **{api_errors}** | Slow (>3s): **{len(slow_calls)}**")
        for c in slow_calls[:5]:
            lines.append(f"  - {c.get('endpoint', '?')} → {c.get('status', '?')} ({c.get('ms', 0)}ms)")

        if llm_total > 0:
            lines.append("")
            lines.append("## LLM Calls")
            lines.append("")
            lines.append(f"Total: **{llm_total}** | Errors: **{llm_errors}**")

    # ── Quick Actions ──
    lines.append("## Quick Actions")
    lines.append("- `scheduler(action='logs', hours=48)` — wider window")
    lines.append("- `scheduler(action='diagnostics')` — full system diagnostics")
    lines.append("- `campaign(action='retry_failed')` — retry failed outreaches")

    return "\n".join(lines)


async def _run_logs_from_backend(hours: int, event_type: str, campaign_id: str) -> str:
    """Fetch the hosted event log (GET /scheduler/events) and format it."""
    import httpx
    from ..services.cloud_sync import _base_url, _headers

    base = _base_url()
    params: dict = {"hours": hours, "limit": 200}
    if event_type:
        params["event_type"] = event_type
    if campaign_id:
        params["campaign_id"] = campaign_id
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        resp = await client.get(
            f"{base}/api/v1/scheduler/events", params=params, headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
    return _format_backend_logs(data, hours, event_type=event_type, campaign_id=campaign_id)


def _format_backend_logs(
    data: dict, hours: int, *, event_type: str, campaign_id: str,
) -> str:
    """The cloud's log in the local log's shape, headed so the reader knows
    whose log it is."""
    summary = data.get("summary") or {}
    events = data.get("events") or []
    scope = f", campaign {campaign_id[:8]}" if campaign_id else ""
    lines = [f"# Scheduler Events ({hours}h, cloud{scope})", ""]
    if not summary and not events:
        lines.append("No events recorded in this window by the cloud scheduler.")
        return "\n".join(lines)
    if summary:
        ordered = sorted(summary.items(), key=lambda kv: -int(kv[1] or 0))
        lines.append(" | ".join(f"{etype}: **{cnt}**" for etype, cnt in ordered))
        lines.append("")
    failures = [e for e in events if e.get("event_type") == "job_failed"]
    if failures:
        lines.append("## Recent Failures")
        lines.append("")
        for evt in failures[:10]:
            ts = evt.get("created_at", 0)
            t = datetime.fromtimestamp(ts).strftime("%d %b %H:%M") if ts else "?"
            ctx = evt.get("context") or {}
            if isinstance(ctx, str):
                try:
                    ctx = json.loads(ctx)
                except Exception:
                    ctx = {}
            jtype = evt.get("job_type") or ctx.get("job_type", "?")
            cat = ctx.get("error_category", "")
            err = str(evt.get("error") or ctx.get("error", ""))[:80]
            cat_tag = f" [{cat}]" if cat else ""
            job_id = (evt.get("job_id") or "")[:8]
            lines.append(f"- [{t}] {jtype} {job_id}{cat_tag}: {err}")
        lines.append("")
    if data.get("has_more"):
        lines.append(f"_More events than shown; narrow with event_type or campaign_id._")
        lines.append("")
    lines.append("## Quick Actions")
    lines.append("- `scheduler(action='logs', hours=48)` — wider window")
    lines.append("- `scheduler(action='diagnostics')` — full system diagnostics")
    lines.append("- `scheduler(action='activity')` — what actually happened on LinkedIn")
    return "\n".join(lines)


async def run_scheduler_activity(
    hours: int = 24,
    campaign_id: str = "",
) -> str:
    """Comprehensive activity report: real DB results, skip reasons, verification.

    Shows what actually happened on LinkedIn (from outreaches/engagements/messages),
    what actions were NOT taken and why, and verification status.

    Args:
        hours: Lookback window in hours (default 24).
        campaign_id: Filter by campaign ID.
    """
    # Try backend mode first
    from ..config import is_backend_mode
    if is_backend_mode():
        try:
            return await _run_activity_from_backend(hours, campaign_id)
        except Exception as e:
            logger.debug("Backend activity report failed, falling back to local: %s", e)

    lines = [f"# Activity Report ({hours}h)", ""]

    # ── 1. Real LinkedIn Results (from actual tables) ──
    changes = await db.get_outreach_changes(hours, campaign_id)
    lines.append("## Verified LinkedIn Results")
    lines.append("")
    inv_verified = changes['invited']
    inv_pending = changes.get("invited_pending", 0)
    inv_total = inv_verified + inv_pending
    if inv_pending > 0 and inv_verified > 0:
        inv_line = f"- Invitations sent: **{inv_total}** ({inv_verified} verified, {inv_pending} pending)"
    elif inv_pending > 0:
        inv_line = f"- Invitations sent: **{inv_total}** (pending verification)"
    else:
        inv_line = f"- Invitations sent: **{inv_verified}**"
    lines.append(inv_line)
    lines.append(f"- Connections accepted: **{changes['accepted']}**")
    lines.append(f"- Replies received: **{changes['replied']}**")
    lines.append(f"- Messages sent: **{changes['messages_sent']}** | received: **{changes['messages_received']}**")

    eng = changes.get("engagements", {})
    eng_pending = changes.get("engagements_pending", {})
    if eng:
        eng_parts = []
        for atype, cnt in sorted(eng.items(), key=lambda x: -x[1]):
            part = f"{cnt} {atype}{'s' if cnt != 1 else ''}"
            pend = eng_pending.get(atype, 0)
            if pend > 0:
                part += f" (+{pend}?)"
            eng_parts.append(part)
        lines.append(f"- Engagements: {', '.join(eng_parts)}")
    else:
        total_pend = sum(eng_pending.values()) if eng_pending else 0
        if total_pend > 0:
            lines.append(f"- Engagements: 0 verified ({total_pend} pending)")
        else:
            lines.append("- Engagements: 0")
    lines.append("")

    # ── 2. Actions Taken (from actions_log) ──
    actions = await db.get_actions_taken(hours, campaign_id)
    if actions:
        lines.append("## Actions Taken")
        lines.append("")
        lines.append("| Action | Total | Success | Error | Blocked | Other |")
        lines.append("|--------|-------|---------|-------|---------|-------|")
        for action_type, counts in sorted(actions.items(), key=lambda x: -x[1]["total"]):
            total = counts["total"]
            success = counts.get("success", 0)
            error = counts.get("error", 0)
            blocked = counts.get("blocked", 0)
            other = total - success - error - blocked
            lines.append(
                f"| {action_type} | {total} | {success} | {error} | {blocked} | {other if other > 0 else '—'} |"
            )
        lines.append("")

    # ── 3. Actions NOT Taken (skip reasons) ──
    skips = await db.get_actions_skipped_detailed(hours, campaign_id)
    if skips:
        lines.append("## Actions NOT Taken (skip reasons)")
        lines.append("")
        lines.append("| Reason | Count | Actions Affected |")
        lines.append("|--------|-------|-----------------|")
        for reason, action_counts in sorted(skips.items(), key=lambda x: -sum(x[1].values())):
            total = sum(action_counts.values())
            affected = ", ".join(sorted(action_counts.keys()))
            lines.append(f"| {reason} | {total} | {affected} |")
        lines.append("")
    else:
        lines.append("## Actions NOT Taken")
        lines.append("")
        lines.append("No skips recorded in this window.")
        lines.append("")

    # ── 4. Verification Status ──
    verification = await db.get_verification_summary(hours)
    confirmed = verification.get("confirmed", 0)
    unconfirmed = verification.get("unconfirmed", 0)
    pending = verification.get("pending", 0)
    checked = confirmed + unconfirmed
    if checked > 0 or pending > 0:
        lines.append("## Verification")
        lines.append("")
        lines.append(f"- Checked: **{checked}** | Confirmed: **{confirmed}** | Unconfirmed: **{unconfirmed}**")
        if pending > 0:
            lines.append(f"- Pending verification: **{pending}** (invited recently, not yet checked)")
        lines.append("")

    # ── Execution Traces ──
    trace_events = await db.get_recent_scheduler_events(
        hours=hours, event_type="job_trace", campaign_id=campaign_id, limit=20,
    )
    trace_error_events = await db.get_recent_scheduler_events(
        hours=hours, event_type="job_trace_error", campaign_id=campaign_id, limit=20,
    )
    all_traces = sorted(
        trace_events + trace_error_events,
        key=lambda e: e.get("created_at", 0),
        reverse=True,
    )[:10]
    if all_traces:
        lines.append("")
        lines.append("## Execution Traces")
        lines.append("")
        for evt in all_traces:
            ctx = evt.get("context", {})
            if isinstance(ctx, str):
                try:
                    ctx = json.loads(ctx)
                except Exception:
                    ctx = {}
            ts = evt.get("created_at", 0)
            t = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "?"
            jtype = ctx.get("job_type", "?")
            dur = ctx.get("total_ms", 0)
            steps = ctx.get("steps", [])
            errors = ctx.get("errors", [])
            api_count = len(ctx.get("api_calls", []))
            llm_count = len(ctx.get("llm_calls", []))
            is_error = bool(errors)

            step_str = " → ".join(s.get("name", "?") for s in steps)
            err_tag = " **ERROR**" if is_error else ""

            lines.append(f"- [{t}] **{jtype}** {dur}ms | {api_count} API, {llm_count} LLM{err_tag}")
            if step_str:
                lines.append(f"  Steps: {step_str}")
            for d in ctx.get("decisions", []):
                lines.append(f"  Decision: {d.get('decision', '?')} — {d.get('reason', '?')}")
            for err in errors:
                lines.append(f"  Error [{err.get('type', '?')}]: {err.get('msg', '?')[:120]}")
            lines.append("")

    # ── Quick Actions ──
    lines.append("## Quick Actions")
    lines.append(f"- `scheduler(action='activity', hours={hours * 2})` — wider window")
    lines.append("- `scheduler(action='logs')` — raw scheduler event log")
    lines.append("- `scheduler(action='diagnostics')` — system health")
    lines.append("- `campaign(action='retry_failed')` — retry failed outreaches")

    return "\n".join(lines)


async def _run_activity_from_backend(hours: int, campaign_id: str) -> str:
    """Fetch activity report from backend API and format it."""
    import httpx
    from ..services.cloud_sync import _base_url, _headers

    base = _base_url()
    params: dict = {"hours": hours}
    if campaign_id:
        params["campaign_id"] = campaign_id

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        resp = await client.get(
            f"{base}/api/v1/scheduler/activity",
            params=params,
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    return _format_backend_activity(data, hours)


def _format_backend_activity(data: dict, hours: int) -> str:
    """Render /scheduler/activity JSON the same way the local report does."""
    lines = [f"# Activity Report ({hours}h)", ""]

    changes = data.get("changes", {})
    lines.append("## Real LinkedIn Results")
    lines.append("")
    lines.append(f"- Invitations sent: **{changes.get('invited', 0)}**")
    lines.append(f"- Connections accepted: **{changes.get('accepted', 0)}**")
    lines.append(f"- Replies received: **{changes.get('replied', 0)}**")
    lines.append(f"- Messages sent: **{changes.get('messages_sent', 0)}** | received: **{changes.get('messages_received', 0)}**")
    eng = changes.get("engagements", {})
    if eng:
        eng_parts = [f"{cnt} {atype}{'s' if cnt != 1 else ''}" for atype, cnt in sorted(eng.items(), key=lambda x: -x[1])]
        lines.append(f"- Engagements: {', '.join(eng_parts)}")
    lines.append("")

    services = data.get("services") or []
    if services:
        lines.append("## Job Metrics")
        lines.append("")
        lines.append("| Type | Total | OK | Skip | Fail | Rate |")
        lines.append("|------|-------|----|------|------|------|")
        for svc in services:
            total = svc.get("total_jobs", 0)
            ok = svc.get("successful", 0)
            skip = svc.get("skipped", 0)
            fail = svc.get("failed", 0)
            rate = svc.get("success_rate", 0)
            lines.append(
                f"| {svc.get('job_type', '?')} | {total} | {ok} | {skip} | {fail} "
                f"| {rate}% |"
            )
        summary = data.get("summary") or {}
        if summary:
            lines.append("")
            lines.append(
                f"- Jobs: **{summary.get('total_jobs', 0)}** "
                f"({summary.get('successful', 0)} ok, "
                f"{sum(s.get('skipped', 0) for s in services)} skipped, "
                f"{summary.get('failed', 0)} failed — "
                f"{summary.get('success_rate', 0)}%)"
            )
        lines.append("")

    # Engagement limit warnings
    limits = data.get("engagement_limits", {})
    if limits:
        comment_today = limits.get("comments_today", 0)
        comment_limit = limits.get("comment_limit", 10)
        react_today = limits.get("reacts_today", 0)
        react_limit = limits.get("react_limit", 20)
        comment_blocked = limits.get("comment_blocked", False)
        unverified = limits.get("unverified_recent", 0)
        if comment_today >= comment_limit or comment_blocked or unverified > 0:
            lines.append("## Engagement Limits")
            lines.append("")
            lines.append(f"- Comments today: **{comment_today}/{comment_limit}**")
            lines.append(f"- Reactions today: **{react_today}/{react_limit}**")
            if comment_blocked:
                lines.append("- **Commenting paused** (LinkedIn limit detected — only reactions sent)")
            if unverified > 0:
                lines.append(f"- **{unverified} unverified** comments (sent but not confirmed on LinkedIn)")
            lines.append("")

    skips = data.get("skips", {})
    if skips:
        lines.append("## Actions NOT Taken (skip reasons)")
        lines.append("")
        lines.append("| Reason | Count | Actions Affected |")
        lines.append("|--------|-------|-----------------|")
        for reason, action_counts in sorted(skips.items(), key=lambda x: -sum(x[1].values())):
            total = sum(action_counts.values())
            affected = ", ".join(sorted(action_counts.keys()))
            lines.append(f"| {reason} | {total} | {affected} |")
        lines.append("")

    verification = data.get("verification", {})
    confirmed = verification.get("confirmed", 0)
    unconfirmed = verification.get("unconfirmed", 0)
    pending_v = verification.get("pending", 0)
    checked = confirmed + unconfirmed
    if checked > 0 or pending_v > 0:
        lines.append("## Verification")
        lines.append("")
        lines.append(f"- Checked: **{checked}** | Confirmed: **{confirmed}** | Unconfirmed: **{unconfirmed}**")
        if pending_v > 0:
            lines.append(f"- Pending verification: **{pending_v}**")
        lines.append("")

    lines.append("## Quick Actions")
    lines.append(f"- `scheduler(action='activity', hours={hours * 2})` — wider window")
    lines.append("- `scheduler(action='logs')` — raw scheduler event log")
    lines.append("- `scheduler(action='diagnostics')` — system health")

    return "\n".join(lines)


async def _followup_cap(campaign: dict[str, Any]) -> int:
    """The follow-ups this campaign can send one person, as the plan quotes it.

    ``campaign_plan.effective_max_followups`` over the campaign's config and
    this install's tier: the stored max_followups under the tier ceiling, 0
    when follow-ups are off. The api's diagnostics payload carries no config,
    so a hosted row falls back to the pulled local copy. This used to read the
    stored value with a default of 5, so "Eligible" and "Maxed" counted
    against a number no scheduler keeps (#1414).
    """
    from ..services.campaign_plan import effective_max_followups, local_tier

    raw = campaign.get("config_json")
    if not raw and campaign.get("id"):
        try:
            row = await db.get_campaign(campaign["id"])
            raw = (row or {}).get("config_json")
        except Exception as exc:
            logger.debug("Campaign %s config unreadable for diagnostics: %s", campaign.get("id"), exc)
    try:
        tier = local_tier()
    except Exception as exc:
        logger.debug("Tier unreadable for diagnostics: %s", exc)
        tier = ""  # effective_max_followups reads blank as Free
    return effective_max_followups(raw, tier)


async def run_scheduler_diagnostics(campaign_id: str = "") -> str:
    """Full diagnostics: activity breakdown, funnel, campaign state, health.

    Args:
        campaign_id: Focus on a specific campaign. Shows all if empty.
    """
    # Try backend mode first (local SQLite is stale in backend mode)
    if is_backend_mode():
        try:
            return await _run_diagnostics_from_backend(campaign_id)
        except Exception as e:
            logger.warning("Backend diagnostics failed, falling back to local: %s", e)
            local = await _format_diagnostics_local(campaign_id)
            warning = (
                "\n> **Note:** Could not reach backend for diagnostics"
                f" ({e}). Showing local view — cloud scheduler may still"
                " be running. Check `gcloud logging read` for live status.\n"
            )
            return local + warning

    return await _format_diagnostics_local(campaign_id)


async def _run_diagnostics_from_backend(campaign_id: str) -> str:
    """Fetch diagnostics from backend API and format with new layout."""
    import httpx
    from ..services.cloud_sync import _base_url, _headers

    base = _base_url()
    params: dict[str, str] = {}
    if campaign_id:
        params["campaign_id"] = campaign_id

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        resp = await client.get(
            f"{base}/api/v1/scheduler/diagnostics",
            params=params,
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    return await _format_diagnostics_backend(data, campaign_id)


async def _format_diagnostics_backend(data: dict[str, Any], campaign_id: str) -> str:
    """Format backend diagnostics JSON into the new markdown layout."""
    lines = ["# Scheduler Diagnostics", ""]

    # ── Today's Activity ──
    action_limits = data.get("action_limits", {})
    if action_limits:
        lines.append("## Today's Activity")
        lines.append("")
        lines.append("| Action | Done | Limit | Status |")
        lines.append("|--------|------|-------|--------|")
        display_order = [
            ("invite", "Invitations"),
            ("follow", "Follows"),
            ("comment", "Comments"),
            ("react", "Reactions"),
            ("endorse", "Endorsements"),
            ("followup", "Follow-ups"),
            ("send_dm", "DMs"),
            ("discover", "Searches"),
        ]
        for key, label in display_order:
            info = action_limits.get(key, {})
            used = info.get("used", 0)
            limit = info.get("limit", 0)
            blocked = info.get("blocked", False)
            if blocked:
                status = "BLOCKED"
            elif limit and used >= limit:
                status = "Limit reached"
            else:
                status = "OK"
            limit_str = f"{limit}/day" if limit else "—"
            lines.append(f"| {label} | {used} | {limit_str} | {status} |")
        lines.append("")

    # ── Outreach Funnel ──
    activity = data.get("today_activity", {})
    changes = activity.get("changes", {}) if isinstance(activity, dict) else {}
    if changes:
        eng = changes.get("engagements", {})
        follows = eng.get("follow", 0)
        comments = eng.get("comment", 0)
        reacts = eng.get("react", 0)
        engagements = comments + reacts
        lines.append("## Outreach Funnel (today)")
        lines.append("")
        lines.append(
            f"Follows: {follows} → Engagements: {engagements} → "
            f"Invites: {changes.get('invited', 0)} → "
            f"Connected: {changes.get('accepted', 0)} → "
            f"Replies: {changes.get('replied', 0)}"
        )
        lines.append("")

    # ── Campaign Breakdown ──
    campaigns = data.get("campaigns", [])
    if campaign_id:
        campaigns = [c for c in campaigns if c.get("id") == campaign_id]
    if campaigns:
        lines.append("## Campaign Breakdown")
        lines.append("")
        lines.append("| Campaign | Mode | Pending | Invited | Connected | Replied | Error |")
        lines.append("|----------|------|---------|---------|-----------|---------|-------|")
        for c in campaigns:
            name = (c.get("name") or "Unnamed")[:25]
            statuses = c.get("outreach_statuses", {})
            # Infer mode from parent data if not in campaign dict
            mode = c.get("mode", "?")
            pending = statuses.get("pending", 0)
            invited = statuses.get("invited", 0)
            connected = statuses.get("connected", 0)
            replied = statuses.get("replied", 0) + statuses.get("hot_lead", 0)
            errored = statuses.get("expired", 0)
            lines.append(
                f"| {name} | {mode} | {pending} | {invited} | "
                f"{connected} | {replied} | {errored} |"
            )
        lines.append("")
    else:
        lines.append("## Campaigns")
        lines.append("")
        lines.append("No active campaigns found.")
        lines.append("")

    # ── Follow-up Pipeline ──
    if campaigns:
        lines.append("## Follow-up Pipeline")
        lines.append("")
        lines.append("| Campaign | Connected | Messaged | Eligible | Maxed | Distribution |")
        lines.append("|----------|-----------|----------|----------|-------|-------------|")
        for c in campaigns:
            cid = c.get("id", "")
            name = (c.get("name") or "Unnamed")[:25]
            max_fu = await _followup_cap(c)
            bd = await db.get_followup_breakdown(cid, max_fu)
            connected = bd["by_status"].get("connected", 0)
            messaged = bd["by_status"].get("messaged", 0)
            dist_parts = [f"#{k}:{v}" for k, v in sorted(bd["distribution"].items())]
            lines.append(
                f"| {name} | {connected} | {messaged} | "
                f"{bd['eligible'] if max_fu else 'off'} | {bd['maxed_out']} | "
                f"{', '.join(dist_parts) or '-'} |"
            )
        lines.append("")

    # ── Scheduler Health ──
    health = data.get("scheduler_health", {})
    rl = data.get("rate_limit", {})
    enabled = data.get("active_campaigns", 0) > 0 or health.get("is_healthy", False)
    tick_age = health.get("tick_age_seconds", 0)
    is_healthy = health.get("is_healthy", False)

    lines.append("## Scheduler Health")
    lines.append("")
    blocked = rl.get("is_blocked", False)
    if blocked:
        remaining_min = rl.get("blocked_remaining_min", 0)
        lines.append(f"- Rate limit: **BLOCKED** ({remaining_min}m remaining)")

    if tick_age > 0:
        if tick_age < 60:
            age_str = f"{tick_age}s ago"
        elif tick_age < 3600:
            age_str = f"{tick_age // 60}m ago"
        else:
            age_str = f"{tick_age // 3600}h {(tick_age % 3600) // 60}m ago"
        health_str = "healthy" if is_healthy else "STALE"
        lines.append(f"- Last tick: **{age_str}** ({health_str})")
    else:
        lines.append("- Last tick: **unknown**")

    lines.append(f"- Mode: **backend**")
    jobs_r = health.get("jobs_running", 0)
    jobs_p = health.get("jobs_pending", 0)
    jobs_f = health.get("jobs_failed", 0)
    lines.append(f"- Queue: {jobs_r} running, {jobs_p} pending, {jobs_f} failed")
    lines.append("")

    # ── 24h Job Summary ──
    job_summary = data.get("job_summary", {})
    outreach = job_summary.get("outreach", {})
    housekeeping = job_summary.get("housekeeping", {})
    o_total = outreach.get("total", 0)
    h_total = housekeeping.get("total", 0)

    if o_total or h_total:
        lines.append("## 24h Job Summary")
        lines.append("")
        if o_total:
            o_ok = outreach.get("success", 0)
            o_fail = outreach.get("failed", 0)
            o_rate = round(o_ok / o_total * 100, 1) if o_total else 0
            lines.append(
                f"- Outreach: **{o_total}** ({o_ok} ok, {o_fail} failed — {o_rate}%)"
            )
            by_type = outreach.get("by_type", {})
            if by_type:
                parts = [f"{t}: {n}" for t, n in sorted(by_type.items(), key=lambda x: -x[1])]
                lines.append(f"  {', '.join(parts)}")
        if h_total:
            h_ok = housekeeping.get("success", 0)
            h_fail = housekeeping.get("failed", 0)
            if h_fail:
                lines.append(f"- Background: **{h_total}** ({h_ok} ok, {h_fail} failed)")
            else:
                lines.append(f"- Background: **{h_total}** (all ok)")
        lines.append("")

    # ── Issues ──
    issues: list[str] = []
    if blocked:
        issues.append("Rate limit BLOCKED — no invitations until cooldown expires")
    if not is_healthy and tick_age > 0:
        issues.append(f"Last tick was {tick_age // 60}m ago — scheduler may be stuck")
    if jobs_r > 10:
        issues.append(f"{jobs_r} jobs running — possible stuck jobs")
    if jobs_f > 0:
        issues.append(f"{jobs_f} failed jobs — `campaign(action='retry_failed')` to retry")

    if issues:
        lines.append("## Issues")
        lines.append("")
        for issue in issues:
            lines.append(f"- {issue}")
    else:
        lines.append("## Status: All Clear")
        lines.append("No issues detected.")

    return "\n".join(lines)


async def _format_diagnostics_local(campaign_id: str) -> str:
    """Format diagnostics from local SQLite (fallback for non-backend mode)."""
    now = int(time.time())
    lines = ["# Scheduler Diagnostics", ""]

    # ── Rate Limit Budget ──
    rl = await db.get_rate_limit_today()
    blocked = rl.get("blocked", False)
    from ..linkedin.rate_limiter import invite_limits_for_display

    _, daily_limit = await invite_limits_for_display(rl)
    sent = rl.get("sent", 0)
    remaining = max(0, daily_limit - sent)

    budget = await db.get_rate_limit_budget()
    lines.append("## Rate Limit Budget")
    lines.append("")
    lines.append("| Action | Used | Limit | Remaining | % Used |")
    lines.append("|--------|------|-------|-----------|--------|")
    display_order = [
        ("invitations", "Invitations"),
        ("follows", "Follows"),
        ("engagements", "Engagements"),
        ("profile_views", "Profile Views"),
        ("endorsements", "Endorsements"),
        ("followups", "Follow-ups"),
        ("dms", "DMs"),
    ]
    for key, label in display_order:
        b = budget.get(key, {})
        used = b.get("used_today", 0)
        limit_val = b.get("limit")
        rem = b.get("remaining")
        pct = b.get("pct_used")
        limit_str = str(limit_val) if limit_val is not None else "—"
        rem_str = str(rem) if rem is not None else "—"
        pct_str = f"{pct}%" if pct is not None else "—"
        lines.append(f"| {label} | {used} | {limit_str} | {rem_str} | {pct_str} |")
    if blocked:
        lines.append("")
        lines.append("> **BLOCKED** — invitation rate limit active")
    lines.append("")

    # ── Daily Safety Caps ──
    try:
        from ..linkedin.rate_limiter import get_daily_cap_summary
        cap_summary = await get_daily_cap_summary()
        if cap_summary:
            lines.append("## Daily Safety Caps")
            lines.append("")
            lines.append("| Action | Used | Cap | Remaining | Risk |")
            lines.append("|--------|------|-----|-----------|------|")
            cap_display = [
                ("invite", "Invitations"),
                ("follow", "Follows"),
                ("profile_view", "Profile Views"),
                ("comment", "Comments"),
                ("react", "Reactions"),
                ("dm", "DMs"),
                ("auto_reply", "Auto-replies"),
                ("withdraw", "Withdrawals"),
                ("_total", "**TOTAL**"),
            ]
            for key, label in cap_display:
                info = cap_summary.get(key)
                if not info:
                    continue
                pct = info["pct"]
                if pct >= 90:
                    risk = "CRITICAL"
                elif pct >= 75:
                    risk = "WARNING"
                else:
                    risk = "OK"
                lines.append(
                    f"| {label} | {info['current']} | {info['cap']} | {info['remaining']} | {risk} |"
                )
            lines.append("")
    except Exception as e:
        logger.debug("Failed to get daily cap summary: %s", e)

    # ── Active Campaigns ──
    campaigns = await db.list_campaigns(status="active")
    if campaign_id:
        campaigns = [c for c in campaigns if c["id"] == campaign_id]

    if campaigns:
        lines.append("## Campaign Breakdown")
        lines.append("")
        lines.append("| Campaign | Mode | Pending | Invited | Connected | Replied |")
        lines.append("|----------|------|---------|---------|-----------|---------|")
        for c in campaigns:
            cid = c["id"]
            name = (c.get("name") or "Unnamed")[:25]
            mode = c.get("mode", "?")
            pending = await db.count_outreaches_by_status(cid, "pending")
            invited = await db.count_outreaches_by_status(cid, "invited")
            connected = await db.count_outreaches_by_status(cid, "connected")
            replied = await db.count_outreaches_by_status(cid, "replied")
            lines.append(
                f"| {name} | {mode} | {pending} | {invited} | {connected} | {replied} |"
            )
        lines.append("")

        # ── Follow-up Pipeline ──
        lines.append("## Follow-up Pipeline")
        lines.append("")
        lines.append("| Campaign | Connected | Messaged | Eligible | Maxed | Distribution |")
        lines.append("|----------|-----------|----------|----------|-------|-------------|")
        for c in campaigns:
            cid = c["id"]
            name = (c.get("name") or "Unnamed")[:25]
            max_fu = await _followup_cap(c)
            bd = await db.get_followup_breakdown(cid, max_fu)
            conn = bd["by_status"].get("connected", 0)
            msg = bd["by_status"].get("messaged", 0)
            dist_parts = [f"#{k}:{v}" for k, v in sorted(bd["distribution"].items())]
            lines.append(
                f"| {name} | {conn} | {msg} | "
                f"{bd['eligible'] if max_fu else 'off'} | {bd['maxed_out']} | "
                f"{', '.join(dist_parts) or '-'} |"
            )
        lines.append("")
    else:
        lines.append("## Campaigns")
        lines.append("")
        lines.append("No active campaigns found.")
        lines.append("")

    # ── Scheduler Health ──
    mode = get_scheduler_mode()
    lines.append("## Scheduler Health")
    lines.append("")
    from ..config import is_scheduler_always_on as _is_always_on
    status_word = {
        "full": "enabled",
        "observe": "observe (collecting, not sending)",
        "off": "disabled",
    }[mode]
    lines.append(f"- Status: **{status_word}** (local)")
    lines.append(f"- Always-on: **{'active' if _is_always_on() else 'inactive'}**")

    # Compact timer health — just show last tick age
    timers_raw = await db.get_setting("scheduler_timers")
    if timers_raw and isinstance(timers_raw, dict):
        latest_ts = max((ts for ts in timers_raw.values() if ts), default=0)
        if latest_ts:
            age_s = now - int(latest_ts)
            if age_s < 60:
                age_str = f"{age_s}s ago"
            elif age_s < 3600:
                age_str = f"{age_s // 60}m ago"
            else:
                age_str = f"{age_s // 3600}h {(age_s % 3600) // 60}m ago"
            is_healthy = age_s < 600
            lines.append(f"- Last tick: **{age_str}** ({'healthy' if is_healthy else 'STALE'})")

    # Job queue counts
    stats = await db.get_scheduler_stats()
    counts = stats.get("counts", [])
    running = sum(r["cnt"] for r in counts if r["status"] == "running")
    pending_jobs = sum(r["cnt"] for r in counts if r["status"] == "pending")
    failed = sum(r["cnt"] for r in counts if r["status"] == "failed")
    lines.append(f"- Queue: {running} running, {pending_jobs} pending, {failed} failed")
    lines.append("")

    # ── 24h Job Summary (split outreach vs housekeeping) ──
    OUTREACH_TYPES = {
        "invite", "follow", "endorse", "engage", "followup", "send_dm",
        "auto_reply", "profile_view_warmup", "email_invite", "discover",
        "inmail",
    }
    metrics = await db.get_job_metrics(24)
    if metrics:
        o_total = o_ok = o_skip = o_fail = 0
        h_total = h_ok = h_fail = 0
        by_type: dict[str, int] = {}
        for jtype, m in metrics.items():
            _skip = m.get("skipped", 0) + m.get("deferred", 0) + m.get("permanent_failure", 0)
            if jtype in OUTREACH_TYPES:
                o_total += m["total"]
                o_ok += m["success"]
                o_skip += _skip
                o_fail += m["failed"]
                if m["success"] > 0:
                    by_type[jtype] = m["success"]
            else:
                h_total += m["total"]
                h_ok += m["success"]
                h_fail += m["failed"]

        lines.append("## 24h Job Summary")
        lines.append("")
        if o_total:
            o_attempted = o_total - o_skip
            o_rate = round(o_ok / o_attempted * 100, 1) if o_attempted else 0
            lines.append(
                f"- Outreach: **{o_total}** ({o_ok} ok, {o_skip} skipped, {o_fail} failed — {o_rate}%)"
            )
            if by_type:
                parts = [f"{t}: {n}" for t, n in sorted(by_type.items(), key=lambda x: -x[1])]
                lines.append(f"  {', '.join(parts)}")
        if h_total:
            if h_fail:
                lines.append(f"- Background: **{h_total}** ({h_ok} ok, {h_fail} failed)")
            else:
                lines.append(f"- Background: **{h_total}** (all ok)")
        lines.append("")

    # ── Plan Quality ──
    try:
        pq = await db.get_plan_quality_metrics(campaign_id, days=7)
        if pq["planned_count"] > 0:
            lines.append("## Plan Quality (7d)")
            lines.append("")
            llm_fb = pq["llm_vs_fallback"]
            lines.append(f"- Plans: {llm_fb['llm']} LLM / {llm_fb['fallback']} fallback")
            lines.append(
                f"- Adoption rate: **{round(pq['adoption_rate'] * 100, 1)}%** "
                f"({pq['executed_count']}/{pq['planned_count']} executed, "
                f"{pq['skipped_count']} skipped)"
            )
            lines.append(
                f"- Diversity: {pq['plan_diversity_score']} "
                f"({pq['unique_action_types']} action types)"
            )
            if pq["avg_feedback_score"] is not None:
                lines.append(f"- Avg feedback score: {pq['avg_feedback_score']}")
            if pq["top_skip_reasons"]:
                reasons = [f"{r}: {c}" for r, c in pq["top_skip_reasons"]]
                lines.append(f"- Top skip reasons: {', '.join(reasons)}")
            lines.append("")
    except Exception as e:
        logger.debug("Plan quality metrics failed: %s", e)

    # ── E2E Latency ──
    try:
        lat = await db.get_e2e_latency_metrics(campaign_id, hours=24)
        has_data = any(lat[k]["count"] > 0 for k in lat)
        if has_data:
            lines.append("## E2E Latency (24h)")
            lines.append("")
            lines.append("| Stage | Avg | P50 | P95 | N |")
            lines.append("|-------|-----|-----|-----|---|")
            labels = [
                ("plan_to_job", "Plan -> Job Start"),
                ("job_execution", "Job Execution"),
                ("total_e2e", "Total E2E"),
            ]
            for key, label in labels:
                s = lat[key]
                if s["count"] == 0:
                    continue
                lines.append(
                    f"| {label} | {_fmt_ms(s['avg_ms'])} | "
                    f"{_fmt_ms(s['p50_ms'])} | {_fmt_ms(s['p95_ms'])} | "
                    f"{s['count']} |"
                )
            lines.append("")
    except Exception as e:
        logger.debug("E2E latency metrics failed: %s", e)

    # ── Issues ──
    issues: list[str] = []
    if blocked:
        issues.append("Rate limit BLOCKED — no invitations until cooldown expires")
    if remaining == 0 and daily_limit > 0:
        issues.append("Daily invitation limit reached — resumes tomorrow")
    if running > 10:
        issues.append(f"{running} jobs running — possible stuck jobs")
    if failed > 0:
        issues.append(f"{failed} failed jobs — `campaign(action='retry_failed')` to retry")
    if mode == "off":
        issues.append("Scheduler is disabled — `scheduler(action='toggle', enabled=True)` to enable")
    elif mode == "observe":
        issues.append(
            "Scheduler is in observe mode — collecting, sending nothing. "
            "`scheduler(action='toggle', enabled=True)` to send"
        )

    if issues:
        lines.append("## Issues")
        lines.append("")
        for issue in issues:
            lines.append(f"- {issue}")
    else:
        lines.append("## Status: All Clear")
        lines.append("No issues detected.")

    return "\n".join(lines)


def _fmt_ms(ms: int | None) -> str:
    """Format milliseconds as human-readable duration."""
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    minutes = ms // 60_000
    seconds = (ms % 60_000) // 1000
    return f"{minutes}m{seconds}s"
