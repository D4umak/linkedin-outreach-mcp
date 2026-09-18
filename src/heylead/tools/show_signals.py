"""Tool: show_signals — Detailed signal feed with filters, scoring, and analytics.

Shows detected buying signals from LinkedIn with filtering by type,
campaign, status, and scoring. Includes top signal-heavy accounts
with recent signal context and individual signal scores.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def run_show_signals(
    signal_type: str = "",
    campaign_id: str = "",
    status: str = "",
    limit: int = 20,
) -> str:
    """Show the signal feed — detected buying signals from LinkedIn.

    Args:
        signal_type: Filter by signal type (e.g., 'keyword_mention', 'job_change').
        campaign_id: Filter by campaign.
        status: Filter by status: 'new', 'classified', 'actioned', 'skipped', 'dismissed'.
        limit: Max signals to show (default 20).

    Returns:
        Formatted signal feed with scoring, top accounts, and analytics.
    """
    from ..db.async_bridge import run_db
    from ..services.signal_service import format_signal_feed

    output_parts: list[str] = []

    # Funnel health warnings (surfaces bottlenecks at the top)
    try:
        health = await run_db(_format_funnel_health)
        if health:
            output_parts.append(health)
    except Exception as e:
        logger.debug("Funnel health check failed: %s", e)

    # Signal summary stats
    try:
        summary = await run_db(_format_signal_summary)
        if summary:
            output_parts.append(summary)
    except Exception as e:
        logger.debug("Signal summary failed: %s", e)

    # Top signal-heavy accounts with recent signal context
    try:
        top_accounts_section = await run_db(_format_top_accounts, limit=5)
        if top_accounts_section:
            output_parts.append(top_accounts_section)
    except Exception as e:
        logger.debug("Top accounts section failed: %s", e)

    # Compound intent events (stacked signals)
    try:
        compound_section = await run_db(_format_compound_intents)
        if compound_section:
            output_parts.append(compound_section)
    except Exception as e:
        logger.debug("Compound intents section failed: %s", e)

    # Signal conversion funnel
    try:
        conversion = await run_db(_format_conversion_funnel)
        if conversion:
            output_parts.append(conversion)
    except Exception as e:
        logger.debug("Signal conversion funnel failed: %s", e)

    # Signal feed with filters
    feed = await run_db(
        format_signal_feed,
        signal_type=signal_type or None,
        campaign_id=campaign_id or None,
        status=status or None,
        limit=limit,
    )
    output_parts.append(feed)

    return "\n".join(output_parts)


def _format_signal_summary() -> str:
    """Format a summary bar with signal stats."""
    from ..db.signal_queries import count_signals_by_type, get_signal_funnel_stats

    stats = get_signal_funnel_stats(days=7)
    total = stats.get("total", 0)
    if total == 0:
        return ""

    by_status = stats.get("by_status", {})
    by_type = stats.get("by_type", {})

    lines: list[str] = []
    lines.append("── Signal Summary (last 7 days) ──\n")

    # Type breakdown on one line
    type_parts = []
    for sig_type, count in sorted(by_type.items(), key=lambda x: -x[1]):
        label = sig_type.replace("_", " ").title()
        type_parts.append(f"{label}: {count}")
    lines.append(f"Total: {total} | " + " | ".join(type_parts[:5]))

    # Pipeline status
    new_count = by_status.get("new", 0)
    classified = by_status.get("classified", 0)
    actioned = by_status.get("actioned", 0)
    skipped = by_status.get("skipped", 0)
    if new_count or classified or actioned or skipped:
        pipeline = (
            f"Pipeline: {new_count} new → {classified} classified → "
            f"{actioned} actioned"
        )
        if skipped:
            pipeline += f" / {skipped} skipped"
        lines.append(pipeline)

    lines.append("")
    return "\n".join(lines)


def _format_top_accounts(limit: int = 5) -> str:
    """Format top signal-heavy accounts with composite scores and recent signals."""
    from ..db.signal_queries import list_signal_accounts, list_signals
    from ..services.signal_service import _format_time_ago

    accounts = list_signal_accounts(limit=limit)
    if not accounts:
        return ""

    lines: list[str] = []
    lines.append("── Top Signal-Heavy Accounts ──\n")

    for i, acct in enumerate(accounts, 1):
        name = acct.get("prospect_name", "Unknown")
        company = acct.get("company") or ""
        total = acct.get("total_signals", 0)
        score = acct.get("composite_score", 0)
        top_type = (acct.get("top_signal_type") or "").replace("_", " ").title()
        linkedin_id = acct.get("linkedin_id", "")

        # Score tier indicator
        if score >= 0.7:
            tier = "\U0001f525"  # fire — hot
        elif score >= 0.4:
            tier = "\U0001f7e1"  # yellow circle — warm
        else:
            tier = "\u26aa"  # white circle — cool

        line = f"  {i}. {tier} **{name}**"
        if company:
            line += f" ({company})"
        line += f" \u2014 Score: {score:.2f}"
        line += f" ({total} signal{'s' if total != 1 else ''}"
        if top_type:
            line += f", top: {top_type}"
        line += ")"
        lines.append(line)

        # Show most recent signal for this account (brief context)
        if linkedin_id:
            try:
                recent = list_signals(linkedin_id=linkedin_id, limit=2)
                for sig in recent:
                    sig_type = sig.get("signal_type", "")
                    content = (sig.get("content") or "")[:80]
                    detected = sig.get("detected_at", 0)
                    age = _format_time_ago(detected)
                    sig_label = sig_type.replace("_", " ")
                    lines.append(f"     → [{age}] {sig_label}: {content}")
            except Exception:
                pass

    lines.append("")
    return "\n".join(lines)


def _format_conversion_funnel() -> str:
    """Format signal-to-conversion funnel stats."""
    from ..db.signal_queries import get_signal_to_conversion_funnel

    data = get_signal_to_conversion_funnel(days=30)
    total = data.get("total_signals", 0)
    if total == 0:
        return ""

    lines: list[str] = []
    lines.append("── Signal Conversion Funnel (last 30 days) ──\n")

    classified = data.get("classified", 0)
    actioned = data.get("actioned", 0)
    outreaches = data.get("outreaches_created", 0)
    funnel = data.get("funnel", {})

    # Pipeline: signals → classified → actioned → outreaches
    lines.append(
        f"Signals: {total} → {classified} classified → "
        f"{actioned} actioned → {outreaches} outreaches"
    )

    # Outreach funnel
    connected = funnel.get("connected", 0)
    replied = funnel.get("replied", 0)
    won = funnel.get("won", 0)
    if outreaches > 0:
        lines.append(
            f"Outreach: {outreaches} → {connected} connected → "
            f"{replied} replied → {won} won"
        )

    # Action breakdown
    by_action = data.get("by_action", {})
    if by_action:
        action_parts = []
        for action, count in sorted(by_action.items(), key=lambda x: -x[1]):
            label = action.replace("_", " ")
            action_parts.append(f"{label}: {count}")
        lines.append(f"Actions: {' | '.join(action_parts[:5])}")

    # Average time from signal to outreach
    avg_hours = data.get("avg_signal_to_outreach_hours")
    if avg_hours is not None:
        if avg_hours < 1:
            time_str = f"{avg_hours * 60:.0f} min"
        elif avg_hours < 24:
            time_str = f"{avg_hours:.1f} hours"
        else:
            time_str = f"{avg_hours / 24:.1f} days"
        lines.append(f"Avg signal → outreach time: {time_str}")

    # Per-type conversion rates (top 5)
    by_type = data.get("by_type", {})
    if by_type:
        lines.append("")
        lines.append("By Signal Type:")
        for sig_type, stats in sorted(
            by_type.items(), key=lambda x: -x[1].get("signals", 0)
        )[:5]:
            label = sig_type.replace("_", " ").title()
            signals_count = stats.get("signals", 0)
            outreaches_count = stats.get("outreaches", 0)
            replied_count = stats.get("replied", 0)
            won_count = stats.get("won", 0)
            act_rate = stats.get("activation_rate", 0)

            parts = [f"{signals_count} signals"]
            if outreaches_count:
                parts.append(f"{outreaches_count} outreaches ({act_rate:.0%} activation)")
            if replied_count:
                parts.append(f"{replied_count} replied")
            if won_count:
                parts.append(f"{won_count} won")
            lines.append(f"  {label}: {' → '.join(parts)}")

    lines.append("")
    return "\n".join(lines)


def _format_compound_intents() -> str:
    """Format compound intent events section."""
    from ..db.signal_queries import get_intent_event_stats, list_intent_events
    from ..services.signal_service import _format_time_ago

    stats = get_intent_event_stats(days=30)
    total = stats.get("total", 0)
    if total == 0:
        return ""

    lines: list[str] = []
    lines.append("── Compound Intent Events (last 30 days) ──\n")

    # Summary by type
    by_type = stats.get("by_type", {})
    type_parts = []
    for etype, edata in sorted(by_type.items(), key=lambda x: -x[1].get("count", 0)):
        label = etype.replace("_", " ").title()
        cnt = edata.get("count", 0)
        avg = edata.get("avg_score", 0)
        type_parts.append(f"{label}: {cnt} (avg: {avg:.2f})")
    lines.append(f"Total: {total} | " + " | ".join(type_parts[:5]))

    # Action breakdown
    by_action = stats.get("by_action", {})
    if by_action:
        action_parts = []
        for action, cnt in sorted(by_action.items(), key=lambda x: -x[1]):
            label = action.replace("_", " ")
            action_parts.append(f"{label}: {cnt}")
        lines.append(f"Actions: {' | '.join(action_parts[:4])}")

    # Active intent events (top 5)
    active = list_intent_events(active_only=True, limit=5)
    if active:
        lines.append("")
        lines.append("Active Intent Events:")
        for ie in active:
            etype = ie.get("event_type", "").replace("_", " ").title()
            score = ie.get("composite_score", 0)
            company = ie.get("company") or ""
            detected = ie.get("detected_at", 0)
            age = _format_time_ago(detected)
            types_list = ie.get("signal_types_list", [])
            types_str = " + ".join(t.replace("_", " ") for t in types_list)
            action = ie.get("action_taken") or "pending"

            # Score tier
            if score >= 0.85:
                tier = "\U0001f525"
            elif score >= 0.7:
                tier = "\U0001f7e1"
            else:
                tier = "\u26aa"

            line = f"  {tier} {etype}"
            if company:
                line += f" ({company})"
            line += f" — {types_str}"
            line += f" | Score: {score:.2f} | {age} | {action}"
            lines.append(line)

    lines.append("")
    return "\n".join(lines)


def _format_funnel_health() -> str:
    """Show funnel health with clear bottleneck warnings."""
    from ..db.signal_queries import get_signal_funnel_stats

    stats = get_signal_funnel_stats(days=7)
    total = stats.get("total", 0)
    if total == 0:
        return ""

    by_status = stats.get("by_status", {})
    new_count = by_status.get("new", 0)
    classified = by_status.get("classified", 0)
    actioned = by_status.get("actioned", 0)

    warnings: list[str] = []

    # Check for classification bottleneck
    if new_count > 0 and classified == 0 and actioned == 0:
        warnings.append(
            f"  !! {new_count} signals stuck at 'new' — classification not running"
        )
    elif new_count > max(classified, 1) * 3:
        warnings.append(
            f"  !! Classification backlog — {new_count} unclassified signals"
        )

    # Check for activation bottleneck
    if classified > 0 and actioned == 0:
        warnings.append(
            f"  !! {classified} classified signals not actioned — check scoring thresholds"
        )

    # Check for all-below-threshold
    by_action = stats.get("by_action", {})
    if by_action:
        below = by_action.get("below_threshold", 0)
        outreach = by_action.get("outreach_created", 0) + by_action.get("campaign_added", 0)
        if below > 0 and outreach == 0:
            warnings.append(
                f"  !! All {below} parked signals marked 'below_threshold' — scoring too strict"
            )

    if not warnings:
        return ""

    lines = ["── Signal Funnel Health ──\n"]
    lines.extend(warnings)
    lines.append("")
    return "\n".join(lines)
