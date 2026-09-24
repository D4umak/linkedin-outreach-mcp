"""Tool: send_inmail — voice-matched InMail to a non-connection.

InMail is the escalation path for prospects who never accepted an invitation.
Unipile already knows how to send one; this tool is the missing layer above
it: the same intent → brief → generate → improve → validate → fix stack as
invitations, plus credit and connection guards so a 1st-degree or a spent
credit never reaches the API. A pending invitation to the same person is
intentional (escalation), not a blocker. Owner rule: LinkedIn sends only
go through HeyLead tools — there is no sidecar script for this.
"""

from __future__ import annotations

import json
import logging
import re

from ..ai.brief_builder import build_message_brief
from ..ai.intent import resolve_intent, select_prompt
from ..ai.llm import LLMClient
from ..ai.llm_validator import llm_validate
from ..ai.message_fixer import fix_message
from ..ai.message_improver import improve_message
from ..ai.message_validator import validate_message
from ..ai.prompt_loader import (
    build_context_block,
    get_prompt_temperature,
    load_expertise_map,
    render_prompt,
)
from ..db import aio as adb
from ..db.async_bridge import run_db
from ..linkedin import UnipileError, get_account_id, get_linkedin_client
from ..linkedin.rate_limiter import check_daily_cap, update_limits_after_send
from ..services.channel_selector import CHANNEL_LINKEDIN
from ..services.connection_sync import is_first_degree, is_first_degree_by_public_id
from ..ops_log import classify_unipile_error_type, log_outbound_send
from .generate_send import _build_campaign_context, try_claim_outreach

# InMail escalates prospects who never accepted, so a live invitation is a
# valid starting point — unlike the DM path, which requires a connection.
_INMAIL_CLAIMABLE_SQL = "('pending', 'invited')"

logger = logging.getLogger(__name__)

# Unipile's refusal when the account holds no InMail entitlement. Shared with
# the resolver's downgrade corroboration — one spelling of the marker.
from ..services.search_account_resolver import _NO_ENTITLEMENT_MARKERS
from ..tier import INMAIL_CAPABILITY_KEY, INMAIL_REFUSED, INMAIL_WORKS


async def _record_inmail_capability(verdict: str) -> None:
    """Persist what an actual send proved about InMail capability."""
    from ..db.queries import save_setting

    await run_db(save_setting, INMAIL_CAPABILITY_KEY, verdict)
    from ..tier import _caps_cache

    _caps_cache.clear()  # the TTL cache must not serve the pre-verdict answer


INMAIL_SUBJECT_MAX = 200
INMAIL_BODY_MAX = 1900

_SUBJECT_BODY_RE = re.compile(
    r"(?is)^\s*subject\s*:\s*(.+?)(?:\n+\s*body\s*:\s*|\n{2,})(.*)\s*$",
)


def _parse_inmail_copy(raw: str) -> tuple[str, str]:
    """Split LLM output into (subject, body). Subject is never hard-coded."""
    text = (raw or "").strip().strip('"').strip("'").strip()
    match = _SUBJECT_BODY_RE.match(text)
    if match:
        subject, body = match.group(1).strip(), match.group(2).strip()
    else:
        lines = text.split("\n", 1)
        subject = lines[0].replace("SUBJECT:", "").replace("Subject:", "").strip()
        body = lines[1].strip() if len(lines) > 1 else ""
    subject = subject.split("\n", 1)[0].strip().strip('"').strip("'")
    return subject[:INMAIL_SUBJECT_MAX], body.strip()


async def _already_sent(outreach_id: str) -> bool:
    def _query(oid: str) -> bool:
        from ..db.schema import get_db
        db = get_db()
        row = db.execute(
            """SELECT 1 FROM actions_log
               WHERE outreach_id = ? AND action_type = 'inmail_sent' AND result = 'success'
               LIMIT 1""",
            (oid,),
        ).fetchone()
        db.close()
        return row is not None
    return await run_db(_query, outreach_id)


