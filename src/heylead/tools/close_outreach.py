"""Tool: close_outreach — Close an outreach with outcome tracking.

Dedicated tool for recording outcomes (won/lost/opt_out) with proper
metadata storage. Replaces the hidden manual status override that used to
live in the review-approval flow, removed along with that flow.
"""

from __future__ import annotations

import json
import logging
import time

from ..db.queries import (
    find_active_campaign,
    get_outreach_with_contact,
    get_setting,
    list_campaigns,
    log_action,
    update_outreach,
)
from ..formatter import outcome_icon, outcome_label
from ..db.async_bridge import run_db
from ..services.cloud_sync import sync_outreach_close

logger = logging.getLogger(__name__)

_OUTCOME_MAP = {
    "won": "closed_happy",
    "lost": "closed_unhappy",
    "opt_out": "opted_out",
}


async def run_close_outreach(
    outreach_id: str = "",
    outcome: str = "won",
    reason: str = "",
    meeting_link: str = "",
    reason_code: str = "",
    reason_note: str = "",
) -> str:
    """Close an outreach with outcome tracking.

    Args:
        outreach_id: The outreach to close. Uses first active hot_lead if empty.
        outcome: "won", "lost", or "opt_out".
        reason: Optional reason/notes for the outcome.
        meeting_link: URL where the meeting was booked (for 'won'). Could be
            the user's booking page or the prospect's shared calendar link.
            Stored as "meeting_link" in outcome_json (source-agnostic).
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
            "Setup required before closing outreaches.\n\n"
            "Please run setup_profile first."
        )

    # ── Validate outcome ──
    if outcome not in _OUTCOME_MAP:
        return (
            f"Invalid outcome: '{outcome}'\n\n"
            "Must be one of:\n"
            "  'won'     — Prospect converted (meeting booked, deal closed)\n"
            "  'lost'    — Prospect declined or went cold\n"
            "  'opt_out' — Prospect requested no further contact"
        )

    new_status = _OUTCOME_MAP[outcome]

    # ── Resolve outreach ──
    if outreach_id:
        record = await run_db(get_outreach_with_contact, outreach_id)
        if not record:
            return f"Outreach not found: `{outreach_id}`"
    else:
        # Find first hot_lead in active campaign
        record = await _find_closeable_outreach()
        if not record:
            return (
                "No outreaches to close.\n\n"
                "All outreaches are either already closed or still pending.\n"
                "Specify an outreach_id to close a specific one."
            )

    outreach_id = record["outreach_id"]
    old_status = record.get("status", "unknown")
    prospect_name = record.get("name", "Unknown")
    title = record.get("title", "")
    company = record.get("company", "")

    # Don't close if already closed
    if old_status in ("closed_happy", "closed_unhappy", "opted_out"):
        return (
            f"**{prospect_name}** is already closed ({outcome_label(old_status)}).\n"
            "No changes made."
        )

    # ── Build outcome data ──
    outcome_data = {
        "outcome": outcome,
        "reason": reason,
        "closed_at": int(time.time()),
        "previous_status": old_status,
    }
    if meeting_link and outcome == "won":
        outcome_data["meeting_link"] = meeting_link

    # Auto-capture prospect's calendar URL from next_action if available
    next_action_raw = record.get("next_action", "")
    if next_action_raw:
        try:
            na = json.loads(next_action_raw)
            if isinstance(na, dict):
                pcal = na.get("prospect_calendar_url") or na.get("calendar_url", "")
                if pcal:
                    outcome_data["prospect_calendar_url"] = pcal
        except (ValueError, TypeError):
            pass

    if reason_code:
        outcome_data["reason_code"] = reason_code
    if reason_note:
        outcome_data["reason_note"] = reason_note

    # ── Update outreach ──
    await run_db(update_outreach, outreach_id,
        status=new_status,
        outcome_json=json.dumps(outcome_data),
        next_action=None,)

    await run_db(log_action, "outreach_closed",
        outreach_id=outreach_id,
        result=new_status,
        details={
            "outcome": outcome,
            "reason": reason,
            "previous_status": old_status,
            "prospect": prospect_name,
        },)

    # Sync to backend so cloud scheduler stops outreach
    synced = await sync_outreach_close(
        outreach_id, outcome, reason, meeting_link,
        reason_code=reason_code, reason_note=reason_note,
    )
    if not synced:
        logger.warning("Could not sync close for outreach %s to backend", outreach_id)

    # ── Archive chat on LinkedIn (non-blocking) ──
    try:
        from ..linkedin import get_account_id, get_linkedin_client
        import json as _json
        account_id = await run_db(get_account_id)
        linkedin_id = ""
        pj = record.get("profile_json")
        if pj:
            try:
                _profile = _json.loads(pj) if isinstance(pj, str) else pj
                linkedin_id = _profile.get("provider_id", "")
            except Exception:
                pass
        if not linkedin_id:
            linkedin_id = record.get("linkedin_id", "")
        if account_id and linkedin_id:
            client = get_linkedin_client()
            try:
                chat_id = await client.find_chat_for_user(account_id, linkedin_id)
                if chat_id:
                    await client.archive_chat(chat_id)
            finally:
                await client.close()
    except Exception:
        pass  # Non-critical — inbox hygiene

    # ── Auto-sync to HubSpot on "won" (non-blocking) ──
    hubspot_synced = False
    if outcome == "won":
        try:
            hubspot_api_key = await run_db(get_setting, "hubspot_api_key", "")
            if hubspot_api_key:
                from ..services.hubspot_service import HubSpotClient
                from ..db.queries import get_crm_mapping, save_crm_mapping, get_messages_for_outreach

                contact_id = record.get("contact_id", "")
                existing = await run_db(get_crm_mapping, contact_id, "hubspot") if contact_id else None
                if contact_id and not (existing and existing.get("crm_contact_id")):
                    hs = HubSpotClient(hubspot_api_key)
                    hs_contact_id = await hs.create_or_update_contact(
                        name=prospect_name,
                        title=title,
                        company=company,
                        linkedin_url=record.get("linkedin_url", ""),
                    )
                    # Create deal
                    campaign_name = ""
                    try:
                        from ..db.queries import get_campaign
                        camp = await run_db(get_campaign, record.get("campaign_id", ""))
                        campaign_name = camp.get("name", "") if camp else ""
                    except Exception:
                        pass
                    deal_name = f"{prospect_name} — {campaign_name}" if campaign_name else prospect_name
                    hs_deal_id = await hs.create_deal(
                        deal_name=deal_name,
                        stage="closedwon",
                        contact_id=hs_contact_id,
                    )
                    # Add conversation notes
                    try:
                        messages = await run_db(get_messages_for_outreach, outreach_id)
                        if messages:
                            conversation = []
                            for msg in messages[:10]:
                                role = "You" if msg.get("role") == "sdr" else prospect_name
                                conversation.append(f"{role}: {msg.get('text', '')}")
                            note_body = (
                                f"<b>HeyLead Conversation ({campaign_name})</b><br><br>"
                                + "<br>".join(conversation)
                            )
                            await hs.create_note(hs_contact_id, note_body)
                    except Exception:
                        pass
                    await run_db(save_crm_mapping, contact_id=contact_id,
                        crm_type="hubspot",
                        crm_contact_id=hs_contact_id,
                        crm_deal_id=hs_deal_id,)
                    hubspot_synced = True
                    logger.info("Auto-synced won deal to HubSpot: %s", prospect_name)
        except Exception as e:
            logger.debug("HubSpot auto-sync failed for %s: %s", prospect_name, e)

    # ── Format result ──
    icon = outcome_icon(new_status)
    label = outcome_label(new_status)

    role = title
    if company:
        role += f" at {company}" if role else company

    output = [
        f"{icon} Outreach closed — {label}",
        "",
        f"   {prospect_name}" + (f" ({role})" if role else ""),
        f"   Previous status: {old_status}",
    ]

    if reason:
        output.append(f"   Reason: {reason}")
    if meeting_link and outcome == "won":
        output.append(f"   Meeting: {meeting_link}")

    if hubspot_synced:
        output.append("")
        output.append("🔄 Auto-synced to HubSpot — contact + deal created.")

    output.append("")
    output.append("This prospect won't receive further automated outreach.")

    return "\n".join(output)


async def _find_closeable_outreach() -> dict | None:
    """Find the first hot_lead or replied outreach in active campaigns."""

    def _query() -> dict | None:
        from ..db.schema import get_db

        db = get_db()

        # Try hot_leads first, then replied, then connected
        row = db.execute(
            """SELECT o.id as outreach_id, o.status, o.updated_at,
                      c.name, c.title, c.company, c.fit_score
               FROM outreaches o
               JOIN contacts c ON o.contact_id = c.id
               JOIN campaigns camp ON o.campaign_id = camp.id
               WHERE camp.status = 'active'
                 AND o.status IN ('hot_lead', 'replied', 'connected')
               ORDER BY
                 CASE o.status
                   WHEN 'hot_lead' THEN 1
                   WHEN 'replied' THEN 2
                   WHEN 'connected' THEN 3
                 END,
                 c.fit_score DESC
               LIMIT 1""",
        ).fetchone()
        db.close()

        return dict(row) if row else None

    return await run_db(_query)
