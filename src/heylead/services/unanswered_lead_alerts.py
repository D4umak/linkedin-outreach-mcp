"""Detect unanswered hot leads and email the account owner.

A booking-link reply that we never answered used to disappear into a Hot
Leads count. The scheduler scans every 15 minutes; after a 2-hour grace
(so a normal auto-reply can fire) we POST one alert per lead per day.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import is_backend_mode
from ..constants import (
    UNANSWERED_LEAD_ALERT_COOLDOWN_SECONDS,
    UNANSWERED_LEAD_SCAN_CAP,
)
from ..dashboard_links import dashboard_url
from ..db.async_bridge import run_db
from ..db.queries import get_unanswered_leads, log_action, was_unanswered_lead_alerted

logger = logging.getLogger(__name__)

_ACTION = "unanswered_lead_alerted"


def format_needs_attention_lines(leads: list[dict[str, Any]]) -> list[str]:
    """MCP / show_status block. Empty input → no lines (hide the section)."""
    if not leads:
        return []
    lines = [f"Needs attention ({len(leads)}):"]
    has_id = False
    for i, lead in enumerate(leads):
        prefix = "└──" if i == len(leads) - 1 else "├──"
        name = lead.get("contact_name") or "Unknown"
        company = lead.get("company") or ""
        reason = lead.get("reason") or "Unanswered"
        who = f"{name} ({company})" if company else name
        lines.append(f"{prefix} {who} — {reason}")
        # The id is the only thing that makes this list actionable — without
        # it there is no way to answer or dismiss the lead you are reading.
        outreach_id = lead.get("outreach_id") or ""
        if outreach_id:
            has_id = True
            pad = "    " if i == len(leads) - 1 else "│   "
            lines.append(f"{pad}`{outreach_id}`")
    if has_id:
        lines.append('    Reply: send_message(action="reply", outreach_id="…")')
        lines.append('    Clear: prospect(action="dismiss", outreach_id="…")')
    lines.append("")
    return lines


def format_needs_attention_digest(leads: list[dict[str, Any]]) -> list[str]:
    """Daily-digest markdown. Empty input → no lines."""
    if not leads:
        return []
    lines = ["## Needs attention"]
    for lead in leads:
        name = lead.get("contact_name") or "Unknown"
        company = lead.get("company") or ""
        reason = lead.get("reason") or "Unanswered"
        campaign = lead.get("campaign_name") or ""
        who = f"{name} ({company})" if company else name
        tail = f" — {campaign}" if campaign else ""
        lines.append(f"- {who} — {reason}{tail}")
    lines.append("")
    return lines


async def _post_unanswered_lead_alert(lead: dict[str, Any]) -> str:
    """POST one lead to the hosted alert route. Returns sent|skipped|error."""
    try:
        from ..config import is_backend_mode as _backend

        if not _backend():
            return "skipped"

        import httpx
        from .cloud_sync import _base_url, _headers

        payload = {
            "outreach_id": lead.get("outreach_id", ""),
            "campaign_name": lead.get("campaign_name", ""),
            "contact_name": lead.get("contact_name", ""),
            "company": lead.get("company", ""),
            "title": lead.get("title", ""),
            "hours_unanswered": lead.get("hours_unanswered", 0),
            "reason": lead.get("reason", ""),
            "last_message_preview": lead.get("last_message_preview", ""),
            "linkedin_url": lead.get("linkedin_url", ""),
            # The backend email renders this as the Overview link.
            "dashboard_url": dashboard_url().rstrip("/"),
        }
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(
                f"{_base_url()}/api/v1/alerts/unanswered-lead",
                json=payload,
                headers=_headers(),
            )
        if resp.status_code == 200:
            return "sent"
        if resp.status_code == 404:
            logger.info("Unanswered-lead alert endpoint not deployed yet")
            return "skipped"
        logger.warning(
            "Unanswered-lead alert failed: HTTP %d — %s",
            resp.status_code, resp.text[:100],
        )
        return "error"
    except Exception as e:
        logger.warning("Unanswered-lead alert failed: %s", e)
        return "error"


async def run_unanswered_lead_scan() -> int:
    """Email the owner about unanswered leads past the grace window.

    Returns how many alerts were accepted (sent) this scan.
    """
    import time

    try:
        leads = await run_db(get_unanswered_leads)
    except Exception as e:
        logger.warning("Unanswered-lead query failed: %s", e)
        return 0

    if not leads:
        return 0

    since = int(time.time()) - UNANSWERED_LEAD_ALERT_COOLDOWN_SECONDS
    sent = 0
    backend = is_backend_mode()

    for lead in leads:
        if sent >= UNANSWERED_LEAD_SCAN_CAP:
            break
        outreach_id = lead.get("outreach_id") or ""
        if not outreach_id:
            continue
        try:
            if await run_db(was_unanswered_lead_alerted, outreach_id, since):
                continue
        except Exception as e:
            logger.debug("Alert cooldown lookup failed: %s", e)
            continue

        if not backend:
            await run_db(
                log_action, _ACTION, outreach_id=outreach_id,
                result="skipped",
                details={"reason": "no_backend", "contact_name": lead.get("contact_name", "")},
                campaign_id=lead.get("campaign_id", ""),
            )
            continue

        result = await _post_unanswered_lead_alert(lead)
        if result == "sent":
            await run_db(
                log_action, _ACTION, outreach_id=outreach_id,
                result="success",
                details={
                    "contact_name": lead.get("contact_name", ""),
                    "reason": lead.get("reason", ""),
                    "hours_unanswered": lead.get("hours_unanswered", 0),
                },
                campaign_id=lead.get("campaign_id", ""),
            )
            sent += 1
        elif result == "skipped":
            await run_db(
                log_action, _ACTION, outreach_id=outreach_id,
                result="skipped",
                details={"reason": "endpoint_unavailable"},
                campaign_id=lead.get("campaign_id", ""),
            )
        else:
            await run_db(
                log_action, _ACTION, outreach_id=outreach_id,
                result="error",
                details={"reason": "post_failed"},
                campaign_id=lead.get("campaign_id", ""),
            )

    if sent:
        logger.info("Unanswered-lead scan emailed %d lead(s)", sent)
    return sent
