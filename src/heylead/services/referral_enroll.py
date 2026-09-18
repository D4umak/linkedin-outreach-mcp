"""Enroll a referred person into the referrer's campaign and jump the queue."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..ai.referral_extractor import ReferralHandoff, detect_referral_from_thread
from ..constants import JOB_EMAIL_INVITE, JOB_INMAIL, JOB_INVITE, JOB_SEND_DM
from ..db.async_bridge import run_db
from ..db.global_contact_queries import (
    get_global_contact_by_linkedin_id,
    is_excluded_by_email,
)
from ..db.queries import (
    create_outreach,
    create_scheduler_job,
    enroll_prospect,
    get_campaign,
    get_contact_analysis,
    get_outreach,
    get_pending_outreach_job,
    get_setting,
    list_campaigns,
    log_action,
    save_contact_analysis,
    update_contact,
    update_outreach,
    _backfill_campaign_contact,
)
from ..linkedin import get_account_id, get_linkedin_client
from ..textutil import first_name, full_name
from ..tier import get_caps
from .channel_selector import _extract_email
from .cloud_sync import cloud_sends_this_job
from .icp_match_scorer import compute_icp_match
from .inbox_match import find_best_inbox_match
from .outreach_channel import choose_first_touch
from .prospect_email import attach_email_to_profile_json, extract_profile_email
from .signal_linker import _match_best_campaign

logger = logging.getLogger(__name__)

_EMAIL_JOB_DELAY = 3 * 60
_LINKEDIN_JOB_DELAY = 10 * 60


def fill_sender_name(sender_id: str, sender_name: str) -> str:
    """Use the inbox name, or the global contact, so inbound enroll is not nameless."""
    name = (sender_name or "").strip()
    if name:
        return name
    lid = (sender_id or "").strip()
    if not lid:
        return ""
    row = get_global_contact_by_linkedin_id(lid)
    return ((row or {}).get("name") or "").strip()


async def maybe_enroll_from_inbound(
    *,
    sender_id: str,
    text: str,
    name: str = "",
    company: str = "",
    campaign_id: str = "",
) -> dict[str, Any] | None:
    """Inbound-stranger path: resolve Ben from the global roster, then enroll."""
    resolved = await run_db(fill_sender_name, sender_id, name)
    return await maybe_enroll_from_reply(
        {
            "linkedin_id": (sender_id or "").strip(),
            "name": resolved,
            "company": (company or "").strip(),
            "campaign_id": (campaign_id or "").strip(),
        },
        text,
    )


async def maybe_enroll_from_reply(contact: dict[str, Any], reply_text: str) -> dict[str, Any] | None:
    """If this reply is a third-person handoff, enroll them on a live campaign."""
    if not reply_text:
        return None
    referrer = await _resolve_referrer(contact)
    campaign_id = await _resolve_campaign(referrer)
    if not campaign_id:
        logger.info("Referral skipped — no campaign to enroll into")
        return None
    from .job_search_guard import is_job_search_config_json
    campaign = await run_db(get_campaign, campaign_id) or {}
    if is_job_search_config_json(campaign.get("config_json")):
        # A job-search reply naming a colleague is the operator's to act on:
        # enrolling would email and DM that colleague within minutes.
        logger.info(
            "Referral skipped — job-search campaign %s, held for operator", campaign_id,
        )
        await run_db(
            log_action,
            "referral_held_job_search",
            result="held",
            details={
                "campaign_id": campaign_id,
                "referrer": (referrer.get("name") or referrer.get("linkedin_id") or ""),
            },
            campaign_id=campaign_id,
        )
        return None
    sender_email = _extract_email(referrer)
    our_emails = await run_db(_our_emails)
    texts = await _thread_texts(referrer, reply_text)
    handoff = detect_referral_from_thread(
        texts,
        sender_emails={sender_email} if sender_email else set(),
        our_emails=our_emails,
        referrer_company=referrer.get("company") or "",
    )
    if not handoff:
        return None
    try:
        return await enroll_referral(
            campaign_id=campaign_id,
            referrer=referrer,
            handoff=handoff,
        )
    except Exception:
        logger.warning("Referral enroll failed for %s", handoff.email or handoff.name, exc_info=True)
        return None


async def enroll_referral(
    *,
    campaign_id: str,
    referrer: dict[str, Any],
    handoff: ReferralHandoff,
) -> dict[str, Any] | None:
    """Create or reuse the referred contact, then queue same-day email + LinkedIn."""
    if handoff.email and await run_db(is_excluded_by_email, handoff.email):
        logger.info("Referral skipped — excluded address %s", handoff.email)
        return None

    profile = await _enrich_linkedin(handoff)
    linkedin_id = (
        (profile.get("provider_id") or profile.get("public_id") or "").strip()
    )
    if not handoff.email and not linkedin_id:
        logger.info("Referral skipped — no unique LinkedIn hit for %s", handoff.name)
        return None
    has_email = bool(handoff.email)
    existing = await run_db(_find_existing, campaign_id, handoff.email, linkedin_id)
    if existing:
        contact_id = existing["id"]
        await _stamp_existing(contact_id, referrer=referrer, handoff=handoff, profile=profile)
        outreach_id = await run_db(
            create_outreach, campaign_id=campaign_id, contact_id=contact_id,
        )
        await run_db(
            update_outreach,
            outreach_id,
            next_action=json.dumps({
                "type": "referral",
                "referral_email_handoff": has_email,
                "referrer_name": (referrer.get("name") or "a colleague").strip(),
                "email": handoff.email,
            }),
        )
        await _ensure_jobs(campaign_id, outreach_id, profile)
        return {"contact_id": contact_id, "outreach_id": outreach_id, "deduped": True}

    campaign = await run_db(get_campaign, campaign_id) or {}
    icp_score = compute_icp_match(
        {
            "name": handoff.name,
            "title": profile.get("title") or "",
            "company": profile.get("company") or handoff.company_guess,
            "profile_json": json.dumps(profile) if profile else "",
        },
        campaign.get("icp_json") or "",
    ).get("icp_match_score", 0.0)
    fit_score = max(float(icp_score or 0), 0.9)

    if handoff.email and not profile.get("email"):
        raw = attach_email_to_profile_json(json.dumps(profile) if profile else None, handoff.email)
        profile = json.loads(raw) if raw else {"email": handoff.email}
    elif handoff.email:
        profile["email"] = profile.get("email") or handoff.email

    referrer_name = (referrer.get("name") or "a colleague").strip()
    source_detail = f"Referred by {referrer_name}"
    outreach_id = await run_db(
        enroll_prospect,
        campaign_id,
        {
            "name": profile.get("name") or handoff.name,
            "title": profile.get("title") or "",
            "company": profile.get("company") or handoff.company_guess,
            "linkedin_url": profile.get("profile_url") or profile.get("linkedin_url") or "",
            "linkedin_id": linkedin_id,
            "profile_json": json.dumps(profile) if profile else "",
            "fit_score": fit_score,
            "email": handoff.email,
        },
        source="referral",
        source_detail=source_detail,
    )
    if not outreach_id:
        return None
    contact_id = ((await run_db(get_outreach, outreach_id)) or {}).get("contact_id")
    if not contact_id:
        return None

    hook = _engagement_hook(referrer, handoff)
    await run_db(save_contact_analysis, contact_id, {
        "referral_email_handoff": has_email,
        "signal_context": {
            "signal_angle": "warm_referral",
            "engagement_hook": hook,
            "signal_summary": handoff.quote,
            "DO_NOT": (
                "Do not pitch as if this is a cold list pull. "
                "Do not omit the referrer's first name."
            ),
        },
        "summary": hook,
    })
    await run_db(
        update_outreach,
        outreach_id,
        next_action=json.dumps({
            "type": "referral",
            "referral_email_handoff": has_email,
            "referrer_name": referrer_name,
            "email": handoff.email,
        }),
    )
    await _ensure_jobs(campaign_id, outreach_id, profile)
    logger.info(
        "Referral enrolled %s (%s) into campaign %s via %s",
        handoff.name, handoff.email, campaign_id[:8], referrer_name,
    )
    return {"contact_id": contact_id, "outreach_id": outreach_id, "deduped": False}


async def _stamp_existing(
    contact_id: str,
    *,
    referrer: dict[str, Any],
    handoff: ReferralHandoff,
    profile: dict[str, Any],
) -> None:
    """Keep Ben's intro on a row that was already in the campaign."""
    existing_analysis = await run_db(get_contact_analysis, contact_id) or {}
    if not isinstance(existing_analysis, dict):
        existing_analysis = {}
    hook = _engagement_hook(referrer, handoff)
    ctx = dict(existing_analysis.get("signal_context") or {}) if isinstance(
        existing_analysis.get("signal_context"), dict
    ) else {}
    ctx.update({
        "signal_angle": "warm_referral",
        "engagement_hook": hook,
        "signal_summary": handoff.quote,
        "DO_NOT": (
            "Do not pitch as if this is a cold list pull. "
            "Do not omit the referrer's first name."
        ),
    })
    existing_analysis["referral_email_handoff"] = bool(handoff.email)
    existing_analysis["signal_context"] = ctx
    await run_db(save_contact_analysis, contact_id, existing_analysis)
    await run_db(
        update_contact, contact_id, fit_score=0.9, source="referral",
        source_detail=f"Referred by {(referrer.get('name') or 'a colleague').strip()}",
    )
    await run_db(
        _backfill_campaign_contact,
        contact_id,
        linkedin_id=(profile.get("provider_id") or profile.get("public_id") or ""),
        linkedin_url=profile.get("profile_url") or profile.get("linkedin_url") or "",
        profile_json=json.dumps(profile) if profile else None,
        title=profile.get("title") or "",
        company=profile.get("company") or handoff.company_guess,
    )


