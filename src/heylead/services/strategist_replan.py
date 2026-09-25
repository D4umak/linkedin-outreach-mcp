"""Rewrite leftover daily-plan actions when a signal, accept, or first reply lands."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from ..ai.agent_loop import AgentBudget, AgentResult, run_agent_loop
from ..ai.schemas import REPLAN_AGENT_STEP
from ..ai.strategist_replan import REPLAN_AGENT_SYSTEM, build_replan_context
from ..constants import (
    STRATEGIST_AVAILABLE_ACTIONS,
    STRATEGIST_MAX_ACTIONS_PER_DAY,
    STRATEGIST_REPLAN_MAX_PER_TICK,
    STRATEGIST_REPLAN_MAX_STEPS,
    STRATEGIST_REPLAN_MAX_TOKENS,
    STRATEGIST_REPLAN_RESULT_CHARS,
    STRATEGIST_REPLAN_TIMEOUT_SECONDS,
    STRATEGIST_REPLAN_VALID_DECISIONS,
)
from ..db.async_bridge import run_db
from . import agent_decisions
from .agent_commons import async_commons_tools
from .agent_context import numbers_for
from .coordinator import after_sibling_loop
from ..db.queries import (
    get_campaign,
    get_messages_for_outreach,
    get_outreach_with_contact,
    log_action,
)
from ..db.signal_queries import list_signals
from ..db.strategist_queries import (
    _today_str,
    get_daily_plan,
    has_replan_today,
    update_planned_actions,
)
from ..flags import flag_enabled
from ..services.communication_strategist import sanitize_human_owned_actions

logger = logging.getLogger(__name__)


@dataclass
class StrategistReplanOutcome:
    decision: str
    reason: str = ""
    applied: bool = False
    summary: str = ""


def strategist_replan_mode(config: dict[str, Any] | None) -> Literal["off", "observe", "act"]:
    """Campaign config → off | observe | act. Unset defaults to act."""
    cfg = config or {}
    raw_mode = cfg.get("strategist_replan_mode")
    if isinstance(raw_mode, str) and raw_mode.strip():
        text = raw_mode.strip().lower()
        if text in {"off", "observe", "act"}:
            return text  # type: ignore[return-value]
        if text in {"on", "true", "yes", "1"}:
            return "act"
        if text in {"false", "no", "0"}:
            return "off"
    if "enable_strategist_replan_agent" in cfg and cfg.get("enable_strategist_replan_agent") not in (None, ""):
        return "act" if flag_enabled(cfg, "enable_strategist_replan_agent", default=False) else "off"
    return "act"


def apply_remaining_actions(
    planned: list[dict[str, Any]],
    executed: list[dict[str, Any]],
    remaining: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep executed action rows; replace leftover with remaining, capped at 3/day."""
    executed_types = {
        str(entry.get("action_type") or "")
        for entry in executed
        if entry.get("action_type") and entry.get("status") != "skipped"
    }
    kept = [action for action in planned if action.get("action_type") in executed_types]
    room = max(0, STRATEGIST_MAX_ACTIONS_PER_DAY - len(kept))
    extra: list[dict[str, Any]] = []
    for item in remaining:
        action_type = str(item.get("action_type") or "")
        if action_type not in STRATEGIST_AVAILABLE_ACTIONS:
            continue
        extra.append({
            "action_type": action_type,
            "priority": 1,
            "timing_preference": str(item.get("timing_preference") or "anytime"),
            "params": item.get("params") if isinstance(item.get("params"), dict) else {},
            "rationale": str(item.get("rationale") or "")[:200],
        })
        if len(extra) >= room:
            break
    if not extra and room:
        extra = [{
            "action_type": "skip_today",
            "priority": 1,
            "timing_preference": "anytime",
            "params": {},
            "rationale": "replan cleared leftover actions",
        }]
    return kept + extra


