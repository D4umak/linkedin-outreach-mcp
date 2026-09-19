"""Tool 5: show_status — Dashboard in the chat.

Shows campaign stats, acceptance rate, reply rate, hot leads,
account health, and free tier usage.

The chat is the front door to the dashboard: status replies link to the
matching dashboard page and, on hosted accounts, attach a snapshot of it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..flags import flag_enabled
from .. import config
from ..config import get_tier
from ..constants import (
    FREE_MAX_CAMPAIGNS,
    FREE_MAX_ENGAGEMENTS,
    FREE_MAX_FOLLOWUPS,
    FREE_MONTHLY_INVITATIONS,
    FREE_MONTHLY_MESSAGES,
    PRO_MAX_FOLLOWUPS,
    TIER_PRO,
)
from ..db import aio as db
from ..db.async_bridge import run_db
from ..constants import SOURCE_LABELS
from ..dashboard_links import dashboard_url
from ..formatter import conversion_rate_display, format_duration, progress_bar, prospect_link, stars
from ..services.health_score import compute_health_score, format_health_score
from ..services.dashboard_snapshot import status_footer
from ..services.unipile_email import mailbox_disconnect_banner

logger = logging.getLogger(__name__)


def _weekly_limit_lines(eta: str) -> list[str]:
    """The at-cap notice. Never ends in "resume in " with nothing after it.

    estimate_weekly_limit_reset() has no weekly signal and returns an empty
    ETA, which printed a dangling "invitations resume in".
    """
    when = f"in {eta}" if eta else "as invitations from the past 7 days age out"
    return [
        f"├── ⚠️ Weekly limit reached: invitations resume {when}",
        "│   Email overflow + engagements continue normally",
    ]


def _monthly_usage_lines(usage: dict, *, active_campaigns: int) -> list[str]:
    """This month's usage, shown as caps only where caps are enforced.

    Self-hosted free: the monthly free row the local send paths enforce.
    Hosted: the cloud sender enforces no monthly free row (create_campaign,
    generate_send, send_followup and reply_to_prospect all skip it via
    apply_free_monthly_caps), so "Invitations 61/50 100%" read as a limit
    that does not exist (10 Sep 2026). Show plain counts instead.
    Self-hosted Pro: nothing to show.
    """
    inv_used = usage.get("invitations_sent", 0) or 0
    msg_used = usage.get("messages_sent", 0) or 0
    if config.apply_free_monthly_caps():
        return [
            "Free Tier Usage (this month):",
            f"├── Invitations: {inv_used}/{FREE_MONTHLY_INVITATIONS} {progress_bar(inv_used, FREE_MONTHLY_INVITATIONS, 15)}",
            f"├── Messages: {msg_used}/{FREE_MONTHLY_MESSAGES} {progress_bar(msg_used, FREE_MONTHLY_MESSAGES, 15)}",
            f"└── Campaigns: {active_campaigns}/{FREE_MAX_CAMPAIGNS}",
            "",
        ]
    if config.is_backend_mode():
        return [
            "Usage this month (counts only, not capped on hosted accounts):",
            f"├── Invitations: {inv_used}",
            f"└── Messages: {msg_used}",
            "",
        ]
    return []


def _monthly_actual_usage() -> dict[str, int]:
    """This month's sends counted from the rows the usage counters track.

    Local-DB twin of the hosted ``usage_actual``. The month boundary is the
    local one because ``get_monthly_usage`` keys on ``date.today()``; the
    hosted counters key on the UTC month and are recomputed server-side.

    An invitation note is not a message — counting it would double-count the
    invitation, which is the same distinction SDR_REAL_DM_SQL draws.
    """
    from datetime import date, datetime

    from ..db.queries import _INVITE_NOTE_WINDOW_SECONDS
    from ..db.schema import get_db as _get_db

    first = date.today().replace(day=1)
    month_start = int(datetime(first.year, first.month, 1).timestamp())
    db = _get_db()
    try:
        inv = db.execute(
            "SELECT COUNT(*) AS cnt FROM outreaches WHERE invited_at >= ?",
            (month_start,),
        ).fetchone()
        msg = db.execute(
            f"""SELECT COUNT(*) AS cnt FROM messages m
                 JOIN outreaches o ON o.id = m.outreach_id
                WHERE m.role = 'sdr' AND m.deleted_at IS NULL
                  AND m.timestamp >= ?
                  AND COALESCE(m.format, '') != 'invite_note'
                  AND NOT (
                      COALESCE(o.invited_at, 0) > 0
                      AND ABS(COALESCE(m.timestamp, 0) - o.invited_at)
                          <= {_INVITE_NOTE_WINDOW_SECONDS}
                  )""",
            (month_start,),
        ).fetchone()
    finally:
        db.close()
    return {
        "invitations_sent": int((dict(inv) if inv else {}).get("cnt") or 0),
        "messages_sent": int((dict(msg) if msg else {}).get("cnt") or 0),
    }


def _usage_drift_lines(
    usage: dict[str, Any], actual: dict[str, Any] | None,
) -> list[str]:
    """Warn only when the stored counters disagree with the rows behind them.

    The old check summed ``invited`` over active and paused campaigns — a
    *lifetime* figure — and compared it against ``invitations_sent +
    messages_sent``, which are counters for the current month. It also weighed
    a per-org campaign list against per-user counters. On 7 Sep 2026 that read
    "123 outreaches reached vs 14 in usage counters" on an install where
    nothing had drifted at all: 123 invitations across the life of two
    campaigns, 14 sends this month. The two can only agree in an account's
    first month, so the warning was permanent — and a warning that is always on
    is worse than no warning, because it teaches you to skip warnings.

    Only an **overcount** is reported. A counter standing higher than the rows
    behind it is wrong however the sends were made — it is a charge with no
    send under it, and it closes the free tier early. The reverse is not a
    defect: the counters are incremented by the hosted executor alone, while
    the rows also include sends the client made itself and pushed up, so a
    mirrored install legitimately holds more rows than the counters ever saw.
    Checked live on 7 Sep 2026 — one workspace read 14 counted against 143
    rows with nothing wrong, another 148 against 69, which is the real thing.
    Warning on both would have restored the always-on warning this replaced.

    (Distinguishing the two directly is not possible in the current schema: a
    synced message and an executor message are the same row, and the jobs that
    would prove authorship are pruned after seven days.)

    Returns no lines when the counter cannot be checked (an older service sends
    no ``usage_actual``): silence beats a comparison that cannot be valid.
    """
    if not actual:
        return []
    fields = ("invitations_sent", "messages_sent")
    stored = sum(int(usage.get(f) or 0) for f in fields)
    counted = sum(int(actual.get(f) or 0) for f in fields)
    # A send that lands between the two reads shifts one by one. Free-tier caps
    # are 50 and 20, so a couple over is noise, not a broken ledger.
    if stored - counted <= 2:
        return []
    return [
        f"⚠️ Usage counters overcount this month: {stored} recorded vs "
        f"{counted} sends on record — the free-tier gate reads the first number",
        "",
    ]


def _pooled_acceptance(stats_rows: list[dict[str, Any]]) -> tuple[float, int]:
    """(acceptance rate, invitations ever sent) pooled across campaign rows.

    ``accepted_today / sent_today`` is not an acceptance rate and never was: an
    invitation accepted today was sent days ago, and one sent today cannot have
    been accepted yet. The workspace audited on 6 Sep 2026 had 23 of 95
    invitations accepted and computed 0/28 = 0%, which scored 2/25 on the
    health score and printed "improve targeting or messaging" underneath a real
    24%. Both figures the score documents itself as wanting — a *cumulative*
    rate and a *lifetime* send count — are already carried on the campaign
    rows, so neither needs to be inferred from one day's counters.

    Reads only ``invited`` and ``connected``, which the hosted ``/api/v1/stats``
    rows and local ``get_campaign_stats`` both provide, so one helper serves the
    hosted and local renderers alike. Capped at 1.0: inbound acceptances can
    outnumber invitations, and the score wants 0-1.
    """
    def _n(row: dict[str, Any], key: str) -> int:
        try:
            return int(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    invited = sum(_n(r, "invited") for r in stats_rows)
    if invited <= 0:
        return 0.0, 0
    connected = sum(_n(r, "connected") for r in stats_rows)
    return min(1.0, connected / invited), invited


async def _needs_attention_lines(backend_leads: list[dict[str, Any]] | None = None) -> list[str]:
    """Operator-facing unanswered-lead list. Prefer hosted stats when given."""
    from ..services.unanswered_lead_alerts import format_needs_attention_lines

    if backend_leads:
        return format_needs_attention_lines(backend_leads)
    try:
        from ..db.queries import get_unanswered_leads
        leads = await run_db(get_unanswered_leads)
    except Exception:
        return []
    return format_needs_attention_lines(leads)


async def _action_health_lines(campaign_id: str = "") -> list[str]:
    """Last-24h skip vs send for the account, or one campaign."""
    try:
        from ..services.action_health import format_action_health_lines, summarize_action_health

        health = await run_db(summarize_action_health, campaign_id=campaign_id)
        return format_action_health_lines(health)
    except Exception as e:  # noqa: BLE001 — a status line must not break status
        logger.debug("Action health read failed: %s", e)
        return []


def _daemon_health_lines() -> list[str]:
    """Lines describing what is scheduling right now, if anything."""
    try:
        from ..daemon import daemon_status
        st = daemon_status()
    except Exception:
        return []

    if not st.get("daemon_configured"):
        return []

    src = st.get("install_source") or {}
    lines: list[str] = []
    if src.get("pypi_rollback_risk"):
        lines.extend([
            "🚨 This install is missing CLOUD_OWNS_ALL_JOBS — "
            "uvx --refresh likely rolled back to PyPI.",
            "   Reinstall: uv tool install --force --reinstall "
            f"{src.get('path') or '.'}",
            "",
        ])

    if st.get("healthy"):
        if st.get("sync_only"):
            lines.extend([
                f"⚙️  Scheduler daemon: sync-only (cloud owns jobs, "
                f"pid {st.get('leader_pid')}, v{st.get('leader_version')})",
                "",
            ])
        else:
            lines.extend([
                f"⚙️  Scheduler daemon: running (pid {st.get('leader_pid')}, "
                f"v{st.get('leader_version')})",
                "",
            ])
        return lines

    from ..config import get_sending_host, is_backend_mode
    hosted_cloud = is_backend_mode() and get_sending_host() == "cloud"
    if hosted_cloud:
        lines.extend([
            "🚨 Scheduler daemon: NOT RUNNING",
            "   Cloud keeps sending. Only local sync/pull is dead.",
            f"   Lock claims pid {st.get('leader_pid')} (kind={st.get('leader_kind')}, "
            f"alive={st.get('leader_alive')}).",
            "   Start it:  launchctl load -w ~/Library/LaunchAgents/dev.heylead.scheduler.plist",
            "",
        ])
        return lines
    lines.extend([
        "🚨 Scheduler daemon: NOT RUNNING",
        "   scheduler_daemon is set, so MCP servers do not schedule — "
        "nothing is scheduling at all.",
        f"   Lock claims pid {st.get('leader_pid')} (kind={st.get('leader_kind')}, "
        f"alive={st.get('leader_alive')}).",
        "   Start it:  launchctl load -w ~/Library/LaunchAgents/dev.heylead.scheduler.plist",
        "",
    ])
    return lines


async def run_show_status(campaign_id: str = "") -> str:
    """Show outreach dashboard.

    If campaign_id is provided: show detailed stats for that campaign.
    If empty: show overview of all campaigns + account health.

    In backend mode: tries live stats from backend first, falls back to
    synced local DB, then raw local DB.

    A recorded LinkedIn-session death banners on top of EVERY variant of the
    dashboard — during the 28 Aug-5 Sep 2026 dead-session week the dashboard
    looked normal while every send was silently discarded.
    """
    banner = ""
    try:
        from ..services.session_health import SETTING_KEY, session_dead_banner_lines
        dead = await db.get_setting(SETTING_KEY)
        lines = session_dead_banner_lines(dead)
        if lines:
            banner = "\n".join(lines)
    except Exception:
        logger.debug("Session-death banner check failed", exc_info=True)

    return banner + await _show_status_body(campaign_id)


async def _show_status_body(campaign_id: str = "") -> str:
    if config.is_backend_mode():
        try:
            from ..services.cloud_sync import ensure_hosted_sending_default
            await ensure_hosted_sending_default()
        except Exception:
            logger.debug("Hosted sending default on status failed", exc_info=True)

    setup_done = await db.get_setting("setup_complete", False)
    if not setup_done:
        return (
            "👋 Welcome to HeyLead — your AI LinkedIn SDR!\n\n"
            "You haven't set up your profile yet. Let's fix that!\n\n"
            "Say 'set up my profile' or run setup_profile, and I'll walk you through "
            "connecting your LinkedIn account in about 2 minutes.\n\n"
            "After setup, you can:\n"
            "  → create_campaign('find me fintech CTOs') — find prospects\n"
            "  → generate_and_send() — craft personalized messages\n"
            "  → check_replies() — see who responded\n"
            "  → show_status() — your outreach dashboard"
        )

    # ── Backend mode: try live stats first, then sync ──
    backend_offline = False

    if config.is_backend_mode() and not campaign_id:
        try:
            from ..services.cloud_sync import BackendAuthError, fetch_live_stats
            live = await fetch_live_stats()
            if live and live.get("campaigns"):
                return await _show_overview_from_backend(live)
            elif live is not None:
                # Backend reachable but returned no campaigns — try syncing local state up
                try:
                    from ..services.cloud_sync import sync_to_cloud
                    await sync_to_cloud()
                except Exception:
                    pass
        except BackendAuthError:
            return _auth_error_message()
        except Exception as e:
            backend_offline = True
            logger.debug("Live stats failed, falling back to sync: %s", e)

        # Fallback: pull changes to update local DB before reading
        try:
            from ..services.cloud_sync import BackendAuthError, ensure_synced
            await ensure_synced()
        except BackendAuthError:
            return _auth_error_message()
        except Exception as e:
            backend_offline = True
            logger.debug("ensure_synced failed: %s", e)

    elif config.is_backend_mode() and campaign_id:
        # For campaign detail, just ensure synced before reading local
        try:
            from ..services.cloud_sync import BackendAuthError, ensure_synced
            await ensure_synced()
        except BackendAuthError:
            return _auth_error_message()
        except Exception as e:
            backend_offline = True
            logger.debug("ensure_synced failed: %s", e)

    # ── Specific campaign view ──
    if campaign_id:
        return await _show_campaign_detail(campaign_id)

    # ── Overview ──
    return await _show_overview(offline=backend_offline)


def _auth_error_message() -> str:
    """Return a user-facing message when the backend JWT is expired or invalid."""
    from ..constants import DEFAULT_BACKEND_URL, LOGIN_URL_PATH

    login_url = f"{DEFAULT_BACKEND_URL}{LOGIN_URL_PATH}"
    return (
        "⚠️ **Authentication expired**\n\n"
        "Your HeyLead session token has expired or is invalid.\n"
        "Your campaigns are still safe on the server — you just need to re-authenticate.\n\n"
        "**To fix:**\n"
        f"1. Open: {login_url}\n"
        "2. Sign in and copy your new token\n"
        "3. Run: setup_profile(backend_jwt='YOUR_NEW_TOKEN')\n\n"
        "After that, show_status() will work again."
    )


async def _dashboard_freshness_lines() -> list[str]:
    """How current heylead.dev is, or nothing if that cannot be read."""
    try:
        from ..services.cloud_sync import dashboard_freshness_lines

        return await dashboard_freshness_lines()
    except Exception as e:  # noqa: BLE001 — a status line must not break status
        logger.debug("Dashboard freshness read failed: %s", e)
        return []


def _activity_counts_today(today_start: int) -> tuple[int, int]:
    """(opening messages, follow-ups) sent today, counted from `messages`.

    Kept module-level so a test can run the real statements against a table
    shaped like production — the previous version was a closure inside the
    caller's ``try:`` and nothing could reach it.

    Two defects lived here, and they had to be fixed together:

    - The follow-up query read ``messages.created_at``. That column has never
      existed; it is ``timestamp``. Both statements shared one ``try:``, so
      the raise zeroed *both* counters and the caller's ``> 0`` guards then
      suppressed both lines. Neither figure has ever been displayed.
    - The DM query counted ``outreaches`` rows at status 'messaged' whose
      ``updated_at`` landed today. A cloud pull re-stamped every row it
      touched on every pull, so that counted re-stamps as sends — 24 against
      a true zero on 22 Aug 2026. Repairing only the column name would have
      unmasked it and shown that 24 to the user for the first time.

    A row's mtime is not evidence that a message was sent, so neither figure
    is derived from it now. An outreach's first sdr message is the opener; any
    later one is a follow-up, which makes the two counts additive rather than
    overlapping. Deleted messages are excluded — delete_message decrements
    followup_count, so a deleted send is already treated as not having landed.
    """
    from ..db.schema import get_db as _get_db

    db = _get_db()
    try:
        dm_row = db.execute(
            """SELECT COUNT(*) AS cnt FROM (
                   SELECT outreach_id, MIN(timestamp) AS first_ts
                     FROM messages
                    WHERE role = 'sdr' AND deleted_at IS NULL
                    GROUP BY outreach_id
               ) WHERE first_ts >= ?""",
            (today_start,),
        ).fetchone()
        fu_row = db.execute(
            """SELECT COUNT(*) AS cnt FROM messages m
                WHERE m.role = 'sdr' AND m.deleted_at IS NULL
                  AND m.timestamp >= ?
                  AND EXISTS (SELECT 1 FROM messages p
                               WHERE p.outreach_id = m.outreach_id
                                 AND p.role = 'sdr' AND p.deleted_at IS NULL
                                 AND p.timestamp < m.timestamp)""",
            (today_start,),
        ).fetchone()
    finally:
        db.close()

    return (
        (dm_row["cnt"] if dm_row else 0) or 0,
        (fu_row["cnt"] if fu_row else 0) or 0,
    )


async def _show_overview(offline: bool = False) -> str:
    """Show overview of all campaigns + account health."""

    campaigns = await db.list_campaigns()
    # Read once, up front: the health score needs the cumulative acceptance
    # rate before the campaign list is rendered, and the render loop below
    # needed the same rows anyway.
    campaign_stats = {c["id"]: await db.get_campaign_stats(c["id"]) for c in campaigns}
    tier = get_tier()
    rate_data = await db.get_rate_limit_today()
    usage = await db.get_monthly_usage()

    if offline and config.is_backend_mode():
        import time as _ts
        _last_pull = await db.get_setting("last_pull_timestamp", 0)
        if not isinstance(_last_pull, (int, float)):
            _last_pull = 0
        _age_s = int(_ts.time()) - int(_last_pull) if _last_pull else 0
        if _age_s > 300:
            output = [f"📊 **HeyLead Dashboard** (offline — last sync {_age_s // 60}m ago)\n"]
            output.append(f"⚠️ Data may be stale — backend unreachable, showing cached data\n")
        else:
            output = [f"📊 **HeyLead Dashboard** (cached — {_age_s}s old)\n"]
    else:
        output = ["📊 **HeyLead Dashboard**\n"]

    # ── Disconnection banner (shown before anything else if LinkedIn session broke) ──
    try:
        from ..linkedin import get_account_id, get_linkedin_client
        from ..linkedin.backend_client import BackendClient
        _acct = await run_db(get_account_id)
        if _acct:
            _client = get_linkedin_client()
            try:
                _ok, _msg = await _client.verify_account(_acct)
                if not _ok:
                    _link = ""
                    if isinstance(_client, BackendClient):
                        try:
                            _link = await _client.get_reconnect_link()
                        except Exception:
                            _link = ""
                    output.append("🚨 **LinkedIn disconnected — sync paused.**")
                    output.append(f"   {_msg}")
                    if _link:
                        output.append(f"   🔗 Reconnect: {_link}")
                    output.append(
                        "   Until you reconnect, new messages and invites won't send."
                    )
                    output.append("")
            finally:
                await _client.close()
    except Exception:
        pass  # Never let the banner check break the dashboard

    try:
        output.extend(await mailbox_disconnect_banner())
    except Exception:
        pass

    # ── Account Health ──
    # Use verified invitation count for display, keep attempted for rate limiting
    _outreach_chg = await db.get_outreach_changes(hours=24)
    sent_verified = _outreach_chg.get("invited", 0)
    sent_pending = _outreach_chg.get("invited_pending", 0)
    sent = sent_verified + sent_pending  # total sent today (verified + pending verification)
    sent_attempted = rate_data.get("sent", 0)  # for rate limit comparison

    weekly_sent = await db.get_weekly_invitation_sum()

    # Try to fetch InMail balance + SSI score + pending invitations (non-blocking)
    inmail_credits = -1
    ssi_data: dict = {}
    linkedin_pending_count: int | None = None
    try:
        from ..linkedin import get_account_id, get_linkedin_client, UnipileError
        account_id = await run_db(get_account_id)
        if account_id:
            client = get_linkedin_client()
            try:
                inmail_data = await client.get_inmail_balance(account_id)
                inmail_credits = inmail_data.get("credits", -1)
            except Exception:
                pass
            try:
                ssi_data = await client.get_ssi_score(account_id)
            except Exception:
                pass
            try:
                from ..linkedin.rate_limiter import get_cached_pending_invitations
                linkedin_pending_count, _ = await get_cached_pending_invitations(client, account_id)
            except Exception:
                pass
            finally:
                await client.close()
    except Exception:
        pass

    # Compute LinkedIn Health Score
    ssi_score = ssi_data.get("score", 0)
    sending_days = await db.get_sending_days_7d()
    total_sent_lifetime = 0
    try:
        def _query_total_sent():
            from ..db.schema import get_db as _get_db
            _db = _get_db()
            row_total = _db.execute("SELECT COALESCE(SUM(sent), 0) as total FROM rate_limits").fetchone()
            _db.close()
            return row_total["total"] if row_total else 0
        total_sent_lifetime = await run_db(_query_total_sent)
    except Exception:
        pass

    from ..linkedin.rate_limiter import (
        estimate_weekly_limit_reset,
        invite_limits_for_display,
    )
    # Hosted: the ceilings the host enforces, remembered from the last pull
    # (the local row carries neither). Self-hosted: the local pace.
    _eff_weekly_cap, daily_limit = await invite_limits_for_display(rate_data)
    # Cumulative, from the campaign rows — not today's acceptances over today's
    # sends, which measures nothing. See _pooled_acceptance.
    acceptance_rate, _ = _pooled_acceptance(list(campaign_stats.values()))
    hs = compute_health_score(
        ssi_score=ssi_score,
        acceptance_rate=acceptance_rate,
        total_sent=total_sent_lifetime,
        daily_sent=sent,
        daily_limit=daily_limit,
        weekly_sent=weekly_sent,
        weekly_limit=_eff_weekly_cap,
        sending_days_7d=sending_days,
    )

    output.append(format_health_score(hs))
    output.append("")

    # ── Scheduler daemon health ──
    # Reported here because the failure it describes is invisible everywhere
    # else: with scheduler_daemon set, MCP servers deliberately do not schedule,
    # so if the daemon is not running then nothing schedules at all and every
    # other line on this page still looks normal.
    output.extend(_daemon_health_lines())

    # DM and follow-up counts from local DB
    from datetime import date, datetime
    _today_start = int(datetime.combine(date.today(), datetime.min.time()).timestamp())
    try:
        dms_today, followups_today = await run_db(_activity_counts_today, _today_start)
    except Exception:
        dms_today = 0
        followups_today = 0

    output.append("Activity:")
    output.append(f"├── Invitations: {sent}/{daily_limit} today")
    if dms_today > 0:
        output.append(f"├── DMs sent: {dms_today} today")
    if followups_today > 0:
        output.append(f"├── Follow-ups: {followups_today} today")
    if dms_today > 0 and sent > 0:
        total_outreach_today = sent + dms_today
        output.append(f"├── Total outreach: {total_outreach_today} today")
    output.append(f"├── Weekly: {weekly_sent}/{_eff_weekly_cap} invitations")
    if linkedin_pending_count is not None:
        output.append(f"├── LinkedIn pending: {linkedin_pending_count} sent invitations")
    if weekly_sent >= _eff_weekly_cap:
        _, _, _eta_msg = await estimate_weekly_limit_reset()
        output.extend(_weekly_limit_lines(_eta_msg))
    # Email overflow stats
    from ..services.channel_selector import has_email_channel
    if await run_db(has_email_channel):
        email_rate = await db.get_email_rate_limit_today()
        email_sent = email_rate.get("sent", 0)
        if email_sent > 0 or sent >= daily_limit or weekly_sent >= _eff_weekly_cap:
            output.append(f"├── 📧 Email overflow: {email_sent} today")
    if inmail_credits >= 0:
        output.append(f"├── InMail credits: {inmail_credits}")
    from ..tier import as_bool
    has_sales_nav = as_bool(await db.get_setting("has_sales_navigator", False))
    if has_sales_nav:
        output.append("├── License: Sales Navigator ✓ (300-char invites, SN search)")
    hubspot_key = await db.get_setting("hubspot_api_key", "")
    if hubspot_key:
        output.append("├── HubSpot: Connected ✓ — crm_sync() to push deals")
    if ssi_score:
        ssi_pillars = ssi_data.get("pillars", [])
        pillar_str = ", ".join(f"{p['name']}: {p['score']}" for p in ssi_pillars if p.get("name")) if ssi_pillars else ""
        ssi_line = f"├── SSI Score: {ssi_score}/100"
        if pillar_str:
            ssi_line += f" ({pillar_str})"
        output.append(ssi_line)
    # Gated on whether anything was ever invited, not on today's sends: a
    # quiet day does not erase a cumulative rate.
    output.append(
        f"└── Acceptance rate: {acceptance_rate:.0%}"
        if any(s.get("invited") for s in campaign_stats.values())
        else "└── Acceptance rate: No data yet"
    )
    output.append("")

    output.extend(await _action_health_lines())

    # ── Monthly usage (a cap only where one is enforced) ──
    output.extend(_monthly_usage_lines(
        usage,
        active_campaigns=len([c for c in campaigns if c['status'] in ('active', 'draft')]),
    ))

    # ── Campaigns ──
    if not campaigns:
        if offline and config.is_backend_mode():
            output.append("⚠️ Backend unreachable — your campaigns may still exist on the server.")
            output.append("Try again in a moment, or re-authenticate with setup_profile(backend_jwt='...').")
        else:
            output.append("No campaigns yet.")
            output.append("Create one: create_campaign(\"your target description\")")
    else:
        output.append(f"Campaigns ({len(campaigns)}):")
        for i, camp in enumerate(campaigns):
            is_last = i == len(campaigns) - 1
            prefix = "└──" if is_last else "├──"

            stats = campaign_stats[camp["id"]]
            status_icon = {
                "active": "🟢",
                "paused": "⏸️",
                "completed": "✅",
                "draft": "📝",
            }.get(camp["status"], "⚪")

            hot = stats.get("hot_leads", 0)
            hot_str = f" 🔥{hot}" if hot > 0 else ""

            # Context-aware label: "DMed" for connections-only, "sent" for invitations
            _camp_cfg = json.loads(camp.get("config_json") or "{}")
            _is_dm_only = (
                flag_enabled(_camp_cfg, "connections_only", default=False)
                or not flag_enabled(_camp_cfg, "enable_invitations")
            )
            _action_label = "DMed" if _is_dm_only else "sent"

            output.append(
                f"{prefix} {status_icon} {camp['name']} — "
                f"{stats['invited']} {_action_label}, "
                f"{stats['connected']} connected, "
                f"{stats['replied']} replied{hot_str}"
            )

    # ── Dashboard freshness ──
    # The numbers above are read straight from the local DB and are correct.
    # heylead.dev shows whatever was last pushed to it, which in observe mode is
    # not these. Say which campaigns it is frozen on, right beneath them.
    output.extend(await _dashboard_freshness_lines())

    output.append("")

    # ── Saved ICPs ──
    icps = await db.list_icps(status="active")
    if icps:
        output.append(f"Saved ICPs ({len(icps)}):")
        for i, icp in enumerate(icps[:5]):
            is_last = i == min(4, len(icps) - 1)
            prefix = "└──" if is_last else "├──"
            confidence = icp.get("confidence", 0.5)
            output.append(
                f"{prefix} `{icp['id'][:8]}...` {icp['name']} "
                f"({stars(confidence)} {confidence:.0%})"
            )
        if len(icps) > 5:
            output.append(f"    ... and {len(icps) - 5} more")
        output.append("    Tip: create_campaign(icp_id=\"<id>\") to use a saved ICP")
        output.append("")

    output.extend(await _needs_attention_lines())

    # ── Hot leads summary ──
    def _query_hot_leads():
        from ..db.schema import get_db as _get_db
        _db = _get_db()
        rows = _db.execute(
            """SELECT c.name, c.title, c.company, c.linkedin_url, c.source,
                      MAX(o.updated_at) as last_update
               FROM outreaches o
               JOIN contacts c ON o.contact_id = c.id
               WHERE o.status = 'hot_lead'
               GROUP BY c.id
               ORDER BY last_update DESC
               LIMIT 5"""
        ).fetchall()
        _db.close()
        return rows
    hot_leads = await run_db(_query_hot_leads)

    if hot_leads:
        output.append(f"🔥 Hot Leads ({len(hot_leads)}):")
        for i, lead in enumerate(hot_leads):
            l = dict(lead)
            is_last = i == len(hot_leads) - 1
            prefix = "└──" if is_last else "├──"
            role = l.get("title", "")
            if l.get("company"):
                role += f" at {l['company']}" if role else l["company"]
            src = l.get("source", "search")
            src_label = SOURCE_LABELS.get(src, "")
            src_tag = f" [{src_label}]" if src_label and src not in ("search", "linkedin_search") else ""
            output.append(f"{prefix} {prospect_link(l['name'], l.get('linkedin_url', ''))} — {role}{src_tag}")
        output.append("")

    # ── Engagement stats ──
    eng_stats = await db.get_engagement_stats()
    eng_comments = eng_stats.get("comments", 0)
    eng_reactions = eng_stats.get("reactions", 0)
    eng_total = eng_comments + eng_reactions
    if eng_total > 0:
        output.append(f"💬 Engagements ({eng_total}):")
        output.append(f"├── Comments: {eng_comments}")
        output.append(f"└── Reactions: {eng_reactions}")
        output.append("")
    elif (not config.is_backend_mode()) and tier != TIER_PRO:
        eng_used = usage.get("engagements_sent", 0)
        if eng_used > 0:
            output.append(f"💬 Engagements: {eng_used}/{FREE_MAX_ENGAGEMENTS} this month")
            output.append("")

    # ── Engagement verification stats ──
    try:
        v_stats = await db.get_engagement_verification_stats()
        v_total = sum(v_stats.values())
        if v_total > 0:
            parts = []
            if v_stats["verified"]:
                parts.append(f"{v_stats['verified']} verified")
            if v_stats["unverified"]:
                parts.append(f"{v_stats['unverified']} unverified")
            if v_stats["trust_api"]:
                parts.append(f"{v_stats['trust_api']} trust_api")
            if v_stats["pending"]:
                parts.append(f"{v_stats['pending']} pending")
            output.append(f"Verification: {' | '.join(parts)}")
            output.append("")
    except Exception as e:
        logger.warning("Verification stats failed: %s", e)

    # ── Follow-up ready hint ──
    max_followups = PRO_MAX_FOLLOWUPS if tier == TIER_PRO else FREE_MAX_FOLLOWUPS
    total_followup_ready = 0
    for camp in campaigns:
        if camp.get("status") in ("active", "draft"):
            total_followup_ready += await db.count_followup_ready(camp["id"], max_followups)
    if total_followup_ready > 0:
        output.append(
            f"💬 {total_followup_ready} prospect{'s' if total_followup_ready != 1 else ''} "
            "ready for follow-up — use send_message(action=\"followup\")"
        )
        output.append("")

    # ── Paused campaigns hint (with pause reason) ──
    paused_campaigns = [c for c in campaigns if c.get("status") == "paused"]
    if paused_campaigns:
        import json as _pj
        from ..linkedin.rate_limiter import estimate_weekly_limit_reset as _ewlr_p
        output.append(f"⏸️ {len(paused_campaigns)} paused campaign(s):")
        for pc in paused_campaigns[:3]:
            _pc_cfg = _pj.loads(pc.get("config_json") or "{}")
            _pr = _pc_cfg.get("pause_reason", "user")
            if _pr == "weekly_limit":
                _, _, _p_eta = await _ewlr_p()
                if pc.get("mode") == "autopilot":
                    output.append(f"   └── {pc['name']} — auto-paused (weekly limit) — auto-resumes in {_p_eta}")
                else:
                    output.append(f"   └── {pc['name']} — paused (weekly limit) — resume_campaign(\"{pc['id'][:8]}...\")")
            elif _pr == "emergency_stop":
                output.append(f"   └── {pc['name']} — emergency stopped — resume_campaign(\"{pc['id'][:8]}...\")")
            else:
                output.append(f"   └── {pc['name']} — resume_campaign(\"{pc['id'][:8]}...\")")
        output.append("")

    # ── Archived campaigns hint ──
    completed_campaigns = [c for c in campaigns if c.get("status") == "completed"]
    if completed_campaigns:
        output.append(f"📦 {len(completed_campaigns)} archived campaign(s)")
        output.append("")

    # ── Brand strategy progress ──
    _brand_plan = await db.get_setting("brand_strategy")
    if _brand_plan and isinstance(_brand_plan, dict) and _brand_plan.get("weeks"):
        import time as _t
        from ..config import is_scheduler_enabled
        _bp_total = sum(len(w.get("actions", [])) for w in _brand_plan.get("weeks", []))
        _bp_done = sum(
            1 for w in _brand_plan.get("weeks", [])
            for a in w.get("actions", []) if a.get("status") == "completed"
        )
        output.append(f"🎯 Brand Strategy: {_bp_done}/{_bp_total} actions")
        output.append(f"   {progress_bar(_bp_done, _bp_total, 15)}")
        _created = _brand_plan.get("created_at", 0)
        if _created:
            _week_idx = min((_t.time() - _created) // 604800, len(_brand_plan["weeks"]) - 1)
            _week_idx = max(0, int(_week_idx))
            _theme = _brand_plan["weeks"][_week_idx].get("theme", "")
            if _theme:
                output.append(f"   Week {_week_idx + 1}: {_theme}")
        # Show automation status when scheduler is active
        if is_scheduler_enabled():
            output.append("   Automation: Active (posts + engagement run automatically)")
            if _bp_done >= _bp_total:
                output.append('   Re-analysis scheduled at 28-day mark')
            else:
                output.append('   Progress: brand_strategy(action="progress")')
        else:
            if _bp_done < _bp_total:
                output.append('   Next: brand_strategy(action="execute")')
            else:
                output.append('   All done! Run brand_strategy(action="progress") for results.')
        output.append("")

    # ── Strategy Engine ──
    try:
        strat = await db.get_strategy_summary()
        if strat.get("active_patterns", 0) > 0 or strat.get("total_actions", 0) > 0:
            top = strat.get("top_pattern")
            top_str = f" | Top: {top['pattern_key']}" if top else ""
            output.append(
                f"\U0001f9e0 Strategy Engine: {strat['active_patterns']} patterns, "
                f"{strat['total_actions']} actions "
                f"({strat.get('validated_actions', 0)} validated)"
                f"{top_str}"
            )
            if strat.get("spawned_campaigns", 0) > 0:
                output.append(f"   Auto-spawned campaigns: {strat['spawned_campaigns']}")
            output.append('   Details: show_strategy()')
            output.append("")
    except Exception:
        pass

    # ── Inbound Pipeline ──
    try:
        funnel = await db.get_inbound_funnel_stats()
        total_inbound = sum(funnel.get("by_status", {}).values())
        if total_inbound > 0:
            by_status = funnel.get("by_status", {})
            by_intent = funnel.get("by_intent", {})
            new_count = by_status.get("new", 0)
            qualified_count = by_status.get("qualified", 0)
            engaged_count = by_status.get("engaged", 0)
            converted_count = by_status.get("converted", 0)

            output.append(f"Inbound Pipeline ({total_inbound} signals):")

            # Status breakdown
            status_parts = []
            if new_count:
                status_parts.append(f"{new_count} new")
            if qualified_count:
                status_parts.append(f"{qualified_count} qualified")
            if engaged_count:
                status_parts.append(f"{engaged_count} engaged")
            if converted_count:
                status_parts.append(f"{converted_count} converted")
            if status_parts:
                output.append(f"   {' | '.join(status_parts)}")

            # Intent breakdown for qualified leads
            buying = by_intent.get("buying_signal", 0)
            networking = by_intent.get("networking", 0)
            partnership = by_intent.get("partnership", 0)
            vendor = by_intent.get("vendor_pitch", 0)
            intent_parts = []
            if buying:
                intent_parts.append(f"{buying} buying signal")
            if networking:
                intent_parts.append(f"{networking} networking")
            if partnership:
                intent_parts.append(f"{partnership} partnership")
            if vendor:
                intent_parts.append(f"{vendor} vendor pitch")
            if intent_parts:
                output.append(f"   Intents: {', '.join(intent_parts)}")

            # Top match
            top_leads = await db.list_inbound_signals(status="qualified", limit=1)
            if top_leads:
                top = top_leads[0]
                top_name = top.get("sender_name", "Unknown")
                top_headline = top.get("sender_headline", "")
                top_conf = top.get("confidence", 0) or 0
                badge = "🟢" if top_conf >= 0.7 else "🟡" if top_conf >= 0.4 else "🔴"
                top_line = f"   Top: {badge} {top_name}"
                if top_headline:
                    top_line += f" — {top_headline}"
                top_line += f" ({top_conf:.0%})"
                output.append(top_line)

            output.append("")
    except Exception as e:
        logger.debug("Inbound pipeline stats failed: %s", e)

    # ── Signal Intelligence ──
    try:
        from ..services.signal_service import format_signal_dashboard
        signal_section = await run_db(format_signal_dashboard, days=7)
        if signal_section:
            output.append(signal_section)
    except Exception as e:
        logger.debug("Signal dashboard failed: %s", e)

    # ── Global contact base ──
    try:
        gc_stats = await db.get_global_contact_stats()
        if gc_stats["total"] > 0:
            output.append("")
            output.append("Contact Base")
            parts = [f"{gc_stats['total']} people"]
            for stage, count in gc_stats["by_lifecycle"].items():
                if count > 0:
                    parts.append(f"{count} {stage}")
            output.append("  " + "  |  ".join(parts))
            output.append("  Run contacts(action='stats') for full breakdown")
    except Exception:
        pass  # Non-critical

    # ── Post Intelligence ──
    try:
        pstats = await db.get_post_collection_stats(days=1)
        total_posts = pstats.get("total_posts", 0)
        if total_posts > 0:
            output.append(f"Post Intelligence ({total_posts} total posts):")
            output.append(f"├── Today: {pstats.get('posts_collected_today', 0)} collected, {pstats.get('posts_analyzed_today', 0)} analyzed")
            output.append(f"├── Authors scanned today: {pstats.get('authors_scanned_today', 0)}")
            coverage = pstats.get("analysis_coverage", 0)
            output.append(f"├── Analysis coverage: {coverage}%")
            try:
                def _query_research_counts():
                    from ..db.schema import get_db as _get_db
                    _db = _get_db()
                    _tc = _db.execute(
                        "SELECT COUNT(*) as cnt FROM contacts WHERE linkedin_id IS NOT NULL"
                    ).fetchone()
                    _rc = _db.execute(
                        "SELECT COUNT(*) as cnt FROM contacts WHERE research_status = 'complete'"
                    ).fetchone()
                    _pc = _db.execute(
                        "SELECT COUNT(*) as cnt FROM contacts WHERE research_status IS NULL OR research_status = 'pending'"
                    ).fetchone()
                    _db.close()
                    return (
                        _tc["cnt"] if _tc else 0,
                        _rc["cnt"] if _rc else 0,
                        _pc["cnt"] if _pc else 0,
                    )
                tc, rc, pc = await run_db(_query_research_counts)
                if tc > 0:
                    output.append(f"├── Research: {rc}/{tc} contacts researched ({pc} pending)")
            except Exception:
                pass
            output.append(f"└── Total analyzed: {pstats.get('total_analyzed', 0)}/{total_posts}")
            output.append("")
    except Exception:
        pass  # Non-critical

    # ── Cross-validation: usage counters vs the rows behind them ──
    # Local counters only count local sends, while the outreach and message
    # rows also arrive by sync from the cloud. In backend mode that guarantees
    # an "undercount" — a live install read 14 recorded vs 102 counted — so the
    # check belongs only where both sides have the same origin. The hosted
    # figures are compared in _show_overview_from_backend instead.
    if not config.is_backend_mode():
        try:
            output.extend(
                _usage_drift_lines(usage, await run_db(_monthly_actual_usage))
            )
        except Exception:
            pass

    # ── ICP Prospect Enrichment ──
    try:
        import time as _time_mod2
        _day_ago = int(_time_mod2.time()) - 86400
        def _query_enrichment_stats():
            from ..db.schema import get_db as _get_db
            _edb = _get_db()
            _s24h = _edb.execute(
                """SELECT source_detail, COUNT(*) as cnt
                   FROM contacts WHERE source = 'auto_enrichment' AND created_at >= ?
                   GROUP BY source_detail ORDER BY cnt DESC""",
                (_day_ago,),
            ).fetchall()
            _sall = _edb.execute(
                """SELECT source_detail, COUNT(*) as cnt
                   FROM contacts WHERE source = 'auto_enrichment'
                   GROUP BY source_detail ORDER BY cnt DESC""",
            ).fetchall()
            _edb.close()
            return _s24h, _sall
        _src_24h, _src_all = await run_db(_query_enrichment_stats)

        if _src_24h or _src_all:
            _labels = {
                "linkedin_search": "LinkedIn Search", "global_contacts": "Global Contacts",
                "signal_reeval": "Re-evaluated Signals", "job_change": "Job Changers",
                "signal_account": "Signal Accounts", "profile_viewer": "Profile Viewers",
                "post_author": "Post Authors", "competitor_commenter": "Competitor Commenters",
                "company_engager": "Company Engagers", "connection": "1st-Degree Connections",
                "post_commenter": "Post Commenters", "inbound_low_conf": "Inbound (Low Conf)",
            }
            _t24 = sum(r["cnt"] for r in _src_24h)
            _tall = sum(r["cnt"] for r in _src_all)
            output.append(f"Prospect Enrichment ({_t24} last 24h, {_tall} total):")
            _c24 = {r["source_detail"]: r["cnt"] for r in _src_24h}
            for row in _src_all:
                tag = row["source_detail"] or "unknown"
                label = _labels.get(tag, tag.replace("_", " ").title())
                c24 = _c24.get(tag, 0)
                output.append(f"  {label}: {c24} (24h) / {row['cnt']} (total)")
            output.append("")
    except Exception:
        pass

    # ── Pipeline Health ──
    try:
        def _query_pipeline_health():
            from ..db.schema import get_db as _get_db
            _hdb = _get_db()
            _health_items = []

            _orphaned = _hdb.execute(
                """SELECT COUNT(*) as cnt FROM outreaches o
                   WHERE o.status = 'connected' AND o.followup_count = 0
                   AND o.id NOT IN (
                       SELECT sj.outreach_id FROM scheduler_jobs sj
                       WHERE sj.outreach_id IS NOT NULL
                         AND sj.job_type IN ('send_dm', 'followup')
                         AND sj.status IN ('pending', 'running')
                   )"""
            ).fetchone()["cnt"]
            if _orphaned > 0:
                _health_items.append(f"  {_orphaned} orphaned prospects (connected, no pending jobs)")

            _unverified = _hdb.execute(
                "SELECT COUNT(*) as cnt FROM engagements WHERE verified_status = 'unverified'"
            ).fetchone()["cnt"]
            if _unverified > 0:
                _health_items.append(f"  {_unverified} unverified engagements")

            _pending_jobs = _hdb.execute(
                """SELECT job_type, COUNT(*) as cnt FROM scheduler_jobs
                   WHERE status = 'pending' GROUP BY job_type ORDER BY cnt DESC LIMIT 5"""
            ).fetchall()
            if _pending_jobs:
                _job_parts = [f"{r['job_type']}={r['cnt']}" for r in _pending_jobs]
                _health_items.append(f"  Pending jobs: {', '.join(_job_parts)}")

            _hdb.close()
            return _health_items

        _health_items = await run_db(_query_pipeline_health)
        if _health_items:
            output.append("Pipeline Health:")
            for item in _health_items:
                output.append(item)
            output.append("")
    except Exception:
        pass

    # ── Quick actions ──
    has_active = any(c.get("status") == "active" for c in campaigns)
    has_paused = len(paused_campaigns) > 0
    has_copilot = any(
        c.get("status") == "active" and c.get("mode") == "copilot"
        for c in campaigns
    )

    output.append("Quick actions:")
    output.append("├── \"any replies?\" → check_replies")
    output.append("├── \"what's next?\" → suggest_next_action")
    output.append("├── \"detailed report\" → campaign_report")
    if has_copilot:
        output.append("├── \"send messages\" → generate_and_send")
    if total_followup_ready > 0:
        output.append("├── \"send follow-up\" → send_followup")
    output.append("├── \"engage posts\" → engage_prospect")
    if has_active:
        output.append("├── \"pause outreach\" → pause_campaign")
    if has_paused:
        output.append("├── \"resume outreach\" → resume_campaign")
    output.append("├── \"export results\" → export_campaign")
    output.append("├── \"compare campaigns\" → compare_campaigns")
    output.append("├── \"edit campaign\" → edit_campaign")
    output.append("├── \"view conversation\" → show_conversation")
    output.append("├── \"retry errors\" → retry_failed")
    output.append("├── \"skip prospect\" → skip_prospect")
    output.append("├── \"stop everything\" → emergency_stop")
    output.append("├── \"brand audit\" → brand_strategy")
    output.append("├── \"sync to CRM\" → crm_sync")
    output.append("├── \"signal feed\" → show_signals")
    output.append("├── \"manage keywords\" → manage_watchlist")
    output.append("├── \"browse contacts\" → contacts")
    output.append("└── \"create campaign\" → create_campaign")
    output.extend(status_footer("overview"))

    return "\n".join(output)


async def _show_overview_from_backend(data: dict) -> str:
    """Format dashboard from live backend stats.

    Uses the same visual style as _show_overview() but with data
    from the backend's GET /api/v1/stats response.
    """
    from ..services.health_score import compute_health_score, format_health_score

    campaigns = data.get("campaigns", [])
    rl = data.get("rate_limits", {})
    usage = data.get("usage", {})
    eng = data.get("engagements", {})
    hot_leads = data.get("hot_leads", [])

    output = ["📊 **HeyLead Dashboard** (live)\n"]

    # ── Account Connectivity Warning ──
    acct_status = data.get("account_status", "connected")
    acct_message = data.get("account_status_message", "")
    if acct_status == "disconnected":
        output.append("⚠️ **LinkedIn Account Disconnected**\n")
        output.append("Your LinkedIn session has expired. All outreach is paused.")
        output.append("Go to https://heylead.dev/auth/login-url to reconnect,")
        output.append("then run setup_profile(backend_jwt='YOUR_TOKEN').\n")
    elif acct_status == "not_connected":
        # The workspace never connected LinkedIn (heylead-api #394) — nothing
        # expired, so the reconnect message above would be wrong.
        output.append("⚠️ **LinkedIn Not Connected**\n")
        output.append(
            f"LinkedIn isn't connected yet — sign in at {dashboard_url('login')} "
            "and connect it in Settings → Connected accounts, then run show_status again.\n"
        )
    elif acct_status == "degraded":
        output.append(f"⚠️ **Account Warning**: {acct_message}\n")

    try:
        output.extend(await mailbox_disconnect_banner())
    except Exception:
        pass

    # ── Account Health ──
    sent = rl.get("sent_today", 0)
    # The cap is enforced on the seat's count across every workspace it sends
    # from. /api/v1/stats' plain weekly_sent is one workspace's count, so a
    # seat inviting from two workspaces would read 28/100 while every invite
    # is being refused at 100. Prefer the seat figure; older backends lack it.
    weekly_sent = rl.get("weekly_sent_seat", rl.get("weekly_sent", 0))

    # Compute health score (SSI not available from backend, use 0).
    # The rate and the lifetime count come from the campaign rows, not from
    # today's counters — see _pooled_acceptance and invite_limits_for_display.
    from ..linkedin.rate_limiter import (
        estimate_weekly_limit_reset as _ewlr2,
        invite_limits_for_display,
    )
    # Both ceilings the hosted sender enforces, from this payload.
    _eff_wc2, daily_limit = await invite_limits_for_display(rl)
    acceptance_rate, total_sent_lifetime = _pooled_acceptance(campaigns)
    sending_days = min(7, weekly_sent) if weekly_sent > 0 else 0  # Approximate
    hs = compute_health_score(
        ssi_score=0,
        acceptance_rate=acceptance_rate,
        total_sent=total_sent_lifetime,
        daily_sent=sent,
        daily_limit=daily_limit,
        weekly_sent=weekly_sent,
        weekly_limit=_eff_wc2,
        sending_days_7d=sending_days,
    )

    output.append(format_health_score(hs))
    output.append("")
    dms_today = rl.get("dms_sent_today", 0)
    followups_today = rl.get("followups_today", 0)
    total_outreach_today = sent + dms_today

    output.append("Activity:")
    output.append(f"├── Invitations: {sent}/{daily_limit} today")
    if dms_today > 0:
        output.append(f"├── DMs sent: {dms_today} today")
    if followups_today > 0:
        output.append(f"├── Follow-ups: {followups_today} today")
    if dms_today > 0 and sent > 0:
        output.append(f"├── Total outreach: {total_outreach_today} today")
    output.append(f"├── Weekly: {weekly_sent}/{_eff_wc2} invitations")
    if weekly_sent >= _eff_wc2:
        _, _, _eta2 = await _ewlr2()
        output.extend(_weekly_limit_lines(_eta2))
    # Email overflow stats (cloud scheduler path)
    from ..services.channel_selector import has_email_channel as _has_email
    if await run_db(_has_email):
        _erl = await db.get_email_rate_limit_today()
        _es = _erl.get("sent", 0)
        if _es > 0 or sent >= daily_limit or weekly_sent >= _eff_wc2:
            output.append(f"├── 📧 Email overflow: {_es} today")
    # Gated on the lifetime send count, not today's: a quiet day does not erase
    # a cumulative rate, and the old `sent > 0` gate hid it on every such day.
    output.append(
        f"└── Acceptance rate: {acceptance_rate:.0%}"
        if total_sent_lifetime > 0
        else "└── Acceptance rate: No data yet"
    )
    output.append("")

    output.extend(await _action_health_lines())

    # ── Monthly usage (hosted: counts, never presented as caps) ──
    output.extend(_monthly_usage_lines(
        usage,
        active_campaigns=len([c for c in campaigns if c.get("status") in ("active", "draft")]),
    ))

    # ── Campaigns ──
    if not campaigns:
        output.append("No campaigns yet.")
        output.append("Create one: create_campaign(\"your target description\")")
    else:
        output.append(f"Campaigns ({len(campaigns)}):")
        for i, camp in enumerate(campaigns):
            is_last = i == len(campaigns) - 1
            prefix = "└──" if is_last else "├──"

            status_icon = {
                "active": "🟢",
                "paused": "⏸️",
                "completed": "✅",
                "draft": "📝",
            }.get(camp.get("status", ""), "⚪")

            hot = camp.get("hot_leads", 0)
            hot_str = f" 🔥{hot}" if hot > 0 else ""

            # Context-aware label: "DMed" for connections-only, "sent" for invitations
            _action_label = "DMed" if camp.get("connections_only", False) else "sent"

            output.append(
                f"{prefix} {status_icon} {camp['name']} — "
                f"{camp.get('invited', 0)} {_action_label}, "
                f"{camp.get('connected', 0)} connected, "
                f"{camp.get('replied', 0)} replied{hot_str}"
            )

    # ── Dashboard freshness ──
    # This path renders the backend's own figures, so a withheld campaign shows
    # here exactly as it does on heylead.dev: frozen, and captioned as current.
    output.extend(await _dashboard_freshness_lines())

    output.append("")

    output.extend(await _needs_attention_lines(data.get("needs_attention") or None))

    # ── Hot leads ──
    if hot_leads:
        output.append(f"🔥 Hot Leads ({len(hot_leads)}):")
        for i, lead in enumerate(hot_leads):
            is_last = i == len(hot_leads) - 1
            prefix = "└──" if is_last else "├──"
            role = lead.get("title", "")
            if lead.get("company"):
                role += f" at {lead['company']}" if role else lead["company"]
            output.append(f"{prefix} {lead.get('name', 'Unknown')} — {role}")
        output.append("")

    # ── Engagement stats ──
    eng_comments = eng.get("comments", 0)
    eng_reactions = eng.get("reactions", 0)
    eng_total = eng_comments + eng_reactions
    if eng_total > 0:
        output.append(f"💬 Engagements ({eng_total}):")
        output.append(f"├── Comments: {eng_comments}")
        output.append(f"└── Reactions: {eng_reactions}")
        output.append("")

    # ── Local-only sections (ICPs, signals, brand, strategy — still from local DB) ──
    try:
        icps = await db.list_icps(status="active")
        if icps:
            output.append(f"Saved ICPs ({len(icps)}):")
            for i, icp in enumerate(icps[:5]):
                is_last = i == min(4, len(icps) - 1)
                prefix = "└──" if is_last else "├──"
                confidence = icp.get("confidence", 0.5)
                output.append(
                    f"{prefix} `{icp['id'][:8]}...` {icp['name']} "
                    f"({stars(confidence)} {confidence:.0%})"
                )
            if len(icps) > 5:
                output.append(f"    ... and {len(icps) - 5} more")
            output.append("    Tip: create_campaign(icp_id=\"<id>\") to use a saved ICP")
            output.append("")
    except Exception:
        pass

    # ── Signal Intelligence ──
    try:
        from ..services.signal_service import format_signal_dashboard
        signal_section = await run_db(format_signal_dashboard, days=7)
        if signal_section:
            output.append(signal_section)
    except Exception:
        pass

    # ── Contact Base ──
    try:
        gc_stats = await db.get_global_contact_stats()
        if gc_stats["total"] > 0:
            output.append("")
            output.append("Contact Base")
            parts = [f"{gc_stats['total']} people"]
            for stage, count in gc_stats["by_lifecycle"].items():
                if count > 0:
                    parts.append(f"{count} {stage}")
            output.append("  " + "  |  ".join(parts))
            output.append("  Run contacts(action='stats') for full breakdown")
    except Exception:
        pass  # Non-critical

    # ── Post Intelligence ──
    try:
        pstats = await db.get_post_collection_stats(days=1)
        total_posts = pstats.get("total_posts", 0)
        if total_posts > 0:
            output.append(f"Post Intelligence ({total_posts} total posts):")
            output.append(f"├── Today: {pstats.get('posts_collected_today', 0)} collected, {pstats.get('posts_analyzed_today', 0)} analyzed")
            output.append(f"├── Authors scanned today: {pstats.get('authors_scanned_today', 0)}")
            coverage = pstats.get("analysis_coverage", 0)
            output.append(f"├── Analysis coverage: {coverage}%")
            try:
                def _query_research_counts_backend():
                    from ..db.schema import get_db as _get_db
                    _rdb = _get_db()
                    _tc = _rdb.execute(
                        "SELECT COUNT(*) as cnt FROM contacts WHERE linkedin_id IS NOT NULL"
                    ).fetchone()
                    _rc = _rdb.execute(
                        "SELECT COUNT(*) as cnt FROM contacts WHERE research_status = 'complete'"
                    ).fetchone()
                    _pc = _rdb.execute(
                        "SELECT COUNT(*) as cnt FROM contacts WHERE research_status IS NULL OR research_status = 'pending'"
                    ).fetchone()
                    _rdb.close()
                    return (
                        _tc["cnt"] if _tc else 0,
                        _rc["cnt"] if _rc else 0,
                        _pc["cnt"] if _pc else 0,
                    )
                tc, rc, pc = await run_db(_query_research_counts_backend)
                if tc > 0:
                    output.append(f"├── Research: {rc}/{tc} contacts researched ({pc} pending)")
            except Exception:
                pass
            output.append(f"└── Total analyzed: {pstats.get('total_analyzed', 0)}/{total_posts}")
            output.append("")
    except Exception:
        pass  # Non-critical

    # ── Cross-validation: usage counters vs the rows behind them ──
    # The service recomputes them; the local mirror is partial in backend mode
    # and would invent a drift of its own.
    try:
        output.extend(_usage_drift_lines(usage, data.get("usage_actual")))
    except Exception:
        pass

    # ── Quick actions ──
    has_active = any(c.get("status") == "active" for c in campaigns)
    has_copilot = any(
        c.get("status") == "active" and c.get("mode") == "copilot"
        for c in campaigns
    )

    output.append("Quick actions:")
    output.append("├── \"any replies?\" → check_replies")
    output.append("├── \"what's next?\" → suggest_next_action")
    output.append("├── \"detailed report\" → campaign_report")
    if has_copilot:
        output.append("├── \"send messages\" → generate_and_send")
    output.append("├── \"engage posts\" → engage_prospect")
    if has_active:
        output.append("├── \"pause outreach\" → pause_campaign")
    output.append("├── \"export results\" → export_campaign")
    output.append("├── \"compare campaigns\" → compare_campaigns")
    output.append("├── \"edit campaign\" → edit_campaign")
    output.append("├── \"view conversation\" → show_conversation")
    output.append("├── \"retry errors\" → retry_failed")
    output.append("├── \"skip prospect\" → skip_prospect")
    output.append("├── \"stop everything\" → emergency_stop")
    output.append("├── \"brand audit\" → brand_strategy")
    output.append("├── \"sync to CRM\" → crm_sync")
    output.append("├── \"signal feed\" → show_signals")
    output.append("├── \"manage keywords\" → manage_watchlist")
    output.append("└── \"create campaign\" → create_campaign")
    output.extend(status_footer("overview"))

    return "\n".join(output)


def _business_hours_line(config: dict) -> str | None:
    """The business-hours line for a campaign, or None when it is the default.

    Since 19 Sep 2026 the cloud sends only in business hours unless a
    campaign switched that off (heylead-api working_hours: weekdays
    08:00-22:00 in the owner's timezone, London as the fallback, when the
    workspace chose no window). Like the other
    settings here, only a departure from what a new campaign starts with is
    shown.
    """
    value = (config or {}).get("send_in_business_hours")
    if value is False or str(value).strip().lower() in ("off", "false", "0", "no"):
        return "🕐 Business hours: off (sends at any hour and on weekends)"
    return None


async def _show_campaign_detail(campaign_id: str) -> str:
    """Show detailed stats for a specific campaign."""

    campaign = await db.get_campaign(campaign_id)
    if not campaign:
        return f"❌ Campaign not found: {campaign_id}"

    stats = await db.get_campaign_stats(campaign_id)
    config = json.loads(campaign.get("config_json") or "{}")
    icp = json.loads(campaign.get("icp_json") or "{}")

    # Calculate days since creation
    import time
    created = campaign.get("created_at", 0)
    days = (int(time.time()) - created) // 86400 if created else 0

    output = [
        f"📊 Campaign: **{campaign['name']}** (Day {days})",
        f"   Mode: {'🤖 Autopilot' if campaign.get('mode') == 'autopilot' else '👤 Copilot'}",
        f"   Target: {config.get('target_description', 'N/A')}",
        "",
    ]

    # Settings summary (only show non-default values)
    settings_lines: list[str] = []

    # Voice mode
    vm = config.get("voice_mode", "text_only")
    if vm != "text_only":
        settings_lines.append(f"🎤 Voice: {vm}")

    # Warm-up toggles — show disabled ones
    toggles = {
        "enable_profile_views": ("Profile Views", True),
        "enable_follows": ("Follows", True),
        "enable_endorsements": ("Endorsements", True),
        "enable_engagements": ("Engagements", True),
        "enable_followups": ("Follow-ups", True),
    }
    disabled = [label for key, (label, default) in toggles.items() if not config.get(key, default)]
    if disabled:
        settings_lines.append(f"⏸️ Disabled: {', '.join(disabled)}")

    # Engagement mode
    em = config.get("engagement_mode", "auto")
    if em != "auto":
        settings_lines.append(f"💬 Engagement: {em.replace('_', ' ')}")

    # Follow-up schedule
    fdd = config.get("followup_delay_days")
    mf = config.get("max_followups")
    # Hidden when it matches what a new campaign starts with
    # (create_campaign.build_campaign_config, aligned with the backend).
    if fdd and fdd != [1, 3, 7, 14]:
        settings_lines.append(f"📅 Follow-up schedule: day {','.join(map(str, fdd))}")
    elif mf and mf != 4:
        settings_lines.append(f"📅 Max follow-ups: {mf}")

    # Business hours
    bh_line = _business_hours_line(config)
    if bh_line:
        settings_lines.append(bh_line)

    # Active days
    ad = config.get("active_days")
    if ad and ad != [0, 1, 2, 3, 4]:
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        settings_lines.append(f"📆 Active days: {','.join(day_names[d] for d in ad if d < 7)}")

    if settings_lines:
        output.append("Settings:")
        for i, line in enumerate(settings_lines):
            prefix = "└──" if i == len(settings_lines) - 1 else "├──"
            output.append(f"{prefix} {line}")
        output.append("")

    # Stats tree
    total = stats.get("total_prospects", 0)
    invited = stats.get("invited", 0)
    connected = stats.get("connected", 0)
    replied = stats.get("replied", 0)
    hot = stats.get("hot_leads", 0)
    skipped = stats.get("skipped", 0)
    pending = total - invited

    _is_dm_only_detail = (
        flag_enabled(config, "connections_only", default=False)
        or not flag_enabled(config, "enable_invitations")
    )
    output.append("Progress:")
    output.append(f"├── DMs sent: {invited}" if _is_dm_only_detail else f"├── Invitations sent: {invited}")
    acc_str = f"{stats['acceptance_rate']:.0%}"
    raw = stats.get('raw_acceptance_rate', 0)
    if raw > 0 and abs(raw - stats['acceptance_rate']) > 0.01:
        acc_str += f" (raw: {raw:.0%})"
    output.append(f"├── Accepted: {connected} ({acc_str})" if invited > 0 else f"├── Accepted: {connected}")
    output.append(f"├── Replies: {replied} ({stats['reply_rate']:.0%})" if connected > 0 else f"├── Replies: {replied}")
    output.append(f"├── Hot leads: {hot} 🔥" if hot > 0 else f"├── Hot leads: {hot}")
    if skipped > 0:
        output.append(f"├── Skipped: {skipped}")
    output.append(f"└── Remaining: {pending} prospects queued")
    output.append("")

    output.extend(await _action_health_lines(campaign_id))

    # Funnel visualization
    if invited > 0:
        output.append("Funnel:")
        output.append(f"├── Queued:    {progress_bar(total, total, 20)} {total}")
        output.append(f"├── Sent:      {progress_bar(invited, total, 20)} {invited}")
        output.append(f"├── Connected: {progress_bar(connected, total, 20)} {connected}")
        output.append(f"├── Replied:   {progress_bar(replied, total, 20)} {replied}")
        output.append(f"└── Hot leads: {progress_bar(hot, total, 20)} {hot}")
        output.append("")

    # Velocity metrics
    velocity = await db.get_campaign_velocity(campaign_id)
    cnt_accept = velocity.get("count_accepted", 0)
    if cnt_accept > 0:
        avg_tta = velocity.get("avg_time_to_accept")
        avg_ttr = velocity.get("avg_time_to_reply")
        output.append("Velocity:")
        output.append(f"\u251c\u2500\u2500 Avg time to accept: {format_duration(avg_tta)}")
        if avg_ttr is not None:
            output.append(f"\u2514\u2500\u2500 Avg time to reply: {format_duration(avg_ttr)}")
        else:
            output.append(f"\u2514\u2500\u2500 Avg time to reply: No replies yet")
        output.append("")

    # Outcomes section
    outcomes = await db.get_campaign_outcomes(campaign_id)
    if outcomes["total_closed"] > 0:
        output.append("Outcomes:")
        output.append(f"\u251c\u2500\u2500 \U0001f3c6 Won: {outcomes['closed_happy']}")
        output.append(f"\u251c\u2500\u2500 \U0001f4c9 Lost: {outcomes['closed_unhappy']}")
        if outcomes["opted_out"] > 0:
            output.append(f"\u251c\u2500\u2500 \U0001f6ab Opted out: {outcomes['opted_out']}")
        output.append(f"\u2514\u2500\u2500 Conversion: {conversion_rate_display(outcomes['closed_happy'], outcomes['closed_unhappy'])}")
        output.append("")

    # Engagement stats for this campaign
    camp_eng = await db.get_engagement_stats(campaign_id)
    camp_comments = camp_eng.get("comments", 0)
    camp_reactions = camp_eng.get("reactions", 0)
    camp_eng_total = camp_comments + camp_reactions
    if camp_eng_total > 0:
        output.append("Engagements:")
        output.append(f"├── Comments: {camp_comments}")
        output.append(f"└── Reactions: {camp_reactions}")
        # Per-campaign verification stats
        try:
            cv = await db.get_engagement_verification_stats(campaign_id)
            cv_total = sum(cv.values())
            if cv_total > 0:
                parts = []
                if cv["verified"]:
                    parts.append(f"{cv['verified']} verified")
                if cv["unverified"]:
                    parts.append(f"{cv['unverified']} unverified")
                if cv["trust_api"]:
                    parts.append(f"{cv['trust_api']} trust_api")
                if cv["pending"]:
                    parts.append(f"{cv['pending']} pending")
                output.append(f"    Verification: {' | '.join(parts)}")
        except Exception:
            pass
        output.append("")

    # Voice memo stats
    voice_stats = await db.get_voice_memo_stats(campaign_id)
    voice_sent = voice_stats.get("voice_sent", 0)
    if voice_sent > 0:
        voice_rr = voice_stats.get("voice_reply_rate", 0)
        text_rr = voice_stats.get("text_reply_rate", 0)
        text_sent = voice_stats.get("text_sent", 0)
        output.append(f"🎤 Voice memos: {voice_sent} sent (text: {text_sent})")
        if voice_stats.get("voice_total_outreaches", 0) > 0:
            output.append(f"   Reply rate: voice {voice_rr:.0%} vs text {text_rr:.0%}")
        output.append("")

    # Stale leads warning
    stale = await db.get_stale_outreaches(campaign_id, stale_days=14)
    if stale:
        output.append(f"⚠️ {len(stale)} stale lead{'s' if len(stale) != 1 else ''} (no activity 14+ days)")
        output.append(f"   → Run campaign_report for details")
        output.append("")

    # Top prospects with status
    def _query_top_contacts():
        from ..db.schema import get_db as _get_db
        _db = _get_db()
        rows = _db.execute(
            """SELECT c.name, c.title, c.company, c.fit_score, o.status
               FROM contacts c
               JOIN outreaches o ON o.contact_id = c.id
               WHERE c.campaign_id = ?
               ORDER BY c.fit_score DESC
               LIMIT 5""",
            (campaign_id,),
        ).fetchall()
        _db.close()
        return rows
    top_contacts = await run_db(_query_top_contacts)

    if top_contacts:
        output.append("Top Prospects:")
        status_icons = {
            "pending": "⏳",
            "invited": "📤",
            "connected": "🤝",
            "messaged": "💬",
            "replied": "📩",
            "hot_lead": "🔥",
            "review_pending": "👀",
            "skipped": "⏭️",
            "opted_out": "🚫",
            "closed_happy": "✅",
            "closed_unhappy": "❌",
            "error": "⚠️",
        }
        for i, contact in enumerate(top_contacts):
            c = dict(contact)
            is_last = i == len(top_contacts) - 1
            prefix = "└──" if is_last else "├──"
            icon = status_icons.get(c.get("status", ""), "⚪")
            role = c.get("title", "")
            if c.get("company"):
                role += f" at {c['company']}" if role else c["company"]
            output.append(f"{prefix} {icon} {c['name']} — {role} ({stars(c.get('fit_score', 0))})")
        output.append("")

    output.extend(status_footer("campaign", campaign_id))
    return "\n".join(output)
