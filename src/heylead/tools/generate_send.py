"""Tool 3: generate_and_send — Generate a personalized message and send (or queue for review).

Core outreach loop:
1. Pick next prospect from campaign queue
2. Generate personalized invitation message (voice-matched)
3. Run 5-stage validation
4. In Copilot mode: show for approval. In Autopilot: send immediately.
5. Track rate limits and scheduling
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..ai.copywriter import provenance as copy_provenance
from ..ai.message_fixer import fix_message
from ..ai.message_generator import check_job_search_draft, generate_message
from ..ai.message_improver import improve_message
from ..ai.message_validator import is_evaluator_refusal, validate_message
from ..ai.llm_validator import llm_validate
from ..ai.prospect_analyzer import analyze_prospect, is_data_gap_analysis
from ..ai.targeting_recheck import (
    TARGETING_MISMATCH_ERROR,
    is_clear_mismatch,
    recheck_campaign_fit,
)
from ..config import apply_free_monthly_caps
from ..constants import (
    FREE_MONTHLY_INVITATIONS,
    MIN_WARMUP_ENGAGEMENTS,
)
from ..db import aio as adb
from ..db.async_bridge import run_db
from ..services.fit_gate import FIT_SKIP_ERROR, fit_gate_verdict, should_fit_skip
from ..db.queries import (
    find_active_campaign,
    get_campaign_context,
    get_contact_analysis,
    get_contacts_for_campaign,
    get_engagement_count_for_outreach,
    get_monthly_usage,
    get_setting,
    increment_sent,
    increment_usage,
    log_action,
    save_contact_analysis,
    save_message,
    update_campaign,
    update_contact,
    update_outreach,
)
from ..formatter import stars
from ..linkedin.rate_limiter import can_send_now, get_next_delay, update_limits_after_send
from ..services.channel_selector import (
    CHANNEL_EMAIL,
    CHANNEL_LINKEDIN,
    select_channel,
    has_email_channel,
    _extract_email,
)
from ..services.provider_id_resolver import is_sendable_invite_id
from ..linkedin import (
    UnipileAuthError,
    UnipileError,
    get_account_id,
    get_linkedin_client,
)

logger = logging.getLogger(__name__)


def try_claim_outreach(outreach_id: str, statuses_sql: str) -> tuple[str, int]:
    """Atomically claim an outreach for sending (CAS on status).

    Returns (original_status, rowcount) — rowcount 0 means another job holds it.
    """
    from ..db.schema import get_db as _get_db_lock
    lock_db = _get_db_lock()
    orig_row = lock_db.execute("SELECT status FROM outreaches WHERE id = ?", (outreach_id,)).fetchone()
    orig_status = orig_row["status"] if orig_row else "pending"
    # Bump updated_at: recovery passes treat a stale-updated_at 'sending' row
    # as stuck — without this, a fresh claim looks hours old immediately.
    count = lock_db.execute(
        f"UPDATE outreaches SET status = 'sending', updated_at = strftime('%s','now') "
        f"WHERE id = ? AND status IN {statuses_sql}",
        (outreach_id,),
    ).rowcount
    lock_db.commit()
    lock_db.close()
    return orig_status, count


def _build_campaign_context(campaign_config: dict, icp_data: dict) -> dict:
    """The thin context dict handed to the generators.

    Must carry campaign_intent and campaign_type: historically it dropped
    every config field, which silently reverted all intent routing to the
    sell default (the job_search seam was dead in the live path for the
    same reason).
    """
    return {
        "target_description": campaign_config.get("target_description", ""),
        "relevance_hook": icp_data.get("relevance_hook", ""),
        "campaign_intent": campaign_config.get("campaign_intent", ""),
        "campaign_type": campaign_config.get("campaign_type", ""),
        "from_email": campaign_config.get("from_email", ""),
    }


def _send_gate_open(channel: str, linkedin_ok: bool, email_ok: bool) -> bool:
    """Whether the send may proceed, per the limit that governs this channel.

    Email overflow exists for the case where LinkedIn is capped, so an email
    send must be judged on the email limits alone — and never the reverse.
    """
    return email_ok if channel == CHANNEL_EMAIL else linkedin_ok


async def _resolve_email_gate(channel: str, linkedin_ok: bool) -> tuple[bool, str]:
    """Whether the email budget permits this send.

    Consulted for every email send, not only when LinkedIn is also capped.
    ``can_email`` used to be initialised True and recomputed only inside the
    ``if not can_send:`` branch, so a campaign whose channel is simply 'email'
    never entered it and sent with no ceiling at all on any day LinkedIn still
    had room. Email overflow is one case; it is not the only one.

    Non-email channels are never gated on the email budget.
    """
    if channel != CHANNEL_EMAIL:
        return True, ""
    from ..linkedin.rate_limiter import can_send_email_now

    return await can_send_email_now()

def _resolve_dm_provider_id(prospect: dict, prospect_data: dict) -> str:
    """The classic member id a DM can be addressed to, or "" if there is none.

    `send_new_message` addresses the member, so a public slug will not do —
    only an ACoAA… id. The miniProfileUrn carried in some stored LinkedIn URLs
    is the one other place the id hides (same fallback as send_followup.py).

    One function, called from two points: once before Step 3 so an
    unaddressable prospect costs no LLM spend, and once at the send itself.
    Kept shared rather than duplicated because the early check is only safe
    while it refuses exactly the rows the late one refuses.
    """
    provider_id = prospect_data.get("provider_id", "") or ""
    if provider_id.startswith("ACo"):
        return provider_id

    url = prospect.get("linkedin_url") or prospect_data.get("linkedin_url") or ""
    if url:
        from urllib.parse import parse_qs, unquote, urlparse

        qs = parse_qs(urlparse(url).query)
        urn = unquote(qs.get("miniProfileUrn", [""])[0])
        if urn:
            extracted = urn.split(":")[-1]
            if extracted.startswith("ACo"):
                return extracted
    return ""


# A local sdr-message count we could not establish. Distinct from 0 on purpose:
# 0 means "this outreach has sent nothing", which is the answer that FIRES the
# cross-campaign guard and marks a live outreach 'skipped'. A failed read must
# never be able to produce that answer.
LOCAL_SDR_COUNT_UNKNOWN = -1


def _count_local_sdr_messages_sql() -> str:
    """SQL for 'how many messages has THIS outreach sent'.

    Kept as a function so a test can run the real statement against a table
    shaped like production. The previous query selected `messages.external_id`,
    a column that does not exist — it is `external_message_id` — so it raised
    `no such column` on every call and a bare `except Exception: pass` swallowed
    the raise. Nothing downstream could tell a broken query from an empty result.
    """
    return "SELECT COUNT(*) FROM messages WHERE outreach_id = ? AND role = 'sdr'"


def _thread_has_only_foreign_sends(
    thread_sdr_msgs: list[dict], local_sdr_count: int,
) -> bool:
    """True when a LinkedIn thread carries sends of ours this outreach never made.

    The guard's question is "did another campaign — or a manual DM — already
    message this person". It used to try to answer that by comparing provider
    message ids, which could never work: `fetch_linkedin_history()` normalizes
    every message to exactly {role, text, timestamp} and discards the id, so the
    id side of the comparison was always empty and the branch was unreachable.
    That is why this guard has never once fired in actions_log.

    Ids are not needed and are not trustworthy here anyway — 699 of 847 sdr
    messages in production carry no external_message_id, so an id comparison
    would have marked our own sends foreign. The local message rows answer the
    question directly and completely: of 162 outreaches with followup_count > 0,
    all 162 have local sdr rows.

    An unknown local count stays quiet — see LOCAL_SDR_COUNT_UNKNOWN.
    """
    if local_sdr_count == LOCAL_SDR_COUNT_UNKNOWN:
        return False
    return bool(thread_sdr_msgs) and local_sdr_count == 0


async def run_generate_and_send(
    campaign_id: str = "",
    force_channel: str = "",
    target_outreach_id: str = "",
) -> str:
    """Generate and send (or queue) a personalized LinkedIn message.

    Flow:
    1. Find the active campaign and next pending prospect
    2. Generate a personalized message using voice signature
    3. Validate the message (5-stage pipeline)
    4. Copilot: show for review. Autopilot: send directly.

    Args:
        campaign_id: Campaign to send from.
        force_channel: Override channel choice ("email" forces email path).
        target_outreach_id: Specific outreach to send (for email overflow).
    """

    # ── Step 0: Pre-checks ──
    # Asked before anything else, including setup: a hosted account never
    # sends from this machine, so no other refusal is worth computing.
    from .organization import refuse_if_hosted_send

    blocked = await refuse_if_hosted_send()
    if blocked:
        return blocked

    setup_done = await adb.get_setting("setup_complete", False)
    if not setup_done:
        return (
            "❌ Setup required before sending messages.\n\n"
            "Please run setup_profile first — it connects your LinkedIn account and "
            "creates your voice signature so messages sound like you.\n\n"
            "Say 'set up my profile' and I'll walk you through it step by step."
        )

    account_id = await run_db(get_account_id)
    if not account_id:
        return "❌ No LinkedIn account connected. Run setup_profile first."

    try:
        client = get_linkedin_client()
    except UnipileError as e:
        return f"❌ {e}"

    # ── Ensure connections are synced at least once for accurate dedup ──
    # Budgeted for the same reason as the DM pre-check below: the scheduler
    # reaches run_generate_and_send through _execute_send_dm, _execute_invite
    # and _execute_email_invite, inside a tick cancelled whole at 55s, and an
    # empty connections table is exactly the state that makes ensure_synced
    # start an unbounded relations walk.
    try:
        from ..services.connection_sync import (
            DEDUP_SYNC_BUDGET_SECONDS,
            get_connection_count,
            ensure_synced,
        )
        if await run_db(get_connection_count, account_id) == 0:
            logger.info("No local connections cached — triggering initial sync for account %s", account_id)
            await ensure_synced(
                client, account_id, time_budget=DEDUP_SYNC_BUDGET_SECONDS,
            )
    except Exception as e:
        logger.warning("Initial connection sync failed (non-critical): %s", e)

    # ── Step 1: Find campaign + next prospect ──
    campaign, err = await adb.find_active_campaign(campaign_id)
    if not campaign:
        return f"❌ {err}"
    campaign_id = campaign["id"]

    from ..services.project_brief import refuse_without_project_brief
    missing = refuse_without_project_brief(campaign)
    if missing:
        return missing

    # Block sends when campaign is paused
    if campaign.get("status") == "paused":
        return (
            f"⏸️ Campaign '{campaign['name']}' is paused.\n\n"
            "No messages will be sent while the campaign is paused.\n"
            "Use resume_campaign() to resume outreach."
        )

    # Find next pending outreach (or specific target for email overflow)
    # For DM sends (connections-only campaigns), also pick 'connected' prospects
    # that haven't been messaged yet.  The reply checker may have detected existing
    # connections and set status to 'connected' before any DM was sent.
    dm_statuses = "('pending', 'connected')" if force_channel == "dm" else "('pending')"

    def _fetch_next_prospect(target_oid: str, cid: str) -> dict | None:
        from ..db.schema import get_db as _get_db
        _db = _get_db()
        if target_oid:
            _row = _db.execute(
                f"""SELECT o.id as outreach_id, o.contact_id, o.variant, o.signal_id,
                          o.next_action, o.status as outreach_status, o.accepted_at, o.invited_at,
                          c.id as contact_db_id, c.campaign_id as contact_campaign_id,
                          c.name, c.title, c.company, c.linkedin_url, c.linkedin_id,
                          c.profile_json, c.analysis_json, c.fit_score, c.source as contact_source, c.status as contact_status
                   FROM outreaches o
                   JOIN contacts c ON o.contact_id = c.id
                   WHERE o.id = ? AND o.status IN {dm_statuses}
                   LIMIT 1""",
                (target_oid,),
            ).fetchone()
        else:
            _row = _db.execute(
                f"""SELECT o.id as outreach_id, o.contact_id, o.variant, o.signal_id,
                          o.next_action, o.status as outreach_status, o.accepted_at, o.invited_at,
                          c.id as contact_db_id, c.campaign_id as contact_campaign_id,
                          c.name, c.title, c.company, c.linkedin_url, c.linkedin_id,
                          c.profile_json, c.analysis_json, c.fit_score, c.source as contact_source, c.status as contact_status
                   FROM outreaches o
                   JOIN contacts c ON o.contact_id = c.id
                   WHERE o.campaign_id = ? AND o.status IN {dm_statuses}
                   ORDER BY c.fit_score DESC
                   LIMIT 1""",
                (cid,),
            ).fetchone()
        _db.close()
        return dict(_row) if _row else None

    row = await run_db(_fetch_next_prospect, target_outreach_id, campaign_id)

    if not row:
        return (
            f"✅ All prospects in this campaign have been reached!\n\n"
            f"Campaign: {campaign['name']}\n"
            "Use show_status to see results, or create_campaign for a new target."
        )

    prospect = row
    outreach_id = prospect["outreach_id"]
    contact_id = prospect["contact_id"]

    from ..services.own_identity import refuse_own_account_target
    own_skip = await refuse_own_account_target(
        prospect, outreach_id=outreach_id, campaign_id=campaign_id,
    )
    if own_skip:
        return own_skip

    # ── Company profile gate: skip business pages ──
    from ..services.dedup_service import is_company_profile
    _prospect_for_check = {
        "name": prospect.get("name", ""),
        "linkedin_url": prospect.get("linkedin_url", ""),
        "title": prospect.get("title", ""),
        "company": prospect.get("company", ""),
    }
    if is_company_profile(_prospect_for_check):
        await adb.update_outreach(outreach_id, status="error",
                        last_attempt_error="Company/business profile — cannot message")
        await adb.log_action("company_profile_skipped", outreach_id=outreach_id,
                   result="skipped",
                   details={"prospect": prospect.get("name", ""), "reason": "company_page"})
        logger.info("Skipping company profile: %s", prospect.get("name", ""))
        return (
            f"⏭️ Skipped **{prospect.get('name', 'Unknown')}** — looks like a company/business page, "
            f"not a personal profile. Marked as error to prevent future retries."
        )

    # ── Exclusion gate: skip contacts excluded from automation ──
    from ..db.global_contact_queries import is_excluded_by_contact_id
    from ..ops_log import log_event, record_skip_excluded
    if await run_db(is_excluded_by_contact_id, contact_id):
        await record_skip_excluded(
            outreach_id=outreach_id, campaign_id=campaign_id, contact_id=contact_id,
        )
        return (
            f"⏭️ Skipped {prospect.get('name', 'Unknown')} — excluded from automation.\n\n"
            "This contact has the 'do-not-automate' tag or 'do_not_contact' lifecycle.\n"
            "Remove the tag with contacts(action='tag', tag='-do-not-automate') to re-enable."
        )

    # Operator path: pick the first-touch channel before claiming so an
    # InMail does not steal a scheduled invite job. force_channel=invite/dm/email
    # skips the picker.
    if not force_channel:
        from ..services.connection_sync import is_first_degree, is_first_degree_by_public_id
        from ..services.outreach_channel import choose_first_touch, first_touch_inmail_enabled
        from ..tier import get_caps
        try:
            pdata = json.loads(prospect.get("profile_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            pdata = {}
        if not isinstance(pdata, dict):
            pdata = {}
        provider_id = (pdata.get("provider_id") or "").strip()
        public_id = (prospect.get("linkedin_id") or pdata.get("public_id") or "").strip()
        first = False
        if provider_id and await run_db(is_first_degree, account_id, provider_id):
            first = True
        elif public_id and await run_db(is_first_degree_by_public_id, account_id, public_id):
            first = True
        try:
            camp_cfg = json.loads(campaign.get("config_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            camp_cfg = {}
        caps = await get_caps()
        inmail_on = first_touch_inmail_enabled(camp_cfg)
        # exclude_connections (9 Sep 2026): connected means skip, not
        # "DM instead of invite". Resolved per prospect — someone who accepted
        # this campaign's own invitation is 1st-degree but not pre-existing.
        from ..services.connection_sync import is_excluded_connection_outreach
        excluded = await is_excluded_connection_outreach(campaign, prospect)
        if inmail_on:
            picked = choose_first_touch(
                is_first_degree=first,
                can_send_credit_inmail=caps.can_send_credit_inmail,
                is_open_profile=bool(pdata.get("is_open_profile")),
                has_provider_id=bool(provider_id),
                exclude_first_degree=excluded,
            )
        else:
            picked = ("skip" if excluded else "dm") if first else "invite"
        log_event(
            "first_touch_channel_picked",
            outreach_id=outreach_id,
            campaign_id=campaign_id,
            channel=picked,
            source="choose_first_touch",
            predicates={
                "is_first_degree": first,
                "is_open_profile": bool(pdata.get("is_open_profile")),
                "has_provider_id": bool(provider_id),
                "inmail_enabled": inmail_on,
                "can_send_credit_inmail": caps.can_send_credit_inmail,
            },
        )
        if picked == "skip":
            from ..services.connection_sync import skip_excluded_connection
            return await skip_excluded_connection(
                outreach_id, campaign_id,
                prospect.get("name", ""), where="first_touch_picker",
            )
        if picked == "inmail":
            from .send_inmail import run_send_inmail
            return await run_send_inmail(
                campaign_id=campaign_id, outreach_id=outreach_id,
            )
        if picked == "dm":
            force_channel = "dm"
    else:
        log_event(
            "first_touch_channel_picked",
            outreach_id=outreach_id,
            campaign_id=campaign_id,
            channel=force_channel,
            source="forced",
        )

    # ── Optimistic lock: claim this outreach atomically ──
    # Prevents duplicate sends when multiple scheduler jobs target the same prospect.
    # CAS: only succeeds if status is still in the expected set (pending or connected for DMs).
    original_status, claimed = await run_db(try_claim_outreach, outreach_id, dm_statuses)
    if not claimed:
        logger.info("Outreach %s already claimed by another job, skipping", outreach_id)
        return "⏸️ Prospect already being processed by another job. Skipping."

    async def _release_claim() -> None:
        """Release the optimistic lock on failure — reset to original status."""
        try:
            def _do_release(oid: str, orig_st: str) -> None:
                from ..db.schema import get_db as _get_db_rel
                rel_db = _get_db_rel()
                rel_db.execute(
                    "UPDATE outreaches SET status = ?, updated_at = strftime('%s','now') WHERE id = ? AND status = 'sending'",
                    (orig_st, oid),
                )
                rel_db.commit()
                rel_db.close()
            await run_db(_do_release, outreach_id, original_status)
        except Exception as e:
            logger.warning("Failed to release outreach claim %s: %s", outreach_id, e)

    # ── Fit score gate: skip prospects below threshold ──
    try:
        cfg = json.loads(campaign.get("config_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        cfg = {}
    should_skip, prospect_fit, campaign_threshold = await run_db(
        should_fit_skip,
        prospect, cfg, outreach_id=outreach_id, outreach_status=original_status,
    )
    if should_skip:
        await _release_claim()
        await adb.update_outreach(
            outreach_id, status="skipped", last_attempt_error=FIT_SKIP_ERROR,
        )
        await adb.log_action(
            "fit_score_below_threshold",
            outreach_id=outreach_id,
            result="skipped",
            details={"fit_score": prospect_fit, "threshold": campaign_threshold},
        )
        return (
            f"⏭️ Skipped {prospect.get('name', 'Unknown')} — fit score {prospect_fit:.2f} "
            f"below threshold {campaign_threshold:.2f}."
        )

    # ── Targeting mismatch gate: skip a clear ICP miss before first send ──
    if (prospect.get("contact_source") or "") != "csv_import":
        try:
            icp_for_recheck = json.loads(campaign.get("icp_json") or "{}")
            verdict = await recheck_campaign_fit(
                prospect=prospect,
                campaign_context={
                    "target_description": cfg.get("target_description", ""),
                },
                icp_data=icp_for_recheck,
            )
            if is_clear_mismatch(verdict):
                await _release_claim()
                await adb.update_outreach(
                    outreach_id, status="skipped",
                    last_attempt_error=TARGETING_MISMATCH_ERROR,
                )
                await adb.log_action(
                    "targeting_mismatch_skipped",
                    outreach_id=outreach_id,
                    result="skipped",
                    details={
                        "reason": verdict.reason,
                        "confidence": verdict.confidence,
                        "source": verdict.source,
                    },
                )
                return (
                    f"⏭️ Skipped {prospect.get('name', 'Unknown')} — "
                    f"not a match for this campaign's targeting."
                )
        except Exception as e:
            logger.warning("First-touch targeting recheck failed (non-critical): %s", e)

    # ── Hard message cap: never send more than MAX messages to any single prospect ──
    MAX_SDR_MESSAGES_PER_PROSPECT = 3
    existing_sdr_count = 0
    try:
        def _get_sdr_count(oid: str) -> int:
            from ..db.queries import count_real_sdr_messages
            return count_real_sdr_messages(oid)
        existing_sdr_count = await run_db(_get_sdr_count, outreach_id)
    except Exception:
        pass
    if existing_sdr_count >= MAX_SDR_MESSAGES_PER_PROSPECT:
        await _release_claim()
        await adb.log_action(
            "message_cap_reached",
            outreach_id=outreach_id,
            result="skipped",
            details={"sent": existing_sdr_count, "limit": MAX_SDR_MESSAGES_PER_PROSPECT},
        )
        # Fix status if inconsistent
        if original_status in ("pending", "connected"):
            await adb.update_outreach(outreach_id, status="messaged")
        return (
            f"⏭️ Message cap reached for {prospect.get('name', 'Unknown')} "
            f"({existing_sdr_count}/{MAX_SDR_MESSAGES_PER_PROSPECT} messages sent)."
        )

    # ── Minimum message gap: at least 1 day between real chat DMs ──
    from ..scheduler.enqueue_gate import MIN_MESSAGE_GAP_SECONDS, message_gap_blocks
    if existing_sdr_count > 0:
        try:
            blocked, gap_details = await run_db(message_gap_blocks, outreach_id)
            if blocked:
                seconds_since = int(gap_details.get("seconds_since") or 0)
                hours_left = (MIN_MESSAGE_GAP_SECONDS - seconds_since) // 3600
                await _release_claim()
                await adb.log_action(
                    "message_gap_too_short",
                    outreach_id=outreach_id,
                    result="skipped",
                    details=gap_details,
                )
                return (
                    f"⏭️ Too soon to message {prospect.get('name', 'Unknown')} again "
                    f"(last message {seconds_since // 3600}h ago, minimum gap 24h). "
                    f"Retry in ~{hours_left}h."
                )
        except Exception as e:
            logger.debug("Message gap check failed (non-critical): %s", e)

    # ── Channel selection ──
    from ..linkedin.rate_limiter import BLOCK_DAILY, BLOCK_WEEKLY
    _, _, block_type = await can_send_now(reserve=False)
    linkedin_limit_hit = block_type in (BLOCK_DAILY, BLOCK_WEEKLY)

    outreach_data = {"channel": "linkedin"}  # Default
    channel = select_channel(
        outreach=outreach_data,
        prospect=prospect,
        campaign_config=json.loads(campaign.get("config_json") or "{}"),
        force_channel=force_channel,
        linkedin_limit_hit=linkedin_limit_hit,
    )

    # ── Step 1.5: Warm-up check (skip for DM channel — existing connections) ──
    eng_count = await adb.get_engagement_count_for_outreach(outreach_id)

    if eng_count < MIN_WARMUP_ENGAGEMENTS and channel != "dm":
        # Skip un-warmed prospects, find a warmed-up one instead
        def _fetch_warmed(cid: str, min_eng: int) -> dict | None:
            from ..db.schema import get_db as _get_db
            db2 = _get_db()
            _row = db2.execute(
                """SELECT o.id as outreach_id, o.contact_id, o.variant, o.signal_id,
                          o.next_action,
                          c.id as contact_db_id, c.campaign_id as contact_campaign_id,
                          c.name, c.title, c.company, c.linkedin_url, c.linkedin_id,
                          c.profile_json, c.analysis_json, c.fit_score, c.source as contact_source, c.status as contact_status
                   FROM outreaches o
                   JOIN contacts c ON o.contact_id = c.id
                   WHERE o.campaign_id = ? AND o.status = 'pending'
                     AND (SELECT COUNT(*) FROM engagements e WHERE e.outreach_id = o.id) >= ?
                   ORDER BY c.fit_score DESC
                   LIMIT 1""",
                (cid, min_eng),
            ).fetchone()
            db2.close()
            return dict(_row) if _row else None

        warmed_row = await run_db(_fetch_warmed, campaign_id, MIN_WARMUP_ENGAGEMENTS)

        if warmed_row:
            prospect = warmed_row
            outreach_id = prospect["outreach_id"]
            contact_id = prospect["contact_id"]
            eng_count = MIN_WARMUP_ENGAGEMENTS  # Known to be warmed
        else:
            await _release_claim()
            return (
                f"⏸️ No warmed-up prospects in '{campaign['name']}'.\n\n"
                f"All pending prospects need engagement warm-up first.\n"
                f"Run engage_prospect() to warm them up, then try again.\n"
                f"Check warm-up status or skip to send anyway."
            )

    # ── Step 2: Check rate limits ──
    can_send, reason, _block = await can_send_now()
    # Evaluated for every email send, whatever LinkedIn's state — see
    # _resolve_email_gate. LinkedIn having room says nothing about the email
    # budget, and a campaign on the email channel never enters the overflow
    # branch below.
    can_email, email_reason = await _resolve_email_gate(channel, can_send)
    if channel == CHANNEL_EMAIL and not can_email:
        await _release_claim()
        return f"⏸️ Email sending paused: {email_reason}\n\nThe queue is ready — will resume automatically."
    if not can_send:
        if channel == CHANNEL_EMAIL:
            pass  # LinkedIn limits do not block email; the gate above decided.
        else:
            await _release_claim()
            return f"⏸️ Sending paused: {reason}\n\nThe queue is ready — will resume automatically."

    # ── Step 2.5: Check LinkedIn pending invitation count ──
    if channel != CHANNEL_EMAIL and channel != "dm":
        from ..linkedin.rate_limiter import (
            check_pending_limit,
            invalidate_pending_cache,
            withdraw_oldest_to_free_spot,
        )
        can_send_pending, pending_reason, pending_block = await check_pending_limit(client, account_id)
        if not can_send_pending:
            # Try to free a spot by withdrawing the oldest invitation
            withdrawal = await withdraw_oldest_to_free_spot(client, account_id)
            if withdrawal.get("success"):
                await adb.log_action(
                    "pending_limit_auto_withdraw",
                    outreach_id=outreach_id,
                    result="success",
                    details={
                        "withdrawn_id": withdrawal["invitation_id"],
                        "days_old": withdrawal["days_old"],
                        "name": withdrawal.get("name", ""),
                    },
                )
                # Re-check after withdrawal
                can_send_pending, pending_reason, _ = await check_pending_limit(client, account_id)

            if not can_send_pending:
                await _release_claim()
                await adb.log_action(
                    "invitation_blocked_pending_limit",
                    outreach_id=outreach_id,
                    result="blocked",
                    details={"reason": pending_reason},
                )
                return (
                    f"⏸️ LinkedIn pending invitation limit reached.\n\n"
                    f"{pending_reason}\n"
                    "Invitations already sent are pending acceptance. "
                    "Will retry when pending count drops."
                )

    # Check free tier monthly limit. Hosted / Pro use LinkedIn daily
    # safety caps (premium 50/day, confirmed-free 15/day) instead.
    if apply_free_monthly_caps():
        usage = await adb.get_monthly_usage()
        if usage.get("invitations_sent", 0) >= FREE_MONTHLY_INVITATIONS:
            await _release_claim()
            return (
                f"⚠️ Free tier limit reached: {FREE_MONTHLY_INVITATIONS} invitations/month.\n\n"
                "Upgrade to Pro ($29/mo) for unlimited outreach.\n"
                "Your campaign will resume next month if you stay on Free."
            )

    # ── Step 3: Generate message ──
    from ..services.own_profile_sync import refresh_own_profile_if_stale
    sender_profile = await refresh_own_profile_if_stale()
    if not sender_profile:
        sender_profile = await adb.get_setting("profile", {})
    voice_signature = await adb.get_setting("voice_signature", {})
    campaign_config = json.loads(campaign.get("config_json") or "{}")
    icp_data = json.loads(campaign.get("icp_json") or "{}")

    campaign_context = _build_campaign_context(campaign_config, icp_data)
    from ..ai.intent import resolve_intent
    campaign_intent = resolve_intent(campaign_config)

    # Load campaign context (offerings, case_studies, social_proofs, preferences)
    campaign_ctx = await adb.get_campaign_context(campaign_id)

    from ..linkedin.profile_normalize import merge_prospect_profile, prospect_data_from_contact

    prospect_data = prospect_data_from_contact(prospect)

    # ── Invitation-target guard: resolve the provider_id BEFORE any LLM spend ──
    # The invitation endpoint accepts a classic member id (ACoAA…) or a public
    # slug. Bare numerics, SN-space ids ("ACw…"), and masked SN display names
    # ("Recruiter at JPMorganChase" via linkedin_id) all draw 400 "User ID does
    # not match provider's expected format" — and this guard used to sit after
    # Step 3, burning a prospect analysis plus the full generate→improve→
    # validate pipeline on every retry of a row that could only fail.
    #
    # The DM channel was exempted from this guard when it was introduced, and
    # kept its own copy at the send itself — i.e. exactly the placement the
    # paragraph above describes as the bug. Production outreach 365b2300 paid
    # for two full generations on 2026-08-19 (message_brief 09:39:25 → error
    # 09:39:46, again 10:53:20 → 10:53:44) for an identifier that could not
    # have worked either time. Nothing between here and the send can supply the
    # missing id — prospect_data gains only company_data, mutual_connections,
    # hiring_intent and hiring_jobs — so refusing here refuses the same rows,
    # only sooner.
    provider_id = ""
    if channel == "dm":
        provider_id = _resolve_dm_provider_id(prospect, prospect_data)
        if not provider_id:
            slug = prospect.get("linkedin_id", "") or prospect_data.get("public_id", "")
            logger.warning(
                "No valid provider_id (ACoAAA) for %s — only have slug '%s'. Cannot send DM.",
                prospect.get("name", "Unknown"), slug,
            )
            await adb.update_outreach(
                outreach_id, status="error",
                last_attempt_error=f"No valid provider_id for DM (only slug: {slug})",
            )
            await client.close()
            return (
                f"❌ No valid provider_id for {prospect.get('name', 'Unknown')}. "
                "Need ACoAAA format for DMs."
            )
    elif channel != CHANNEL_EMAIL:
        provider_id = (
            prospect_data.get("provider_id", "")
            or prospect.get("linkedin_id", "")
            or prospect_data.get("public_id", "")
        )
        if not provider_id:
            await adb.update_outreach(outreach_id, status="error")
            await client.close()
            return f"❌ No LinkedIn ID for {prospect.get('name', 'Unknown')}. Skipping."

        if not is_sendable_invite_id(provider_id):
            # Try to extract a usable id from profile enrichment
            for _fallback in (prospect_data.get("provider_id", ""),
                              prospect_data.get("public_id", "")):
                if _fallback and is_sendable_invite_id(_fallback):
                    provider_id = _fallback
                    break
            else:
                await adb.update_outreach(outreach_id, status="error",
                                last_attempt_error=f"Invalid provider_id format: {provider_id}")
                await adb.log_action("invitation_bad_id", outreach_id=outreach_id,
                           result="error",
                           details={"provider_id": provider_id,
                                    "prospect": prospect.get("name", "Unknown")})
                await client.close()
                return (
                    f"❌ Invalid LinkedIn ID format for {prospect.get('name', 'Unknown')} "
                    f"({provider_id}). Needs ACoAAA or slug format."
                )

    # ── Company Enrichment: fetch company profile for better personalization ──
    company_name = prospect_data.get("company") or prospect.get("company", "")
    if company_name and not prospect_data.get("company_data"):
        try:
            company_data = await client.get_company_profile(account_id, company_name)
            if company_data and company_data.get("name"):
                prospect_data["company_data"] = company_data
                logger.info(f"Enriched company data for {company_name}: {company_data.get('industry', 'unknown industry')}")
        except Exception as e:
            logger.debug(f"Company enrichment failed for {company_name} (non-critical): {e}")

    # ── Pre-send connection check ──
    # For DMs: use LOCAL connections DB as the definitive gate (not Unipile API).
    # The Unipile profile API can return stale/wrong is_relationship data,
    # causing 403 subscription_required when we DM a non-connection.
    mutual_info = ""
    _profile_for_enrichment: dict = {}
    _dm_connection_verified = False  # Fail closed: must be explicitly set True

    if channel == "dm":
        from ..services.connection_sync import (
            PRECHECK_SYNC_BUDGET_SECONDS,
            is_first_degree,
            is_first_degree_by_public_id,
            should_run_precheck_sync,
            sync_connections,
            get_sync_age,
        )
        dm_prov_id = (prospect_data.get("provider_id") or "").strip()
        dm_pub_id = (prospect_data.get("public_id") or prospect.get("linkedin_id", "")).strip()

        # Re-sync connections if older than 30 min to catch recent accepts.
        # Budgeted: the scheduler reaches this through _execute_send_dm,
        # _execute_invite and _execute_email_invite, inside a tick cancelled
        # whole at 55s, and an unbounded relations walk is up to 200 pages
        # (get_relations pages at min(limit, 100) against max_relations=20000)
        # at a 30s HTTP timeout each.
        try:
            sync_age = await run_db(get_sync_age, account_id)
            if (sync_age is None or sync_age > 1800) and should_run_precheck_sync():
                await sync_connections(
                    client, account_id, time_budget=PRECHECK_SYNC_BUDGET_SECONDS,
                )
        except Exception as e:
            logger.warning("Connection re-sync before DM failed: %s", e)

        # Check local DB — this is the definitive 1st-degree source
        if dm_prov_id and await run_db(is_first_degree, account_id, dm_prov_id):
            _dm_connection_verified = True
        elif dm_pub_id and await run_db(is_first_degree_by_public_id, account_id, dm_pub_id):
            _dm_connection_verified = True

        if not _dm_connection_verified:
            from ..services.dm_connection_guard import verify_dm_eligible
            guard = await verify_dm_eligible(
                account_id,
                prospect,
                {
                    "id": outreach_id,
                    "status": prospect.get("outreach_status") or original_status,
                    "accepted_at": prospect.get("accepted_at"),
                    "name": prospect.get("name", ""),
                },
                client=client,
                profile=prospect_data,
            )
            if guard.allowed:
                _dm_connection_verified = True
            else:
                result = "deferred" if guard.outcome == "defer" else "error"
                if guard.outcome == "error":
                    await adb.update_outreach(
                        outreach_id, status="error",
                        last_attempt_error=guard.message,
                    )
                await adb.log_action(
                    "dm_not_connected", outreach_id=outreach_id,
                    result=result,
                    details={"prospect": prospect.get("name", ""),
                             "provider_id": guard.provider_id,
                             "public_id": guard.public_id,
                             "outcome": guard.outcome},
                )
                logger.warning(
                    "Blocking DM to %s — %s",
                    prospect.get("name", ""), guard.message,
                )
                return (
                    f"⏭️ Skipped {prospect.get('name', 'prospect')} — "
                    f"{guard.message}"
                )

    # Profile enrichment + connection detection for invitation path
    _profile_enrich_error = ""
    try:
        linkedin_id = prospect.get("linkedin_id") or prospect_data.get("public_id") or ""
        if linkedin_id:
            _profile_for_enrichment = await client.get_profile(account_id, linkedin_id)
            if isinstance(_profile_for_enrichment, dict) and _profile_for_enrichment:
                is_rel = _profile_for_enrichment.get("is_relationship", False)
                net_dist = _profile_for_enrichment.get("network_distance", "")
                is_connected = is_rel or net_dist == "FIRST_DEGREE"

                if channel != "dm" and is_connected:
                    from ..services.connection_sync import mark_connected
                    prov_id = (
                        prospect_data.get("provider_id", "")
                        or prospect.get("linkedin_id", "")
                    )
                    await run_db(mark_connected, account_id, prov_id, prospect.get("name", ""), linkedin_id)
                    await adb.update_outreach(outreach_id, status="connected", channel=CHANNEL_LINKEDIN)
                    await adb.log_action("pre_send_already_connected", outreach_id=outreach_id,
                               result="connected",
                               details={"prospect": prospect.get("name", ""),
                                        "network_distance": net_dist})
                    return (
                        f"Already connected with {prospect.get('name', 'prospect')} "
                        f"(detected via profile) — skipping invitation, queued for follow-up DM."
                    )
    except Exception as e:
        logger.warning("Profile connection check failed for %s: %s", prospect.get("name", ""), e)
        _profile_enrich_error = str(e)[:200]
    else:
        _profile_enrich_error = ""

    prospect_data = merge_prospect_profile(prospect_data, prospect, _profile_for_enrichment)
    contact_db_id = prospect.get("contact_db_id") or contact_id
    if contact_db_id:
        try:
            persist = {"profile_json": json.dumps(prospect_data)}
            if prospect_data.get("title"):
                persist["title"] = prospect_data["title"]
            if prospect_data.get("company"):
                persist["company"] = prospect_data["company"]
            await run_db(update_contact, contact_db_id, **persist)
        except Exception:
            logger.debug("Persisting merged profile failed", exc_info=True)

    # ── Mutual Connections: check for warm intro paths ──
    try:
        if isinstance(_profile_for_enrichment, dict) and _profile_for_enrichment:
            shared = _profile_for_enrichment.get("shared_connections") or _profile_for_enrichment.get("mutual_connections") or 0
            if shared and int(shared) > 0:
                mutual_info = f"You share {shared} mutual connection(s) with this prospect."
                prospect_data["mutual_connections"] = int(shared)
                logger.info("Warm intro path: %d mutual connections with %s", shared, prospect.get("name", ""))
    except Exception as e:
        logger.debug("Mutual connection check failed (non-critical): %s", e)

    # ── Job Intent Signals: check if prospect's company is hiring ──
    try:
        prospect_co = prospect_data.get("company") or prospect.get("company", "")
        campaign_target = campaign_context.get("target_description", "")
        if prospect_co and campaign_target:
            from ..services.intent_signals import search_job_intent, build_intent_context
            intent_companies = await search_job_intent(
                client, account_id, campaign_target, limit=10,
            )
            # Find if prospect's company is among hiring companies
            co_lower = prospect_co.lower().strip()
            for ic in intent_companies:
                if ic["company_name"].lower().strip() == co_lower:
                    prospect_data["hiring_intent"] = ic["intent_signal"]
                    prospect_data["hiring_jobs"] = ic["jobs"][:3]
                    logger.info("Hiring intent found for %s: %s", prospect_co, ic["intent_signal"])
                    break
    except Exception as e:
        logger.debug("Job intent check failed (non-critical): %s", e)

    # ── Prospect Intelligence: analyze or load cached ──
    prospect_analysis = None
    contact_db_id = prospect.get("contact_db_id") or contact_id
    cached = await adb.get_contact_analysis(contact_db_id)
    if (
        cached
        and is_data_gap_analysis(cached)
        and (prospect_data.get("title") or prospect_data.get("company"))
    ):
        logger.info(
            "Ignoring data-gap analysis cache for %s — profile now has title/company",
            prospect.get("name", "Unknown"),
        )
        cached = None
    if cached:
        prospect_analysis = cached
        logger.info(f"Loaded cached prospect analysis for {prospect.get('name', 'Unknown')}")
    else:
        try:
            prospect_analysis = await analyze_prospect(
                prospect=prospect_data,
                campaign_context=campaign_context,
                icp_data=icp_data,
            )
            await adb.save_contact_analysis(contact_db_id, prospect_analysis)
            logger.info(f"Generated and cached prospect analysis for {prospect.get('name', 'Unknown')}")
        except Exception as e:
            logger.warning(f"Prospect analysis failed, proceeding without: {e}")

    analysis_source = (
        "cache" if cached else ("live" if prospect_analysis else "none")
    )
    profile_live = bool(_profile_for_enrichment)
    from ..ops_log import present_fields
    field_src: dict = {}
    if isinstance(prospect, dict):
        field_src.update({k: prospect.get(k) for k in ("name", "title", "company")})
    if isinstance(prospect_data, dict):
        field_src.update(prospect_data)
    if isinstance(_profile_for_enrichment, dict):
        field_src.update(_profile_for_enrichment)
    log_event(
        "enrichment_resolved",
        outreach_id=outreach_id,
        campaign_id=campaign_id,
        source=analysis_source if prospect_analysis or cached else (
            "live" if profile_live else "none"
        ),
        skip_reason=_profile_enrich_error or None,
        predicates={"present": present_fields(field_src)},
    )

    # The cached signal_context reflects whichever signal last scored highest,
    # not necessarily the one that created this outreach — rebuild it from the
    # outreach's trigger signal so the message references the right post.
    try:
        trigger_signal_id = (prospect.get("signal_id") or "").strip()
        if trigger_signal_id:
            from ..db.signal_queries import get_signal
            from ..services.signal_activator import apply_trigger_signal_context

            trigger_signal = await run_db(get_signal, trigger_signal_id)
            if trigger_signal:
                prospect_analysis = apply_trigger_signal_context(
                    prospect_analysis, trigger_signal
                )
    except Exception as e:
        logger.debug("Trigger signal context rebuild failed (non-critical): %s", e)

    # ── A/B Test Variant: inject variant instructions if test is running ──
    outreach_variant = prospect.get("variant") if prospect else None
    copy_provenance.note_variant("")  # only an applied instruction is an arm
    if outreach_variant and campaign_id:
        try:
            running_tests = await adb.list_ab_tests(campaign_id, status="running")
            running_tests = [
                t for t in running_tests
                if (t.get("test_type") or "message") != "headline"
            ]
            if running_tests:
                test = running_tests[0]
                variant_desc = test["variant_a"] if outreach_variant == "A" else test["variant_b"]
                variant_instruction = (
                    f"\n\nA/B TEST ACTIVE — This prospect is in Variant {outreach_variant}.\n"
                    f"Messaging instruction for this variant: {variant_desc}\n"
                    f"Follow this instruction precisely for controlled testing."
                )
                campaign_ctx = campaign_ctx or {}
                existing_prefs = campaign_ctx.get("campaign_preferences", "")
                campaign_ctx["campaign_preferences"] = (existing_prefs + variant_instruction).strip()
                copy_provenance.note_variant(outreach_variant)
                logger.info("Applied A/B variant %s instruction for outreach", outreach_variant)
        except Exception as e:
            logger.debug("A/B variant injection failed (non-critical): %s", e)

    # ── Pre-outreach conversation history ──
    # Check for ANY existing conversation with this prospect — not just DM campaigns.
    # This prevents spamming people who already messaged us (inbound) or who we
    # already have a thread with from a different campaign.
    conversation_history: list[dict] | None = None
    conv_lookup_failed = ""
    try:
        conv_provider_id = (
            prospect_data.get("provider_id", "")
            or prospect.get("linkedin_id", "")
            or prospect_data.get("public_id", "")
        )
        if conv_provider_id:
            stored_chat_id = ""
            try:
                from ..db.queries import get_outreach
                stored_row = await run_db(get_outreach, outreach_id)
                stored_chat_id = ((stored_row or {}).get("chat_id") or "").strip()
            except Exception:
                stored_chat_id = ""
            conv_chat_id = await client.find_chat_for_user(
                account_id, conv_provider_id, raise_on_error=True,
                preferred_chat_id=stored_chat_id or None,
            )
            if conv_chat_id:
                if conv_chat_id != stored_chat_id:
                    await adb.update_outreach(outreach_id, chat_id=conv_chat_id)
                conv_sender_id = sender_profile.get("provider_id", "")
                if conv_sender_id:
                    from ..services.conversation_enricher import fetch_linkedin_history
                    conversation_history = await fetch_linkedin_history(
                        client, account_id, conv_chat_id, conv_sender_id, limit=15,
                    )
                    if conversation_history:
                        logger.info(
                            "Found %d prior messages with %s for outreach",
                            len(conversation_history), prospect.get("name", "Unknown"),
                        )
    except Exception as e:
        # Every failure here is fatal to the question this guard answers, not
        # "non-critical" as it read before. An unreachable API, a 429, an
        # unreadable body — and, once a chat *has* been found, a history fetch
        # that then fails — all leave us unable to say whether a conversation
        # with this person is already running. Carrying on with
        # conversation_history=None answers "there is none", which is how a
        # cold opener lands in a live thread. Defer instead: the claim is
        # released below and the row keeps its status, so the scheduler brings
        # it back when the API is answering again.
        conv_lookup_failed = str(e) or e.__class__.__name__
        if getattr(getattr(e, "response", None), "status_code", None) == 404:
            # The chat found above is gone. Defer like any other failure, and
            # forget it: find_chat_for_user returns a stored chat id without
            # scanning, so a dead one kept here would defer this row on every
            # pass. The next pass scans for the chat the person has now.
            conv_lookup_failed = "the LinkedIn chat we had on file no longer exists (404)"
            try:
                await adb.update_outreach(outreach_id, chat_id=None)
            except Exception as clear_err:
                logger.debug("Could not forget the gone chat id: %s", clear_err)
        logger.warning(
            "Pre-outreach chat lookup failed for %s — deferring rather than "
            "assuming no conversation: %s", prospect.get("name", "Unknown"), e,
        )

    if conv_lookup_failed:
        await _release_claim()
        await adb.log_action(
            "send_deferred_chat_lookup_failed",
            outreach_id=outreach_id,
            result="deferred",
            details={"error": conv_lookup_failed[:200]},
        )
        return (
            f"⏸️ Deferred {prospect.get('name', 'Unknown')} — could not check "
            f"for an existing conversation ({conv_lookup_failed[:120]}).\n\n"
            "Nothing was sent. Will retry automatically."
        )

    # ── Pre-send safety: abort if ANY prior conversation exists with prospect ──
    # Two checks:
    # 1. If prospect ever messaged us (inbound) → skip entirely (they initiated contact)
    # 2. If we already sent messages outside this outreach → skip (cross-campaign overlap)
    if conversation_history:
        prospect_msgs = [m for m in conversation_history if m.get("role") == "prospect"]
        sdr_msgs = [m for m in conversation_history if m.get("role") == "sdr"]
        local_sdr_count = LOCAL_SDR_COUNT_UNKNOWN
        try:
            def _count_local_sdr(oid: str) -> int:
                from ..db.schema import get_db as _get_db_conv
                _conv_db = _get_db_conv()
                _row = _conv_db.execute(
                    _count_local_sdr_messages_sql(), (oid,),
                ).fetchone()
                _conv_db.close()
                return int(_row[0]) if _row else 0
            local_sdr_count = await run_db(_count_local_sdr, outreach_id)
        except Exception as e:
            # Leave the count UNKNOWN so the guard stays quiet. This used to
            # swallow `no such column: external_id` in silence on every call.
            logger.warning(
                "Cross-campaign guard: local sent-message count failed for %s: %s",
                outreach_id, e,
            )

        if prospect_msgs:
            # Prospect messaged us at some point — this is an existing conversation
            last_prospect_text = prospect_msgs[-1].get("text", "")[:100]
            await _release_claim()
            await adb.log_action(
                "dm_skipped_existing_conversation",
                outreach_id=outreach_id,
                result="skipped",
                details={
                    "prospect_messages": len(prospect_msgs),
                    "sdr_messages": len(sdr_msgs),
                    "last_prospect_msg": last_prospect_text,
                },
            )
            if original_status in ("pending", "connected"):
                await adb.update_outreach(outreach_id, status="replied")
            return (
                f"⏭️ Skipped {prospect.get('name', 'Unknown')} — "
                f"existing conversation detected ({len(prospect_msgs)} inbound message(s)).\n"
                f"   Last from them: \"{last_prospect_text}\"\n"
                f"Use send_message(action='reply') to respond, or prospect(action='skip') to remove."
            )
        elif _thread_has_only_foreign_sends(sdr_msgs, local_sdr_count):
            # We sent messages from another campaign/manually — don't pile on
            await _release_claim()
            await adb.log_action(
                "dm_skipped_cross_campaign_thread",
                outreach_id=outreach_id,
                result="skipped",
                details={
                    "foreign_sdr_messages": len(sdr_msgs),
                    "last_msg": sdr_msgs[-1].get("text", "")[:100],
                },
            )
            await adb.update_outreach(outreach_id, status="skipped")
            return (
                f"⏭️ Skipped {prospect.get('name', 'Unknown')} — "
                f"already messaged in another thread ({len(sdr_msgs)} prior message(s))."
            )

    # ── Generate → Improve → Validate → Fix pipeline ──
    message = ""
    reasoning = ""
    validation = None
    from ..tier import get_caps
    note_max = (await get_caps()).invite_note_max_chars

    # Build the message brief — WHAT this message must say (ai/brief_builder)
    from ..ai.brief_builder import build_message_brief
    message_brief = build_message_brief(
        intent=campaign_intent,
        campaign_config=campaign_config,
        campaign_ctx=campaign_ctx,
        prospect=prospect_data,
        icp_data=icp_data,
        analysis=prospect_analysis,
        touch="invite",
    )

    from ..services.action_timeline import action_timeline_text
    action_timeline = await action_timeline_text(outreach_id)

    # Attempt 1: Generate + Improve + Validate (+ Fix if needed)
    try:
        result = await generate_message(
            prospect=prospect_data,
            sender_profile=sender_profile,
            voice_signature=voice_signature,
            campaign_context=campaign_context,
            prospect_analysis=prospect_analysis,
            campaign_ctx=campaign_ctx,
            conversation_history=conversation_history,
            brief=message_brief,
            max_chars=note_max,
            action_timeline=action_timeline,
        )
        message = result["message"]
        reasoning = result.get("reasoning", "")
    except Exception as e:
        logger.error(f"Message generation failed: {e}")
        await _release_claim()
        return f"❌ Failed to generate message: {e}"

    # The job-search writer refused its own draft twice (api #1416). Held,
    # not handed to Fix: Fix would write a message from nothing.
    if result.get("held"):
        await adb.log_action("validation_blocked", outreach_id=outreach_id, result="blocked",
                   details={"issues": [result["held"]]})
        await _release_claim()
        return (
            f"⚠️ Message for {prospect.get('name', 'Unknown')} was held: "
            f"{result['held']}\n\nThe message was NOT sent."
        )

    def _validate(text: str):
        """validate_message, plus the job-search presumption check (api #1416)."""
        checked = validate_message(text, voice_signature, note_max)
        check_job_search_draft(
            checked, text, prospect=prospect_data, campaign_config=campaign_context,
            campaign_ctx=campaign_ctx, analysis=prospect_analysis,
        )
        return checked

    # Log reasoning for debugging/quality analysis
    if reasoning:
        await adb.log_action("message_reasoning", outreach_id=outreach_id,
                   details={"reasoning": reasoning[:500]})
    from dataclasses import asdict as _asdict
    await adb.log_action("message_brief", outreach_id=outreach_id,
               details=_asdict(message_brief))

    # The model sometimes writes a skip rationale as the "message". Improve
    # and Fix would turn that into a sendable buyer ask to the wrong person.
    if is_evaluator_refusal(message):
        await _release_claim()
        await adb.update_outreach(outreach_id, status="skipped")
        await adb.log_action(
            "evaluator_refusal_skipped",
            outreach_id=outreach_id,
            result="skipped",
            details={"draft": message[:300]},
        )
        return (
            f"⏭️ Skipped {prospect.get('name', 'Unknown')} — model refused the "
            f"brief (not an ICP match). The refusal was not sent."
        )

    # Improve stage — polish for naturalness
    try:
        message = await improve_message(
            draft=message,
            voice_signature=voice_signature,
            message_type="invitation",
            max_chars=note_max,
            intent=campaign_intent,
            brief=message_brief,
        )
    except Exception as e:
        logger.warning(f"Improve stage failed, using raw message: {e}")

    # Validate (rule-based)
    validation = _validate(message)

    # LLM validation — context-sensitive checks (guardrails, company names, etc.)
    if validation.is_valid:
        try:
            sender_company = sender_profile.get("company", "")
            prospect_co = prospect_data.get("company", prospect.get("company", ""))
            llm_result = await llm_validate(
                message=message,
                history=[],
                company=sender_company,
                message_type="invitation",
                prospect_company=prospect_co,
                max_chars=note_max,
                intent=campaign_intent,
            )
            if not llm_result.is_valid:
                validation.issues.extend(llm_result.issues)
                validation.is_valid = False
                logger.info("LLM validation caught invitation issues: %s", llm_result.issues)
        except Exception as e:
            logger.warning(f"LLM validation skipped: {e}")

    # Fix stage — if validation failed, surgically fix issues
    if not validation.is_valid:
        logger.info(f"Validation failed, attempting fix: {validation.issues}")
        try:
            message = await fix_message(
                message=message,
                issues=validation.issues,
                voice_signature=voice_signature,
                message_type="invitation",
                max_chars=note_max,
                intent=campaign_intent,
                brief=message_brief,
            )
            validation = _validate(message)
        except Exception as e:
            logger.warning(f"Fix stage failed: {e}")

    # Last resort: regenerate from scratch if still invalid
    if not validation.is_valid:
        logger.info(f"Fix failed, regenerating from scratch: {validation.issues}")
        try:
            result = await generate_message(
                prospect=prospect_data,
                sender_profile=sender_profile,
                voice_signature=voice_signature,
                campaign_context=campaign_context,
                prospect_analysis=prospect_analysis,
                campaign_ctx=campaign_ctx,
                conversation_history=conversation_history,
                max_chars=note_max,
            )
            message = result["message"]
            message = await improve_message(
                draft=message,
                voice_signature=voice_signature,
                message_type="invitation",
                max_chars=note_max,
                intent=campaign_intent,
                brief=message_brief,
            )
            validation = _validate(message)
        except Exception as e:
            logger.error(f"Regeneration failed: {e}")

    if not validation or not validation.is_valid:
        issues_text = "\n".join(f"  ⚠️ {issue}" for issue in (validation.issues if validation else []))
        # Never send a message that failed validation.
        logger.warning(f"Blocked invalid message: {issues_text}")
        await adb.log_action("validation_blocked", outreach_id=outreach_id, result="blocked",
                   details={"issues": validation.issues if validation else []})
        await _release_claim()
        return (
            f"⚠️ Message for {prospect.get('name', 'Unknown')} failed validation "
            f"after Generate → Improve → Fix pipeline.\n\n"
            f"Issues:\n{issues_text}\n\n"
            "The message was NOT sent to protect your account.\n"
            "Check message validation errors and try again."
        )

    # ── Step 4: Copilot vs Autopilot ──
    prospect_name = prospect.get("name", "Unknown")
    prospect_title = prospect.get("title", "")
    prospect_company = prospect.get("company", "")
    prospect_url = prospect.get("linkedin_url", "")
    fit_score = prospect.get("fit_score", 0)

    role_str = prospect_title
    if prospect_company:
        role_str += f" at {prospect_company}" if role_str else prospect_company

    # Send immediately
    if not _send_gate_open(channel, can_send, can_email):
        await adb.update_outreach(outreach_id, status="pending", next_action=message)
        return f"⏸️ Queued for later: {reason}"

    # ── EMAIL CHANNEL PATH ──
    if channel == CHANNEL_EMAIL:
        return await _send_email_outreach(
            client, account_id, outreach_id, prospect, prospect_data,
            prospect_name, role_str, message, voice_signature,
            campaign_context, campaign_ctx, prospect_analysis,
            release_claim=_release_claim,
        )

    # ── DM CHANNEL PATH (connections-only campaigns) ──
    if channel == "dm":
        # Already resolved before Step 3; re-resolved here so the send reads
        # the id from one place. Reaching the refusal below now means the
        # early guard let something through, not routine spend.
        provider_id = _resolve_dm_provider_id(prospect, prospect_data)
        if not provider_id:
            slug = prospect.get("linkedin_id", "") or prospect_data.get("public_id", "")
            logger.warning(
                "No valid provider_id (ACoAAA) for %s — only have slug '%s'. Cannot send DM.",
                prospect_name, slug,
            )
            await adb.update_outreach(outreach_id, status="error",
                            last_attempt_error=f"No valid provider_id for DM (only slug: {slug})")
            await client.close()
            return f"❌ No valid provider_id for {prospect_name}. Need ACoAAA format for DMs."

        try:
            from ..ops_log import log_outbound_send
            log_outbound_send(
                "attempt",
                outreach_id=outreach_id,
                campaign_id=campaign_id,
                channel="dm",
                step_index="dm",
                text=message,
                provider_id=provider_id,
            )
            result = await client.send_new_message(
                account_id=account_id,
                provider_id=provider_id,
                text=message,
            )
            if result.get("chat_id"):
                await adb.update_outreach(outreach_id, chat_id=result["chat_id"])
            log_outbound_send(
                "result",
                outreach_id=outreach_id,
                campaign_id=campaign_id,
                channel="dm",
                step_index="dm",
                text=message,
                provider_id=provider_id,
                success=bool(result.get("success")),
                unipile_id=result.get("chat_id") or "",
                error_type=(result.get("error") or "")[:80] or None,
                verified=None,
            )
            if result.get("success"):
                # Inline read-back verification: confirm the DM actually
                # appeared in the conversation before updating status.
                dm_chat_id = result.get("chat_id", "")
                dm_verified = True  # default to trusted if no chat_id
                if dm_chat_id:
                    try:
                        dm_verified = await client.verify_message_sent(
                            account_id, dm_chat_id, message,
                        )
                    except Exception as e:
                        logger.warning("DM verification error for %s: %s", prospect_name, e)
                        dm_verified = True  # fail open — trust API response

                if not dm_verified:
                    logger.warning(
                        "DM to %s not confirmed in conversation (chat=%s)",
                        prospect_name, dm_chat_id,
                    )
                    await adb.update_outreach(outreach_id,
                                    last_attempt_error="DM delivery not confirmed")
                    await adb.log_action("dm_unverified", outreach_id=outreach_id,
                               result="warning",
                               details={"prospect": prospect_name,
                                        "chat_id": dm_chat_id})
                    await _release_claim()
                    await client.close()
                    return (
                        f"⚠️ DM to {prospect_name} was not confirmed as delivered.\n"
                        f"The API returned success but the message was not found "
                        f"in the conversation. Will retry on next scheduler tick."
                    )

                applied = await adb.update_outreach(
                    outreach_id, status="messaged", channel="linkedin",
                    last_attempt_error=None, followup_count=1,
                    expected_status="sending",
                )
                if not applied:
                    logger.warning(
                        "Outreach %s left sending before DM success write; leaving current status",
                        outreach_id,
                    )
                # Persist provider_id into contact's profile_json so that
                # check_replies can match future messages by provider_id.
                # Search results often lack provider_id, causing reply
                # detection to fail for DM-only / connections-only campaigns.
                if provider_id and contact_id:
                    try:
                        def _backfill_provider_id(cid: str, prov_id: str) -> None:
                            from ..db.schema import get_db as _get_db_pj
                            _db = _get_db_pj()
                            _r = _db.execute(
                                "SELECT profile_json FROM contacts WHERE id = ?",
                                (cid,),
                            ).fetchone()
                            if _r and _r["profile_json"]:
                                pj = json.loads(_r["profile_json"])
                                if not pj.get("provider_id"):
                                    pj["provider_id"] = prov_id
                                    _db.execute(
                                        "UPDATE contacts SET profile_json = ? WHERE id = ?",
                                        (json.dumps(pj), cid),
                                    )
                                    _db.commit()
                            _db.close()
                        await run_db(_backfill_provider_id, contact_id, provider_id)
                    except Exception:
                        pass  # Non-critical — don't break DM flow
                local_msg_id = await adb.save_message(outreach_id, role="sdr", text=message)
                await adb.increment_usage("messages_sent")
                await adb.log_action("dm_sent", outreach_id=outreach_id, result="success",
                           details={"prospect": prospect_name,
                                     "message_length": len(message),
                                     "verified": True})
                # Schedule async post-send re-verification for extra confidence
                if dm_chat_id:
                    from ..services.engagement_verifier import schedule_post_send_verify
                    schedule_post_send_verify(
                        account_id=account_id, chat_id=dm_chat_id,
                        sent_text=message, outreach_id=outreach_id,
                        local_message_id=local_msg_id, message_type="dm",
                        voice_signature=voice_signature,
                    )
                await client.close()
                return (
                    f"✅ DM sent to {prospect_name} ({role_str})\n"
                    f'   "{message}"\n'
                )
            else:
                error = result.get("error", "Unknown error")
                # Permanent failures (subscription_required, not connected)
                # should mark the outreach as error to stop retries
                if result.get("permanent"):
                    await adb.update_outreach(outreach_id, status="error",
                                    last_attempt_error=error)
                    await adb.log_action("dm_permanent_failure", outreach_id=outreach_id,
                               result="error", details={"prospect": prospect_name,
                                                        "error": error[:200]})
                else:
                    await _release_claim()
                    await adb.update_outreach(outreach_id, last_attempt_error=error)
                await client.close()
                return f"❌ DM failed for {prospect_name}: {error}"
        except Exception as e:
            await _release_claim()
            await adb.update_outreach(outreach_id, last_attempt_error=str(e))
            await client.close()
            return f"❌ DM failed for {prospect_name}: {e}"

    # ── LINKEDIN CHANNEL PATH ──
    # Guard: skip company pages — they can't receive connection invitations
    first_name = prospect_data.get("first_name", "")
    last_name = prospect_data.get("last_name", "")
    _pname = (prospect_data.get("name") or prospect.get("name", "")).strip()
    _pcompany = (prospect_data.get("company") or prospect.get("company", "")).strip()
    is_likely_company = (
        (not first_name and not last_name and _pname and _pcompany and _pname.lower() == _pcompany.lower())
        or prospect_data.get("is_company", False)
    )
    if is_likely_company:
        await adb.update_outreach(outreach_id, status="skipped",
                        last_attempt_error="Skipped: company page, not a person")
        await adb.log_action("invitation_company_skip", outreach_id=outreach_id,
                   result="skipped", details={"prospect": _pname})
        await client.close()
        return f"Skipped {_pname}: looks like a company page, not a person."

    # provider_id was resolved and format-guarded before Step 3 — an
    # unsendable id never reaches message generation, let alone Unipile.

    # Track invite attempt
    try:
        current = await adb.get_outreach(outreach_id) or {}
        attempts = (current.get("invite_attempts") or 0) + 1
        await adb.update_outreach(outreach_id, invite_attempts=attempts)
    except Exception:
        attempts = 1

    # Circuit-breaker: skip after too many failed attempts
    from ..constants import MAX_INVITE_ATTEMPTS
    if attempts > MAX_INVITE_ATTEMPTS:
        await adb.update_outreach(outreach_id, status="skipped",
                        last_attempt_error=f"Exceeded {MAX_INVITE_ATTEMPTS} invite attempts")
        await adb.log_action("invitation_max_attempts", outreach_id=outreach_id,
                   result="skipped", details={"attempts": attempts, "prospect": prospect_name})
        await client.close()
        return f"Skipped {prospect_name}: exceeded {MAX_INVITE_ATTEMPTS} invite attempts."

    # ── Final connection guard: check local connections DB ──
    try:
        from ..services.connection_sync import is_first_degree, is_first_degree_by_public_id, mark_connected
        clean_prov_id = (prospect_data.get("provider_id") or "").strip()
        clean_pub_id = (prospect_data.get("public_id") or prospect.get("linkedin_id", "")).strip()
        is_connected = False
        matched_by = ""
        if clean_prov_id and await run_db(is_first_degree, account_id, clean_prov_id):
            is_connected = True
            matched_by = f"provider_id={clean_prov_id}"
        elif clean_pub_id and await run_db(is_first_degree_by_public_id, account_id, clean_pub_id):
            is_connected = True
            matched_by = f"public_id={clean_pub_id}"
        if is_connected:
            await run_db(mark_connected, account_id, clean_prov_id or provider_id, prospect_name, clean_pub_id)
            # exclude_connections: this branch used to set status='connected',
            # which is precisely how an excluded person got a DM instead of an
            # invitation. Park them instead (9 Sep 2026).
            from ..services.connection_sync import is_excluded_connection_outreach
            if await is_excluded_connection_outreach(campaign, prospect):
                from ..services.connection_sync import skip_excluded_connection
                await client.close()
                return await skip_excluded_connection(
                    outreach_id, campaign_id, prospect_name,
                    where="pre_send_connection_guard",
                )
            await adb.update_outreach(outreach_id, status="connected", channel=CHANNEL_LINKEDIN)
            await adb.log_action("pre_send_connection_guard", outreach_id=outreach_id,
                       result="connected",
                       details={"prospect": prospect_name, "matched_by": matched_by})
            logger.info("Connection guard caught %s (%s) — skipping invitation", prospect_name, matched_by)
            return (
                f"Already connected with {prospect_name} "
                f"(detected via local connections DB) — skipping invitation, queued for follow-up DM."
            )
    except Exception as e:
        logger.warning("Connection guard check failed (non-blocking): %s", e)

    # Live connection check via Unipile API — catches connections not yet
    # in the local DB (e.g. accepted between sync intervals).
    try:
        relation = await client.check_existing_relation(account_id, provider_id)
        if relation.get("connected") or relation.get("has_chat"):
            import time as _time
            from ..services.connection_sync import (
                is_excluded_uninvited,
                skip_excluded_connection,
            )
            # Not the date-comparing helper: this live check is the first time
            # we have heard of the edge, so there is no stored date to compare
            # and recording one now would read as "connected during the
            # campaign". We never invited them and LinkedIn says we are
            # connected — the edge predates the campaign by construction.
            if await is_excluded_uninvited(campaign, prospect):
                await client.close()
                return await skip_excluded_connection(
                    outreach_id, campaign_id, prospect_name,
                    where="pre_send_api_guard",
                )
            await adb.update_outreach(outreach_id, status="connected", channel=CHANNEL_LINKEDIN, accepted_at=int(_time.time()))
            await adb.log_action("pre_send_api_guard", outreach_id=outreach_id,
                       result="connected",
                       details={"prospect": prospect_name, "source": "unipile_api"})
            await client.close()
            return (
                f"ℹ️ {prospect_name} is already a 1st-degree connection.\n"
                "Skipping invitation — use send_message(action=\"followup\") to send a DM."
            )
        if relation.get("pending_invite"):
            await adb.update_outreach(outreach_id, status="invited")
            await client.close()
            return f"ℹ️ {prospect_name} already has a pending invitation. Skipping."
    except Exception as e:
        logger.debug("Live connection check failed for %s: %s — proceeding with invitation", prospect_name, e)

    from ..services import experiment_service as _headline_ab

    headline_variant = None
    if _headline_ab.HEADLINE_AB_ENABLED and campaign_id:
        headline_variant = await _headline_ab.swap_headline_for_batch(campaign_id)

    # Send the invitation via Unipile
    from ..ops_log import log_outbound_send
    log_outbound_send(
        "attempt",
        outreach_id=outreach_id,
        campaign_id=campaign_id,
        channel="invite",
        step_index="invite",
        text=message,
        provider_id=provider_id,
    )
    result = await client.send_invitation(
        account_id=account_id,
        provider_id=provider_id,
        message=message,
    )
    log_outbound_send(
        "result",
        outreach_id=outreach_id,
        campaign_id=campaign_id,
        channel="invite",
        step_index="invite",
        text=message,
        provider_id=provider_id,
        success=bool(result.get("success")),
        http_status=result.get("response_status") or None,
        unipile_id=result.get("invitation_id") or "",
        error_type=(result.get("error") or "")[:80] or None,
    )

    try:
        # Update rate limits
        await adb.increment_sent()
        new_limit = await update_limits_after_send(blocked=result.get("blocked", False))

        if result["success"]:
            # Update DB
            applied = await adb.update_outreach(
                outreach_id, status="invited", channel=CHANNEL_LINKEDIN,
                last_attempt_error=None, expected_status="sending",
            )
            if not applied:
                logger.warning(
                    "Outreach %s left sending before invite success write; leaving current status",
                    outreach_id,
                )
            # format='invite_note', not the 'text' default: an invitation note
            # is not a delivered DM, and a reader that treats any sdr message
            # as one (stuck-send recovery) fabricates a conversation with a
            # prospect who never accepted.
            await adb.save_message(outreach_id, role="sdr", text=message, format="invite_note")
            await adb.increment_usage("invitations_sent")
            await adb.log_action("invitation_sent", outreach_id=outreach_id, result="success",
                       details={
                           "prospect": prospect_name,
                           "message_length": len(message),
                           "invitation_id": result.get("invitation_id", ""),
                           "response_status": result.get("response_status", ""),
                       })
            if headline_variant:
                await _headline_ab.persist_invite_headline_variant(
                    campaign_id, outreach_id, headline_variant,
                )
            # Invalidate pending invitation cache after successful send
            from ..linkedin.rate_limiter import invalidate_pending_cache
            invalidate_pending_cache()

            delay = get_next_delay()
            delay_minutes = delay // 60

            return (
                f"✅ Sent to {prospect_name} ({role_str})\n"
                f'   "{message}"\n\n'
                f"⏱️ Next send in ~{delay_minutes} minutes\n"
                f"📊 Daily limit: {new_limit}/day"
            )
        else:
            error = result.get("error", "Unknown error")
            if result.get("auth_error"):
                # Account disconnected — don't change outreach status, just alert user
                await adb.update_outreach(outreach_id, status="pending")
                await adb.log_action("auth_error", outreach_id=outreach_id, result="blocked",
                           details={"error": error})
                return (
                    "🔑 LinkedIn account disconnected.\n\n"
                    "Run setup_profile() again to reconnect your LinkedIn account."
                )
            elif result.get("rate_limited_422"):
                # 422 temporary_provider_limit — LinkedIn restricts personalized
                # invites (weekly limit or non-Sales Navigator). Retry without message.
                if message:
                    await adb.log_action("invitation_retry_no_message", outreach_id=outreach_id,
                               result="retrying", details={"error": error})
                    log_outbound_send(
                        "attempt",
                        outreach_id=outreach_id,
                        campaign_id=campaign_id,
                        channel="invite",
                        step_index="invite",
                        text="",
                        provider_id=provider_id,
                        retry="invitation_retry_no_message",
                    )
                    result2 = await client.send_invitation(
                        account_id=account_id,
                        provider_id=provider_id,
                        message="",
                    )
                    log_outbound_send(
                        "result",
                        outreach_id=outreach_id,
                        campaign_id=campaign_id,
                        channel="invite",
                        step_index="invite",
                        text="",
                        provider_id=provider_id,
                        success=bool(result2.get("success")),
                        retry="invitation_retry_no_message",
                        error_type=(result2.get("error") or "")[:80] or None,
                    )
                    if result2["success"]:
                        applied = await adb.update_outreach(
                            outreach_id, status="invited", channel=CHANNEL_LINKEDIN,
                            last_attempt_error=None, expected_status="sending",
                        )
                        if not applied:
                            logger.warning(
                                "Outreach %s left sending before invite-retry success write; leaving current status",
                                outreach_id,
                            )
                        await adb.save_message(outreach_id, role="sdr", text="", format="invite_note")
                        await adb.increment_usage("invitations_sent")
                        await adb.log_action("invitation_sent", outreach_id=outreach_id, result="success",
                                   details={"prospect": prospect_name, "message_length": 0,
                                            "note": "retried without message (422 rate limit)"})
                        return (
                            f"✅ Sent to {prospect_name} (without personalized message)\n"
                            f"   LinkedIn restricted personalized invites — sent blank invite.\n\n"
                            f"📊 Daily limit: {new_limit}/day"
                        )
                    # Retry also failed — treat as rate limit block
                    error = result2.get("error", error)
                # No message or retry also failed: keep as pending, don't skip
                await adb.update_outreach(outreach_id, status="pending", last_attempt_error=error[:500])
                await adb.log_action("invitation_blocked", outreach_id=outreach_id, result="blocked",
                           details={"error": error, "note": "422 weekly/personalized limit"})
                return (
                    f"⚠️ LinkedIn invitation limit (422): {error}\n\n"
                    f"Daily limit reduced to {new_limit}. Will retry later."
                )
            elif result.get("blocked"):
                await adb.update_outreach(outreach_id, status="pending", last_attempt_error=error[:500])
                await adb.log_action("invitation_blocked", outreach_id=outreach_id, result="blocked",
                           details={"error": error})
                return (
                    f"⚠️ LinkedIn blocked the send: {error}\n\n"
                    f"Daily limit reduced to {new_limit}. Will retry later."
                )
            elif "already connected" in str(error).lower() or "invitation pending" in str(error).lower():
                # 409 = already connected or invitation pending — mark as connected
                from ..services.connection_sync import mark_connected
                await run_db(mark_connected, account_id, provider_id, prospect_name)
                await adb.update_outreach(outreach_id, status="connected", channel=CHANNEL_LINKEDIN,
                                last_attempt_error=None)
                await adb.log_action("already_connected_409", outreach_id=outreach_id, result="connected",
                           details={"prospect": prospect_name, "error": error})
                return (
                    f"Already connected with {prospect_name} — queued for follow-up DM."
                )
            elif "422" in str(error):
                # 422 = invalid profile or permanently rejected — don't retry
                await adb.update_outreach(outreach_id, status="skipped", last_attempt_error=error[:500])
                await adb.log_action("invitation_permanent_error", outreach_id=outreach_id, result="skipped",
                           details={"error": error, "attempts": attempts})
                return f"Skipped {prospect_name}: permanent error (422) — {error}"
            else:
                await adb.update_outreach(outreach_id, status="error", last_attempt_error=error[:500])
                await adb.log_action("invitation_failed", outreach_id=outreach_id, result="error",
                           details={"error": error})
                return f"❌ Send failed for {prospect_name}: {error}"
    finally:
        await client.close()


async def _send_email_outreach(
    client: Any,
    account_id: str,
    outreach_id: str,
    prospect: dict,
    prospect_data: dict,
    prospect_name: str,
    role_str: str,
    linkedin_message: str,
    voice_signature: dict,
    campaign_context: dict,
    campaign_ctx: dict | None,
    prospect_analysis: dict | None,
    release_claim: Any,
) -> str:
    """Send outreach via email channel instead of LinkedIn.

    Generates a proper email (subject + body) and sends via connected email account.

    ``release_claim`` is the caller's CAS release. Every LinkedIn early exit
    releases; this path was never handed the closure, so all four of its exits
    returned with the row still ``status='sending'``. Pre-send guards release
    (nothing was delivered). A *raised* send deliberately does not — see below.
    """
    from ..ai.email_generator import generate_email, merge_email_prospect
    from ..services.unipile_email import (
        confirm_email_in_sent,
        email_send_known_denied,
        resolve_email_account_id,
    )

    async def _release() -> None:
        # release_claim is REQUIRED, not optional. The original defect was the
        # caller never passing it, and a default of None made that silently
        # legal — a test can only notice such an omission after someone writes
        # one, whereas a required parameter makes it a TypeError at the call.
        await release_claim()

    try:
        from ..services.prospect_email import extract_profile_email

        identity = ""
        for blob in (campaign_ctx, campaign_context):
            if isinstance(blob, dict):
                identity = extract_profile_email(blob.get("from_email"))
                if identity:
                    break
        email_account_id = await resolve_email_account_id(
            client, identity_email=identity,
        )
    except Exception as e:
        # Resolution distinguishes "could not ask" from "no mailbox" and raises
        # for the former; nothing has been sent, so the row goes back.
        await _release()
        await client.close()
        return f"❌ Could not check for a connected mailbox: {e}"
    if not email_account_id:
        await _release()
        await client.close()
        return (
            "❌ No email account connected. "
            "Connect Gmail/Outlook with account(action='connect_email'). "
            "Do not use Mail.app."
        )

    # Extract prospect email
    prospect_email = _extract_email(prospect) or _extract_email(prospect_data)
    if not prospect_email:
        # Can't send email without an address — fall back to LinkedIn
        await _release()
        await client.close()
        return (
            f"❌ No email address found for {prospect_name}.\n\n"
            "Email outreach requires a prospect email. Falling back to LinkedIn.\n"
            "Run generate_and_send() again (will use LinkedIn)."
        )

    # Generate email
    from ..services.own_profile_sync import refresh_own_profile_if_stale
    sender_profile = await refresh_own_profile_if_stale()
    if not sender_profile:
        sender_profile = await adb.get_setting("profile", {})
    full_ctx = dict(campaign_context)
    if campaign_ctx:
        full_ctx.update(campaign_ctx)

    try:
        email_result = await generate_email(
            prospect=merge_email_prospect(prospect, prospect_data),
            sender_profile=sender_profile,
            voice_signature=voice_signature,
            campaign_context=full_ctx,
            prospect_analysis=prospect_analysis,
            campaign_ctx=campaign_ctx,
            body=linkedin_message,
        )
    except Exception as e:
        await _release()
        await client.close()
        return f"❌ Email generation failed: {e}"

    subject = email_result["subject"]
    body = email_result["body"]
    if not body:
        await _release()
        await client.close()
        return (
            f"❌ Email draft for {prospect_name} failed quality checks and was not sent."
        )

    # Send via Unipile email API. Keep the client open through the SENT
    # check — a 502/timeout is unknown, and the only safe next read is the
    # outbox, not a second POST.
    try:
        result = await client.send_email(
            account_id=email_account_id,
            to_email=prospect_email,
            to_name=prospect_name,
            subject=subject,
            body=body,
            tracking_label=f"campaign_{outreach_id[:8]}",
        )
    except Exception as e:
        result = {"success": False, "error": f"Email send failed: {e}"}

    async def _record_email_success() -> str:
        applied = await adb.update_outreach(
            outreach_id, status="invited", channel=CHANNEL_EMAIL,
            expected_status="sending",
        )
        if not applied:
            logger.warning(
                "Outreach %s left sending before email success write; leaving current status",
                outreach_id,
            )
        await adb.save_message(
            outreach_id, role="sdr", text=f"[EMAIL] Subject: {subject}\n\n{body}",
        )
        await adb.increment_usage("invitations_sent")
        from ..linkedin.rate_limiter import increment_email_sent as _inc_email
        await _inc_email()
        await adb.log_action(
            "email_sent", outreach_id=outreach_id, result="success",
            details={
                "prospect": prospect_name,
                "email": prospect_email,
                "subject": subject,
                "channel": "email",
            },
        )
        return (
            f"📧 Email sent to {prospect_name} ({role_str})\n"
            f"   To: {prospect_email}\n"
            f"   Subject: {subject}\n"
            f'   Body: "{body[:150]}..."\n\n'
            "Open/click tracking enabled."
        )

    try:
        if result.get("success"):
            return await _record_email_success()

        error = result.get("error", "Unknown error")
        if email_send_known_denied(result):
            await adb.update_outreach(
                outreach_id, status="pending", last_attempt_error=error[:500],
            )
            await adb.log_action(
                "email_blocked", outreach_id=outreach_id, result="blocked",
                details={"error": error, "channel": "email"},
            )
            return f"⚠️ Email not sent to {prospect_name}: {error}\n\nWill retry later."

        found = await confirm_email_in_sent(
            client, email_account_id, prospect_email, subject,
        )
        if found:
            logger.info(
                "Email send reported failure but SENT has %s → %s; recording success",
                subject, prospect_email,
            )
            return await _record_email_success()

        # Unknown outcome, no SENT copy. Do not release to pending — that
        # is how a later tick generates a second, different cold email.
        await adb.update_outreach(
            outreach_id, status="error", last_attempt_error=error[:500],
        )
        await adb.log_action(
            "email_failed", outreach_id=outreach_id, result="error",
            details={"error": error, "channel": "email", "outcome": "unknown"},
        )
        return f"❌ Email failed for {prospect_name}: {error}"
    finally:
        await client.close()