async def run_send_inmail(campaign_id: str = "", outreach_id: str = "") -> str:
    """Generate and send an InMail to a non-connection.

    A pending invitation on the same outreach is allowed — this is how
    buyer campaigns escalate founders who never accepted.
    """
    setup_done = await adb.get_setting("setup_complete", False)
    if not setup_done:
        return "Setup required before sending InMail. Run setup_profile first."

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected. Run setup_profile first."

    if not outreach_id:
        campaign, err = await adb.find_active_campaign(campaign_id)
        if not campaign:
            return f"{err}"
        return (
            "outreach_id is required for InMail. Pick a specific prospect "
            "(pending invitation is fine — that is the escalation)."
        )

    row = await adb.get_outreach_with_contact(outreach_id)
    if not row:
        return f"Outreach not found: {outreach_id}"

    from ..services.own_identity import refuse_own_account_target
    own_skip = await refuse_own_account_target(
        row, outreach_id=outreach_id, campaign_id=row.get("campaign_id") or "",
    )
    if own_skip:
        return own_skip

    campaign = await adb.get_campaign(row["campaign_id"])
    if not campaign:
        return "Campaign not found for this outreach."
    if campaign_id and campaign["id"] != campaign_id:
        return "outreach_id does not belong to that campaign."
    if campaign.get("status") == "paused":
        return f"Campaign '{campaign['name']}' is paused. Resume it before sending InMail."

    from ..services.project_brief import refuse_without_project_brief
    missing = refuse_without_project_brief(campaign)
    if missing:
        return missing

    try:
        campaign_cfg = json.loads(campaign.get("config_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        campaign_cfg = {}
    from ..services.fit_gate import FIT_SKIP_ERROR, should_fit_skip
    should_skip, fit, threshold = await run_db(
        should_fit_skip,
        row, campaign_cfg,
        outreach_id=outreach_id,
        outreach_status=row.get("status") or "",
    )
    if should_skip:
        prospect_name = row.get("name") or "Unknown"
        await adb.update_outreach(
            outreach_id, status="skipped", last_attempt_error=FIT_SKIP_ERROR,
        )
        await adb.log_action(
            "fit_score_below_threshold",
            outreach_id=outreach_id,
            result="skipped",
            details={"fit_score": fit, "threshold": threshold},
        )
        return (
            f"Skipped {prospect_name} — fit score {fit:.2f} below threshold {threshold:.2f}."
        )

    prospect_name = row.get("name") or "Unknown"
    from ..linkedin.profile_normalize import prospect_data_from_contact
    prospect_data = prospect_data_from_contact(row)

    provider_id = (prospect_data.get("provider_id") or "").strip()
    public_id = (row.get("linkedin_id") or prospect_data.get("public_id") or "").strip()

    if not provider_id:
        from ..ops_log import record_channel_skip
        await record_channel_skip(
            "inmail_skipped",
            outreach_id=outreach_id,
            campaign_id=campaign["id"],
            skip_reason="missing_provider_id",
            details={"prospect": prospect_name},
        )
        return (
            f"Cannot send InMail to {prospect_name}: missing provider_id. "
            "InMail needs the LinkedIn provider_id (ACoAAA…), not a public slug."
        )

    # Inverse of the DM path: InMail is for non-connections. Same local
    # connections table `_check_is_first_degree` reads (is_first_degree /
    # is_first_degree_by_public_id). Pending invitations are not connections.
    if await run_db(is_first_degree, account_id, provider_id) or (
        public_id and await run_db(is_first_degree_by_public_id, account_id, public_id)
    ):
        from ..ops_log import record_channel_skip
        await record_channel_skip(
            "inmail_skipped",
            outreach_id=outreach_id,
            campaign_id=campaign["id"],
            skip_reason="already_connected",
            details={"prospect": prospect_name},
        )
        # With exclude_connections on, "use a DM instead" is exactly the
        # redirection the setting exists to stop (9 Sep 2026): the
        # row is parked, not handed to the DM path.
        from ..services.connection_sync import (
            is_excluded_connection_outreach,
            skip_excluded_connection,
        )
        if await is_excluded_connection_outreach(campaign, row):
            return await skip_excluded_connection(
                outreach_id, campaign.get("id", ""), prospect_name,
                where="send_inmail",
            )
        return (
            f"{prospect_name} is a 1st-degree connection. InMail is for "
            "non-connections — use send_message(action='followup') to DM them."
        )

    if await _already_sent(outreach_id):
        from ..ops_log import record_channel_skip
        await record_channel_skip(
            "inmail_skipped",
            outreach_id=outreach_id,
            campaign_id=campaign["id"],
            skip_reason="already_sent",
            details={"prospect": prospect_name},
        )
        return (
            f"⏭️ InMail already sent to {prospect_name} on this outreach. "
            "Skipping (idempotent)."
        )

    # The actions_log check above cannot stand alone: the row it looks for is
    # written after the send, and generation sits in between. Claim the
    # outreach before generating so a concurrent caller loses the CAS rather
    # than spending a second metered InMail credit.
    orig_status, claimed = await run_db(
        try_claim_outreach, outreach_id, _INMAIL_CLAIMABLE_SQL
    )
    if not claimed:
        from ..ops_log import record_channel_skip
        await record_channel_skip(
            "inmail_skipped",
            outreach_id=outreach_id,
            campaign_id=campaign["id"],
            skip_reason="claim_lost",
            details={"prospect": prospect_name},
        )
        return (
            f"⏭️ InMail to {prospect_name} is already being sent by another job. "
            "Skipping (idempotent)."
        )

    claim_resolved = False

    async def _release_claim() -> None:
        await adb.update_outreach(outreach_id, status=orig_status)

    # Re-check under the claim. The check above may have been answered before a
    # concurrent winner wrote its actions_log row, and 'invited' — the status
    # that winner leaves behind — is itself claimable, so a stale answer would
    # otherwise buy a second metered credit.
    if await _already_sent(outreach_id):
        from ..ops_log import record_channel_skip
        await _release_claim()
        await record_channel_skip(
            "inmail_skipped",
            outreach_id=outreach_id,
            campaign_id=campaign["id"],
            skip_reason="already_sent",
            details={"prospect": prospect_name},
        )
        return (
            f"⏭️ InMail already sent to {prospect_name} on this outreach. "
            "Skipping (idempotent)."
        )

    try:
        client = get_linkedin_client()
    except UnipileError as e:
        await _release_claim()
        return f"{e}"
    except Exception as e:
        # get_linkedin_client() reads config.json, so a malformed or unreadable
        # file raises a JSON/OS error rather than UnipileError. Letting that
        # escape strands the claim: the row stays 'sending' and later runs skip
        # it as in flight until the 10-minute recovery pass. The wording lands
        # in the executor's permanent-skip bucket ("setup required") — no retry
        # can parse a broken config file.
        logger.error("InMail client unavailable: %s", e)
        await _release_claim()
        return (
            f"Setup required before sending InMail to {prospect_name}: "
            f"LinkedIn client unavailable ({str(e)[:200]})."
        )

    # Open Profile members accept InMail at zero credit cost (LinkedIn bills
    # nothing; Unipile's separate ~800/mo pool applies), so a spent balance —
    # or a free tier that never had one — must not refuse them. Only the
    # credits==0 gate is bypassed: 1st-degree refusal, the CAS claim, the
    # shared 8/day cap and the inmail_sent marker all still apply.
    flagged_open = bool(prospect_data.get("is_open_profile"))
    is_open_profile = flagged_open
    try:
        live = await client.get_profile(account_id, provider_id)
        if isinstance(live, dict) and "is_open_profile" in live:
            is_open_profile = bool(live.get("is_open_profile"))
            prospect_data["is_open_profile"] = is_open_profile
    except Exception as e:
        logger.debug("Open Profile refresh failed for %s: %s", prospect_name, e)

    if flagged_open and not is_open_profile:
        await adb.log_action(
            "inmail_unreachable",
            outreach_id=outreach_id,
            campaign_id=campaign["id"],
            result="skipped",
            details={"prospect": prospect_name, "reason": "stale_open_profile"},
        )
        await _release_claim()
        await client.close()
        return (
            f"InMail skipped for {prospect_name} — Open Profile flag was stale. "
            f"Invite can follow."
        )

    try:
        if is_open_profile:
            # The balance cannot change the outcome for an Open Profile send,
            # so skip the round trip — it sits inside the claim window.
            credits = -1
        else:
            inmail_data = await client.get_inmail_balance(account_id)
            try:
                credits = int(inmail_data.get("credits", -1))
            except (TypeError, ValueError):
                credits = -1
            # -1 means the balance endpoint is unavailable; only a reported 0 refuses.
            if credits == 0:
                return (
                    f"No InMail credits remaining. Sales Navigator Core is "
                    f"50/month. Wait for the reset before sending to {prospect_name}."
                )

        # Only a RECORDED refusal short-circuits here — LinkedIn has already
        # answered, so paying to generate a message it will decline is waste.
        # A merely unknown or absent entitlement does not block: an explicit
        # send_message(action='inmail') is how the question gets answered, and
        # the planner's own capability gate keeps the automatic escalation from
        # queueing these. Open Profile sends bypass credits entirely.
        from ..db.queries import get_setting as _get_setting
        verdict = str(await run_db(_get_setting, INMAIL_CAPABILITY_KEY, "") or "")
        if verdict == INMAIL_REFUSED and not is_open_profile:
            return (
                f"InMail is not available on this LinkedIn account — LinkedIn "
                f"refused a previous send on entitlement grounds. "
                f"Skipping (idempotent) for {prospect_name}."
            )

        can_send, current, cap, _block = await check_daily_cap("inmail")
        if not can_send:
            return (
                f"InMail sending paused: daily cap reached ({current}/{cap}). "
                "This quota is separate from invitations."
            )

        campaign_config = json.loads(campaign.get("config_json") or "{}")
        icp_data = json.loads(campaign.get("icp_json") or "{}")
        campaign_ctx = await adb.get_campaign_context(campaign["id"])
        campaign_context = _build_campaign_context(campaign_config, icp_data)
        campaign_intent = resolve_intent(campaign_config)

        if not prospect_data.get("name"):
            prospect_data["name"] = prospect_name
        prospect_data.setdefault("title", row.get("title") or "")
        prospect_data.setdefault("company", row.get("company") or "")

        analysis = None
        try:
            cached = await adb.get_contact_analysis(row["contact_id"])
            from ..ai.prospect_analyzer import is_data_gap_analysis
            if (
                cached
                and is_data_gap_analysis(cached)
                and (prospect_data.get("title") or prospect_data.get("company"))
            ):
                cached = None
            if cached:
                analysis = cached
        except Exception:
            pass

        # Rebuild signal_context from this outreach's trigger signal — the
        # contact-level cache may describe a different, later-scored post.
        try:
            trigger_signal_id = (row.get("signal_id") or "").strip()
            if trigger_signal_id:
                from ..db.signal_queries import get_signal
                from ..services.signal_activator import apply_trigger_signal_context

                trigger_signal = await run_db(get_signal, trigger_signal_id)
                if trigger_signal:
                    analysis = apply_trigger_signal_context(analysis, trigger_signal)
        except Exception as e:
            logger.debug("Trigger signal context rebuild failed (non-critical): %s", e)

        message_brief = build_message_brief(
            intent=campaign_intent,
            campaign_config=campaign_config,
            campaign_ctx=campaign_ctx,
            prospect=prospect_data,
            icp_data=icp_data,
            analysis=analysis,
        )

        sender_profile = await adb.get_setting("profile", {}) or {}
        voice_signature = await adb.get_setting("voice_signature", {}) or {}

        prompt_name = select_prompt("outreach_inmail", campaign_intent)
        system_name = select_prompt("outreach_system", campaign_intent)
        ctx = build_context_block(
            channel="inmail",
            sender=sender_profile,
            prospect=prospect_data,
            campaign_config=campaign_context,
            voice=voice_signature,
            campaign_context=campaign_ctx,
            analysis=analysis,
            max_chars=INMAIL_BODY_MAX,
            brief=message_brief,
            expertise_map=await load_expertise_map(),
        )
        system = render_prompt(system_name, ctx)
        prompt = render_prompt(prompt_name, ctx)
        from ..services.action_timeline import (
            action_timeline_text,
            append_action_timeline,
        )
        prompt = append_action_timeline(
            prompt, await action_timeline_text(outreach_id),
        )

        try:
            raw = await LLMClient().generate(
                prompt,
                system=system,
                temperature=get_prompt_temperature(prompt_name),
            )
        except Exception as e:
            logger.error("InMail generation failed: %s", e)
            # Parks the candidate for 24h (the fallback query's attempt
            # window keys on any inmail_sent row) instead of re-picking it
            # next tick for another LLM lap.
            await adb.log_action(
                "inmail_sent",
                outreach_id=outreach_id,
                campaign_id=campaign["id"],
                result="failed",
                details={"prospect": prospect_name, "error": str(e)[:200]},
            )
            return f"Failed to generate InMail: {e}"

        subject, body = _parse_inmail_copy(raw)
        if not subject or not body:
            return (
                f"Generated InMail for {prospect_name} was missing a subject or body. "
                "Nothing was sent."
            )

        try:
            body = await improve_message(
                draft=body,
                voice_signature=voice_signature,
                message_type="inmail",
                max_chars=INMAIL_BODY_MAX,
                intent=campaign_intent,
                brief=message_brief,
            )
        except Exception as e:
            logger.warning("InMail improve stage failed, using draft: %s", e)

        validation = validate_message(body, voice_signature, INMAIL_BODY_MAX)
        if validation.is_valid:
            try:
                llm_result = await llm_validate(
                    message=body,
                    history=[],
                    company=sender_profile.get("company", ""),
                    message_type="inmail",
                    prospect_company=prospect_data.get("company", ""),
                    max_chars=INMAIL_BODY_MAX,
                    intent=campaign_intent,
                )
                if not llm_result.is_valid:
                    validation.issues.extend(llm_result.issues)
                    validation.is_valid = False
            except Exception as e:
                logger.warning("InMail LLM validation skipped: %s", e)

        if not validation.is_valid:
            try:
                body = await fix_message(
                    message=body,
                    issues=validation.issues,
                    voice_signature=voice_signature,
                    message_type="inmail",
                    max_chars=INMAIL_BODY_MAX,
                    intent=campaign_intent,
                    brief=message_brief,
                )
                validation = validate_message(body, voice_signature, INMAIL_BODY_MAX)
            except Exception as e:
                logger.warning("InMail fix stage failed: %s", e)

        if not validation.is_valid:
            issues_text = "\n".join(f"  - {issue}" for issue in validation.issues)
            await adb.log_action(
                "inmail_sent",
                outreach_id=outreach_id,
                campaign_id=campaign["id"],
                result="skipped",
                details={
                    "prospect": prospect_name,
                    "issues": validation.issues,
                    "reason": "salesy_unfixed",
                },
            )
            return (
                f"InMail for {prospect_name} skipped after validation "
                f"(invite can follow).\n{issues_text}"
            )

        from ..ops_log import log_outbound_send
        log_outbound_send(
            "attempt",
            outreach_id=outreach_id,
            campaign_id=campaign["id"],
            channel="inmail",
            step_index="inmail",
            text=body,
            provider_id=provider_id,
            prompt_name=prompt_name,
        )
        result = await client.send_inmail(
            account_id=account_id,
            provider_id=provider_id,
            subject=subject,
            body=body,
        )
        log_outbound_send(
            "result",
            outreach_id=outreach_id,
            campaign_id=campaign["id"],
            channel="inmail",
            step_index="inmail",
            text=body,
            provider_id=provider_id,
            success=bool(result.get("success")),
            unipile_id=result.get("chat_id") or "",
            error_type=classify_unipile_error_type(result.get("error")) or None,
        )
        await update_limits_after_send(blocked=result.get("blocked", False))

        if result.get("success"):
            if is_open_profile:
                remaining_after = credits
            else:
                remaining_after = max(credits - 1, 0) if credits > 0 else credits
            logger.info(
                "InMail sent to %s; credits remaining ~%s",
                prospect_name, remaining_after,
            )

            # The success marker must land BEFORE the status write below:
            # 'invited' is inside the claimable set, so the moment it is
            # written a concurrent job can win the CAS — and both
            # _already_sent checks only protect it if this row exists.
            await _record_inmail_capability(INMAIL_WORKS)
            await adb.log_action(
                "inmail_sent",
                outreach_id=outreach_id,
                campaign_id=campaign["id"],
                result="success",
                details={
                    "prospect": prospect_name,
                    "subject": subject,
                    "body_length": len(body),
                    "credits_remaining": credits,
                    "intent": campaign_intent,
                    "prompt": prompt_name,
                    "system_prompt": system_name,
                    "open_profile": is_open_profile,
                },
            )
            await adb.update_outreach(
                outreach_id, status="invited", channel=CHANNEL_LINKEDIN,
                last_attempt_error=None,
            )
            claim_resolved = True
            await adb.save_message(
                outreach_id, role="sdr",
                text=f"[INMAIL] Subject: {subject}\n\n{body}",
            )
            # The scheduler executor maps success on the leading ✅ — keep it.
            open_note = " (Open Profile — no credit consumed)" if is_open_profile else ""
            return (
                f"✅ InMail sent to {prospect_name}{open_note}\n"
                f"   Subject: {subject}\n"
                f'   "{body[:180]}{"…" if len(body) > 180 else ""}"\n'
                f"   Credits remaining: {'n/a' if is_open_profile else remaining_after}"
            )

        error = result.get("error") or "Unknown error"
        # LinkedIn answering "feature not subscribed" settles a question no
        # endpoint does: whether this account can send a credit InMail at all.
        # Any other failure — a timeout, a 5xx — says nothing about
        # entitlement and must not be recorded as a verdict.
        if any(m in error.lower() for m in _NO_ENTITLEMENT_MARKERS):
            await _record_inmail_capability(INMAIL_REFUSED)
            from ..db.queries import cancel_stale_first_touch_jobs
            await run_db(cancel_stale_first_touch_jobs, refused=True)
            try:
                from ..services.search_account_resolver import redetect_tier
                await redetect_tier(client, account_id)
            except Exception as e:
                logger.debug("Redetect after InMail refusal failed: %s", e)
        unreachable = (
            "no_connection_with_recipient" in error.lower()
            or "not to be first degree" in error.lower()
        )
        await adb.log_action(
            "inmail_unreachable" if unreachable else "inmail_sent",
            outreach_id=outreach_id,
            campaign_id=campaign["id"],
            result="skipped" if unreachable else "failed",
            details={
                "prospect": prospect_name,
                "subject": subject,
                "body_length": len(body),
                "credits_remaining": credits,
                "intent": campaign_intent,
                "prompt": prompt_name,
                "error": error[:300],
            },
        )
        if unreachable:
            return (
                f"InMail unreachable for {prospect_name} — "
                f"not first-degree / closed Open Profile. Invite can follow."
            )
        return f"InMail failed for {prospect_name}: {error}"
    finally:
        # Every path out of this block that did not send must give the claim
        # back, including exceptions — otherwise the row sits in 'sending' and
        # later runs skip it as already in flight.
        if not claim_resolved:
            await _release_claim()
        await client.close()
