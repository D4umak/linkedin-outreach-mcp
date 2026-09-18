"""Tool: skip_prospect — Remove a prospect from the outreach queue.

Marks a prospect as 'skipped' so they won't receive any more
invitations, follow-ups, or engagements. Useful for bad-fit
prospects found during copilot review.
"""

from __future__ import annotations

import logging

from ..db.queries import (
    find_active_campaign,
    get_outreach,
    get_setting,
    log_action,
    update_outreach,
)
from ..db.schema import get_db
from ..db.async_bridge import run_db
from ..services.cloud_sync import sync_outreach_skip

logger = logging.getLogger(__name__)


async def run_skip_prospect(
    outreach_id: str = "",
    campaign_id: str = "",
    reason_code: str = "",
    reason_note: str = "",
) -> str:
    """Skip a prospect: leave them out of THIS campaign. No other effect.

    Skip is not Stop. It is campaign-scoped, carries no workspace-wide
    suppression, and does not stop outreach from another campaign. To stop
    reaching someone everywhere, close them with outcome='opt_out'.

    If outreach_id is provided, skips that specific outreach.
    Otherwise, finds the next pending prospect in the campaign.

    Args:
        outreach_id: The outreach to skip. Auto-selects if empty.
        campaign_id: Which campaign. Uses the active one if empty.
        reason_code: One of 'not_a_fit', 'negative_reply', 'asked_to_stop',
            'handled_elsewhere', 'other'. Only the first two are treated as
            evidence about targeting; the rest are facts about that one
            person and never move a segment's ranking.
        reason_note: Free text alongside the code.
    """

    # ── Pre-checks ──
    setup_done = await run_db(get_setting, "setup_complete", False)
    if not setup_done:
        return (
            "Setup required before skipping prospects.\n\n"
            "Please run setup_profile first."
        )

    # ── Resolve outreach ──
    if outreach_id:
        outreach = await run_db(get_outreach, outreach_id)
        if not outreach:
            return f"Outreach not found: {outreach_id}"
        if outreach["status"] == "skipped":
            return "This prospect is already skipped."
    else:
        # Find next pending prospect in campaign
        campaign, err = await run_db(find_active_campaign, campaign_id)
        if not campaign:
            return err
        campaign_id = campaign["id"]

        # Find next pending outreach
        def _find_next_pending():
            db = get_db()
            r = db.execute(
                """SELECT o.id as outreach_id, o.status, o.campaign_id, o.contact_id
                   FROM outreaches o
                   JOIN contacts c ON o.contact_id = c.id
                   WHERE o.campaign_id = ?
                     AND o.status = 'pending'
                   -- DESC, matching every other definition of "the next prospect":
                   -- generate_send's queue read, get_next_pending_outreach,
                   -- and this function's own "Next up" hint below. This was
                   -- ASC, so skipping with no id terminally skipped the
                   -- WORST-fit prospect while the one the user was looking at
                   -- stayed in the queue -- and was then printed as "Next up".
                   ORDER BY c.fit_score DESC, o.created_at ASC
                   LIMIT 1""",
                (campaign_id,),
            ).fetchone()
            db.close()
            return r
        row = await run_db(_find_next_pending)

        if not row:
            return (
                f"No pending prospects to skip in '{campaign.get('name', 'campaign')}'.\n\n"
                "All prospects have already been invited or skipped."
            )
        outreach_id = row["outreach_id"]
        outreach = await run_db(get_outreach, outreach_id)
        if not outreach:
            return "Outreach not found."

    # ── Get contact info for display ──
    def _get_contact_info():
        db = get_db()
        r = db.execute(
            """SELECT name, title, company, fit_score
               FROM contacts WHERE id = ?""",
            (outreach["contact_id"],),
        ).fetchone()
        db.close()
        return r
    contact = await run_db(_get_contact_info)

    contact_name = "Unknown"
    role_str = ""
    if contact:
        c = dict(contact)
        contact_name = c.get("name", "Unknown")
        role_str = c.get("title", "")
        if c.get("company"):
            role_str += f" at {c['company']}" if role_str else c["company"]

    # ── Skip the prospect ──
    old_status = outreach["status"]
    await run_db(
        update_outreach, outreach_id,
        status="skipped", next_action=None, last_attempt_error="operator_skip",
    )
    await run_db(log_action, "prospect_skipped",
        outreach_id=outreach_id,
        result="skipped",
        details={
            "prospect": contact_name,
            "previous_status": old_status,
            "reason_code": reason_code,
            "reason_note": reason_note,
        },)

    # Sync to backend so cloud scheduler stops outreach to this prospect
    synced = await sync_outreach_skip(
        outreach_id, reason=reason_note,
        reason_code=reason_code, reason_note=reason_note,
    )
    if not synced:
        logger.warning("Could not sync skip for outreach %s to backend", outreach_id)

    output = [
        f"Skipped **{contact_name}**" + (f" ({role_str})" if role_str else ""),
        f"   Previous status: {old_status}",
        "",
    ]

    # ── Show next pending prospect as hint ──
    cid = outreach.get("campaign_id", campaign_id)
    if cid:
        def _get_next_and_count():
            db = get_db()
            nr = db.execute(
                """SELECT c.name, c.title, c.company, c.fit_score
                   FROM outreaches o
                   JOIN contacts c ON o.contact_id = c.id
                   WHERE o.campaign_id = ?
                     AND o.status = 'pending'
                   ORDER BY c.fit_score DESC
                   LIMIT 1""",
                (cid,),
            ).fetchone()
            pc = db.execute(
                "SELECT COUNT(*) as c FROM outreaches WHERE campaign_id = ? AND status = 'pending'",
                (cid,),
            ).fetchone()["c"]
            db.close()
            return nr, pc
        next_row, pending_count = await run_db(_get_next_and_count)

        if next_row:
            n = dict(next_row)
            next_name = n.get("name", "Unknown")
            next_role = n.get("title", "")
            if n.get("company"):
                next_role += f" at {n['company']}" if next_role else n["company"]
            output.append(f"Next up: {next_name} ({next_role})")
            output.append(f"{pending_count} prospect{'s' if pending_count != 1 else ''} remaining in queue.")
        else:
            output.append("No more pending prospects in this campaign.")

    return "\n".join(output)