def parse_remaining_json(raw: str) -> list[dict[str, Any]]:
    text = (raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def leftover_actions(plan: dict[str, Any]) -> list[dict[str, Any]]:
    executed_types = {e.get("action_type") for e in (plan.get("executed_actions") or [])}
    leftover = []
    for action in plan.get("planned_actions") or []:
        action_type = action.get("action_type")
        if action_type in executed_types or action_type == "skip_today":
            continue
        leftover.append(action)
    return leftover


def plan_trigger(plan: dict[str, Any], outreach: dict[str, Any], signals: list[dict[str, Any]]) -> str:
    created = int(plan.get("created_at") or 0)
    accepted = int(outreach.get("accepted_at") or 0)
    replied = int(outreach.get("first_reply_at") or 0)
    if accepted and accepted >= created:
        return "acceptance"
    if replied and replied >= created:
        return "first_reply"
    for sig in signals:
        detected = int(sig.get("detected_at") or 0)
        if detected >= created:
            return f"signal:{sig.get('signal_type') or 'unknown'}"
    return ""


async def replan_signaled_plans(campaign_id: str) -> int:
    """Run the replan agent for signaled leftover plans. Safe to call from the 15-min tick."""
    from ..db.strategist_queries import get_campaign_daily_plans

    campaign = await run_db(get_campaign, campaign_id)
    config: dict[str, Any] = {}
    if campaign:
        try:
            config = json.loads(campaign.get("config_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            config = {}
    if strategist_replan_mode(config) == "off":
        return 0

    today = _today_str()
    plans = await run_db(get_campaign_daily_plans, campaign_id, today)
    ran = 0
    for plan in plans:
        if ran >= STRATEGIST_REPLAN_MAX_PER_TICK:
            break
        outreach_id = plan.get("outreach_id") or ""
        if not outreach_id:
            continue
        outcome = await maybe_run_strategist_replan(outreach_id, config=config)
        if outcome.reason != "no trigger" and outcome.reason != "already replanned" and outcome.reason != "no leftover":
            ran += 1
    return ran


async def maybe_run_strategist_replan(
    outreach_id: str,
    *,
    config: dict[str, Any] | None = None,
    call_llm_fn: Any = None,
) -> StrategistReplanOutcome:
    """Replan leftover actions for one outreach. Failure → keep."""
    try:
        mode = strategist_replan_mode(config)
        if mode == "off":
            return StrategistReplanOutcome(decision="keep", reason="agent off")

        today = _today_str()
        if await run_db(has_replan_today, outreach_id, today):
            return StrategistReplanOutcome(decision="keep", reason="already replanned")

        plan = await run_db(get_daily_plan, outreach_id, today)
        if not plan:
            return StrategistReplanOutcome(decision="keep", reason="no plan")
        if not leftover_actions(plan):
            return StrategistReplanOutcome(decision="keep", reason="no leftover")

        outreach = await run_db(get_outreach_with_contact, outreach_id)
        if not outreach:
            return StrategistReplanOutcome(decision="keep", reason="outreach missing")

        signals = await run_db(_signals_for_outreach, outreach, int(plan.get("created_at") or 0))
        trigger = plan_trigger(plan, outreach, signals)
        if not trigger:
            return StrategistReplanOutcome(decision="keep", reason="no trigger")

        campaign_id = str(outreach.get("campaign_id") or "")
        numbers = await numbers_for(campaign_id, actor="strategist")
        context = build_replan_context(
            name=str(outreach.get("name") or ""),
            status=str(outreach.get("status") or ""),
            trigger=trigger,
            numbers=numbers,
        )
        async def read_thread() -> str:
            return await run_db(_format_thread_sync, outreach_id)

        tools = {
            "read_plan": lambda: _format_plan(plan),
            "read_signals": lambda: _format_signals(signals),
            "read_thread": read_thread,
            "read_status": lambda: _format_status(outreach, trigger),
            **async_commons_tools(
                agent="strategist", campaign_id=campaign_id, outreach_id=outreach_id,
            ),
        }
        result = await run_agent_loop(
            system=REPLAN_AGENT_SYSTEM,
            context=context,
            tools=tools,
            schema=REPLAN_AGENT_STEP,
            budget=AgentBudget(
                max_steps=STRATEGIST_REPLAN_MAX_STEPS,
                max_tokens=STRATEGIST_REPLAN_MAX_TOKENS,
                timeout_seconds=float(STRATEGIST_REPLAN_TIMEOUT_SECONDS),
                result_chars=STRATEGIST_REPLAN_RESULT_CHARS,
            ),
            call_llm_fn=call_llm_fn,
            valid_decisions=STRATEGIST_REPLAN_VALID_DECISIONS,
        )
        await after_sibling_loop(
            agent="strategist",
            campaign_id=campaign_id,
            decision=result.decision,
            reason=result.reason,
            config=config,
        )
        return await _apply_decision(outreach_id, plan, outreach, result, mode=mode, numbers=numbers)
    except Exception as e:
        logger.warning("Strategist replan failed: %s", e)
        return StrategistReplanOutcome(
            decision="keep",
            reason=str(e)[:240],
            summary="keep — replan could not run",
        )


def _signals_for_outreach(outreach: dict[str, Any], since: int) -> list[dict[str, Any]]:
    campaign_id = outreach.get("campaign_id") or ""
    contact_id = outreach.get("contact_id") or ""
    linkedin_id = str(outreach.get("linkedin_id") or "")
    found: list[dict[str, Any]] = []
    if not campaign_id:
        return found
    for sig in list_signals(campaign_id=campaign_id, limit=30):
        if int(sig.get("detected_at") or 0) < since:
            continue
        if contact_id and sig.get("prospect_id") == contact_id:
            found.append(sig)
        elif linkedin_id and sig.get("linkedin_id") == linkedin_id:
            found.append(sig)
    return found


def _format_plan(plan: dict[str, Any]) -> str:
    return json.dumps({
        "planned": plan.get("planned_actions") or [],
        "executed": plan.get("executed_actions") or [],
        "leftover": leftover_actions(plan),
    }, default=str)[:2000]


def _format_signals(signals: list[dict[str, Any]]) -> str:
    rows = [
        {
            "type": s.get("signal_type"),
            "content": (s.get("content") or "")[:180],
            "detected_at": s.get("detected_at"),
        }
        for s in signals[:8]
    ]
    return json.dumps(rows, default=str) if rows else "(no new signals)"


def _format_thread_sync(outreach_id: str) -> str:
    messages = get_messages_for_outreach(outreach_id)[-8:]
    lines = []
    for message in messages:
        role = message.get("role") or "?"
        text = (message.get("text") or "").replace("\n", " ")[:200]
        lines.append(f"{role}: {text}")
    return "\n".join(lines) or "(no messages)"


def _format_status(outreach: dict[str, Any], trigger: str) -> str:
    return (
        f"status={outreach.get('status') or ''}\n"
        f"accepted_at={outreach.get('accepted_at') or 0}\n"
        f"first_reply_at={outreach.get('first_reply_at') or 0}\n"
        f"trigger={trigger}"
    )


async def _apply_decision(
    outreach_id: str,
    plan: dict[str, Any],
    outreach: dict[str, Any],
    result: AgentResult,
    *,
    mode: str,
    numbers: dict[str, Any] | None = None,
) -> StrategistReplanOutcome:
    decision = result.decision if result.decision in {"keep", "revise", "hold"} else "keep"
    reason = result.reason or ("replan could not decide" if result.decision == "none" else "")
    remaining = parse_remaining_json(result.extras.get("remaining_json") or "")
    remaining = sanitize_human_owned_actions(str(outreach.get("status") or ""), remaining)

    for step in result.steps:
        await run_db(
            log_action, "strategist_replan_step",
            outreach_id=outreach_id,
            result=str(step.get("action") or ""),
            details=step,
        )
    await run_db(
        log_action, "strategist_replan_decision",
        outreach_id=outreach_id,
        result=decision,
        details={
            "reason": reason,
            "mode": mode,
            "plan_date": _today_str(),
            "remaining": remaining,
            "exhausted": result.exhausted,
        },
    )

    applied = False
    if decision == "revise" and mode == "act":
        updated = apply_remaining_actions(
            plan.get("planned_actions") or [],
            plan.get("executed_actions") or [],
            remaining,
        )
        updated = sanitize_human_owned_actions(str(outreach.get("status") or ""), updated)
        await run_db(update_planned_actions, outreach_id, updated)
        applied = True
        summary = f"revised — {reason}" if reason else "revised"
    elif decision == "revise":
        summary = f"revise recommended (observe) — {reason}" if reason else "revise recommended (observe)"
    elif decision == "hold" and mode == "act":
        updated = apply_remaining_actions(
            plan.get("planned_actions") or [],
            plan.get("executed_actions") or [],
            [{"action_type": "skip_today", "rationale": reason or "held for operator"}],
        )
        updated = sanitize_human_owned_actions(str(outreach.get("status") or ""), updated)
        await run_db(update_planned_actions, outreach_id, updated)
        applied = True
        summary = f"hold — {reason}" if reason else "hold"
    elif decision == "hold":
        summary = f"hold — {reason}" if reason else "hold"
    else:
        summary = f"keep — {reason}" if reason else "keep"

    decision_id = await run_db(
        agent_decisions.record_decision, actor="strategist", kind=decision,
        campaign_id=str(outreach.get("campaign_id") or ""), outreach_ids=[outreach_id],
        applied=applied, numbers=numbers,
    )
    if applied:
        await run_db(agent_decisions.stamp_rows, [outreach_id], decision_id)
    return StrategistReplanOutcome(
        decision=decision, reason=reason, applied=applied, summary=summary,
    )