async def _enrich_linkedin(handoff: ReferralHandoff) -> dict[str, Any]:
    account_id = await run_db(get_account_id)
    if not account_id:
        return {}
    try:
        client = get_linkedin_client()
    except Exception:
        return {}
    keywords = " ".join(p for p in (handoff.name, handoff.company_guess) if p).strip()
    try:
        hits = await client.search_people(account_id, keywords=keywords, count=5)
    except Exception:
        logger.debug("Referral LinkedIn search failed for %s", handoff.name, exc_info=True)
        hits = []
    hit = _pick_search_hit(hits, handoff)
    if not hit:
        return {}
    identifier = hit.get("provider_id") or hit.get("public_id") or ""
    profile: dict[str, Any] = dict(hit)
    if identifier:
        try:
            fetched = await client.get_profile(account_id, identifier)
            if isinstance(fetched, dict) and fetched:
                profile.update(fetched)
        except Exception:
            logger.debug("Referral profile fetch failed for %s", identifier, exc_info=True)
    return profile


def _pick_search_hit(hits: list[dict[str, Any]] | None, handoff: ReferralHandoff) -> dict[str, Any] | None:
    if not hits:
        return None
    needle = handoff.name.lower().strip()
    exact = [
        h for h in hits
        if isinstance(h, dict) and (h.get("name") or "").lower().strip() == needle
    ]
    if len(exact) == 1:
        return exact[0]
    if len(hits) == 1 and isinstance(hits[0], dict):
        return hits[0]
    first = needle.split()[0] if needle else ""
    if first:
        partial = [
            h for h in hits
            if isinstance(h, dict) and first in (h.get("name") or "").lower()
        ]
        if len(partial) == 1:
            return partial[0]
    return None


