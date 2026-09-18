"""Communication Strategist — AI-planned daily action plans per prospect.

Replaces the hardcoded warm-up sequence with dynamic, LLM-generated daily plans.
Runs once per local day at STRATEGIST_PLANNING_HOUR via JOB_DAILY_STRATEGY
(see engine._daily_strategy_due), with plan execution checked every 15 min
via JOB_EXECUTE_STRATEGY_PLANS.

Integration flow:
  Daily:
    run_daily_strategy() →
      _evaluate_yesterday_plans()     # feedback loop
      for each active campaign:
        _collect_prospect_context()   # gather data
        _batch_plan_actions()         # LLM call per batch of 50
        _store_daily_plans()          # persist to DB

  Every 15 min:
    execute_planned_actions(campaign_id) →
      read today's plans
      find next unexecuted action with right timing
      create scheduler_job → existing executor handles it
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any

from ..flags import flag_enabled
from ..constants import (
    STRATEGIST_AVAILABLE_ACTIONS,
    STRATEGIST_BATCH_SIZE,
    STRATEGIST_FEEDBACK_LOOKBACK_DAYS,
    STRATEGIST_FEEDBACK_TOP_N,
    STRATEGIST_MAX_ACTIONS_PER_DAY,
    STRATEGIST_MAX_PROSPECTS,
    JOB_ENGAGE,
    JOB_ENDORSE,
    JOB_FOLLOW,
    JOB_FOLLOWUP,
    JOB_INMAIL,
    JOB_INVITE,
    JOB_EMAIL_INVITE,
    JOB_PROFILE_VIEW,
    JOB_SEND_DM,
)
from ..db.async_bridge import run_db

logger = logging.getLogger(__name__)

# Statuses a human already owns. The LLM still proposes follow-ups here
# (Chris Morgan, 22–23 Aug 2026); fallback already emits skip_today.
_HUMAN_OWNED_STATUSES = frozenset({
    "replied", "hot_lead", "reverse_pitch",
    "skipped", "opted_out", "closed_happy", "closed_unhappy",
})
_AUTONOMOUS_TOUCH_ACTIONS = frozenset({
    "followup", "send_dm", "invite", "inmail", "email", "voice_memo",
    "profile_view", "follow", "endorse", "engage_comment", "engage_react",
})


def sanitize_human_owned_actions(status: str, actions: list[dict]) -> list[dict]:
    """Replace autonomous touches with skip_today once a human owns the thread.

    Fallback planning already does this for replied/messaged. The LLM path
    does not, and execute_planned_actions will schedule whatever was stored.
    """
    if status not in _HUMAN_OWNED_STATUSES:
        return actions
    if not any(a.get("action_type") in _AUTONOMOUS_TOUCH_ACTIONS for a in actions):
        return actions
    return [{
        "action_type": "skip_today",
        "priority": 1,
        "timing_preference": "anytime",
        "params": {},
        "rationale": "Active conversation: let human manage",
    }]


# ──────────────────────────────────────────────
# Main Entry Point (daily)
# ──────────────────────────────────────────────

async def run_daily_strategy() -> dict[str, Any]:
    """Main entry point. Runs once per day. Generates daily plans for all active campaigns."""
    summary: dict[str, Any] = {
        "plans_created": 0,
        "plans_evaluated": 0,
        "campaigns_processed": 0,
        "llm_calls": 0,
        "fallback_used": 0,
        "errors": [],
    }

    # Phase 1: Evaluate yesterday's plans (feedback loop)
    try:
        evaluated = await _evaluate_yesterday_plans()
        summary["plans_evaluated"] = evaluated
    except Exception as e:
        logger.error("Plan evaluation failed: %s", e)
        summary["errors"].append(f"evaluation: {e}")

    # Phase 2: Generate today's plans for all active autopilot campaigns
    from ..db.queries import list_campaigns

    campaigns = await run_db(list_campaigns, status="active")
    for campaign in campaigns:
        if campaign.get("mode") != "autopilot":
            continue

        campaign_id = campaign["id"]
        try:
            created = await _plan_campaign_prospects(campaign, summary)
            summary["plans_created"] += created
            summary["campaigns_processed"] += 1
        except Exception as e:
            logger.error("Strategy planning failed for campaign %s: %s", campaign_id, e)
            summary["errors"].append(f"campaign {campaign_id[:8]}: {e}")

    logger.info(
        "Daily strategy complete: %d plans, %d evaluated, %d campaigns, %d LLM calls",
        summary["plans_created"],
        summary["plans_evaluated"],
        summary["campaigns_processed"],
        summary["llm_calls"],
    )
    return summary


# ──────────────────────────────────────────────
# Campaign-Level Planning
# ──────────────────────────────────────────────

async def _plan_campaign_prospects(campaign: dict, summary: dict) -> int:
    """Generate daily plans for all prospects in a single campaign."""
    from ..db.strategist_queries import (
        get_feedback_data,
        get_prospects_needing_plans,
        save_daily_plan,
    )

    campaign_id = campaign["id"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Get prospects that need plans
    prospects = await run_db(get_prospects_needing_plans, campaign_id)
    if not prospects:
        return 0

    # Cap at STRATEGIST_MAX_PROSPECTS
    prospects = prospects[:STRATEGIST_MAX_PROSPECTS]

    # Resolve and persist each prospect's timezone once, from whatever
    # location text we hold; execution windows then use it.
    await _resolve_prospect_timezones(prospects)

    # Get feedback from recent plans
    feedback = await run_db(
        get_feedback_data, STRATEGIST_FEEDBACK_LOOKBACK_DAYS, STRATEGIST_FEEDBACK_TOP_N,
        campaign_id,
    )

    # Prepare campaign context (ICP, voice) — once per campaign
    campaign_context = await _build_campaign_context(campaign)

    # Detect DM-only campaign (connections-only, no invitations)
    try:
        config = json.loads(campaign.get("config_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        config = {}
    dm_only = not flag_enabled(config, "enable_invitations")

    # Batch processing
    created = 0
    for i in range(0, len(prospects), STRATEGIST_BATCH_SIZE):
        batch = prospects[i : i + STRATEGIST_BATCH_SIZE]
        try:
            plans = await _batch_plan_actions(batch, campaign_context, feedback, dm_only=dm_only)
            summary["llm_calls"] += 1

            status_by_oid = {p.get("outreach_id"): p.get("status", "") for p in batch}
            for plan in plans:
                outreach_id = plan.get("outreach_id")
                if not outreach_id:
                    continue

                actions = plan.get("actions", [])

                # Validate: cap at STRATEGIST_MAX_ACTIONS_PER_DAY
                actions = actions[:STRATEGIST_MAX_ACTIONS_PER_DAY]

                # Validate action types
                actions = [
                    a for a in actions if a.get("action_type") in STRATEGIST_AVAILABLE_ACTIONS
                ]

                prospect_row = next(
                    (p for p in batch if p.get("outreach_id") == outreach_id),
                    {},
                )
                first_touch, email_ok = await _legal_channels(prospect_row, dm_only=dm_only)
                from .action_timeline import constrain_planned_actions
                actions = constrain_planned_actions(
                    actions,
                    status=status_by_oid.get(outreach_id, ""),
                    first_touch=first_touch,
                    email_allowed=email_ok,
                )

                if not actions:
                    actions = [
                        {
                            "action_type": "skip_today",
                            "priority": 1,
                            "timing_preference": "anytime",
                            "params": {},
                            "rationale": "LLM returned no valid actions",
                        }
                    ]

                actions = sanitize_human_owned_actions(
                    status_by_oid.get(outreach_id, ""), actions,
                )

                await run_db(save_daily_plan, outreach_id, campaign_id, today, actions, "llm")
                created += 1

        except Exception as e:
            logger.warning("LLM batch planning failed, falling back to heuristic: %s", e)
            # FALLBACK: Generate heuristic plans for this batch
            for prospect in batch:
                first_touch, email_ok = await _legal_channels(prospect, dm_only=dm_only)
                fallback_actions = _generate_fallback_plan(
                    prospect, dm_only=dm_only, first_touch=first_touch,
                    email_allowed=email_ok,
                )
                await run_db(
                    save_daily_plan,
                    prospect["outreach_id"],
                    campaign_id,
                    today,
                    fallback_actions,
                    "fallback",
                )
                created += 1
                summary["fallback_used"] += 1

    return created


# ──────────────────────────────────────────────
# LLM Batch Call
# ──────────────────────────────────────────────

async def _batch_plan_actions(
    prospects: list[dict],
    campaign_context: dict,
    feedback: dict,
    *,
    dm_only: bool = False,
) -> list[dict]:
    """Call LLM to generate daily plans for a batch of ~50 prospects."""
    from ..ai.strategist_prompts import (
        STRATEGIST_SYSTEM,
        STRATEGIST_BATCH_PROMPT,
        format_feedback_section,
        format_prospect_for_llm,
    )

    # Build the prompt
    prospects_section = "\n".join(format_prospect_for_llm(p) for p in prospects)
    feedback_section = format_feedback_section(feedback)

    # Add DM-only campaign context so the LLM plans send_dm instead of invite
    dm_only_section = ""
    if dm_only:
        dm_only_section = (
            "\n\n## IMPORTANT: DM-Only Campaign\n"
            "This is a connections-only campaign. All prospects are existing 1st-degree connections.\n"
            "- Use 'send_dm' instead of 'invite' for all pending/connected prospects\n"
            "- Do NOT plan 'invite' actions — they are already connected\n"
            "- Skip warm-up actions (profile_view, follow, endorse) — not needed for existing connections\n"
            "- Focus on send_dm and followup actions only"
        )

    prompt = STRATEGIST_BATCH_PROMPT.format(
        campaign_name=campaign_context.get("name", ""),
        icp_summary=campaign_context.get("icp_summary", ""),
        voice_summary=campaign_context.get("voice_summary", ""),
        feedback_section=feedback_section,
        prospect_count=len(prospects),
        prospects_section=prospects_section,
    ) + dm_only_section

    # Try backend LLM proxy
    from ..config import is_backend_mode, has_local_llm_key

    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client

        client = get_linkedin_client()
        try:
            result = await client.plan_daily_actions(prompt)
        finally:
            await client.close()
        return result.get("plans", [])

    # Fallback: local BYOK
    if has_local_llm_key():
        from ..ai.llm import LLMClient

        raw = await LLMClient().generate(prompt, system=STRATEGIST_SYSTEM, temperature=0.3)
        return _parse_plans_json(raw)

    # No LLM available — will be caught by caller and fall back to heuristic
    raise RuntimeError("No LLM available (no backend mode and no local API key)")


def _parse_plans_json(raw: str) -> list[dict]:
    """Parse LLM JSON response, with repair for markdown fences."""
    import re

    # Try direct parse
    try:
        parsed = json.loads(raw)
        return parsed.get("plans", [])
    except (json.JSONDecodeError, AttributeError):
        pass

    # Try extracting from markdown code blocks
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if match:
        try:
            parsed = json.loads(match.group(1))
            return parsed.get("plans", [])
        except (json.JSONDecodeError, AttributeError):
            pass

    logger.warning("Failed to parse strategist LLM output")
    return []


# ──────────────────────────────────────────────
# Heuristic Fallback
# ──────────────────────────────────────────────

def _generate_fallback_plan(
    prospect: dict,
    *,
    dm_only: bool = False,
    first_touch: str = "invite",
    email_allowed: bool = False,
) -> list[dict]:
    """Generate a heuristic plan when LLM is unavailable.

    Mirrors the existing hardcoded sequence logic but returns it as a plan.
    When *dm_only* is True (connections-only campaigns), plans send_dm instead
    of invite and skips warm-up — prospects are already connected.
    """
    status = prospect.get("status", "pending")
    engagement_count = prospect.get("engagement_count", 0)
    engagement_types = (prospect.get("engagement_types") or "").split(",")
    engagement_types = [t.strip() for t in engagement_types if t.strip()]

    actions: list[dict] = []

    if status in ("pending", "connected") and dm_only:
        # DM-only campaign: send DM directly, no warm-up needed
        actions.append({
            "action_type": "send_dm",
            "priority": 1,
            "timing_preference": "morning",
            "params": {},
            "rationale": "Core outreach: send DM to existing connection",
        })

    elif status == "pending":
        has_profile_view = "profile_view" in engagement_types or "view" in engagement_types
        has_follow = "follow" in engagement_types
        has_endorse = "endorse" in engagement_types
        has_engagement = any(t in engagement_types for t in ("comment", "react"))

        # Core action: always present — warm-up must never delay outreach.
        # timing_preference="anytime" ensures the invite is never deferred
        # to the next day when a warm-up action consumes the morning window.
        actions.append({
            "action_type": "send_dm" if first_touch == "dm" else first_touch,
            "priority": 2,
            "timing_preference": "anytime",
            "params": {},
            "rationale": f"Core outreach: {first_touch}",
        })

        # Optional warm-up: one action alongside (not before) core outreach
        warmup = None
        if not has_profile_view:
            warmup = {"action_type": "profile_view", "rationale": "Warm-up: profile view notification", "timing_preference": "morning"}
        elif not has_follow:
            warmup = {"action_type": "follow", "rationale": "Warm-up: follow notification", "timing_preference": "morning"}
        elif not has_endorse:
            warmup = {"action_type": "endorse", "rationale": "Warm-up: endorse skills", "timing_preference": "afternoon"}
        elif not has_engagement:
            warmup = {"action_type": "engage_comment", "rationale": "Warm-up: engage with post", "timing_preference": "afternoon"}

        if warmup:
            warmup.update({"priority": 1, "params": {}})
            actions.insert(0, warmup)

    elif status == "connected":
        actions.append({
            "action_type": "followup",
            "priority": 1,
            "timing_preference": "morning",
            "params": {},
            "rationale": "Connected prospect: follow-up DM",
        })

    elif status in ("replied", "messaged"):
        actions.append({
            "action_type": "skip_today",
            "priority": 1,
            "timing_preference": "anytime",
            "params": {},
            "rationale": "Active conversation: let human manage",
        })

    elif status == "invited":
        if email_allowed:
            actions.append({
                "action_type": "email",
                "priority": 1,
                "timing_preference": "anytime",
                "params": {},
                "rationale": "Quiet LinkedIn invite — continue the story by email",
            })
        elif engagement_count < 3:
            actions.append({
                "action_type": "engage_react",
                "priority": 1,
                "timing_preference": "afternoon",
                "params": {},
                "rationale": "Keep warm while waiting for invite acceptance",
            })
        else:
            actions.append({
                "action_type": "skip_today",
                "priority": 1,
                "timing_preference": "anytime",
                "params": {},
                "rationale": "Enough engagement, waiting for acceptance",
            })

    if not actions:
        actions.append({
            "action_type": "skip_today",
            "priority": 1,
            "timing_preference": "anytime",
            "params": {},
            "rationale": "No action needed in current state",
        })

    return actions


# ──────────────────────────────────────────────
# Plan Execution (every 15 min)
# ──────────────────────────────────────────────

async def execute_planned_actions(campaign_id: str) -> int:
    """Read today's plans and create scheduler_jobs for unexecuted actions.

    Called every 15 min by the engine. Returns count of jobs created.
    """
    try:
        from ..services.strategist_replan import replan_signaled_plans
        await replan_signaled_plans(campaign_id)
    except Exception as e:
        logger.debug("Strategist replan skipped: %s", e)

    from .coordinator import coordinator_blocks_send
    if await run_db(coordinator_blocks_send, campaign_id):
        logger.info("coordinator hold — not enqueueing planned actions for %s", campaign_id[:8])
        return 0

    from ..db.queries import get_pending_job_count, get_pending_outreach_job
    from ..db.strategist_queries import get_campaign_daily_plans, mark_action_skipped

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    plans = await run_db(get_campaign_daily_plans, campaign_id, today)

    now = int(time.time())
    jobs_created = 0

    for plan in plans:
        outreach_id = plan["outreach_id"]
        # The prospect's own hour when we know their zone; the user's otherwise.
        current_hour = _local_hour(now, plan.get("prospect_timezone") or "")
        planned = sanitize_human_owned_actions(
            plan.get("outreach_status", ""), plan["planned_actions"],
        )
        executed = plan["executed_actions"]
        executed_types = {e["action_type"] for e in executed}

        for action in planned:
            action_type = action.get("action_type")
            if action_type in executed_types:
                continue  # Already done or skipped — no re-logging
            if action_type == "skip_today":
                continue  # Intentional — no logging needed

            # Check timing preference
            timing = action.get("timing_preference", "anytime")
            if not _is_right_timing(timing, current_hour):
                continue  # Will be retried in the right window — don't log as skip

            # Map strategy action_type to job_type
            job_type = _map_action_to_job_type(action_type)
            if not job_type:
                await run_db(mark_action_skipped, outreach_id, today, action_type,
                             f"unmapped_action_type: {action_type}")
                continue

            # Dedup: skip if this specific outreach already has a pending/running job
            if await run_db(get_pending_outreach_job, outreach_id, job_type):
                await run_db(mark_action_skipped, outreach_id, today, action_type,
                             f"duplicate_job: pending {job_type} already exists")
                continue

            # Dedup: don't flood the campaign queue
            pending = await run_db(get_pending_job_count, campaign_id, job_type)
            if pending > 2:
                await run_db(mark_action_skipped, outreach_id, today, action_type,
                             f"queue_full: {pending} pending {job_type} jobs")
                continue

            from ..scheduler.enqueue_gate import can_enqueue_outreach
            from ..scheduler import planner as planner_mod

            ok, reason, _details = await can_enqueue_outreach(
                job_type, campaign_id, outreach_id,
            )
            if not ok:
                await run_db(
                    mark_action_skipped, outreach_id, today, action_type, reason,
                )
                continue

            # Create scheduler job with randomized delay (5-20 min)
            delay = random.randint(300, 1200)
            job_id = await run_db(
                planner_mod._create_gated_job,
                campaign_id=campaign_id,
                job_type=job_type,
                scheduled_at=now + delay + (jobs_created * random.randint(180, 420)),
                outreach_id=outreach_id,
            )
            if not job_id:
                await run_db(
                    mark_action_skipped, outreach_id, today, action_type,
                    "gated_create_refused",
                )
                continue
            jobs_created += 1

            # Core outreach (invite, send_dm, followup, etc.) is never gated
            # behind warm-up — keep iterating so both warm-up AND core get
            # scheduled in the same cycle.  Warm-up actions still pace at
            # one-per-cycle per prospect.
            _CORE_JOB_TYPES = {
                "invite", "inmail", "email_invite", "send_dm", "followup",
                "auto_reply", "discover",
            }
            if job_type not in _CORE_JOB_TYPES:
                break  # One warm-up action at a time per prospect

    return jobs_created


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _map_action_to_job_type(action_type: str) -> str | None:
    """Map strategy action type to existing scheduler job type."""
    mapping = {
        "profile_view": JOB_PROFILE_VIEW,
        "follow": JOB_FOLLOW,
        "endorse": JOB_ENDORSE,
        "engage_comment": JOB_ENGAGE,
        "engage_react": JOB_ENGAGE,
        "invite": JOB_INVITE,
        "inmail": JOB_INMAIL,
        "email": JOB_EMAIL_INVITE,
        "send_dm": JOB_SEND_DM,
        "followup": JOB_FOLLOWUP,
        "voice_memo": JOB_FOLLOWUP,
    }
    return mapping.get(action_type)


def _local_hour(now: float | None = None, tz_name: str = "") -> int:
    """Hour of day in ``tz_name`` when given and valid, else the user's
    timezone (config, else the machine's, else UTC).

    Plans carry the prospect's inferred zone (contacts.timezone); when that is
    unknown the user's own working timezone is the proxy. The old UTC check
    put a "morning" action at 3 AM for a US user.
    """
    from ..config import get_timezone

    ts = time.time() if now is None else now
    from zoneinfo import ZoneInfo
    for candidate in (tz_name, get_timezone()):
        if not candidate:
            continue
        try:
            return datetime.fromtimestamp(ts, ZoneInfo(candidate)).hour
        except Exception:
            continue
    return datetime.fromtimestamp(ts, timezone.utc).hour


async def _resolve_prospect_timezones(prospects: list[dict]) -> int:
    """Fill contacts.timezone for prospects that lack one. Returns count set.

    Location hints, most reliable first: the contact's own profile_json,
    the connections row, the global directory. Unrecognised locations stay
    empty and fall back to the user's timezone at execution time.
    """
    from ..db.strategist_queries import set_contact_timezone
    from .prospect_timezone import infer_from_hints

    resolved = 0
    for p in prospects:
        if p.get("prospect_timezone"):
            continue
        profile_location = ""
        try:
            pj = json.loads(p.get("profile_json") or "{}")
            if isinstance(pj, dict):
                profile_location = str(pj.get("location") or pj.get("geo") or "")
        except (ValueError, TypeError):
            pass
        tz_name = infer_from_hints(
            profile_location, p.get("connection_location"), p.get("directory_location"),
        )
        if not tz_name or not p.get("contact_id"):
            continue
        try:
            await run_db(set_contact_timezone, p["contact_id"], tz_name)
            p["prospect_timezone"] = tz_name
            resolved += 1
        except Exception as e:
            logger.debug("Timezone persist failed for %s: %s", p.get("contact_id"), e)
    return resolved


# Window starts, local hours. A preference is "not before", never "only
# during": an action whose window has passed still runs later the same day
# (working hours and the enqueue gate bound the evening), instead of being
# silently dropped when the plan was written after its window.
_TIMING_STARTS = {"morning": 8, "afternoon": 12, "evening": 16}


def _is_right_timing(timing: str, current_local_hour: int) -> bool:
    """Check if the current local hour has reached the timing preference."""
    start = _TIMING_STARTS.get(timing)
    if start is None:
        return True  # "anytime" or unknown
    return current_local_hour >= start


async def _legal_channels(prospect: dict, *, dm_only: bool) -> tuple[str, bool]:
    """First-touch picker + email legality for one prospect row."""
    from .action_timeline import email_is_legal
    from .outreach_channel import choose_first_touch
    from ..tier import get_caps

    try:
        profile = json.loads(prospect.get("profile_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        profile = {}
    if not isinstance(profile, dict):
        profile = {}
    status = prospect.get("status") or "pending"
    is_first = bool(prospect.get("is_first_degree")) or status in {
        "connected", "messaged",
    }
    if dm_only:
        first_touch = "dm"
    else:
        caps = await get_caps()
        first_touch = choose_first_touch(
            is_first_degree=is_first,
            can_send_credit_inmail=caps.can_send_credit_inmail,
            is_open_profile=bool(profile.get("is_open_profile")),
            has_provider_id=bool(profile.get("provider_id")),
        )
    from .channel_selector import _extract_email
    has_address = bool(_extract_email(prospect) or _extract_email(profile))
    from ..db.queries import get_setting
    mailbox = bool(await run_db(get_setting, "email_account_id", "") or "")
    invited_at = prospect.get("invited_at")
    analysis = {}
    try:
        raw_analysis = prospect.get("analysis_json") or "{}"
        analysis = json.loads(raw_analysis) if isinstance(raw_analysis, str) else raw_analysis
    except (TypeError, ValueError):
        analysis = {}
    if not isinstance(analysis, dict):
        analysis = {}
    referral_handoff = bool(
        analysis.get("referral_email_handoff")
        or (analysis.get("signal_context") or {}).get("signal_angle") == "warm_referral"
    )
    email_ok = email_is_legal(
        has_mailbox=mailbox,
        has_address=has_address,
        enable_email=True,
        status=status,
        linkedin_first_touch_attempted=bool(invited_at) or status in {
            "invited", "connected", "messaged", "withdrawn", "expired",
        },
        linkedin_unreachable=status in {"withdrawn", "expired"} or (
            status == "pending" and not profile.get("provider_id")
        ),
        has_replied=status in {"replied", "hot_lead"},
        referral_handoff=referral_handoff,
        is_first_degree=is_first,
        invited_at=int(invited_at) if invited_at else None,
        now=int(time.time()),
    )
    return first_touch, email_ok


async def _evaluate_yesterday_plans() -> int:
    """Score yesterday's plans based on outcomes. Returns count scored."""
    from ..db.strategist_queries import score_yesterday_plans

    return await run_db(score_yesterday_plans)


async def _build_campaign_context(campaign: dict) -> dict:
    """Build concise campaign context for the LLM prompt."""
    icp_summary = ""
    try:
        icp = json.loads(campaign.get("icp_json") or "{}")
        if isinstance(icp, dict):
            personas = icp.get("personas", [])
            if personas:
                icp_summary = "; ".join(
                    f"{p.get('title', '')} at {p.get('company_type', '')}" for p in personas[:3]
                )
    except Exception:
        pass

    from ..db.queries import get_setting

    # voice_signature is rewritten at runtime (switch_account, setup_profile),
    # so it must be read fresh — no module cache. run_db keeps the sync read
    # off the event loop thread, where get_db() raises.
    voice = await run_db(get_setting, "voice_signature", {})
    voice_summary = ""
    if isinstance(voice, dict):
        voice_summary = voice.get("tone", "professional") + ", " + voice.get("style", "")

    return {
        "name": campaign.get("name", ""),
        "icp_summary": icp_summary[:200],
        "voice_summary": voice_summary[:100],
    }
