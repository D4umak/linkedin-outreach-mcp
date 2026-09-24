"""Tool: campaign_report — Detailed campaign analytics with outcomes and stale lead warnings.

Provides deeper analytics than show_status, focused on outcome tracking,
conversion rates, stale lead detection, and engagement ROI.
"""

from __future__ import annotations

import json
import logging
import time as _time

from ..db import aio as db
from ..db.async_bridge import run_db
from ..constants import SOURCE_LABELS
from ..formatter import (
    conversion_rate_display,
    format_duration,
    funnel_bar,
    outcome_icon,
    outcome_label,
    person_line,
    progress_bar,
    sparkline,
    stars,
    table,
)

logger = logging.getLogger(__name__)


def _person(row: dict) -> str:
    """A won, lost or stale lead as `[Name](url) — title at company`."""
    return person_line(
        row.get("name") or "Unknown",
        row.get("linkedin_url") or "",
        title=row.get("title") or "",
        company=row.get("company") or "",
    )


async def run_campaign_report(
    campaign_id: str = "",
) -> str:
    """Generate a detailed analytics report for a campaign.

    Args:
        campaign_id: Which campaign to report on. Uses the active campaign if empty.
    """

    # ── Pre-checks ──
    setup_done = await db.get_setting("setup_complete", False)
    if not setup_done:
        return (
            "Setup required before generating reports.\n\n"
            "Please run setup_profile first."
        )

    # ── Sync from backend before reading local DB ──
    try:
        from .. import config
        if config.is_backend_mode():
            from ..services.cloud_sync import ensure_synced
            await ensure_synced()
    except Exception as e:
        logger.debug("Pre-report sync failed: %s", e)

    # ── Resolve campaign ──
    campaign, err = await db.find_active_campaign(campaign_id)
    if not campaign and not campaign_id:
        campaigns = await db.list_campaigns()
        if not campaigns:
            return (
                "No campaigns to report on.\n\n"
                "Create one first: create_campaign(\"your target description\")"
            )
        campaign = campaigns[0]
    elif not campaign:
        return err

    campaign_id = campaign["id"]
    config = {}
    try:
        config = json.loads(campaign.get("config_json", "{}") or "{}")
    except (json.JSONDecodeError, TypeError):
        pass

    # ── Load all data ──
    stats = await db.get_campaign_stats(campaign_id)
    outcomes = await db.get_campaign_outcomes(campaign_id)
    eng_stats = await db.get_engagement_stats(campaign_id)
    stale = await db.get_stale_outreaches(campaign_id, stale_days=14)
    velocity = await db.get_campaign_velocity(campaign_id)

    # Calculate days since creation
    created = campaign.get("created_at", 0)
    days = (int(_time.time()) - created) // 86400 if created else 0

    # ── Section 1: Header ──
    mode_str = "\U0001f916 Autopilot" if campaign.get("mode") == "autopilot" else "\U0001f464 Copilot"
    status_str = campaign.get("status", "active").capitalize()

    output = [
        f"\U0001f4ca Campaign Report: **{campaign['name']}** (Day {days})",
        f"   Mode: {mode_str} | Status: {status_str}",
        f"   Target: {config.get('target_description', 'N/A')}",
        "",
    ]

    # ── Section 2: Funnel ──
    total = stats.get("total_prospects", 0)
    invited = stats.get("invited", 0)
    connected = stats.get("connected", 0)
    replied = stats.get("replied", 0)
    hot = stats.get("hot_leads", 0)

    won = stats.get("closed_happy", 0)

    if total > 0:
        output.append("Funnel:")
        output.append(f"  {funnel_bar('Prospects', total, total)}")
        output.append(f"  {funnel_bar('Invited', invited, total)}")
        output.append(f"  {funnel_bar('Connected', connected, total)}")
        output.append(f"  {funnel_bar('Replied', replied, total)}")
        output.append(f"  {funnel_bar('Hot Lead', hot, total)}")
        output.append(f"  {funnel_bar('Won', won, total)}")
        output.append("")

    # ── Section 2.5: Velocity (time-based metrics) ──
    avg_tta = velocity.get("avg_time_to_accept")
    avg_ttr = velocity.get("avg_time_to_reply")
    avg_full = velocity.get("avg_time_invite_to_reply")
    cnt_accept = velocity.get("count_accepted", 0)
    cnt_reply = velocity.get("count_replied", 0)

    if cnt_accept > 0 or cnt_reply > 0:
        output.append("Velocity:")
        if cnt_accept > 0:
            output.append(f"\u251c\u2500\u2500 Avg time to accept: {format_duration(avg_tta)} (n={cnt_accept})")
            fastest = velocity.get("fastest_accept")
            slowest = velocity.get("slowest_accept")
            if fastest is not None and slowest is not None and cnt_accept > 1:
                output.append(f"\u2502   Range: {format_duration(fastest)} \u2014 {format_duration(slowest)}")
        if cnt_reply > 0:
            output.append(f"\u251c\u2500\u2500 Avg time to reply: {format_duration(avg_ttr)} (n={cnt_reply})")
            fastest_reply = velocity.get("fastest_reply")
            slowest_reply = velocity.get("slowest_reply")
            if fastest_reply is not None and slowest_reply is not None and cnt_reply > 1:
                output.append(f"\u2502   Range: {format_duration(fastest_reply)} \u2014 {format_duration(slowest_reply)}")
        if avg_full is not None:
            output.append(f"\u2514\u2500\u2500 Avg invite \u2192 reply: {format_duration(avg_full)}")
        output.append("")

    # ── Section 2.6: Read rate ──
    read_data = await db.get_read_rate(campaign_id)
    if read_data["total_sdr_messages"] > 0:
        rr = read_data["read_rate"]
        read_count = read_data["read_messages"]
        total_msgs = read_data["total_sdr_messages"]
        output.append(f"Read rate: {rr:.0%} ({read_count}/{total_msgs} messages read)")
        output.append("")

    # ── Section 3: Outcomes ──
    closed_happy = outcomes.get("closed_happy", 0)
    closed_unhappy = outcomes.get("closed_unhappy", 0)
    opted_out = outcomes.get("opted_out", 0)
    total_closed = outcomes.get("total_closed", 0)

    # Count active leads
    active_hot = hot
    active_replied = stats.get("replied", 0) - closed_happy - closed_unhappy
    if active_replied < 0:
        active_replied = 0

    output.append("Outcomes:")
    output.append(f"\u251c\u2500\u2500 \U0001f3c6 Won: {closed_happy}")
    output.append(f"\u251c\u2500\u2500 \U0001f4c9 Lost: {closed_unhappy}")
    if opted_out > 0:
        output.append(f"\u251c\u2500\u2500 \U0001f6ab Opted out: {opted_out}")
    output.append(f"\u251c\u2500\u2500 Conversion: {conversion_rate_display(closed_happy, closed_unhappy)}")

    active_parts = []
    if active_hot > 0:
        active_parts.append(f"{active_hot} hot")
    if active_replied > 0:
        active_parts.append(f"{active_replied} replied")
    active_str = ", ".join(active_parts) if active_parts else "none"
    output.append(f"\u2514\u2500\u2500 Active leads: {active_str}")
    output.append("")

    # ── Section 4: Won deals detail ──
    won_deals = [o for o in outcomes.get("outcomes", []) if o["status"] == "closed_happy"]
    deal_timelines = {t["name"]: t for t in velocity.get("per_deal_timelines", [])}
    if won_deals:
        output.append("Won deals:")
        for i, deal in enumerate(won_deals):
            is_last = i == len(won_deals) - 1
            prefix = "\u2514\u2500\u2500" if is_last else "\u251c\u2500\u2500"
            reason_str = f" ({deal['reason']})" if deal.get("reason") else ""
            output.append(f"{prefix} " + _person(deal) + reason_str)
            # Per-deal timeline
            tl = deal_timelines.get(deal["name"])
            if tl:
                parts = []
                if tl.get("time_to_accept") is not None:
                    parts.append(f"accept: {format_duration(tl['time_to_accept'])}")
                if tl.get("time_to_reply") is not None:
                    parts.append(f"reply: {format_duration(tl['time_to_reply'])}")
                if parts:
                    cont = "    " if is_last else "\u2502   "
                    arrow = " \u2192 "
                    output.append(f"{cont}Timeline: {arrow.join(parts)}")
        output.append("")

    # ── Section 5: Lost deals detail (if any) ──
    lost_deals = [o for o in outcomes.get("outcomes", []) if o["status"] == "closed_unhappy"]
    if lost_deals:
        output.append("Lost deals:")
        for i, deal in enumerate(lost_deals):
            is_last = i == len(lost_deals) - 1
            prefix = "\u2514\u2500\u2500" if is_last else "\u251c\u2500\u2500"
            reason_str = f" ({deal['reason']})" if deal.get("reason") else ""
            output.append(f"{prefix} " + _person(deal) + reason_str)
        # Aggregate loss reasons
        loss_reasons: dict[str, int] = {}
        for deal in lost_deals:
            reason = (deal.get("reason") or "").strip()
            if reason:
                loss_reasons[reason] = loss_reasons.get(reason, 0) + 1
        if len(loss_reasons) >= 2:
            output.append("")
            output.append("Common loss reasons:")
            sorted_reasons = sorted(loss_reasons.items(), key=lambda x: -x[1])
            for i, (reason, count) in enumerate(sorted_reasons[:5]):
                is_last = i == min(4, len(sorted_reasons) - 1)
                prefix = "\u2514\u2500\u2500" if is_last else "\u251c\u2500\u2500"
                output.append(f"{prefix} {reason} ({count}x)")
        output.append("")

    # ── Section 6: Stale leads warning ──
    if stale:
        output.append(f"\u26a0\ufe0f Stale leads (no activity 14+ days):")
        for i, s in enumerate(stale[:5]):
            is_last = i == min(4, len(stale) - 1)
            prefix = "\u2514\u2500\u2500" if is_last else "\u251c\u2500\u2500"
            src = s.get("source", "search")
            src_label = SOURCE_LABELS.get(src, "")
            src_tag = f", {src_label}" if src_label and src not in ("search", "linkedin_search") else ""
            output.append(f"{prefix} " + _person(s) + f" (last activity: {s['days_stale']}d ago{src_tag})")
        if len(stale) > 5:
            output.append(f"    ... and {len(stale) - 5} more")
        output.append('   \u2192 Use prospect(action="close") to resolve or send_message(action="followup") to re-engage')
        output.append("")

    # ── Section 7: Engagement metrics ──
    comments = eng_stats.get("comments", 0)
    reactions = eng_stats.get("reactions", 0)
    eng_total = comments + reactions
    if eng_total > 0:
        output.append("Engagement:")
        output.append(f"\u251c\u2500\u2500 Comments: {comments}")
        output.append(f"\u251c\u2500\u2500 Reactions: {reactions}")
        output.append(f"\u2514\u2500\u2500 Total touches: {eng_total}")
        output.append("")

        # Show recent engagement details
        recent = await db.get_recent_engagements(campaign_id, limit=5)
        if recent:
            output.append("Recent engagements:")
            for eng in recent:
                name = eng.get("prospect_name") or "Unknown"
                action = eng.get("action_type", "")
                post_snippet = (eng.get("post_text") or "")[:60]
                status = eng.get("status", "sent")
                status_tag = f" [{status}]" if status != "sent" else ""
                if action == "comment":
                    comment = (eng.get("comment_text") or "")[:80]
                    output.append(f"  \u2022 {name}: commented{status_tag} \u2014 \"{comment}\"")
                    if post_snippet:
                        output.append(f"    on: \"{post_snippet}...\"")
                else:
                    output.append(f"  \u2022 {name}: {eng.get('reaction_type', 'LIKE')}{status_tag} on \"{post_snippet}...\"")
            output.append("")

    # ── Section 7.5: Voice Memo Stats ──
    voice_stats = await db.get_voice_memo_stats(campaign_id)
    voice_sent = voice_stats.get("voice_sent", 0)
    text_sent = voice_stats.get("text_sent", 0)
    if voice_sent > 0:
        voice_rr = voice_stats.get("voice_reply_rate", 0)
        text_rr = voice_stats.get("text_reply_rate", 0)
        output.append("Voice Memo Stats:")
        output.append(f"\u251c\u2500\u2500 Voice messages sent: {voice_sent}")
        output.append(f"\u251c\u2500\u2500 Text messages sent: {text_sent}")
        if voice_stats.get("voice_total_outreaches", 0) > 0 and voice_stats.get("text_total_outreaches", 0) > 0:
            diff = voice_rr - text_rr
            diff_str = f" ({'+' if diff > 0 else ''}{diff:.0%} diff)" if abs(diff) > 0.01 else ""
            output.append(f"\u2514\u2500\u2500 Voice vs text reply rate: {voice_rr:.0%} vs {text_rr:.0%}{diff_str}")
        else:
            output.append(f"\u2514\u2500\u2500 Reply rate data: collecting (need more voice outreaches)")
        output.append("")

    # ── Section 7.7: Delivery Verification ──
    try:
        def _query_verification(cid: str) -> dict[str, int]:
            from ..db.schema import get_db as _get_verify_db
            _vdb = _get_verify_db()
            _ver_rows = _vdb.execute(
                """SELECT verified_status, COUNT(*) as cnt FROM engagements
                   WHERE campaign_id = ? OR campaign_id IS NULL
                   GROUP BY verified_status""",
                (cid,),
            ).fetchall()
            result = {r["verified_status"] or "pending": r["cnt"] for r in _ver_rows}
            _vdb.close()
            return result

        _ver_stats = await run_db(_query_verification, campaign_id)
        _ver_total = sum(_ver_stats.values())
        if _ver_total > 0:
            _verified = _ver_stats.get("verified", 0)
            _trust_api = _ver_stats.get("trust_api", 0)
            _unverified = _ver_stats.get("unverified", 0)
            _confirmed = _verified + _trust_api
            _rate = _confirmed / _ver_total * 100 if _ver_total > 0 else 0
            output.append("Delivery Verification:")
            output.append(f"\u251c\u2500\u2500 Confirmed: {_confirmed}/{_ver_total} ({_rate:.0f}%)")
            if _unverified > 0:
                output.append(f"\u2514\u2500\u2500 Unverified: {_unverified}")
            else:
                output.append(f"\u2514\u2500\u2500 All actions verified")
            output.append("")
    except Exception:
        pass

    # ── Section 8: Account health ──
    acc_rate = stats.get("acceptance_rate", 0)
    if invited > 0:
        raw = stats.get("raw_acceptance_rate", 0)
        output.append(f"Acceptance rate: {acc_rate:.0%}")
        if raw > 0 and abs(raw - acc_rate) > 0.01:
            output.append(f"   (raw incl. pending: {raw:.0%})")
        output.append("")

    # ── Section 9: A/B Test Results ──
    v_stats = await db.get_variant_stats(campaign_id)
    if "A" in v_stats and "B" in v_stats:
        va = v_stats["A"]
        vb = v_stats["B"]
        output.append("A/B Test Split:")
        va_invited = va.get("invited", 0)
        va_won = va.get("won", 0)
        va_win_str = f" ({va_won/va_invited:.0%} win)" if va_invited > 0 and va_won > 0 else ""
        output.append(
            f"\u251c\u2500\u2500 Variant A: {va['total']} prospects, "
            f"{va['acceptance_rate']}% accept, {va['reply_rate']}% reply, "
            f"{va_won} won{va_win_str}"
        )
        vb_invited = vb.get("invited", 0)
        vb_won = vb.get("won", 0)
        vb_win_str = f" ({vb_won/vb_invited:.0%} win)" if vb_invited > 0 and vb_won > 0 else ""
        output.append(
            f"\u2514\u2500\u2500 Variant B: {vb['total']} prospects, "
            f"{vb['acceptance_rate']}% accept, {vb['reply_rate']}% reply, "
            f"{vb_won} won{vb_win_str}"
        )
        # Show running tests
        running = await db.list_ab_tests(campaign_id, status="running")
        for t in running:
            output.append(f"   Running: {t['name']}")
            output.append(f"     A: {t['variant_a']}")
            output.append(f"     B: {t['variant_b']}")
        # Show completed tests
        completed = await db.list_ab_tests(campaign_id, status="completed")
        for t in completed[:2]:
            winner = t.get("winner", "?")
            output.append(f"   Completed: {t['name']} \u2192 Winner: Variant {winner}")
        output.append("")

    # ── Section 9.5: Experiment Insights + A/B Evaluation ──
    try:
        from ..services.experiment_service import (
            evaluate_ab_tests,
            evaluate_signal_vs_cold,
            get_experiment_summary,
            get_signal_conversion_rates,
        )

        ab_results = await run_db(evaluate_ab_tests)
        for r in ab_results:
            output.append(f"  {r}")
        from ..services.experiment_service import finish_headline_tests
        await finish_headline_tests()

        # Signal vs Cold evaluation
        signal_results = await run_db(evaluate_signal_vs_cold)
        if signal_results:
            output.append("Signal vs Cold Analysis:")
            for r in signal_results:
                output.append(f"  {r}")
            output.append("")

        # Signal conversion rates by type
        conversion_rates = await run_db(get_signal_conversion_rates)
        if conversion_rates:
            output.append("Signal Conversion Rates:")
            for sig_type, rates in sorted(
                conversion_rates.items(), key=lambda x: -x[1].get("volume", 0)
            ):
                vol = rates.get("volume", 0)
                if vol < 3:
                    continue  # Skip types with too few data points
                label = sig_type.replace("_", " ").title()
                acc = rates.get("acceptance_rate", 0)
                reply = rates.get("reply_rate", 0)
                won = rates.get("won", 0)
                output.append(
                    f"  {label}: {vol} outreaches, "
                    f"{acc:.0%} accept, {reply:.0%} reply, {won} won"
                )
            output.append("")

        exp_insight = await run_db(get_experiment_summary, campaign_id)
        if exp_insight:
            output.append("Experiment Insights:")
            output.append(f"  {exp_insight}")
            output.append("")
    except Exception:
        pass

    # ── Section 9.6: Strategy Engine ──
    try:
        strat_summary = await db.get_strategy_summary()
        if strat_summary.get("active_patterns", 0) > 0:
            output.append("\U0001f9e0 **Strategy Engine:**")
            output.append(
                f"  Patterns: {strat_summary['active_patterns']} active | "
                f"Actions: {strat_summary['total_actions']} "
                f"({strat_summary.get('validated_actions', 0)} validated)"
            )
            top = strat_summary.get("top_pattern")
            if top:
                output.append(
                    f"  Top pattern: {top['pattern_key']} "
                    f"(${top.get('estimated_revenue_impact', 0):,.0f}/prospect)"
                )
            # Revenue for this campaign
            funnel = await db.get_revenue_weighted_funnel()
            if funnel and funnel.get("total_pipeline"):
                output.append(
                    f"  Pipeline value: ${funnel['total_pipeline']:,.0f} | "
                    f"Won value: ${funnel.get('won_value', 0):,.0f}"
                )
            # Recent actions for this campaign
            actions = await db.list_strategy_actions(campaign_id=campaign_id, limit=3)
            if actions:
                output.append("  Recent actions:")
                for a in actions:
                    output.append(f"    - {a['action_type']} ({a['status']})")
            output.append("")
    except Exception:
        pass

    # ── Section 10: Cohort Analysis ──
    cohorts = await db.get_cohort_analysis(campaign_id)
    real_cohorts = [c for c in cohorts if c["cohort"] != "Not invited"]
    if len(real_cohorts) >= 2:
        output.append("📊 Cohort Analysis (by invitation week):")
        cohort_headers = ["Week", "Invited", "Connected", "Replied", "Won", "Lost", "Accept%", "Reply%"]
        cohort_rows = []
        for c in real_cohorts:
            cohort_rows.append([
                c["cohort"],
                str(c.get("invited", 0)),
                str(c.get("connected", 0)),
                str(c.get("replied", 0)),
                str(c.get("won", 0)),
                str(c.get("lost", 0)),
                f"{c['acceptance_rate']:.0f}%",
                f"{c['reply_rate']:.0f}%",
            ])
        output.append(table(cohort_headers, cohort_rows))
        output.append("")

    # ── Section 11: Activity Timeline ──
    ts_data = await db.get_time_series_stats(campaign_id)
    if len(ts_data) >= 3:
        invite_vals = [d.get("invites", 0) for d in ts_data]
        reply_vals = [d.get("replies", 0) for d in ts_data]
        output.append("📈 Activity (last 30 days):")
        output.append(f"  Invites:     {sparkline(invite_vals)} (total: {sum(invite_vals)})")
        output.append(f"  Replies:     {sparkline(reply_vals)} (total: {sum(reply_vals)})")
        if ts_data:
            output.append(f"  Period: {ts_data[0]['date']} → {ts_data[-1]['date']}")
        output.append("")

    # ── Section 12: Signal Attribution ──
    try:
        perf = await db.get_signal_performance(campaign_id)
        sig_stats = perf["signal"]
        cold_stats_perf = perf["cold"]

        if sig_stats["total"] > 0:
            output.append("📡 Signal Attribution:")
            output.append(
                f"├── Signal-triggered: {sig_stats['total']} outreaches "
                f"({sig_stats['connected']} connected, "
                f"{sig_stats['replied']} replied, "
                f"{sig_stats['won']} won)"
            )
            if sig_stats["invited"] > 0:
                output.append(
                    f"│   Accept: {sig_stats['acceptance_rate']:.0%} | "
                    f"Reply: {sig_stats['reply_rate']:.0%}"
                )
            output.append(
                f"├── Cold outreach: {cold_stats_perf['total']} outreaches "
                f"({cold_stats_perf['connected']} connected, "
                f"{cold_stats_perf['replied']} replied, "
                f"{cold_stats_perf['won']} won)"
            )
            if cold_stats_perf["invited"] > 0:
                output.append(
                    f"│   Accept: {cold_stats_perf['acceptance_rate']:.0%} | "
                    f"Reply: {cold_stats_perf['reply_rate']:.0%}"
                )

            # Signal lift
            lift = perf.get("signal_lift", {})
            acc_lift = lift.get("acceptance")
            reply_lift = lift.get("reply")
            lift_parts = []
            if acc_lift is not None and acc_lift != 0:
                direction = "↑" if acc_lift > 0 else "↓"
                lift_parts.append(f"acceptance {direction}{abs(acc_lift):.0%}")
            if reply_lift is not None and reply_lift != 0:
                direction = "↑" if reply_lift > 0 else "↓"
                lift_parts.append(f"reply {direction}{abs(reply_lift):.0%}")
            if lift_parts:
                output.append(f"└── Signal lift: {', '.join(lift_parts)}")
            else:
                output.append(f"└── Signal lift: measuring (need more data)")

            # Breakdown by signal type
            by_type = perf.get("by_signal_type", {})
            if by_type:
                output.append("")
                output.append("Signal type breakdown:")
                for stype, st in sorted(by_type.items(), key=lambda x: -x[1]["total"]):
                    label = stype.replace("_", " ").title()
                    output.append(
                        f"  {label}: {st['total']} → "
                        f"{st['connected']} connected, "
                        f"{st['replied']} replied, "
                        f"{st['won']} won"
                    )
            output.append("")
    except Exception as e:
        logger.debug("Signal attribution failed: %s", e)

    # ── Section 13: Warm-Up Effectiveness ──
    try:
        wu_data = await db.get_warmup_effectiveness(campaign_id)
        wu = wu_data.get("warmed_up", {})
        di = wu_data.get("direct_invite", {})
        lift = wu_data.get("lift", {})
        # Only show if we have meaningful data in both groups
        if wu.get("total", 0) >= 3 and di.get("total", 0) >= 3:
            output.append("")
            output.append("### Warm-Up Effectiveness")
            output.append("")
            output.append("| Group | Invited | Accepted | Rate | Replied | Reply Rate | Avg Days |")
            output.append("|-------|---------|----------|------|---------|------------|----------|")
            for label, g in [("With warm-up", wu), ("Direct invite", di)]:
                days_str = f"{g['avg_days_to_accept']}d" if g.get("avg_days_to_accept") else "—"
                output.append(
                    f"| {label} | {g['total']} | {g['accepted']} | "
                    f"{g['acceptance_rate']}% | {g['replied']} | "
                    f"{g['reply_rate']}% | {days_str} |"
                )
            acc_lift = lift.get("acceptance_rate_lift_pp", 0)
            reply_lift = lift.get("reply_rate_lift_pp", 0)
            direction = "+" if acc_lift >= 0 else ""
            output.append(
                f"\nLift: **{direction}{acc_lift}pp** acceptance, "
                f"**{'+' if reply_lift >= 0 else ''}{reply_lift}pp** reply rate"
            )
            if wu.get("warmup_actions_avg"):
                output.append(f"Avg warm-up actions before invite: {wu['warmup_actions_avg']}")
    except Exception as e:
        logger.debug("Warm-up effectiveness failed: %s", e)

    # ── Section 14: Remaining queue ──
    pending = total - invited
    if pending > 0:
        output.append(f"\U0001f4e6 {pending} prospects still queued for outreach.")

    from ..services.dashboard_snapshot import status_footer

    # campaign_id is the resolved campaign, not the (possibly empty) argument.
    output.extend(status_footer("campaign", campaign_id))
    return "\n".join(output)
