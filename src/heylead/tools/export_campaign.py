"""Tool: export_campaign — Export campaign results as a formatted table.

Generates a copy-paste-friendly table of all prospects with their
outreach status, fit score, messages sent, and engagement count.
"""

from __future__ import annotations

import json
import logging

import csv
import io

from ..db import aio as db
from ..db.async_bridge import run_db
from ..formatter import format_duration, stars, table

logger = logging.getLogger(__name__)


async def run_export_campaign(campaign_id: str = "", format: str = "table") -> str:
    """Export campaign results as a formatted table, CSV, or JSON."""

    setup_done = await db.get_setting("setup_complete", False)
    if not setup_done:
        return (
            "Setup required before exporting.\n\n"
            "Please run setup_profile first."
        )

    # Resolve campaign
    campaign, err = await db.find_active_campaign(campaign_id)
    if not campaign and not campaign_id:
        # Fallback: try any campaign (not just active)
        campaigns = await db.list_campaigns()
        if not campaigns:
            return (
                "No campaigns to export.\n\n"
                "Create one first: create_campaign(\"your target description\")"
            )
        campaign = campaigns[0]
    elif not campaign:
        return err
    campaign_id = campaign["id"]

    # Fetch prospect data with outreach + engagement + message counts
    def _fetch_export():
        from ..db.schema import get_db as _get_db
        _db = _get_db()
        _rows = _db.execute(
            """SELECT o.id as outreach_id,
                      c.name, c.title, c.company, c.linkedin_url, c.fit_score,
                      o.status, o.followup_count, o.outcome_json,
                      o.invited_at, o.accepted_at, o.first_reply_at,
                      (SELECT COUNT(*) FROM engagements e WHERE e.outreach_id = o.id) as engagement_count,
                      (SELECT COUNT(*) FROM messages m WHERE m.outreach_id = o.id AND m.role = 'sdr') as messages_sent
               FROM contacts c
               JOIN outreaches o ON o.contact_id = c.id
               WHERE c.campaign_id = ?
               ORDER BY c.fit_score DESC""",
            (campaign_id,),
        ).fetchall()
        _db.close()
        return _rows
    rows = await run_db(_fetch_export)

    if not rows:
        return (
            f"Campaign '{campaign['name']}' has no prospects yet.\n\n"
            "Prospects are added when you run create_campaign()."
        )

    # Status display mapping
    status_display = {
        "pending": "Pending",
        "invited": "Invited",
        "connected": "Connected",
        "messaged": "Messaged",
        "replied": "Replied",
        "hot_lead": "Hot Lead",
        "review_pending": "Review",
        "skipped": "Skipped",
        "error": "Error",
        "opted_out": "Opted Out",
        "closed_happy": "Won",
        "closed_unhappy": "Lost",
    }

    # Check if any outreaches have outcomes
    has_outcomes = any(dict(row).get("outcome_json") for row in rows)

    # Check if any outreaches have velocity timestamps
    has_velocity = any(dict(row).get("invited_at") for row in rows)

    # Build table
    headers = ["ID", "Name", "Title", "Company", "Status", "Fit", "Msgs", "Eng."]
    if has_velocity:
        headers.extend(["Inv\u2192Acc", "Acc\u2192Reply"])
    if has_outcomes:
        headers.append("Outcome")
    table_rows = []
    for row in rows:
        r = dict(row)
        row_data = [
            r.get("outreach_id", "")[:8],
            r.get("name", "Unknown"),
            (r.get("title", "") or "")[:30],
            (r.get("company", "") or "")[:20],
            status_display.get(r.get("status", ""), r.get("status", "")),
            stars(r.get("fit_score", 0)),
            str(r.get("messages_sent", 0)),
            str(r.get("engagement_count", 0)),
        ]
        if has_velocity:
            inv_at = r.get("invited_at")
            acc_at = r.get("accepted_at")
            rep_at = r.get("first_reply_at")
            tta_str = format_duration(acc_at - inv_at) if (inv_at and acc_at) else ""
            ttr_str = format_duration(rep_at - acc_at) if (acc_at and rep_at) else ""
            row_data.extend([tta_str, ttr_str])
        if has_outcomes:
            outcome_text = ""
            if r.get("outcome_json"):
                try:
                    odata = json.loads(r["outcome_json"])
                    outcome_text = (odata.get("reason", "") or "")[:30]
                except (json.JSONDecodeError, TypeError):
                    pass
            row_data.append(outcome_text)
        table_rows.append(row_data)

    table_output = table(headers, table_rows)

    # Summary stats
    stats = await db.get_campaign_stats(campaign_id)

    # ── CSV format ──
    if format == "csv":
        full_data = await db.get_full_campaign_export(campaign_id)
        buf = io.StringIO()
        writer = csv.writer(buf)
        csv_headers = [
            "Name", "Title", "Company", "LinkedIn URL", "LinkedIn ID",
            "Fit Score", "Status", "Channel", "Follow-ups", "Variant",
            "Invited At", "Accepted At", "First Reply At", "Outcome",
        ]
        writer.writerow(csv_headers)
        for r in full_data:
            outcome = ""
            if r.get("outcome_json"):
                try:
                    odata = json.loads(r["outcome_json"])
                    outcome = odata.get("reason", "")
                except (json.JSONDecodeError, TypeError):
                    pass
            writer.writerow([
                r.get("name", ""),
                r.get("title", ""),
                r.get("company", ""),
                r.get("linkedin_url", ""),
                r.get("linkedin_id", ""),
                f"{r.get('fit_score', 0):.2f}",
                r.get("status", ""),
                r.get("channel", "linkedin"),
                r.get("followup_count", 0),
                r.get("variant", ""),
                r.get("invited_at", ""),
                r.get("accepted_at", ""),
                r.get("first_reply_at", ""),
                outcome,
            ])
        return (
            f"📋 CSV Export: **{campaign['name']}** ({len(full_data)} prospects)\n\n"
            "```csv\n" + buf.getvalue() + "```\n\n"
            "Copy the CSV above into a spreadsheet or save as .csv file."
        )

    # ── JSON format ──
    if format == "json":
        full_data = await db.get_full_campaign_export(campaign_id)
        cohorts = await db.get_cohort_analysis(campaign_id)
        export = {
            "campaign": {
                "id": campaign_id,
                "name": campaign.get("name", ""),
                "status": campaign.get("status", ""),
                "mode": campaign.get("mode", ""),
            },
            "stats": {
                "total": stats.get("total_prospects", 0),
                "invited": stats.get("invited", 0),
                "connected": stats.get("connected", 0),
                "replied": stats.get("replied", 0),
                "hot_leads": stats.get("hot_leads", 0),
                "acceptance_rate": stats.get("acceptance_rate", 0),
                "reply_rate": stats.get("reply_rate", 0),
            },
            "cohorts": cohorts,
            "contacts": full_data,
        }
        return (
            f"📋 JSON Export: **{campaign['name']}** ({len(full_data)} prospects)\n\n"
            "```json\n" + json.dumps(export, indent=2, default=str) + "\n```\n\n"
            "Copy the JSON above for API integration or data analysis."
        )

    # ── Default: table format ──
    output = [
        f"📋 Export: **{campaign['name']}** ({len(rows)} prospects)\n",
        table_output,
        "",
        "Summary:",
        f"├── Invited: {stats['invited']} | Connected: {stats['connected']} | "
        f"Replied: {stats['replied']} | Hot leads: {stats['hot_leads']}",
        f"└── Acceptance rate: {stats['acceptance_rate']:.0%} | "
        f"Reply rate: {stats['reply_rate']:.0%}",
        "",
        "Copy the table above to paste into spreadsheets or documents.",
        "Tip: Use export_campaign(format='csv') for spreadsheet import or format='json' for data.",
    ]

    return "\n".join(output)
