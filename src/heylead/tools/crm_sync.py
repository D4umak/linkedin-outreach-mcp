"""Tool: crm_sync — Sync campaign contacts and deals with HubSpot CRM.

action='push' (the default) sends won deals, hot leads, or all contacts to
HubSpot as contacts + deals, tracking what was sent to avoid duplicates.
action='pull' reads deals back for mapped contacts: a closed-won deal records
its amount and marks the outreach won (services/crm_pull.py, heylead-api#1212).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..db.queries import (
    find_active_campaign,
    get_crm_mapping,
    get_hot_lead_outreaches,
    get_messages_for_outreach,
    get_setting,
    get_unsynced_won_outreaches,
    list_campaigns,
    save_crm_mapping,
)
from ..formatter import table
from ..db.async_bridge import run_db
from ..services import revenue

logger = logging.getLogger(__name__)


async def run_crm_sync(
    campaign_id: str = "",
    filter: str = "won",
    hubspot_api_key: str = "",
    action: str = "push",
) -> str:
    """Sync campaign contacts/deals with HubSpot CRM.

    Args:
        campaign_id: Campaign to sync. Uses the most recent if empty (push);
            every campaign if empty (pull).
        filter: Which contacts to push: "won" (default), "hot_leads", or "all".
        hubspot_api_key: Optional — override the saved API key.
        action: "push" (default) or "pull".
    """
    action = (action or "push").strip().lower()
    if action not in ("push", "pull"):
        return f"Unknown action '{action}'. Use 'push' or 'pull'."
    from ..services.hubspot_service import HubSpotClient, HubSpotError

    # Check setup
    setup_done = await run_db(get_setting, "setup_complete", False)
    if not setup_done:
        return "Setup required. Run setup_profile first."

    # Get HubSpot API key
    api_key = hubspot_api_key or await run_db(get_setting, "hubspot_api_key", "")
    if not api_key:
        return (
            "**HubSpot not connected.**\n\n"
            "To connect HubSpot:\n"
            "1. Go to HubSpot → Settings → Integrations → Private Apps\n"
            "2. Create a private app with CRM scopes (contacts, deals, notes)\n"
            "3. Copy the access token\n"
            "4. Run: `crm_sync(hubspot_api_key='your-token-here')`\n\n"
            "The key will be saved for future syncs."
        )

    # Save key if newly provided
    if hubspot_api_key:
        from ..db.queries import save_setting
        await run_db(save_setting, "hubspot_api_key", hubspot_api_key)

    if action == "pull":
        return await _run_pull(api_key, campaign_id)

    # Resolve campaign
    campaign, err = await run_db(find_active_campaign, campaign_id)
    if not campaign and not campaign_id:
        campaigns = await run_db(list_campaigns)
        if campaigns:
            campaign = campaigns[0]
    if not campaign:
        return err or "No campaign found."

    campaign_id = campaign["id"]
    campaign_name = campaign.get("name", "")

    # Initialize HubSpot client
    hs = HubSpotClient(api_key)

    # Test connection
    try:
        valid = await hs.test_connection()
        if not valid:
            return "❌ HubSpot API key is invalid. Check your token and try again."
    except Exception as e:
        return f"❌ HubSpot connection failed: {e}"

    # Determine what to sync
    outreaches: list[dict] = []
    if filter == "won":
        outreaches = await run_db(get_unsynced_won_outreaches, campaign_id)
        filter_label = "won deals"
    elif filter == "hot_leads":
        outreaches = await run_db(get_hot_lead_outreaches, campaign_id)
        filter_label = "hot leads"
    else:
        # All — combine both
        outreaches = await run_db(get_unsynced_won_outreaches, campaign_id) + await run_db(get_hot_lead_outreaches, campaign_id)
        filter_label = "won + hot leads"

    if not outreaches:
        return (
            f"No unsynced {filter_label} in campaign **{campaign_name}**.\n\n"
            "All matching contacts have already been synced to HubSpot."
        )

    # Sync each outreach
    synced_contacts = 0
    synced_deals = 0
    errors: list[str] = []
    synced_rows: list[list[str]] = []

    for outreach in outreaches:
        contact_id = outreach["contact_id"]
        name = outreach.get("name", "Unknown")
        title = outreach.get("title", "")
        company = outreach.get("company", "")
        linkedin_url = outreach.get("linkedin_url", "")

        # Check if already synced
        existing = await run_db(get_crm_mapping, contact_id, "hubspot")
        if existing and existing.get("crm_contact_id"):
            continue

        try:
            # Create/update HubSpot contact
            hs_contact_id = await hs.create_or_update_contact(
                name=name,
                title=title,
                company=company,
                linkedin_url=linkedin_url,
            )
            synced_contacts += 1

            # Create deal for won outreaches
            hs_deal_id = ""
            outcome = {}
            if outreach.get("outcome_json"):
                try:
                    outcome = json.loads(outreach["outcome_json"])
                except (json.JSONDecodeError, TypeError):
                    pass

            won = revenue.is_won(outreach["status"]) if "status" in outreach else bool(outcome)
            if won or outcome.get("reason"):
                deal_name = f"{name} — {campaign_name}"  # nosemgrep: person-line-without-link -- a HubSpot deal title (person + campaign), not a chat line
                stored = await run_db(revenue.get_deal, outreach["outreach_id"]) or {}
                amount = stored.get("deal_value")
                hs_deal_id = await hs.create_deal(
                    deal_name=deal_name,
                    stage="closedwon" if won else "qualifiedtobuy",
                    amount=f"{amount:.2f}" if won and amount is not None else "",
                    contact_id=hs_contact_id,
                )
                synced_deals += 1

            # Add conversation notes
            try:
                messages = await run_db(get_messages_for_outreach, outreach["outreach_id"])
                if messages:
                    conversation = []
                    for msg in messages[:10]:  # Cap at 10 messages
                        role = "You" if msg.get("role") == "sdr" else name
                        conversation.append(f"{role}: {msg.get('text', '')}")

                    note_body = (
                        f"<b>HeyLead Conversation ({campaign_name})</b><br><br>"
                        + "<br>".join(conversation)
                    )
                    await hs.create_note(hs_contact_id, note_body)
            except Exception as e:
                logger.debug("Note creation failed for %s: %s", name, e)

            # Save mapping
            await run_db(save_crm_mapping, contact_id=contact_id,
                crm_type="hubspot",
                crm_contact_id=hs_contact_id,
                crm_deal_id=hs_deal_id,)

            synced_rows.append([
                name[:25],
                (title or "—")[:25],
                (company or "—")[:20],
                "✓ Contact" + (" + Deal" if hs_deal_id else ""),
            ])

        except HubSpotError as e:
            errors.append(f"{name}: {e}")
            logger.error("HubSpot sync failed for %s: %s", name, e)
        except Exception as e:
            errors.append(f"{name}: {e}")
            logger.error("Unexpected sync error for %s: %s", name, e)

    # Build output
    parts: list[str] = []
    parts.append(f"## ✅ HubSpot Sync Complete")
    parts.append(f"**Campaign**: {campaign_name}")
    parts.append(f"**Filter**: {filter_label}")
    parts.append(f"**Synced**: {synced_contacts} contacts, {synced_deals} deals")

    if synced_rows:
        parts.append("")
        headers = ["Name", "Title", "Company", "Synced"]
        parts.append(table(headers, synced_rows))

    if errors:
        parts.append("")
        parts.append(f"⚠️ {len(errors)} errors:")
        for err in errors[:5]:
            parts.append(f"  - {err}")

    parts.append("")
    parts.append("Contacts are now in HubSpot with LinkedIn data and conversation history.")

    return "\n".join(parts)


async def _run_pull(api_key: str, campaign_id: str = "") -> str:
    """crm_sync(action='pull'): read closed-won deals and their amounts back."""
    from ..services.crm_pull import pull_deals
    from ..services.hubspot_service import HubSpotClient

    hs = HubSpotClient(api_key)
    try:
        if not await hs.test_connection():
            return "HubSpot API key is invalid. Check your token and try again."
    except Exception as e:
        return f"HubSpot connection failed: {e}"

    summary = await pull_deals(hs, campaign_id)
    if not summary["checked"]:
        return (
            "No HubSpot contacts to read yet.\n\n"
            "Push first with crm_sync(action='push'); a pull reads the deals of "
            "contacts HeyLead has already sent to HubSpot."
        )
    parts = [
        "## HubSpot pull",
        f"Checked {summary['checked']} contacts: {summary['marked_won']} newly won, "
        f"{summary['amount_updated']} amounts updated, {summary['open']} with no closed-won deal.",
    ]
    if summary["changed"]:
        rows = [
            [
                str(c["name"])[:25],
                revenue.format_amount(float(c["amount"]), c["currency"]) if c["amount"] is not None else "no amount",
                "marked won" if c["marked_won"] else "amount updated",
            ]
            for c in summary["changed"]
        ]
        parts += ["", table(["Name", "Deal", "Change"], rows)]
    totals = await run_db(revenue.revenue_by_currency, campaign_id)
    if totals:
        parts += ["", "Revenue won: " + ", ".join(
            revenue.format_amount(v, k) for k, v in sorted(totals.items(), key=lambda kv: -kv[1])
        )]
    if summary["cloud_synced"]:
        parts.append(f"Sent {summary['cloud_synced']} deals to your hosted workspace.")
    if summary["errors"]:
        parts += ["", f"{len(summary['errors'])} errors:"] + [f"  - {e}" for e in summary["errors"][:5]]
    parts += ["", "An open or reopened deal never un-wins an outreach."]
    return "\n".join(parts)
