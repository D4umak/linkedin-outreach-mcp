"""Coordinator — digest after each sibling loop. Never sends LinkedIn."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from typing import Any, Literal

from ..ai.agent_loop import AgentBudget, run_agent_loop
from ..ai.coordinator import COORDINATOR_SYSTEM, build_coordinator_context
from ..ai.schemas import COORDINATOR_AGENT_STEP
from ..constants import (
    COORDINATOR_MAX_STEPS,
    COORDINATOR_MAX_TOKENS,
    COORDINATOR_RESULT_CHARS,
    COORDINATOR_TIMEOUT_SECONDS,
    COORDINATOR_VALID_DECISIONS,
)
from ..db.async_bridge import run_db
from ..db.queries import get_campaign
from ..flags import flag_enabled
from . import agent_decisions
from .agent_context import numbers_for
from .agent_commons import (
    DIGEST_MAX_CHARS,
    beat_is_stale,
    async_commons_tools,
    get_digest,
    get_hold,
    list_beats,
    list_live_notes,
    record_agent_beat,
    upsert_campaign_row,
)

logger = logging.getLogger(__name__)

CoordinatorMode = Literal["off", "observe", "act"]


async def _quiet_test_llm(prompt: str, **kwargs: Any) -> str:
    """Keep sibling unit tests offline. Production never hits this path."""
    return json.dumps({
        "action": "decide", "decision": "none",
        "reason": "test sidecar", "done": True, "note": "",
    })


def coordinator_blocks_send(campaign_id: str) -> bool:
    """True when an act-mode coordinator hold is live for this campaign."""
    if not (campaign_id or "").strip():
        return False
    return get_hold(campaign_id) is not None


def coordinator_mode(config: dict[str, Any] | None) -> CoordinatorMode:
    cfg = config or {}
    raw_mode = cfg.get("coordinator_agent_mode")
    if isinstance(raw_mode, str) and raw_mode.strip():
        text = raw_mode.strip().lower()
        if text in {"off", "observe", "act"}:
            return text  # type: ignore[return-value]
        if text in {"on", "true", "yes", "1"}:
            return "act"
        if text in {"false", "no", "0"}:
            return "off"
    if "enable_coordinator_agent" in cfg and cfg.get("enable_coordinator_agent") not in (None, ""):
        return "act" if flag_enabled(cfg, "enable_coordinator_agent", default=False) else "off"
    return "act"


def _sibling_mode(agent: str, config: dict[str, Any]) -> str:
    # Lazy imports: the four siblings call after_sibling_loop.
    if agent == "reply":
        from .reply_agent import reply_agent_mode
        return reply_agent_mode(config)
    if agent == "strategist":
        from .strategist_replan import strategist_replan_mode
        return strategist_replan_mode(config)
    if agent == "closer":
        from .hot_lead_closer import hot_lead_closer_mode
        return hot_lead_closer_mode(config)
    if agent == "icp_research":
        from .icp_research import icp_research_mode
        return icp_research_mode(config)
    if agent == "product":
        from .product_agent import product_agent_mode
        return product_agent_mode(config)
    if agent == "coordinator":
        return coordinator_mode(config)
    return "observe"


# Siblings whose decisions are about ONE prospect; none of them may ground a
# campaign hold. Mirrors heylead-api app/services/coordinator.py (23 Sep 2026).
PROSPECT_SCOPED_AGENTS = frozenset({"reply", "closer", "strategist", "send_fit"})
PROSPECT_SCOPE_LABEL = "[one prospect]"


def campaign_is_live(campaign_id: str) -> bool:
    """Only an active campaign sends, so only it can be coordinated or held."""
    if not campaign_id:
        return True  # account-level runs are observe-only (see _run)
    camp = get_campaign(campaign_id)
    return bool(camp) and str(camp.get("status") or "") == "active"


def campaign_level_evidence(campaign_id: str, now: int | None = None) -> list[str]:
    """Live notes with no prospect on them; sibling beats never count."""
    notes = [
        n for n in list_live_notes(campaign_id=campaign_id, now=now)
        if not str(n.get("outreach_id") or "")
    ]
    return [f"campaign notes:{len(notes)}"] if notes else []


def build_digest_body(campaign_id: str, config: dict[str, Any], now: int | None = None) -> str:
    import time

    when = int(now if now is not None else time.time())
    beats = list_beats(campaign_id=campaign_id)
    notes = list_live_notes(campaign_id=campaign_id, now=when)
    parts: list[str] = []
    for beat in beats:
        agent = str(beat.get("agent") or "?")
        if agent == "coordinator":
            continue
        decision = str(beat.get("decision") or "?")
        age_s = max(0, when - int(beat.get("created_at") or 0))
        stale = beat_is_stale(beat, mode=_sibling_mode(agent, config), now=when)
        flag = " stale" if stale else ""
        scope = PROSPECT_SCOPE_LABEL if agent in PROSPECT_SCOPED_AGENTS else ""
        parts.append(f"{agent}:{decision}{scope} ({age_s // 60}m){flag}")
    parts.append(f"notes:{len(notes)}")
    text = " | ".join(parts) or "(empty)"
    return text[:DIGEST_MAX_CHARS]


async def after_sibling_loop(
    *,
    agent: str,
    campaign_id: str,
    decision: str,
    reason: str,
    config: dict[str, Any] | None = None,
) -> None:
    await run_db(
        record_agent_beat,
        agent=agent,
        campaign_id=campaign_id,
        decision=decision,
        reason=reason,
    )
    try:
        await maybe_run_coordinator(
            campaign_id,
            trigger_agent=agent,
            config=config,
        )
    except Exception:
        logger.warning("coordinator failed after %s", agent, exc_info=True)


async def maybe_run_coordinator(
    campaign_id: str,
    *,
    trigger_agent: str,
    call_llm_fn: Any = None,
    config: dict[str, Any] | None = None,
) -> None:
    if trigger_agent == "coordinator":
        return
    try:
        await _run(campaign_id, trigger_agent=trigger_agent, call_llm_fn=call_llm_fn, config=config)
    except Exception:
        logger.warning("coordinator failed", exc_info=True)


async def _run(
    campaign_id: str,
    *,
    trigger_agent: str,
    call_llm_fn: Any,
    config: dict[str, Any] | None,
) -> None:
    cid = campaign_id or ""
    if not await run_db(campaign_is_live, cid):
        return
    cfg = config
    if cfg is None and cid:
        camp = await run_db(get_campaign, cid) or {}
        raw = camp.get("config_json") or "{}"
        try:
            loaded = json.loads(raw)
            cfg = loaded if isinstance(loaded, dict) else {}
        except (json.JSONDecodeError, TypeError):
            cfg = {}
    cfg = cfg or {}
    mode = coordinator_mode(cfg)
    if not cid and mode == "act":
        mode = "observe"

    digest = await run_db(build_digest_body, cid, cfg)
    await run_db(
        upsert_campaign_row,
        kind="digest",
        campaign_id=cid,
        body=digest,
        reason=f"after {trigger_agent}",
    )
    await run_db(
        record_agent_beat,
        agent="coordinator",
        campaign_id=cid,
        decision="digest",
        reason=f"after {trigger_agent}",
    )
    if mode == "off":
        return

    if call_llm_fn is None and os.environ.get("PYTEST_CURRENT_TEST"):
        call_llm_fn = _quiet_test_llm

    tools = async_commons_tools(agent="coordinator", campaign_id=cid)
    digest_row = await run_db(get_digest, cid)
    numbers = await numbers_for(cid, actor="coordinator")

    def read_digest() -> str:
        return (digest_row or {}).get("body") or "(no digest)"

    result = await run_agent_loop(
        system=COORDINATOR_SYSTEM,
        context=build_coordinator_context(trigger_agent=trigger_agent, campaign_id=cid, numbers=numbers),
        tools={**tools, "read_digest": read_digest},
        schema=COORDINATOR_AGENT_STEP,
        budget=AgentBudget(
            max_steps=COORDINATOR_MAX_STEPS,
            max_tokens=COORDINATOR_MAX_TOKENS,
            timeout_seconds=float(COORDINATOR_TIMEOUT_SECONDS),
            result_chars=COORDINATOR_RESULT_CHARS,
        ),
        call_llm_fn=call_llm_fn,
        valid_decisions=COORDINATOR_VALID_DECISIONS,
    )
    if result.decision == "hold" and cid and not await run_db(campaign_level_evidence, cid):
        # The rule lives here, not only in the prompt, because the prompt did not hold.
        result = replace(
            result, decision="none",
            reason=f"hold refused: no campaign-level evidence ({result.reason})",
        )
    await run_db(
        record_agent_beat,
        agent="coordinator",
        campaign_id=cid,
        decision=result.decision,
        reason=result.reason,
    )
    if not cid:
        return
    # A hold is about the whole campaign, so it is scored on every row of it.
    await run_db(
        agent_decisions.record_decision, actor="coordinator", kind=result.decision,
        campaign_id=cid, applied=mode == "act" and result.decision == "hold", numbers=numbers,
        scope=agent_decisions.SCOPE_CAMPAIGN,
    )
    if mode == "act" and result.decision == "hold":
        await run_db(
            upsert_campaign_row,
            kind="hold",
            campaign_id=cid,
            body=result.reason,
            reason=result.reason,
        )


# Re-export for tests
__all__ = [
    "after_sibling_loop",
    "build_digest_body",
    "coordinator_blocks_send",
    "coordinator_mode",
    "get_digest",
    "get_hold",
    "maybe_run_coordinator",
]
