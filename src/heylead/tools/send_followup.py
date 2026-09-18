"""Tool: send_followup — Generate and send follow-up DMs after connection acceptance.

Core follow-up loop:
1. Find next "connected" outreach that needs a follow-up
2. Load conversation history
3. Resolve chat_id (prospect's linkedin_id → Unipile chat)
4. Generate personalized follow-up via 3-stage pipeline
5. In Copilot mode: show for approval. In Autopilot: send immediately.
6. Update outreach status and followup_count
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..ai.followup_generator import followup_char_limit, generate_followup
from ..ai.llm_validator import llm_validate
from ..ai.message_fixer import fix_message
from ..ai.message_improver import improve_message
from ..ai.message_validator import is_evaluator_refusal, validate_followup
from ..ai.prospect_analyzer import analyze_prospect, is_data_gap_analysis
from ..config import apply_free_monthly_caps, get_tier
from ..constants import (
    COPILOT_APPROVAL_THRESHOLD,
    FREE_MAX_FOLLOWUPS,
    FREE_MONTHLY_MESSAGES,
    PRO_FOLLOWUP_SCHEDULE_DAYS,
    PRO_MAX_FOLLOWUPS,
    TIER_PRO,
)
from ..db import aio as db
from ..db.async_bridge import run_db
from ..linkedin import (
    UnipileAuthError,
    UnipileError,
    get_account_id,
    get_linkedin_client,
)

logger = logging.getLogger(__name__)


async def run_send_followup(
    campaign_id: str = "",
    outreach_id: str = "",
    format: str = "text",
) -> str:
    """Generate and send (or queue) a personalized follow-up DM.

    Flow:
    1. Find the active campaign and next "connected" outreach
    2. Check tier limits and follow-up schedule
    3. Load conversation history and resolve chat_id
    4. Generate follow-up using voice signature
    5. Validate the message (follow-up pipeline)
    6. Copilot: show for review. Autopilot: send directly.

    Args:
        format: "text" (default) or "voice" (generates audio via Hume TTS).
    """

    # ── Step 0: Pre-checks ──
    setup_done = await db.get_setting("setup_complete", False)
    if not setup_done:
        return (
            "Setup required before sending follow-ups.\n\n"
            "Please run setup_profile first."
        )

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected. Run setup_profile first."

    try:
        client = get_linkedin_client()
    except UnipileError as e:
        return f"{e}"

    # ── Step 1: Find campaign + follow-up candidate ──
    tier = get_tier()
    max_followups = PRO_MAX_FOLLOWUPS if tier == TIER_PRO else FREE_MAX_FOLLOWUPS

    if outreach_id:
        # Specific outreach requested
        outreach = await db.get_outreach(outreach_id)
        if not outreach:
            await client.close()
            return f"Outreach not found: {outreach_id}"
        if outreach["status"] != "connected":
            await client.close()
            return (
                f"Outreach status is '{outreach['status']}', not 'connected'.\n"
                "Follow-ups can only be sent to connected prospects."
            )
        if outreach.get("followup_count", 0) >= max_followups:
            await client.close()
            return (
                f"Max follow-ups reached ({max_followups}) for this outreach.\n"
                f"{'Upgrade to Pro for up to 5 follow-ups.' if tier != TIER_PRO else 'Maximum follow-ups sent.'}"
            )
        campaign = await db.get_campaign(outreach["campaign_id"])
        if not campaign:
            await client.close()
            return "Campaign not found for this outreach."
        campaign_id = campaign["id"]

        from ..services.project_brief import refuse_without_project_brief
        missing = refuse_without_project_brief(campaign)
        if missing:
            await client.close()
            return missing

        # Pause has to hold on THIS path, not just the no-id one below. The
        # scheduler reaches follow-ups through outreach_id, and pause only
        # touches 'pending' outreaches — so every already-'connected' prospect
        # keeps a queued job that sails past _check_outreach_still_valid. A
        # user pausing at night otherwise finds follow-ups went out anyway,
        # which is the one thing pause exists to prevent.
        if campaign.get("status") == "paused":
            await client.close()
            return (
                f"Campaign '{campaign['name']}' is paused.\n\n"
                "No messages will be sent while the campaign is paused.\n"
                "Use resume_campaign() to resume outreach."
            )

        candidate = await db.get_outreach_with_contact(outreach_id)
        if not candidate:
            await client.close()
            return "Could not load contact data for this outreach."
    else:
        # Find campaign
        campaign, err = await db.find_active_campaign(campaign_id)
        if not campaign:
            await client.close()
            return err
        campaign_id = campaign["id"]

        from ..services.project_brief import refuse_without_project_brief
        missing = refuse_without_project_brief(campaign)
        if missing:
            await client.close()
            return missing

        # Block sends when campaign is paused
        if campaign.get("status") == "paused":
            await client.close()
            return (
                f"Campaign '{campaign['name']}' is paused.\n\n"
                "No messages will be sent while the campaign is paused.\n"
                "Use resume_campaign() to resume outreach."
            )

        # Find next follow-up candidate
        candidates = await db.get_followup_candidates(campaign_id, max_followups)
        if not candidates:
            # Log detailed breakdown of why no candidates are eligible
            breakdown = await db.get_followup_breakdown(campaign_id, max_followups)
            await db.log_action(
                "followup_no_candidates",
                result="skipped",
                details={
                    "campaign_id": campaign_id,
                    "campaign_name": campaign["name"],
                    "total_connected_messaged": breakdown["total"],
                    "eligible": breakdown["eligible"],
                    "maxed_out": breakdown["maxed_out"],
                    "max_followups": breakdown["max_followups"],
                    "distribution": breakdown["distribution"],
                    "by_status": breakdown["by_status"],
                    "reason": (
                        "no connected/messaged prospects"
                        if breakdown["total"] == 0
                        else f"all {breakdown['maxed_out']} maxed out at {max_followups} followups"
                        if breakdown["eligible"] == 0
                        else "eligible but filtered by delay schedule"
                    ),
                },
            )
            logger.info(
                "No followup candidates for '%s': total=%d, eligible=%d, maxed=%d",
                campaign["name"],
                breakdown["total"],
                breakdown["eligible"],
                breakdown["maxed_out"],
            )
            await client.close()
            return (
                f"No prospects ready for follow-up in '{campaign['name']}'.\n\n"
                "Prospects need to accept your connection first (status: 'connected').\n"
                "Use check_replies() to detect new connections, or generate_and_send() "
                "to reach new prospects."
            )
        candidate = candidates[0]
        outreach_id = candidate["outreach_id"]

    # ── Exclusion gate: skip contacts excluded from automation ──
    from ..db.global_contact_queries import is_excluded_by_contact_id
    from ..ops_log import record_skip_excluded
    _cid = candidate.get("contact_id", "")
    if _cid and await run_db(is_excluded_by_contact_id, _cid):
        await record_skip_excluded(
            outreach_id=outreach_id,
            campaign_id=candidate.get("campaign_id") or "",
            contact_id=_cid,
        )
        await client.close()
        return (
            f"Skipped {candidate.get('name', 'Unknown')} — excluded from automation.\n\n"
            "This contact has the 'do-not-automate' tag or 'do_not_contact' lifecycle.\n"
            "Remove the tag with contacts(action='tag', tag='-do-not-automate') to re-enable."
        )

    # ── Step 2: Check tier limits ──
    # Hosted accounts are not capped by the local free row (the cloud sender
    # never enforces it); same rule as create_campaign and generate_send.
    if apply_free_monthly_caps():
        usage = await db.get_monthly_usage()
        if usage.get("messages_sent", 0) >= FREE_MONTHLY_MESSAGES:
            await client.close()
            return (
                f"Free tier limit reached: {FREE_MONTHLY_MESSAGES} messages/month.\n\n"
                "Upgrade to Pro ($29/mo) for unlimited follow-ups."
            )

    # A local invite_note is not an in-thread intro. Messaging often has
    # nothing from us yet — write an opener, do not "follow up" on a
    # note the prospect never saw. Email-channel rows have no LinkedIn
    # thread; leave those on the email follow-up path.
    from ..db.queries import has_real_sdr_message
    outreach_row = await db.get_outreach(outreach_id) or {}
    outreach_channel = (
        outreach_row.get("channel")
        or candidate.get("channel")
        or "linkedin"
    )
    if (
        outreach_id
        and outreach_channel != "email"
        and not await run_db(has_real_sdr_message, outreach_id)
    ):
        logger.info(
            "Outreach %s has no in-thread DM — writing an opener, not a follow-up",
            outreach_id[:8],
        )
        await client.close()
        from .generate_send import run_generate_and_send
        return await run_generate_and_send(
            campaign_id=campaign_id,
            force_channel="dm",
            target_outreach_id=outreach_id,
        )

    # ── Step 3: Check follow-up schedule ──
    # Minimum 1-day delay between ANY messages (including the first DM).
    # Pro tier uses the full escalating schedule for subsequent follow-ups.
    MIN_MESSAGE_GAP_DAYS = 1
    followup_count = candidate.get("followup_count", 0)
    this_followup = followup_count + 1
    max_chars = followup_char_limit(this_followup)

    # Check last actual message timestamp from DB (more reliable than updated_at)
    last_sdr_ts = 0
    try:
        from ..db.queries import last_real_sdr_message_ts
        last_sdr_ts = await run_db(last_real_sdr_message_ts, outreach_id)
    except Exception:
        pass

    # Fallback to outreach updated_at if no message timestamps
    if not last_sdr_ts:
        outreach_data = await db.get_outreach(outreach_id) or {}
        last_sdr_ts = outreach_data.get("updated_at", 0)

    days_since_last = (int(time.time()) - last_sdr_ts) // 86400 if last_sdr_ts else 999

    # Determine required delay
    if tier == TIER_PRO and followup_count > 0:
        schedule_idx = min(followup_count, len(PRO_FOLLOWUP_SCHEDULE_DAYS) - 1)
        required_days = PRO_FOLLOWUP_SCHEDULE_DAYS[schedule_idx]
    else:
        required_days = MIN_MESSAGE_GAP_DAYS

    # The gap is between OUR messages. An invitation note is not one of them
    # here: it rides the invitation and lands in the thread only when the
    # prospect accepts, so on a thread that holds nothing but the note the
    # first follow-up is the opening message and goes out on acceptance
    # (the hosted scheduler's hot delay). Counting the note kept every
    # same-day accepter waiting a day for a message that was due now.
    from ..db.queries import has_real_sdr_message
    if not await run_db(has_real_sdr_message, outreach_id):
        days_since_last = 999

    if days_since_last < required_days:
        wait_days = required_days - days_since_last
        await db.log_action(
            "followup_delay_pending",
            outreach_id=outreach_id,
            result="skipped",
            details={
                "prospect_name": candidate.get("name", "Unknown"),
                "followup_number": followup_count + 1,
                "days_since_last": days_since_last,
                "required_days": required_days,
                "wait_days": wait_days,
                "schedule": PRO_FOLLOWUP_SCHEDULE_DAYS,
            },
        )
        logger.info(
            "Followup #%d for %s delayed: %dd since last, need %dd (wait %dd)",
            followup_count + 1,
            candidate.get("name", "?"),
            days_since_last,
            required_days,
            wait_days,
        )
        await client.close()
        return (
            f"Too early for follow-up #{followup_count + 1}.\n\n"
            f"Schedule: wait {required_days} days between follow-ups.\n"
            f"Last message was {days_since_last} day(s) ago.\n"
            f"Next follow-up in {wait_days} day{'s' if wait_days != 1 else ''}."
        )

    # ── Step 4: Resolve chat_id ──
    # Prefer provider_id (ACoAAA format) from profile_json — that's what
    # chat attendees use.  Fall back to linkedin_id (public_id / slug).
    prospect_linkedin_id = ""
    pj = candidate.get("profile_json")
    if pj:
        try:
            import json as _json
            _profile = _json.loads(pj) if isinstance(pj, str) else pj
            prospect_linkedin_id = _profile.get("provider_id", "")
        except Exception:
            pass
    if not prospect_linkedin_id:
        prospect_linkedin_id = candidate.get("linkedin_id", "")
    if not prospect_linkedin_id:
        await client.close()
        return f"No LinkedIn ID for {candidate.get('name', 'Unknown')}. Cannot send DM."

    stored_chat_id = (candidate.get("chat_id") or "").strip()
    try:
        chat_id = await client.find_chat_for_user(
            account_id, prospect_linkedin_id,
            preferred_chat_id=stored_chat_id or None,
        )
    except Exception as e:
        logger.error(f"Failed to find chat: {e}")
        chat_id = None
    if chat_id and chat_id != stored_chat_id:
        from ..db.queries import update_outreach
        await run_db(update_outreach, outreach_id, chat_id=chat_id)

    # No existing chat — will create one when sending (first DM after acceptance).
    # Flag so the send step uses send_new_message() instead of send_message().
    create_new_chat = chat_id is None

    # For new chats, we need the provider_id (not the public slug).
    # Extract from miniProfileUrn in the LinkedIn URL if available.
    prospect_provider_id = prospect_linkedin_id
    if create_new_chat and not prospect_linkedin_id.startswith("ACo"):
        url = candidate.get("linkedin_url") or ""
        from urllib.parse import urlparse, parse_qs, unquote
        qs = parse_qs(urlparse(url).query)
        urn = unquote(qs.get("miniProfileUrn", [""])[0])
        if urn:
            prospect_provider_id = urn.split(":")[-1]

    # ── Step 5: Load conversation history (enriched with LinkedIn) ──
    sender_provider_id = (await db.get_setting("profile", {})).get("provider_id", "")
    if chat_id and sender_provider_id:
        try:
            from ..services.conversation_enricher import get_enriched_conversation
            messages = await get_enriched_conversation(
                client, account_id, chat_id, outreach_id, sender_provider_id,
            )
        except Exception as e:
            logger.warning("Conversation enrichment failed, using local-only: %s", e)
            messages = await db.get_messages_for_outreach(outreach_id)
    else:
        messages = await db.get_messages_for_outreach(outreach_id)

    # ── Step 5a: Hard message cap — never exceed MAX messages per prospect ──
    MAX_SDR_MESSAGES_PER_PROSPECT = 3
    existing_sdr_msgs = [m for m in await db.get_messages_for_outreach(outreach_id) if m.get("role") == "sdr"]
    if len(existing_sdr_msgs) >= MAX_SDR_MESSAGES_PER_PROSPECT:
        await client.close()
        await db.log_action(
            "followup_cap_reached",
            outreach_id=outreach_id,
            result="skipped",
            details={"sent": len(existing_sdr_msgs), "limit": MAX_SDR_MESSAGES_PER_PROSPECT},
        )
        return (
            f"⏭️ Message cap reached for {candidate.get('name', 'Unknown')} "
            f"({len(existing_sdr_msgs)}/{MAX_SDR_MESSAGES_PER_PROSPECT} messages sent)."
        )

    # ── Step 5b: Pre-send safety — abort if prospect ever messaged us ──
    # Check for ANY prospect message in the conversation, not just the most recent.
    # This catches inbound leads who messaged first (e.g. months ago) — we shouldn't
    # pile automated follow-ups on top of their messages.
    prospect_msgs = [m for m in messages if m.get("role") == "prospect"]
    if prospect_msgs:
        last_prospect_text = prospect_msgs[-1].get("text", "")[:100]
        await client.close()
        await db.log_action(
            "followup_skipped_prospect_replied",
            outreach_id=outreach_id,
            result="skipped",
            details={
                "prospect_messages": len(prospect_msgs),
                "last_prospect_msg": last_prospect_text,
            },
        )
        # Update status so scheduler stops trying to follow up
        outreach_data_check = await db.get_outreach(outreach_id) or {}
        if outreach_data_check.get("status") in ("connected", "messaged"):
            await db.update_outreach(outreach_id, status="replied")
        return (
            f"⏭️ Skipped follow-up for {candidate.get('name', 'Unknown')} — "
            f"prospect has {len(prospect_msgs)} message(s) in conversation.\n"
            f"   Last from them: \"{last_prospect_text}\"\n"
            f"Use send_message(action='reply') to respond."
        )

    # ── Step 5c: Load warm-up engagement history ──
    engagement_history: list[dict] = []
    try:
        def _fetch_engagements(oid: str) -> list[dict]:
            from ..db.schema import get_db as _get_db_eng
            _eng_db = _get_db_eng()
            rows = _eng_db.execute(
                "SELECT action_type, post_text, text, reaction_type, created_at "
                "FROM engagements WHERE outreach_id = ? AND status IN ('sent', 'verified') "
                "ORDER BY created_at ASC",
                (oid,),
            ).fetchall()
            _eng_db.close()
            return [dict(r) for r in rows]

        engagement_history = await run_db(_fetch_engagements, outreach_id)
        if engagement_history:
            logger.info("Loaded %d engagements for outreach %s", len(engagement_history), outreach_id[:8])
    except Exception as e:
        logger.debug("Engagement history load skipped: %s", e)

    # ── Step 6: Generate follow-up message ──
    from ..services.own_profile_sync import refresh_own_profile_if_stale
    sender_profile = await refresh_own_profile_if_stale()
    if not sender_profile:
        sender_profile = await db.get_setting("profile", {})
    voice_signature = await db.get_setting("voice_signature", {})
    campaign_config = json.loads(campaign.get("config_json") or "{}")
    icp_data = json.loads(campaign.get("icp_json") or "{}")

    campaign_context = {
        "target_description": campaign_config.get("target_description", ""),
        "relevance_hook": icp_data.get("relevance_hook", ""),
        "booking_link": campaign_config.get("booking_link", ""),
        "campaign_intent": campaign_config.get("campaign_intent", ""),
        "campaign_type": campaign_config.get("campaign_type", ""),
    }

    # Load campaign context (offerings, case_studies, social_proofs, preferences)
    campaign_ctx = await db.get_campaign_context(campaign_id)

    from ..linkedin.profile_normalize import prospect_data_from_contact
    prospect_data = prospect_data_from_contact(candidate)

    # ── Prospect Intelligence: load cached or generate ──
    prospect_analysis = None
    contact_db_id = candidate.get("contact_id") or candidate.get("contact_db_id", "")
    if contact_db_id:
        prospect_analysis = await db.get_contact_analysis(contact_db_id)
        if (
            prospect_analysis
            and is_data_gap_analysis(prospect_analysis)
            and (prospect_data.get("title") or prospect_data.get("company"))
        ):
            prospect_analysis = None
    if not prospect_analysis:
        try:
            prospect_analysis = await analyze_prospect(
                prospect=prospect_data,
                campaign_context=campaign_context,
                icp_data=icp_data,
            )
            if contact_db_id:
                await db.save_contact_analysis(contact_db_id, prospect_analysis)
        except Exception as e:
            logger.warning(f"Prospect analysis failed, proceeding without: {e}")

    # The cached signal_context describes whichever of the prospect's signals
    # last scored highest, not the one that created this outreach — so a
    # follow-up would reference a different post than the first touch did.
    # The invitation and InMail paths already correct for this; this one was
    # missed, which is how the thread that opens with a stadium post ends up
    # following up about someone else's AI funding round.
    try:
        trigger_signal_id = ((await db.get_outreach(outreach_id) or {}).get("signal_id") or "").strip()
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

    # Format conversation history for the generator
    conversation_history = [
        {"role": m["role"], "text": m["text"]}
        for m in messages
    ]

    # ── Generate → Improve → Validate → Fix pipeline ──
    message = ""
    reasoning = {}
    validation = None
    previous_sdr_messages = [m["text"] for m in messages if m.get("role") == "sdr"]

    from ..ai.brief_builder import build_message_brief
    from ..ai.intent import resolve_intent
    from dataclasses import asdict as _asdict
    campaign_intent = resolve_intent(campaign_config)
    message_brief = build_message_brief(
        intent=campaign_intent,
        campaign_config=campaign_config,
        campaign_ctx=campaign_ctx,
        prospect=prospect_data,
        icp_data=icp_data,
        analysis=prospect_analysis,
        touch="followup",
    )
    await db.log_action(
        "message_brief", outreach_id=outreach_id, details=_asdict(message_brief),
    )

    # Attempt 1: Generate + Improve + Validate (+ Fix if needed)
    memory_entry = None
    try:
        result = await generate_followup(
            prospect=prospect_data,
            sender_profile=sender_profile,
            voice_signature=voice_signature,
            campaign_context=campaign_context,
            conversation_history=conversation_history,
            followup_number=followup_count + 1,
            prospect_analysis=prospect_analysis,
            campaign_ctx=campaign_ctx,
            outreach_id=outreach_id,
            engagement_history=engagement_history,
            brief=message_brief,
            icp_data=icp_data,
        )
        message = result.get("message", "")
        reasoning = result.get("reasoning", {})
        memory_entry = result.get("memory_entry")
    except Exception as e:
        logger.error(f"Follow-up generation failed: {e}")
        await client.close()
        return f"Failed to generate follow-up: {e}"

    if is_evaluator_refusal(message):
        await db.update_outreach(outreach_id, status="skipped")
        await db.log_action(
            "evaluator_refusal_skipped",
            outreach_id=outreach_id,
            result="skipped",
            details={"draft": message[:300], "type": "followup"},
        )
        await client.close()
        return (
            f"⏭️ Skipped {candidate.get('name', 'Unknown')} — model refused "
            f"the brief. The refusal was not sent."
        )

    # Improve stage — polish for naturalness
    try:
        message = await improve_message(
            draft=message,
            voice_signature=voice_signature,
            message_type="followup",
            max_chars=max_chars,
            intent=campaign_intent,
            brief=message_brief,
        )
    except Exception as e:
        logger.warning(f"Improve stage failed, using raw message: {e}")

    # Validate (rule-based)
    validation = validate_followup(
        message,
        voice_signature,
        previous_messages=previous_sdr_messages,
        max_chars=max_chars,
        followup_number=this_followup,
    )

    # LLM validation — context-sensitive checks (v63 validate prompt)
    if validation.is_valid:
        try:
            sender_company = sender_profile.get("company", "")
            llm_result = await llm_validate(
                message=message,
                history=conversation_history,
                company=sender_company,
                calendar_link=campaign_context.get("booking_link", ""),
            )
            if not llm_result.is_valid:
                validation.issues.extend(llm_result.issues)
                validation.is_valid = False
                logger.info("LLM validation caught issues: %s", llm_result.issues)
        except Exception as e:
            logger.warning(f"LLM validation skipped: {e}")

    # Fix stage — if validation failed, surgically fix issues
    if not validation.is_valid:
        logger.info(f"Follow-up validation failed, attempting fix: {validation.issues}")
        try:
            message = await fix_message(
                message=message,
                issues=validation.issues,
                voice_signature=voice_signature,
                message_type="followup",
                max_chars=max_chars,
                intent=campaign_intent,
                brief=message_brief,
            )
            validation = validate_followup(
                message,
                voice_signature,
                previous_messages=previous_sdr_messages,
                max_chars=max_chars,
                followup_number=this_followup,
            )
        except Exception as e:
            logger.warning(f"Fix stage failed: {e}")

    # Last resort: regenerate from scratch if still invalid
    if not validation.is_valid:
        logger.info(f"Fix failed, regenerating follow-up from scratch: {validation.issues}")
        try:
            result = await generate_followup(
                prospect=prospect_data,
                sender_profile=sender_profile,
                voice_signature=voice_signature,
                campaign_context=campaign_context,
                conversation_history=conversation_history,
                followup_number=followup_count + 1,
                prospect_analysis=prospect_analysis,
                campaign_ctx=campaign_ctx,
                outreach_id=outreach_id,
                brief=message_brief,
                icp_data=icp_data,
            )
            message = result.get("message", "")
            reasoning = result.get("reasoning", {})
            memory_entry = result.get("memory_entry")
            message = await improve_message(
                draft=message,
                voice_signature=voice_signature,
                message_type="followup",
                max_chars=max_chars,
                intent=campaign_intent,
                brief=message_brief,
            )
            validation = validate_followup(
                message,
                voice_signature,
                previous_messages=previous_sdr_messages,
                max_chars=max_chars,
                followup_number=this_followup,
            )
        except Exception as e:
            logger.error(f"Regeneration failed: {e}")

    if not validation or not validation.is_valid:
        issues_text = "\n".join(f"  ⚠️ {issue}" for issue in (validation.issues if validation else []))
        logger.warning(f"Blocked invalid follow-up: {issues_text}")
        await db.log_action("validation_blocked", outreach_id=outreach_id, result="blocked",
                   details={"issues": validation.issues if validation else [], "type": "followup"})
        await client.close()
        return (
            f"⚠️ Follow-up for {candidate.get('name', 'Unknown')} failed validation "
            f"after Generate → Improve → Fix pipeline.\n\n"
            f"Issues:\n{issues_text}\n\n"
            "The message was NOT sent to protect your account.\n"
            "Use send_message() to write one yourself."
        )

    from ..guardrails import prepare_outbound_text
    message = prepare_outbound_text(message, kind="dm")

    # ── Step 7: Send ──
    prospect_name = candidate.get("name", "Unknown")
    prospect_title = candidate.get("title", "")
    prospect_company = candidate.get("company", "")

    role_str = prospect_title
    if prospect_company:
        role_str += f" at {prospect_company}" if role_str else prospect_company

    # Autopilot — send immediately

    # ── Optimistic lock: claim this outreach atomically ──
    # Prevents duplicate follow-ups when multiple scheduler jobs target the same prospect.
    # CAS: only succeeds if status is still 'connected' or 'messaged'.
    # 'messaged' is needed for DM-only campaigns and subsequent follow-ups.
    def _try_claim_followup(oid: str) -> tuple[str, int]:
        from ..db.schema import get_db as _get_db_lock
        lock_db = _get_db_lock()
        pre_lock_row = lock_db.execute(
            "SELECT status FROM outreaches WHERE id = ?", (oid,),
        ).fetchone()
        pre_status = pre_lock_row["status"] if pre_lock_row else "connected"
        rows = lock_db.execute(
            "UPDATE outreaches SET status = 'sending_followup', updated_at = strftime('%s','now') WHERE id = ? AND status IN ('connected', 'messaged')",
            (oid,),
        ).rowcount
        lock_db.commit()
        lock_db.close()
        return pre_status, rows

    pre_lock_status, claimed = await run_db(_try_claim_followup, outreach_id)
    if not claimed:
        logger.info("Outreach %s already claimed by another follow-up job, skipping", outreach_id)
        await client.close()
        return "Prospect already being processed by another follow-up job. Skipping."

    async def _release_followup_claim() -> None:
        """Release the optimistic lock on failure — reset status to pre-lock value."""
        try:
            def _do_release(oid: str, orig_st: str) -> None:
                from ..db.schema import get_db as _get_db_rel
                rel_db = _get_db_rel()
                rel_db.execute(
                    "UPDATE outreaches SET status = ?, updated_at = strftime('%s','now') WHERE id = ? AND status = 'sending_followup'",
                    (orig_st, oid),
                )
                rel_db.commit()
                rel_db.close()
            await run_db(_do_release, outreach_id, pre_lock_status)
        except Exception as e:
            logger.error("Failed to release follow-up claim for %s: %s", outreach_id, e)

    # ── EMAIL FOLLOW-UP PATH ──
    outreach_record = await db.get_outreach(outreach_id)
    outreach_channel = (outreach_record or {}).get("channel", "linkedin")
    if outreach_channel == "email":
        # Terminal dead end, checked before the send: an email-channel outreach
        # whose contact has no address can never be emailed. Releasing the claim
        # alone would hand it back to the next tick, which re-runs the whole
        # generate/improve/validate pipeline — all of which happens *before* the
        # claim — and fails at the same guard for ever. The DM path marks its
        # equivalent dead end (not 1st-degree) as 'error'; this mirrors it.
        from ..services.channel_selector import _extract_email as _addr

        if not _addr(candidate):
            await _release_followup_claim()
            error_msg = "No email address on an email-channel outreach"
            await db.update_outreach(
                outreach_id, status="error", last_attempt_error=error_msg,
            )
            await db.log_action(
                "email_no_address", outreach_id=outreach_id, result="error",
                details={"prospect": prospect_name},
            )
            logger.warning(
                "Blocking email follow-up to %s — no address on the contact",
                prospect_name,
            )
            await client.close()
            return (
                f"⏭️ Skipped follow-up to {prospect_name} — no email address "
                "on an email-channel outreach."
            )

        try:
            result = await _send_email_followup(
                client, account_id, outreach_id, candidate,
                prospect_name, role_str, message, followup_count,
                max_followups, voice_signature, reasoning, memory_entry,
            )
        except Exception as e:
            # The claim must not survive an exception: nothing else moves the
            # outreach out of 'sending_followup' for ten minutes.
            logger.error("Email follow-up failed for %s: %s", outreach_id, e)
            await _release_followup_claim()
            try:
                await client.close()
            except Exception:
                pass
            return f"Email follow-up failed for {prospect_name}: {e}"
        # Release unconditionally. This is a no-op once the send has moved the
        # outreach on, because _do_release's UPDATE is guarded by
        # "AND status = 'sending_followup'" — so the success path, which sets
        # status='messaged', is untouched.
        #
        # It used to release only when the returned text contained "failed" or
        # "error". Both of the email path's guard clauses word their refusal
        # without either — "No email account connected for follow-up", "No
        # email address for X" — so each pinned the outreach in
        # 'sending_followup' with nothing else able to clear it, and the
        # prospect was never followed up again. A string-matched release puts
        # every future early return one word away from the same leak.
        await _release_followup_claim()
        return result

    # ── Pre-send connection check (mirrors generate_send.py) ──
    # Verify prospect is actually a 1st-degree connection before DM.
    # Without this, stale connections table entries cause 403 subscription_required.
    from ..services.dm_connection_guard import verify_dm_eligible
    guard = await verify_dm_eligible(
        account_id,
        candidate,
        {
            "id": outreach_id,
            "status": candidate.get("status") or candidate.get("outreach_status"),
            "accepted_at": candidate.get("accepted_at"),
            "name": prospect_name,
        },
        client=client,
        profile={"provider_id": prospect_provider_id, "public_id": candidate.get("public_id") or ""},
    )
    if not guard.allowed:
        await _release_followup_claim()
        result = "deferred" if guard.outcome == "defer" else "error"
        if guard.outcome == "error":
            await db.update_outreach(
                outreach_id, status="error", last_attempt_error=guard.message,
            )
        await db.log_action(
            "dm_not_connected", outreach_id=outreach_id,
            result=result,
            details={"prospect": prospect_name, "outcome": guard.outcome},
        )
        logger.warning("Blocking follow-up DM to %s — %s", prospect_name, guard.message)
        await client.close()
        return f"⏭️ Skipped follow-up to {prospect_name} — {guard.message}"

    # ── LINKEDIN DM PATH ──
    actual_format = "text"
    audio_path = ""
    if format == "voice":
        try:
            from ..ai.voice_memo_generator import generate_voice_memo, cleanup_voice_memo
            from ..config import is_voice_memo_enabled
            if is_voice_memo_enabled():
                voice_result = await generate_voice_memo(
                    message,
                    voice_signature=voice_signature,
                    humanize=campaign_config.get("voice_humanize", True),
                    noise_type=campaign_config.get("voice_noise_type", "auto"),
                    noise_volume=campaign_config.get("voice_noise_volume", "subtle"),
                )
                if voice_result.get("success"):
                    audio_path = voice_result["audio_path"]
                    actual_format = "voice"
                else:
                    logger.warning("Voice gen failed (%s), falling back to text", voice_result.get("error"))
        except Exception as e:
            logger.warning("Voice gen error (%s), falling back to text", e)

    try:
        if actual_format == "voice" and audio_path:
            if create_new_chat:
                send_result = await client.send_new_voice_message(
                    account_id=account_id,
                    provider_id=prospect_provider_id,
                    audio_path=audio_path,
                )
            else:
                send_result = await client.send_voice_message(
                    account_id=account_id,
                    chat_id=chat_id,
                    audio_path=audio_path,
                )
            # If voice send failed, fall back to text
            if not send_result.get("success"):
                logger.warning("Voice send failed (%s), falling back to text", send_result.get("error"))
                actual_format = "text"

        if actual_format == "text":
            from ..ops_log import log_outbound_send
            step = f"followup_{followup_count + 1}"
            log_outbound_send(
                "attempt",
                outreach_id=outreach_id,
                campaign_id=campaign_id,
                channel="dm",
                step_index=step,
                text=message,
                provider_id=prospect_provider_id,
                chat_id=chat_id if not create_new_chat else "",
                msg_format=actual_format,
            )
            if create_new_chat:
                send_result = await client.send_new_message(
                    account_id=account_id,
                    provider_id=prospect_provider_id,
                    text=message,
                )
            else:
                send_result = await client.send_message(
                    account_id=account_id,
                    chat_id=chat_id,
                    text=message,
                )
            resolved_chat = send_result.get("chat_id") or chat_id or ""
            if resolved_chat:
                from ..db.queries import update_outreach
                await run_db(update_outreach, outreach_id, chat_id=resolved_chat)
            log_outbound_send(
                "result",
                outreach_id=outreach_id,
                campaign_id=campaign_id,
                channel="dm",
                step_index=step,
                text=message,
                provider_id=prospect_provider_id,
                chat_id=resolved_chat,
                success=bool(send_result.get("success")),
                error_type=(send_result.get("error") or "")[:80] or None,
                msg_format=actual_format,
            )
    except UnipileAuthError:
        await _release_followup_claim()
        await client.close()
        if audio_path:
            try:
                from ..ai.voice_memo_generator import cleanup_voice_memo
                cleanup_voice_memo(audio_path)
            except Exception:
                pass
        return (
            "LinkedIn account disconnected.\n\n"
            "Run setup_profile() again to reconnect."
        )
    except Exception as e:
        await _release_followup_claim()
        await client.close()
        if audio_path:
            try:
                from ..ai.voice_memo_generator import cleanup_voice_memo
                cleanup_voice_memo(audio_path)
            except Exception:
                pass
        return f"Failed to send follow-up: {e}"
    finally:
        # The client stays open past this block: delivery is confirmed by
        # reading the conversation back, and a closed httpx session makes that
        # read fail, which used to record every delivered DM as unverified.
        if audio_path:
            try:
                from ..ai.voice_memo_generator import cleanup_voice_memo
                cleanup_voice_memo(audio_path)
            except Exception:
                pass

    if send_result.get("success"):
        # Verify the DM was actually delivered by reading back the conversation.
        # DMs can only be sent to 1st-degree connections — if delivery fails
        # silently we must not count it.
        verify_chat_id = chat_id
        if create_new_chat and send_result.get("chat_id"):
            verify_chat_id = send_result["chat_id"]
        dm_verified = False
        if verify_chat_id and actual_format == "text":
            try:
                dm_verified = await client.verify_message_sent(
                    account_id, verify_chat_id, message,
                )
            except Exception as e:
                logger.warning("DM verification error: %s", e)
        else:
            # Voice messages can't be text-verified; trust the API response
            dm_verified = True

        await client.close()

        if not dm_verified:
            logger.warning(
                "DM to %s not confirmed in conversation (chat_id=%s)",
                prospect_name, verify_chat_id,
            )
            await _release_followup_claim()
            await db.update_outreach(
                outreach_id,
                last_attempt_error="DM delivery not confirmed — message not found in conversation",
            )
            await db.log_action(
                "followup_delivery_failed",
                outreach_id=outreach_id,
                result="unverified",
                details={
                    "prospect": prospect_name,
                    "chat_id": verify_chat_id or "",
                    "message_id": send_result.get("message_id", ""),
                },
            )
            return (
                f"⚠️ DM to {prospect_name} was not confirmed as delivered.\n"
                "The API accepted the request but the message was not found "
                "in the conversation. Will retry on next run."
            )

        # Update DB — delivery confirmed
        new_count = followup_count + 1
        await db.update_outreach(outreach_id, status="messaged", followup_count=new_count, last_attempt_error=None)
        local_msg_id = await db.save_message(outreach_id, role="sdr", text=message, format=actual_format)
        await db.increment_usage("messages_sent")

        # Save conversation memory for future follow-ups
        if memory_entry:
            try:
                await db.save_outreach_memory(outreach_id, memory_entry)
            except Exception as e:
                logger.debug("Memory save failed: %s", e)
        await db.log_action(
            "followup_sent",
            outreach_id=outreach_id,
            result="success",
            details={
                "prospect": prospect_name,
                "followup_number": new_count,
                "message_length": len(message),
                "reasoning": reasoning,
                "message_id": send_result.get("message_id", ""),
                "verified": True,
            },
        )

        # Post-send background verification (garble check + auto-delete)
        if actual_format == "text" and verify_chat_id:
            from ..services.engagement_verifier import schedule_post_send_verify
            schedule_post_send_verify(
                account_id=account_id, chat_id=verify_chat_id,
                sent_text=message, outreach_id=outreach_id,
                local_message_id=local_msg_id, message_type="followup",
                voice_signature=voice_signature,
            )

        remaining = max_followups - new_count
        return (
            f"Sent follow-up #{new_count} to {prospect_name} ({role_str})\n"
            f'   "{message}"\n\n'
            f"Follow-ups remaining: {remaining}/{max_followups}"
        )
    else:
        await client.close()
        await _release_followup_claim()
        error = send_result.get("error", "Unknown error")
        await db.log_action(
            "followup_failed",
            outreach_id=outreach_id,
            result="error",
            details={"error": error},
        )
        return f"Follow-up failed for {prospect_name}: {error}"


async def _send_email_followup(
    client: Any,
    account_id: str,
    outreach_id: str,
    candidate: dict,
    prospect_name: str,
    role_str: str,
    message: str,
    followup_count: int,
    max_followups: int,
    voice_signature: dict,
    reasoning: str,
    memory_entry: dict | None,
) -> str:
    """Send a follow-up via email channel."""
    from ..ai.email_generator import generate_email_followup
    from ..services.channel_selector import _extract_email
    from ..services.unipile_email import (
        confirm_email_in_sent,
        email_send_known_denied,
        resolve_email_account_id,
    )

    email_account_id = await resolve_email_account_id(client)
    if not email_account_id:
        await client.close()
        return (
            "No email account connected for follow-up. "
            "Connect Gmail/Outlook with account(action='connect_email'). "
            "Do not use Mail.app."
        )

    # The same daily ceiling the first touch obeys. Follow-ups are the
    # highest-volume email path and were the one the cap missed entirely.
    # Checked before generation so a capped day costs no LLM calls.
    from ..linkedin.rate_limiter import can_send_email_now

    allowed, why = await can_send_email_now()
    if not allowed:
        await client.close()
        return f"⏸️ Email follow-up to {prospect_name} not sent — {why}."

    prospect_email = _extract_email(candidate)
    if not prospect_email:
        await client.close()
        return f"No email address for {prospect_name}. Cannot send email follow-up."

    # Get previous email subject from messages
    previous_messages = await db.get_messages_for_outreach(outreach_id)
    previous_subject = ""
    previous_body = ""
    for msg in previous_messages:
        if msg.get("role") == "sdr":
            text = msg.get("text", "")
            if text.startswith("[EMAIL] Subject: "):
                lines = text.split("\n\n", 1)
                previous_subject = lines[0].replace("[EMAIL] Subject: ", "")
                previous_body = lines[1] if len(lines) > 1 else ""
            break

    sender_profile = await db.get_setting("profile", {})
    try:
        email_result = await generate_email_followup(
            prospect={"name": prospect_name, **candidate},
            sender_profile=sender_profile,
            voice_signature=voice_signature,
            previous_subject=previous_subject,
            previous_body=previous_body,
            followup_count=followup_count,
        )
    except Exception as e:
        await client.close()
        return f"Email follow-up generation failed: {e}"

    subject = email_result["subject"]
    body = email_result["body"]
    if not body:
        await client.close()
        return f"Email follow-up draft for {prospect_name} failed quality checks and was not sent."

    try:
        result = await client.send_email(
            account_id=email_account_id,
            to_email=prospect_email,
            to_name=prospect_name,
            subject=subject,
            body=body,
        )
    except Exception as e:
        result = {"success": False, "error": f"Email follow-up send failed: {e}"}

    async def _record_followup_success() -> str:
        new_count = followup_count + 1
        await db.update_outreach(outreach_id, status="messaged", followup_count=new_count)
        await db.save_message(outreach_id, role="sdr", text=f"[EMAIL] Subject: {subject}\n\n{body}")
        await db.increment_usage("messages_sent")
        from ..linkedin.rate_limiter import increment_email_sent as _inc_email

        await _inc_email()

        if memory_entry:
            try:
                await db.save_outreach_memory(outreach_id, memory_entry)
            except Exception:
                pass

        await db.log_action(
            "email_followup_sent", outreach_id=outreach_id, result="success",
            details={
                "prospect": prospect_name,
                "followup_number": new_count,
                "channel": "email",
                "subject": subject,
            },
        )

        remaining = max_followups - new_count
        return (
            f"📧 Email follow-up #{new_count} sent to {prospect_name} ({role_str})\n"
            f"   Subject: {subject}\n"
            f'   Body: "{body[:120]}..."\n\n'
            f"Follow-ups remaining: {remaining}/{max_followups}"
        )

    try:
        if result.get("success"):
            return await _record_followup_success()

        error = result.get("error", "Unknown error")
        if email_send_known_denied(result):
            # Known miss — leave status alone so the caller can release
            # sending_followup back to connected and retry later.
            await db.log_action(
                "email_followup_failed", outreach_id=outreach_id, result="blocked",
                details={"error": error, "channel": "email"},
            )
            return f"Email follow-up failed for {prospect_name}: {error}"

        found = await confirm_email_in_sent(
            client, email_account_id, prospect_email, subject,
        )
        if found:
            return await _record_followup_success()

        # Unknown miss. Mark error so the caller's CAS release (status must
        # still be sending_followup) becomes a no-op and we do not resend.
        await db.update_outreach(
            outreach_id, status="error", last_attempt_error=error[:500],
        )
        await db.log_action(
            "email_followup_failed", outreach_id=outreach_id, result="error",
            details={"error": error, "channel": "email", "outcome": "unknown"},
        )
        return f"Email follow-up failed for {prospect_name}: {error}"
    finally:
        await client.close()
