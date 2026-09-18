"""Tool: compare_campaigns — Side-by-side comparison of campaign performance.

Compares 2+ campaigns on key metrics: acceptance rate, reply rate,
hot leads, outcomes, and conversion rate. Highlights the best performer.
"""

from __future__ import annotations

import logging

from ..db import aio as db
from ..formatter import conversion_rate_display, format_duration, table

logger = logging.getLogger(__name__)


async def run_compare_campaigns(campaign_ids: str = "") -> str:
    """Compare 2+ campaigns side by side.

    Args:
        campaign_ids: Comma-separated campaign IDs. Compares all active/paused if empty.
    """

    # ── Pre-check ──
    setup_done = await db.get_setting("setup_complete", False)
    if not setup_done:
        return (
            "Setup required before comparing campaigns.\n\n"
            "Please run setup_profile first."
        )

    # ── Resolve campaigns ──
    if campaign_ids:
        ids = [cid.strip() for cid in campaign_ids.split(",") if cid.strip()]
        campaigns = []
        for cid in ids:
            camp = await db.get_campaign(cid)
            if camp:
                campaigns.append(camp)
            else:
                return f"Campaign not found: `{cid}`"
    else:
        campaigns = [
            c for c in await db.list_campaigns()
            if c.get("status") in ("active", "paused", "draft")
        ]

    if len(campaigns) < 2:
        return (
            "Need at least 2 campaigns to compare.\n\n"
            f"Found {len(campaigns)} campaign(s). "
            "Create more campaigns or specify IDs:\n"
            "  compare_campaigns(campaign_ids='id1,id2')"
        )

    # ── Gather stats ──
    campaign_data = []
    for camp in campaigns:
        cid = camp["id"]
        stats = await db.get_campaign_stats(cid)
        outcomes = await db.get_campaign_outcomes(cid)
        eng = await db.get_engagement_stats(cid)
        velocity = await db.get_campaign_velocity(cid)
        campaign_data.append({
            "campaign": camp,
            "stats": stats,
            "outcomes": outcomes,
            "engagement": eng,
            "velocity": velocity,
        })

    # ── Build comparison table ──
    headers = ["Metric"] + [d["campaign"]["name"][:20] for d in campaign_data]

    def _pct(val: float) -> str:
        return f"{val:.0%}" if val > 0 else "\u2014"

    rows = [
        ["Status"] + [d["campaign"].get("status", "?").capitalize() for d in campaign_data],
        ["Prospects"] + [str(d["stats"]["total_prospects"]) for d in campaign_data],
        ["Invited"] + [str(d["stats"]["invited"]) for d in campaign_data],
        ["Connected"] + [str(d["stats"]["connected"]) for d in campaign_data],
        ["Replied"] + [str(d["stats"]["replied"]) for d in campaign_data],
        ["Hot Leads"] + [str(d["stats"]["hot_leads"]) for d in campaign_data],
        ["Accept Rate"] + [_pct(d["stats"]["acceptance_rate"]) for d in campaign_data],
        ["Reply Rate"] + [_pct(d["stats"]["reply_rate"]) for d in campaign_data],
        ["\U0001f3c6 Won"] + [str(d["outcomes"]["closed_happy"]) for d in campaign_data],
        ["\U0001f4c9 Lost"] + [str(d["outcomes"]["closed_unhappy"]) for d in campaign_data],
        ["Conversion"] + [
            conversion_rate_display(d["outcomes"]["closed_happy"], d["outcomes"]["closed_unhappy"])
            for d in campaign_data
        ],
        ["Engagements"] + [str(d["engagement"].get("total", 0)) for d in campaign_data],
        ["Avg Accept"] + [
            format_duration(d["velocity"].get("avg_time_to_accept"))
            for d in campaign_data
        ],
        ["Avg Reply"] + [
            format_duration(d["velocity"].get("avg_time_to_reply"))
            for d in campaign_data
        ],
    ]

    table_output = table(headers, rows)

    # ── Determine winners ──
    winners = []

    # Best acceptance rate
    acc_candidates = [
        (d["stats"]["acceptance_rate"], d["campaign"]["name"])
        for d in campaign_data if d["stats"]["invited"] > 0
    ]
    if acc_candidates:
        best = max(acc_candidates, key=lambda x: x[0])
        if best[0] > 0:
            winners.append(f"Best acceptance rate: **{best[1]}** ({best[0]:.0%})")

    # Best reply rate
    reply_candidates = [
        (d["stats"]["reply_rate"], d["campaign"]["name"])
        for d in campaign_data if d["stats"]["connected"] > 0
    ]
    if reply_candidates:
        best = max(reply_candidates, key=lambda x: x[0])
        if best[0] > 0:
            winners.append(f"Best reply rate: **{best[1]}** ({best[0]:.0%})")

    # Most won deals
    won_candidates = [
        (d["outcomes"]["closed_happy"], d["campaign"]["name"])
        for d in campaign_data
    ]
    best = max(won_candidates, key=lambda x: x[0])
    if best[0] > 0:
        winners.append(f"Most deals won: **{best[1]}** ({best[0]})")

    # Fastest acceptance
    tta_candidates = [
        (d["velocity"]["avg_time_to_accept"], d["campaign"]["name"])
        for d in campaign_data
        if d["velocity"].get("avg_time_to_accept") is not None
    ]
    if tta_candidates:
        best = min(tta_candidates, key=lambda x: x[0])
        winners.append(f"Fastest acceptance: **{best[1]}** ({format_duration(best[0])})")

    # Most hot leads
    hot_candidates = [
        (d["stats"]["hot_leads"], d["campaign"]["name"])
        for d in campaign_data
    ]
    best = max(hot_candidates, key=lambda x: x[0])
    if best[0] > 0:
        winners.append(f"Most hot leads: **{best[1]}** ({best[0]})")

    # ── Assemble output ──
    output = [
        f"\U0001f4ca Campaign Comparison ({len(campaigns)} campaigns)\n",
        table_output,
        "",
    ]

    if winners:
        output.append("Highlights:")
        for i, w in enumerate(winners):
            is_last = i == len(winners) - 1
            prefix = "\u2514\u2500\u2500" if is_last else "\u251c\u2500\u2500"
            output.append(f"{prefix} {w}")
        output.append("")

    output.append("Tip: campaign_report(campaign_id='...') for a deep-dive on any campaign.")

    from ..services.dashboard_snapshot import status_footer

    output.extend(status_footer("campaigns", snapshot=False))
    return "\n".join(output)
