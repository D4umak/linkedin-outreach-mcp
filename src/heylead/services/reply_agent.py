"""Campaign-reply exception agent — tools, mode, hold persistence."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from ..ai.agent_loop import AgentBudget, AgentResult, run_agent_loop
from ..ai.reply_agent import (
    REPLY_AGENT_SYSTEM,
    booking_target_available,
    build_reply_agent_context,
)
from ..ai.schemas import AGENT_STEP
from ..constants import (
    HOLD_FOR_OPERATOR,
    REPLY_AGENT_MAX_STEPS,
    REPLY_AGENT_MAX_TOKENS,
    REPLY_AGENT_RESULT_CHARS,
    REPLY_AGENT_TIMEOUT_SECONDS,
)
from ..db.async_bridge import run_db
from ..db.queries import (
    get_campaign,
    get_contact_analysis,
    get_messages_for_outreach,
    get_prospect_timeline,
    log_action,
    update_outreach,
)
from ..flags import flag_enabled
from .agent_commons import async_commons_tools
from .coordinator import after_sibling_loop

logger = logging.getLogger(__name__)

ReplyAgentKind = Literal["continue", "hold", "skip", "book"]


@dataclass
class ReplyAgentOutcome:
    kind: ReplyAgentKind
    reason: str = ""
    message: str = ""


def reply_agent_mode(config: dict[str, Any] | None) -> Literal["off", "observe", "act"]:
    """Campaign config → off | observe | act. Unset defaults to act."""
    cfg = config or {}
    raw_mode = cfg.get("reply_agent_mode")
    if isinstance(raw_mode, str) and raw_mode.strip():
        text = raw_mode.strip().lower()
        if text in {"off", "observe", "act"}:
            return text  # type: ignore[return-value]
        if text in {"on", "true", "yes", "1"}:
            return "act"
        if text in {"false", "no", "0"}:
            return "off"
    if "enable_reply_agent" in cfg and cfg.get("enable_reply_agent") not in (None, ""):
        return "act" if flag_enabled(cfg, "enable_reply_agent", default=False) else "off"
    return "act"


def parse_operator_hold(next_action: str | None) -> dict[str, Any] | None:
    raw = (next_action or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != HOLD_FOR_OPERATOR:
        return None
    return payload


def is_fresh_hold(hold: dict[str, Any], last_prospect_ts: int) -> bool:
    try:
        held_ts = int(hold.get("message_ts") or 0)
    except (TypeError, ValueError):
        held_ts = 0
    return held_ts >= int(last_prospect_ts or 0)


def persist_operator_hold(
    outreach_id: str,
    reason: str,
    message_ts: int = 0,
    message_id: str = "",
    extra: dict[str, Any] | None = None,
    decision_id: str = "",
) -> dict[str, Any]:
    """Park the thread for a person. ``decision_id`` names the agent decision
    that parked it (heylead-api#1209); a hold from anything else clears it."""
    payload: dict[str, Any] = {
        "type": HOLD_FOR_OPERATOR,
        "reason": (reason or "needs a human")[:240],
        "decided_at": int(time.time()),
        "message_ts": int(message_ts or 0),
        "message_id": message_id or "",
    }
    if extra:
        for key in ("prospect_calendar_url", "calendar_url"):
            if extra.get(key):
                payload[key] = extra[key]
    update_outreach(outreach_id, next_action=json.dumps(payload), decision_id=decision_id or None)
    return payload


def apply_reply_agent_decision(
    result: AgentResult,
    *,
    mode: str,
    has_booking_target: bool,
) -> ReplyAgentOutcome:
    """Map a kernel decision onto continue / hold / skip. Does not persist."""
    decision = result.decision
    reason = result.reason or decision

    if decision == "hold":
        return ReplyAgentOutcome(
            kind="hold",
            reason=reason,
            message=f"Held for operator — {reason}. No reply will be sent.",
        )
    if decision == "skip":
        return ReplyAgentOutcome(
            kind="skip",
            reason=reason,
            message=f"Reply agent skipped this job — {reason}.",
        )
    if decision == "book":
        if mode == "act" and not has_booking_target:
            return ReplyAgentOutcome(
                kind="hold",
                reason=reason or "book requested without a calendar",
                message=(
                    "Held for operator — they want a meeting but no calendar "
                    "or booking link is available."
                ),
            )
        return ReplyAgentOutcome(kind="book", reason=reason)
    return ReplyAgentOutcome(kind="continue", reason=reason)


async def maybe_run_reply_agent(
    *,
    outreach_id: str,
    campaign: dict[str, Any] | None,
    campaign_config: dict[str, Any],
    candidate: dict[str, Any],
    last_prospect_msg: dict[str, Any],
    prospect_calendar_url: str = "",
    call_llm_fn: Any = None,
) -> ReplyAgentOutcome:
    """Run the exception loop. Caller must only invoke this for autonomous replies."""
    mode = reply_agent_mode(campaign_config)
    if mode == "off":
        return ReplyAgentOutcome(kind="continue", reason="agent off")

    has_booking = booking_target_available(prospect_calendar_url, campaign_config)
    context = build_reply_agent_context(
        prospect_name=candidate.get("name") or "Unknown",
        title=candidate.get("title") or "",
        company=candidate.get("company") or "",
        sentiment=str(last_prospect_msg.get("sentiment") or "neutral"),
        reply_text=str(last_prospect_msg.get("text") or ""),
        has_booking_target=has_booking,
    )

    campaign_id = ""
    if campaign:
        campaign_id = str(campaign.get("id") or "")
    contact_db_id = candidate.get("contact_db_id") or candidate.get("contact_id") or ""

    async def read_thread() -> str:
        return await run_db(_format_thread, outreach_id)

    async def read_timeline() -> str:
        return await run_db(_format_timeline, outreach_id)

    async def read_icp() -> str:
        return await run_db(_format_icp, campaign_id, campaign_config)

    async def read_fit() -> str:
        return await run_db(_format_fit, contact_db_id, candidate)

    budget = AgentBudget(
        max_steps=REPLY_AGENT_MAX_STEPS,
        max_tokens=REPLY_AGENT_MAX_TOKENS,
        timeout_seconds=float(REPLY_AGENT_TIMEOUT_SECONDS),
        result_chars=REPLY_AGENT_RESULT_CHARS,
    )

    result = await run_agent_loop(
        system=REPLY_AGENT_SYSTEM,
        context=context,
        tools={
            "read_thread": read_thread,
            "read_timeline": read_timeline,
            "read_icp": read_icp,
            "read_fit": read_fit,
            **async_commons_tools(
                agent="reply", campaign_id=campaign_id, outreach_id=outreach_id,
            ),
        },
        schema=AGENT_STEP,
        budget=budget,
        call_llm_fn=call_llm_fn,
    )
    await after_sibling_loop(
        agent="reply",
        campaign_id=campaign_id,
        decision=result.decision,
        reason=result.reason,
        config=campaign_config,
    )

    for step in result.steps:
        await run_db(
            log_action, "reply_agent_step",
            outreach_id=outreach_id,
            result=str(step.get("action") or ""),
            details=step,
        )
    await run_db(
        log_action, "reply_agent_decision",
        outreach_id=outreach_id,
        result=result.decision,
        details={
            "reason": result.reason,
            "mode": mode,
            "exhausted": result.exhausted,
        },
    )

    outcome = apply_reply_agent_decision(
        result, mode=mode, has_booking_target=has_booking,
    )
    if mode == "observe":
        return ReplyAgentOutcome(
            kind="continue",
            reason=f"observe — {outcome.reason}",
            message=outcome.message,
        )
    if outcome.kind == "hold":
        extra: dict[str, Any] = {}
        if prospect_calendar_url:
            extra["prospect_calendar_url"] = prospect_calendar_url
        from .agent_decisions import record_decision
        decision_id = await run_db(
            record_decision, actor="reply", kind="hold",
            campaign_id=str((campaign or {}).get("id") or ""), outreach_ids=[outreach_id], applied=True,
        )
        await run_db(
            persist_operator_hold,
            outreach_id,
            outcome.reason,
            int(last_prospect_msg.get("timestamp") or 0),
            str(last_prospect_msg.get("id") or ""),
            extra or None,
            decision_id=decision_id,
        )
    return outcome


def _format_thread(outreach_id: str) -> str:
    messages = get_messages_for_outreach(outreach_id)[-12:]
    lines = []
    for m in messages:
        role = m.get("role") or "?"
        sent = m.get("sentiment") or ""
        text = (m.get("text") or "").replace("\n", " ")[:280]
        tag = f"{role}/{sent}" if sent else role
        lines.append(f"{tag}: {text}")
    return "\n".join(lines) or "(no messages)"


def _format_timeline(outreach_id: str) -> str:
    events = get_prospect_timeline(outreach_id, days=30)[-15:]
    lines = []
    for ev in events:
        action = ev.get("action") or ev.get("event_type") or "event"
        ts = ev.get("timestamp") or ""
        lines.append(f"{ts} {action}")
    return "\n".join(lines) or "(no timeline)"


def _format_icp(campaign_id: str, campaign_config: dict[str, Any]) -> str:
    target = (campaign_config or {}).get("target_description") or ""
    icp_blob = ""
    if campaign_id:
        camp = get_campaign(campaign_id)
        if camp:
            icp_blob = (camp.get("icp_json") or "")[:1200]
    return f"target: {target}\nicp: {icp_blob or '(none)'}"


def _format_fit(contact_db_id: str, candidate: dict[str, Any]) -> str:
    fit = candidate.get("fit_score")
    analysis = None
    if contact_db_id:
        analysis = get_contact_analysis(str(contact_db_id))
    return json.dumps({
        "fit_score": fit,
        "analysis": analysis or {},
    }, default=str)[:2000]
