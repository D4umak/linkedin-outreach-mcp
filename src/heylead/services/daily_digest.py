"""Daily digest: compile 24h metrics and deliver as MCP notification.

The digest summarizes: invitations sent, acceptance rate, replies,
hot leads, job success/failure rates, and any issues detected.

Triggered by JOB_DAILY_DIGEST scheduled once per day.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from ..db.async_bridge import run_db
from ..db.queries import (
    count_inbound_dms_today,
    get_daily_auto_reply_count,
    get_daily_brand_engage_count,
    get_daily_brand_post_count,
    get_daily_engagement_counts_all,
    get_daily_engagement_counts_pending,
    get_daily_signal_outreach_count,
    get_daily_voice_memo_count,
    get_job_metrics,
    get_outreach_changes,
    get_rate_limit_today,
    get_recent_scheduler_events,
    get_scheduler_event_summary,
    get_sending_days_7d,
    get_weekly_invitation_sum,
    list_campaigns,
)

logger = logging.getLogger(__name__)

# Human-readable labels for enrichment source tags
_SOURCE_LABELS = {
    "linkedin_search": "LinkedIn Search",
    "global_contacts": "Global Contacts",
    "signal_reeval": "Re-evaluated Signals",
    "job_change": "Job Changers",
    "signal_account": "Signal Accounts",
    "profile_viewer": "Profile Viewers",
    "post_author": "Post Authors",
    "competitor_commenter": "Competitor Commenters",
    "company_engager": "Company Page Engagers",
    "connection": "1st-Degree Connections",
    "post_commenter": "Post Commenters",
    "inbound_low_conf": "Inbound (Low Conf)",
}


async def compile_daily_digest() -> str:
    """Compile a plain-text daily digest of the last 24 hours.

    Returns markdown string suitable for MCP tool output or email body.
    """
    now = datetime.now()
    lines = [
        f"# Daily Digest — {now.strftime('%Y-%m-%d')}",
        "",
    ]

    # ── Rate Limits / Sends ──
    rl = await run_db(get_rate_limit_today)
    # One resolver for both ceilings: a hosted account's come from the backend,
    # not from this row's column default. See invite_limits_for_display.
    from ..linkedin.rate_limiter import invite_limits_for_display

    weekly_cap, daily_limit = await invite_limits_for_display(rl)
    # Use verified invitation count instead of optimistic rate_limits.sent
    outreach_changes = await run_db(get_outreach_changes, hours=24)
    sent_verified = outreach_changes.get("invited", 0)
    sent_pending = outreach_changes.get("invited_pending", 0)
    sent_attempted = rl.get("sent", 0)  # keep for rate limit comparison
    lines.append("## Outreach")
    inv_line = f"- Invitations sent today: **{sent_verified}/{daily_limit}**"
    if sent_pending > 0:
        inv_line += f" (+{sent_pending} pending verification)"
    lines.append(inv_line)

    # Show DMs (follow-ups) separately from invitations
    try:
        from ..db.queries import get_monthly_usage
        usage = await run_db(get_monthly_usage)
        dms = usage.get("messages_sent", 0)
        if dms > 0:
            lines.append(f"- DMs sent (verified) this month: **{dms}**")
    except Exception:
        pass
    lines.append("")

    # ── Weekly Limits ──
    from ..linkedin.rate_limiter import estimate_weekly_limit_reset
    weekly_sent = await run_db(get_weekly_invitation_sum)
    daily_max = daily_limit
    weekly_pct = round(weekly_sent / weekly_cap * 100, 1) if weekly_cap > 0 else 0
    sending_days = await run_db(get_sending_days_7d)

    lines.append("## Weekly Limits")
    lines.append(f"- LinkedIn invitations: **{weekly_sent}/{weekly_cap}** ({weekly_pct}%)")
    lines.append(f"- Daily max: {daily_max} | Sending days: {sending_days}/7")

    # Email overflow stats
    try:
        from ..db.queries import get_weekly_email_sum  # noqa: F811
        from ..services.channel_selector import has_email_channel
        if has_email_channel():
            weekly_email = await run_db(get_weekly_email_sum)
            lines.append(f"- Email overflow: **{weekly_email}** this week")
    except Exception:
        pass

    if weekly_sent >= weekly_cap:
        at_limit, _, eta_msg = await estimate_weekly_limit_reset()
        lines.append(f"- **LIMIT REACHED** — invitations resume in {eta_msg}")
    elif weekly_pct >= 80:
        remaining = weekly_cap - weekly_sent
        lines.append(f"- Approaching limit — {remaining} invitations remaining")
    lines.append("")

    # ── Daily Activity ──
    lines.append("## Daily Activity")
    eng_counts = await run_db(get_daily_engagement_counts_all)
    eng_pending = await run_db(get_daily_engagement_counts_pending)
    comment_react = eng_counts.get("comment", 0) + eng_counts.get("react", 0)
    action_counts = [
        ("Invitations", sent_verified),
        ("Engagements (comment/react)", comment_react),
        ("Profile Views", eng_counts.get("profile_view", 0)),
        ("Follows", eng_counts.get("follow", 0)),
        ("Endorsements", eng_counts.get("endorse", 0)),
        ("Auto-replies", await run_db(get_daily_auto_reply_count)),
        ("Inbound DMs", await run_db(count_inbound_dms_today)),
        ("Voice Memos", await run_db(get_daily_voice_memo_count)),
        ("Brand Posts", await run_db(get_daily_brand_post_count)),
        ("Brand Engagements", await run_db(get_daily_brand_engage_count)),
        ("Signal Outreaches", await run_db(get_daily_signal_outreach_count)),
    ]
    for label, used in action_counts:
        lines.append(f"- {label}: **{used}**")
    lines.append("")

    # ── Campaign Summary ──
    campaigns = await run_db(list_campaigns, status="active")
    if campaigns:
        lines.append("## Active Campaigns")
        for c in campaigns:
            name = c.get("name") or "Unnamed"
            mode = c.get("mode", "?")
            lines.append(f"- **{name}** ({mode})")
        lines.append("")

    # ── Event Summary (24h) ──
    summary = await run_db(get_scheduler_event_summary, 24)
    if summary:
        lines.append("## Event Summary (24h)")
        parts = [f"{etype}: {cnt}" for etype, cnt in summary.items()]
        lines.append(", ".join(parts))
        lines.append("")

    # ── Job Metrics ──
    metrics = await run_db(get_job_metrics, 24)
    if metrics:
        total_jobs = sum(m["total"] for m in metrics.values())
        total_ok = sum(m["success"] for m in metrics.values())
        total_fail = sum(m["failed"] for m in metrics.values())
        overall_rate = round(total_ok / total_jobs * 100, 1) if total_jobs > 0 else 0

        lines.append("## Job Performance (24h)")
        lines.append(
            f"- Total: **{total_jobs}** | Success: {total_ok} | "
            f"Failed: {total_fail} | Rate: {overall_rate}%"
        )

        # Top failures by type
        failures = [(jt, m) for jt, m in metrics.items() if m["failed"] > 0]
        if failures:
            lines.append("- Failures by type:")
            for jtype, m in sorted(failures, key=lambda x: x[1]["failed"], reverse=True):
                lines.append(f"  - {jtype}: {m['failed']} failures")
        lines.append("")

    # ── Recent Failures ──
    recent_failures = await run_db(get_recent_scheduler_events, hours=24, event_type="job_failed", limit=5)
    if recent_failures:
        lines.append("## Recent Failures")
        for evt in recent_failures:
            ts = evt.get("created_at", 0)
            t = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "?"
            ctx = evt.get("context", {})
            jtype = ctx.get("job_type", "?")
            cat = ctx.get("error_category", "")
            err = ctx.get("error", "")[:60]
            lines.append(f"- [{t}] {jtype} [{cat}]: {err}")
        lines.append("")

    # ── Engagement Anomalies ──
    all_recent = await run_db(get_recent_scheduler_events, hours=24, event_type="", limit=500)
    anomaly_events = [
        e for e in all_recent
        if (e.get("event_type") or "").startswith("anomaly_")
    ]
    if anomaly_events:
        lines.append("## Engagement Anomalies")
        for evt in anomaly_events[:10]:
            ts = evt.get("created_at", 0)
            t = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "?"
            ctx = evt.get("context", {})
            severity = ctx.get("severity", "?")
            summary = ctx.get("summary", "")[:80]
            severity_icon = "!!" if severity == "critical" else "!"
            lines.append(f"- [{t}] [{severity_icon} {severity}] {summary}")
        lines.append("")

    # ── Recent Comments ──
    try:
        from ..db.queries import get_recent_engagements
        recent = await run_db(get_recent_engagements, limit=20)
        comment_entries = [e for e in recent if e.get("action_type") == "comment"][:3]
        if comment_entries:
            lines.append("## Recent Comments")
            for eng in comment_entries:
                name = eng.get("prospect_name") or "Unknown"
                title = eng.get("prospect_title") or ""
                text = (eng.get("comment_text") or "")[:120]
                ts = eng.get("created_at", 0)
                if ts:
                    t = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                    lines.append(f'- "{text}" -> {name} ({title}) -- {t}')
                else:
                    lines.append(f'- "{text}" -> {name} ({title})')
            lines.append("")
    except Exception:
        pass

    # ── Hot Leads ──
    # Count positive replies from events
    positive_events = await run_db(get_recent_scheduler_events,
        hours=24, event_type="job_completed", limit=500
    )
    reply_jobs = [
        e for e in positive_events
        if (e.get("context") or {}).get("job_type") in ("check_replies", "qualify_inbound")
    ]
    if reply_jobs:
        lines.append(f"## Replies & Inbound: {len(reply_jobs)} processed")
        lines.append("")

    # ── ICP Prospect Enrichment ──
    try:
        def _get_enrichment_stats():
            from ..db.schema import get_db as _get_enrich_db
            edb = _get_enrich_db()

            # Count prospects added by enrichment source (last 24h and all-time)
            day_ago = int(time.time()) - 86400
            rows_24h = edb.execute(
                """SELECT source_detail, COUNT(*) as cnt
                   FROM contacts
                   WHERE source = 'auto_enrichment' AND created_at >= ?
                   GROUP BY source_detail
                   ORDER BY cnt DESC""",
                (day_ago,),
            ).fetchall()
            rows_all = edb.execute(
                """SELECT source_detail, COUNT(*) as cnt
                   FROM contacts
                   WHERE source = 'auto_enrichment'
                   GROUP BY source_detail
                   ORDER BY cnt DESC""",
            ).fetchall()
            edb.close()
            return rows_24h, rows_all

        source_rows_24h, source_rows_all = await run_db(_get_enrichment_stats)

        if source_rows_24h or source_rows_all:
            lines.append("## ICP Prospect Enrichment")
            total_24h = sum(r["cnt"] for r in source_rows_24h)
            total_all = sum(r["cnt"] for r in source_rows_all)
            lines.append(f"Total: {total_24h} added (last 24h), {total_all} all-time")
            lines.append("")
            if source_rows_all:
                lines.append("| Source | Last 24h | All-time |")
                lines.append("|--------|----------|----------|")
                counts_24h = {r["source_detail"]: r["cnt"] for r in source_rows_24h}
                for row in source_rows_all:
                    tag = row["source_detail"] or "unknown"
                    label = _SOURCE_LABELS.get(tag, tag.replace("_", " ").title())
                    c24 = counts_24h.get(tag, 0)
                    lines.append(f"| {label} | {c24} | {row['cnt']} |")
                lines.append("")
    except Exception:
        pass

    # ── Pipeline Health ──
    health_warnings = []
    try:
        def _get_health_warnings():
            from ..db.schema import get_db as _get_health_db
            warnings = []
            hdb = _get_health_db()

            # Connected but un-messaged (orphaned prospects)
            orphaned = hdb.execute(
                """SELECT COUNT(*) as cnt FROM outreaches o
                   WHERE o.status = 'connected' AND o.followup_count = 0
                   AND o.id NOT IN (
                       SELECT sj.outreach_id FROM scheduler_jobs sj
                       WHERE sj.outreach_id IS NOT NULL
                         AND sj.job_type IN ('send_dm', 'followup')
                         AND sj.status IN ('pending', 'running')
                   )"""
            ).fetchone()["cnt"]
            if orphaned > 0:
                warnings.append(f"- {orphaned} connected prospects with NO pending jobs (orphaned)")

            # Connected but un-messaged total
            unmessaged = hdb.execute(
                "SELECT COUNT(*) as cnt FROM outreaches WHERE status = 'connected' AND followup_count = 0"
            ).fetchone()["cnt"]
            if unmessaged > 0:
                warnings.append(f"- {unmessaged} connected prospects never messaged (followup_count=0)")

            # followup_count vs actual messages mismatch
            mismatches = hdb.execute(
                """SELECT COUNT(*) as cnt FROM outreaches o
                   WHERE o.followup_count != (
                       SELECT COUNT(*) FROM messages m WHERE m.outreach_id = o.id AND m.role = 'sdr'
                   ) AND o.status NOT IN ('pending', 'skipped')"""
            ).fetchone()["cnt"]
            if mismatches > 0:
                warnings.append(f"- {mismatches} outreaches with followup_count/message count mismatch")

            # Unverified engagements
            unverified = hdb.execute(
                "SELECT COUNT(*) as cnt FROM engagements WHERE verified_status = 'unverified'"
            ).fetchone()["cnt"]
            if unverified > 0:
                warnings.append(f"- {unverified} unverified engagements")

            # Time since last successful DM
            last_dm = hdb.execute(
                """SELECT MAX(timestamp) as ts FROM actions_log
                   WHERE action_type = 'dm_sent' AND result = 'success'"""
            ).fetchone()
            if last_dm and last_dm["ts"]:
                hours_since = (int(time.time()) - last_dm["ts"]) / 3600
                if hours_since > 48:
                    warnings.append(f"- No DM sent in {int(hours_since)}h")

            hdb.close()
            return warnings

        health_warnings.extend(await run_db(_get_health_warnings))
    except Exception:
        pass

    if health_warnings:
        lines.append("")
        lines.append("## Pipeline Health Warnings")
        lines.extend(health_warnings)
        lines.append("")

    try:
        from .action_health import format_action_health_lines, summarize_action_health

        health = await run_db(summarize_action_health)
        lines.append("## Action Health")
        lines.extend(format_action_health_lines(health))
        lines.append("")
    except Exception:
        pass

    try:
        from .unanswered_lead_alerts import format_needs_attention_digest
        from ..db.queries import get_unanswered_leads

        attention = await run_db(get_unanswered_leads)
        lines.extend(format_needs_attention_digest(attention))
    except Exception:
        pass

    # ── Metric Trend Anomalies ──
    try:
        from .anomaly_detector import detect_metric_anomalies
        metric_anomalies = await run_db(detect_metric_anomalies, days=14)
        if metric_anomalies:
            lines.append("")
            lines.append("## Metric Trends")
            for a in metric_anomalies:
                icon = "!!" if a.severity == "critical" else "!"
                lines.append(f"- [{icon}] {a.summary}")
            lines.append("")
    except Exception:
        pass

    lines.append("---")
    lines.append("Run `scheduler(action='logs')` for full details.")

    return "\n".join(lines)
