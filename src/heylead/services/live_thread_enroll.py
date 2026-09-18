"""Enroll a live LinkedIn thread onto the campaign it already fits.

check_replies used to list strangers and stop. Inbound only enrolled after it
sent a DM. If you already answered, the person never joined the roster.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from ..constants import MIN_FIT_SCORE_THRESHOLD
from ..db.async_bridge import run_db
from ..db.queries import (
    enroll_prospect,
    get_campaign,
    get_messages_for_outreach,
    list_campaigns,
    log_action,
    save_message,
    update_outreach,
)
from .dedup_service import is_company_profile
from .icp_match_scorer import compute_icp_match
from .inbox_match import find_best_inbox_match
from .signal_linker import _match_best_campaign

logger = logging.getLogger(__name__)

FetchProfile = Callable[[str], dict[str, Any] | None]


def enroll_matching_live_thread(
    *,
    sender_id: str,
    name: str,
    text: str = "",
    headline: str = "",
    company: str = "",
    linkedin_url: str = "",
    we_already_wrote: bool = False,
    fetch_profile: FetchProfile | None = None,
    message_id: str = "",
    timestamp: int | None = None,
) -> dict[str, Any] | None:
    """Put a live chat on the best-fitting active campaign. Does not send."""
    sender_id = (sender_id or "").strip()
    name = (name or "").strip()
    if not sender_id or not name:
        return None

    existing = find_best_inbox_match(sender_id)
    if existing:
        return _attach_to_existing(
            existing, text=text, we_already_wrote=we_already_wrote,
            message_id=message_id, timestamp=timestamp,
        )

    headline = (headline or "").strip()
    company = (company or "").strip()
    if not headline and not company and fetch_profile:
        fetched = fetch_profile(sender_id) or {}
        headline = (fetched.get("headline") or fetched.get("title") or "").strip()
        company = (fetched.get("company") or company).strip()
        linkedin_url = linkedin_url or (fetched.get("linkedin_url") or "")

    if not headline and not company and not (text or "").strip():
        return None

    prospect = {
        "name": name,
        "title": headline,
        "company": company,
        "linkedin_id": sender_id,
        "provider_id": sender_id,
        "linkedin_url": linkedin_url,
        "profile_json": json.dumps({
            "provider_id": sender_id,
            "headline": headline,
            "is_open_profile": False,
        }),
    }
    if is_company_profile(prospect):
        return None

    campaigns = list_campaigns(status="active")
    campaign_id = _match_best_campaign(
        {
            "content": text or "",
            "prospect_title": f"{headline} {company}".strip(),
        },
        campaigns,
    )
    if not campaign_id:
        return None

    campaign = get_campaign(campaign_id)
    if not campaign:
        return None
    try:
        cfg = json.loads(campaign.get("config_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        cfg = {}
    try:
        min_fit = float(cfg.get("min_fit_score", MIN_FIT_SCORE_THRESHOLD))
    except (TypeError, ValueError):
        min_fit = MIN_FIT_SCORE_THRESHOLD

    fit = float(compute_icp_match(prospect, campaign.get("icp_json"))["icp_match_score"])
    if fit < min_fit:
        return None
    prospect["fit_score"] = fit

    status = "messaged" if we_already_wrote else "replied"
    outreach_id = enroll_prospect(
        campaign_id,
        prospect,
        source="inbound_dm",
        source_detail="live_thread",
        status=status,
    )
    if not outreach_id:
        return None

    now = int(time.time())
    update_outreach(outreach_id, accepted_at=now, status=status)
    if text:
        save_message(
            outreach_id, role="prospect", text=text,
            external_message_id=message_id or None,
            timestamp=timestamp,
        )
    log_action(
        "live_thread_enrolled",
        outreach_id=outreach_id,
        campaign_id=campaign_id,
        result=status,
        details={"reason": "fits_active_campaign", "fit_score": fit},
    )
    return {
        "outreach_id": outreach_id,
        "campaign_id": campaign_id,
        "status": status,
        "name": name,
    }


def _first_outbound_touch(contact: dict[str, Any], messages: list[dict]) -> int:
    touches = [int(contact.get("invited_at") or 0)]
    touches += [
        int(m.get("timestamp") or 0)
        for m in messages
        if m.get("role") == "sdr"
    ]
    positive = [t for t in touches if t > 0]
    return min(positive) if positive else 0


def _attach_to_existing(
    existing: dict[str, Any],
    *,
    text: str,
    we_already_wrote: bool,
    message_id: str,
    timestamp: int | None = None,
) -> dict[str, Any] | None:
    outreach_id = existing.get("outreach_id") or ""
    if not outreach_id:
        return None
    messages = get_messages_for_outreach(outreach_id)
    if _first_outbound_touch(existing, messages) > 0:
        return None
    # A pending refill with an old inbox thread is not a live conversation.
    # Only attach when we already treated them as contacted (invite outside
    # HeyLead, operator-marked connected) or we already wrote in this chat.
    live_status = existing.get("status") in {
        "invited", "connected", "messaged", "replied", "hot_lead",
    }
    if not live_status and not we_already_wrote:
        return None
    status = "messaged" if we_already_wrote else "replied"
    kwargs: dict[str, Any] = {"status": status}
    if not existing.get("accepted_at"):
        kwargs["accepted_at"] = int(time.time())
    update_outreach(outreach_id, **kwargs)
    if text:
        save_message(
            outreach_id, role="prospect", text=text,
            external_message_id=message_id or None,
            timestamp=timestamp,
        )
    log_action(
        "live_thread_enrolled",
        outreach_id=outreach_id,
        campaign_id=existing.get("campaign_id") or "",
        result=status,
        details={"reason": "roster_without_outbound_touch"},
    )
    return {
        "outreach_id": outreach_id,
        "campaign_id": existing.get("campaign_id") or "",
        "status": status,
        "name": existing.get("name") or "",
    }


async def enrich_and_enroll_live_thread(
    *,
    sender_id: str,
    name: str,
    text: str = "",
    headline: str = "",
    company: str = "",
    linkedin_url: str = "",
    we_already_wrote: bool = False,
    message_id: str = "",
    timestamp: int | None = None,
    client: Any = None,
    account_id: str = "",
) -> dict[str, Any] | None:
    """Same as enroll_matching_live_thread, with one profile fetch if needed."""
    if not (headline or company) and client and account_id and sender_id:
        try:
            profile = await client.get_profile(account_id, sender_id)
        except Exception:
            logger.debug("live-thread profile fetch failed for %s", sender_id, exc_info=True)
            profile = {}
        if profile:
            headline = headline or (profile.get("headline") or profile.get("occupation") or "")
            company = company or (profile.get("company") or "")
            linkedin_url = linkedin_url or (profile.get("linkedin_url") or profile.get("profile_url") or "")
            name = name or (profile.get("name") or "")
    return await run_db(
        enroll_matching_live_thread,
        sender_id=sender_id,
        name=name,
        text=text,
        headline=headline,
        company=company,
        linkedin_url=linkedin_url,
        we_already_wrote=we_already_wrote,
        message_id=message_id,
        timestamp=timestamp,
    )
