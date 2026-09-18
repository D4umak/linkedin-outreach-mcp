"""Tool: dismiss_lead — Clear a lead off the Needs attention strip.

A lead who wrote in April and was never answered sits on Overview forever.
The only exits used to be answering them, skipping them, or closing them with
an outcome — and for a hosted account none of those were reachable, because
`close` resolves the outreach against the LOCAL database and a hosted-only row
is not there.

Dismissing records the same close-as-lost the operator would have recorded by
hand, so the row leaves both the Needs attention strip (status is no longer
hot_lead/replied) and the Hot Leads count (which is status-only). Because that
writes a real outcome into campaign analytics, it takes two calls: the first
shows who would be dismissed, the second carries confirm=True.
"""

from __future__ import annotations

import json
import logging
import time

from ..dashboard_links import dashboard_url
from ..db.async_bridge import run_db
from ..db.queries import get_outreach_with_contact, log_action

logger = logging.getLogger(__name__)

_DISMISS_REASON = "Dismissed from Needs attention"


async def _find_hosted_lead(outreach_id: str) -> dict | None:
    """Look a hosted-only outreach up in the backend's own Overview payload.

    Returns a record shaped like the local one, or None when the backend is
    unreachable or does not know this id.
    """
    from ..config import is_backend_mode

    if not is_backend_mode():
        return None
    try:
        from ..services.cloud_sync import fetch_live_stats

        stats = await fetch_live_stats()
    except Exception as e:
        logger.warning("Could not fetch hosted stats to resolve %s: %s", outreach_id, e)
        return None
    if not stats:
        return None

    for lead in stats.get("needs_attention") or []:
        if lead.get("outreach_id") == outreach_id:
            return {
                "outreach_id": outreach_id,
                "status": "hot_lead",
                "name": lead.get("contact_name") or "Unknown",
                "title": lead.get("title") or "",
                "company": lead.get("company") or "",
                "campaign_name": lead.get("campaign_name") or "",
                "reason": lead.get("reason") or "",
                "_hosted_only": True,
            }
    for lead in stats.get("hot_leads") or []:
        if lead.get("outreach_id") == outreach_id:
            return {
                "outreach_id": outreach_id,
                "status": "hot_lead",
                "name": lead.get("prospect_name") or lead.get("name") or "Unknown",
                "title": lead.get("prospect_title") or lead.get("title") or "",
                "company": lead.get("prospect_company") or lead.get("company") or "",
                "campaign_name": lead.get("campaign_name") or "",
                "reason": "",
                "_hosted_only": True,
            }
    return None


def _who(record: dict) -> str:
    name = record.get("name") or "Unknown"
    company = record.get("company") or ""
    return f"{name} ({company})" if company else name


async def run_dismiss_lead(
    outreach_id: str = "",
    reason: str = "",
    confirm: bool = False,
) -> str:
    """Dismiss a lead from Needs attention by closing it as lost.

    Args:
        outreach_id: The outreach to dismiss. Required — there is no
            auto-select, because dismissing the wrong lead is not obvious
            afterwards.
        reason: Optional note stored with the outcome.
        confirm: Must be True to actually dismiss. The first call without it
            returns a preview of who would be dismissed.
    """
    if not outreach_id:
        return (
            "Error: 'outreach_id' is required for action='dismiss'.\n\n"
            "show_status() lists each Needs attention lead with its id."
        )

    record = await run_db(get_outreach_with_contact, outreach_id)
    hosted_only = False
    if not record:
        record = await _find_hosted_lead(outreach_id)
        hosted_only = bool(record)
    if not record:
        return (
            f"Outreach not found: `{outreach_id}`\n\n"
            "It is not in the local database and the hosted Overview does not "
            "list it either. Run show_status() for current ids."
        )

    old_status = record.get("status", "unknown")
    if old_status in ("closed_happy", "closed_unhappy", "opted_out", "skipped"):
        return (
            f"**{_who(record)}** is already closed ({old_status}).\n"
            "No changes made."
        )

    if not confirm:
        lines = [
            "Dismiss this lead?",
            "",
            f"  Prospect: {_who(record)}",
        ]
        if record.get("title"):
            lines.append(f"  Title:    {record['title']}")
        if record.get("campaign_name"):
            lines.append(f"  Campaign: {record['campaign_name']}")
        if record.get("reason"):
            lines.append(f"  Flagged:  {record['reason']}")
        lines.extend([
            f"  Status:   {old_status} → closed_unhappy (lost)",
            "",
            "This records a *lost* outcome against the campaign's conversion "
            "rate and drops the lead from both Needs attention and the Hot "
            "Leads count. It is not undone by them replying again.",
            "",
            "To go ahead:",
            f'  prospect(action="dismiss", outreach_id="{outreach_id}", confirm=True)',
            "",
            "To answer them instead:",
            f'  prospect(action="conversation", outreach_id="{outreach_id}")',
        ])
        return "\n".join(lines)

    note = reason or _DISMISS_REASON

    if not hosted_only:
        from ..db.queries import update_outreach

        await run_db(
            update_outreach, outreach_id,
            status="closed_unhappy",
            outcome_json=json.dumps({
                "outcome": "lost",
                "reason": note,
                "closed_at": int(time.time()),
                "previous_status": old_status,
                "dismissed": True,
            }),
            next_action=None,
        )

    from ..services.cloud_sync import sync_outreach_close

    synced = await sync_outreach_close(outreach_id, "lost", note, "")

    await run_db(
        log_action, "lead_dismissed",
        outreach_id=outreach_id,
        result="success" if (synced or not hosted_only) else "error",
        details={
            "prospect": record.get("name", ""),
            "previous_status": old_status,
            "reason": note,
            "hosted_only": hosted_only,
            "synced": synced,
        },
    )

    if hosted_only and not synced:
        return (
            f"Could not dismiss **{_who(record)}**.\n\n"
            "This lead lives only on the hosted backend and the close request "
            "did not go through. Nothing was changed. Check the connection and "
            f"retry, or dismiss it from {dashboard_url('actions')}."
        )

    out = [
        f"Dismissed **{_who(record)}** — recorded as lost.",
        "",
        f"  {old_status} → closed_unhappy",
        f"  Note: {note}",
    ]
    if not synced:
        out.append(
            "\n⚠️  Backend sync failed — the hosted Overview may still show "
            "this lead until the next sync."
        )
    return "\n".join(out)