def _find_existing(campaign_id: str, email: str, linkedin_id: str) -> dict[str, Any] | None:
    from ..db.schema import get_db

    db = get_db()
    rows = db.execute(
        "SELECT * FROM contacts WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchall()
    db.close()
    email_l = (email or "").strip().lower()
    lid = (linkedin_id or "").strip().lower()
    for row in rows:
        contact = dict(row)
        if lid and (contact.get("linkedin_id") or "").strip().lower() == lid:
            return contact
        stored = extract_profile_email(contact).strip().lower()
        if email_l and stored == email_l:
            return contact
        try:
            blob = json.loads(contact.get("profile_json") or "{}")
        except (TypeError, ValueError):
            blob = {}
        if lid and isinstance(blob, dict):
            if (blob.get("provider_id") or "").strip().lower() == lid:
                return contact
            if (blob.get("public_id") or "").strip().lower() == lid:
                return contact
    return None


async def _ensure_jobs(
    campaign_id: str,
    outreach_id: str,
    profile: dict[str, Any],
) -> None:
    now = int(time.time())
    mailbox = bool(await run_db(get_setting, "email_account_id", "") or "")
    address = bool(extract_profile_email(profile) or profile.get("email"))
    if mailbox and address:
        await _queue_job(
            campaign_id, outreach_id, JOB_EMAIL_INVITE, now + _EMAIL_JOB_DELAY,
        )

    provider_id = (profile.get("provider_id") or "").strip()
    public_id = (profile.get("public_id") or "").strip()
    if not provider_id and not public_id:
        return
    try:
        caps = await get_caps()
        can_inmail = bool(getattr(caps, "can_send_credit_inmail", False))
    except Exception:
        can_inmail = False
    first = choose_first_touch(
        is_first_degree=await run_db(_is_connected, provider_id, public_id),
        can_send_credit_inmail=can_inmail,
        is_open_profile=bool(profile.get("is_open_profile")),
        has_provider_id=bool(provider_id),
    )
    job_type = {
        "dm": JOB_SEND_DM,
        "inmail": JOB_INMAIL,
        "invite": JOB_INVITE,
    }[first]
    await _queue_job(campaign_id, outreach_id, job_type, now + _LINKEDIN_JOB_DELAY)


async def _queue_job(
    campaign_id: str,
    outreach_id: str,
    job_type: str,
    scheduled_at: int,
) -> None:
    if await run_db(cloud_sends_this_job, job_type, campaign_id, outreach_id):
        return
    if not await run_db(get_pending_outreach_job, outreach_id, job_type):
        await run_db(
            create_scheduler_job,
            campaign_id, job_type, scheduled_at, outreach_id,
        )


async def _resolve_referrer(contact: dict[str, Any]) -> dict[str, Any]:
    row = dict(contact)
    lid = (
        (row.get("linkedin_id") or row.get("sender_id") or row.get("provider_id") or "")
        .strip()
    )
    if lid:
        row["linkedin_id"] = row.get("linkedin_id") or lid
        missing = not any((row.get(k) or "").strip() for k in ("name", "company", "title"))
        if missing or not (row.get("name") or "").strip():
            global_row = await run_db(get_global_contact_by_linkedin_id, lid)
            if global_row:
                for key in ("name", "title", "company"):
                    if not (row.get(key) or "").strip():
                        row[key] = global_row.get(key) or ""
    return row


async def _resolve_campaign(referrer: dict[str, Any]) -> str:
    cid = (referrer.get("campaign_id") or "").strip()
    if cid and await run_db(get_campaign, cid):
        return cid
    lid = (referrer.get("linkedin_id") or "").strip()
    if lid:
        inbox = await run_db(find_best_inbox_match, lid)
        inbox_cid = ((inbox or {}).get("campaign_id") or "").strip()
        if inbox_cid and await run_db(get_campaign, inbox_cid):
            return inbox_cid
    from .inbound_relationship import campaign_id_from_referrer_thread
    thread_cid = await run_db(campaign_id_from_referrer_thread, referrer)
    if thread_cid and await run_db(get_campaign, thread_cid):
        return thread_cid
    active = await run_db(list_campaigns, "active")
    if not active:
        return ""
    matched = _match_best_campaign(
        {
            "content": " ".join(
                p for p in (
                    referrer.get("name"),
                    referrer.get("title"),
                    referrer.get("company"),
                ) if p
            ),
            "prospect_title": f"{referrer.get('title') or ''} {referrer.get('company') or ''}".strip(),
        },
        active,
    )
    if matched:
        return matched
    if len(active) == 1:
        return active[0]["id"]
    autopilot = [c for c in active if (c.get("mode") or "") == "autopilot"]
    if len(autopilot) == 1:
        return autopilot[0]["id"]
    return ""


async def _thread_texts(referrer: dict[str, Any], reply_text: str) -> list[str]:
    texts: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        text = (raw or "").strip()
        if text and text not in seen:
            seen.add(text)
            texts.append(text)

    sender_id = (referrer.get("linkedin_id") or "").strip()
    if sender_id:
        for prior in await run_db(_inbound_contents, sender_id):
            _add(prior)
        for prior in await _chat_texts(sender_id):
            _add(prior)
    _add(reply_text)
    return texts


def _inbound_contents(sender_id: str) -> list[str]:
    from ..db.schema import get_db

    db = get_db()
    rows = db.execute(
        """SELECT content FROM inbound_signals
           WHERE sender_id = ? AND content IS NOT NULL AND content != ''
           ORDER BY created_at ASC""",
        (sender_id,),
    ).fetchall()
    db.close()
    return [str(r["content"]) for r in rows if r["content"]]


async def _chat_texts(sender_id: str) -> list[str]:
    account_id = await run_db(get_account_id)
    if not account_id or not sender_id:
        return []
    try:
        client = get_linkedin_client()
        messages = await client.get_messages_by_sender(account_id, sender_id, limit=20)
    except Exception:
        logger.debug("Referral chat lookback failed for %s", sender_id, exc_info=True)
        return []
    found: list[str] = []
    for msg in messages or []:
        if isinstance(msg, dict):
            text = (msg.get("text") or "").strip()
            if text:
                found.append(text)
    return found


def _is_connected(provider_id: str, public_id: str) -> bool:
    """Local connections table — 1st degree gets a DM, not an invite."""
    from .connection_sync import is_first_degree, is_first_degree_by_public_id
    from ..linkedin import get_account_id as _account_id

    account_id = _account_id()
    if not account_id:
        return False
    if provider_id and is_first_degree(account_id, provider_id):
        return True
    if public_id and is_first_degree_by_public_id(account_id, public_id):
        return True
    return False


def _engagement_hook(referrer: dict[str, Any], handoff: ReferralHandoff) -> str:
    # Both halves of this sentence go out to a real prospect, so both names
    # come from the shared helpers rather than a local copy of the idiom.
    #
    # The old line defaulted first, then split: with no referrer name it read
    # "A colleague".split()[0] and the message opened "A asked me to reach
    # you." Defaulting after the split fixes that. It also could not see a
    # zero-width name — U+200B and U+FEFF are not whitespace, so .strip() left
    # one truthy and .split() handed it back as the first name, which prints
    # as nothing at all.
    raw_name = referrer.get("name")
    name = full_name(raw_name, fallback="A colleague")
    first = first_name(raw_name, fallback="A colleague")
    title = (referrer.get("title") or "").strip()
    company = (referrer.get("company") or handoff.company_guess or "").strip()
    role = ", ".join(p for p in (title, company) if p)
    if role:
        return (
            f"{name} ({role}) asked me to reach you. "
            f"They said you are the commercial contact."
        )
    return f"{first} asked me to reach you."


def _our_emails() -> set[str]:
    found: set[str] = set()
    raw = get_setting("profile", "") or ""
    if raw:
        try:
            profile = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            profile = {}
        if isinstance(profile, dict):
            addr = extract_profile_email(profile)
            if addr:
                found.add(addr)
    return found
