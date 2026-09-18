"""Short-loop agent kernel: JSON steps, tool registry, hard budget.

Hosted LLM has no native tool_choice. Each turn is one call_llm JSON object.
The model names a tool or decides; Python runs the tool and appends the result.
"""

from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..constants import LLM_TIER_REASONING

logger = logging.getLogger(__name__)

ToolFn = Callable[..., str | Awaitable[str]]


@dataclass(frozen=True)
class AgentBudget:
    max_steps: int
    max_tokens: int
    timeout_seconds: float
    result_chars: int


@dataclass
class AgentResult:
    decision: str
    reason: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    exhausted: bool = False
    extras: dict[str, str] = field(default_factory=dict)


_VALID_DECISIONS = frozenset({"hold", "skip", "reply", "book", "none"})
_CORE_STEP_KEYS = frozenset({"action", "decision", "reason", "done"})


async def run_agent_loop(
    *,
    system: str,
    context: str,
    tools: dict[str, ToolFn],
    schema: dict[str, Any],
    budget: AgentBudget,
    call_llm_fn: Callable[..., Awaitable[str]] | None = None,
    valid_decisions: frozenset[str] | None = None,
) -> AgentResult:
    """Run a bounded observe/act JSON loop. Exhaustion or LLM failure → none."""
    allowed = valid_decisions if valid_decisions is not None else _VALID_DECISIONS
    if call_llm_fn is None:
        from .llm_router import call_llm

        async def call_llm_fn(prompt: str, **kwargs: Any) -> str:
            return await call_llm(
                prompt,
                system=system,
                temperature=0.2,
                max_tokens=budget.max_tokens,
                json_mode=True,
                schema=schema,
                # An agent judges; it does not write copy.
                tier=LLM_TIER_REASONING,
            )

    transcript = context.strip()
    steps: list[dict[str, Any]] = []
    started = time.monotonic()

    for _ in range(budget.max_steps):
        if time.monotonic() - started >= budget.timeout_seconds:
            return AgentResult(decision="none", reason="timeout", steps=steps, exhausted=True)
        prompt = (
            f"{transcript}\n\n"
            "Return a JSON object with action, decision, reason, done. "
            "Call a tool action when you need more context. "
            "Use action=decide and done=true when you can choose."
        )
        try:
            raw = await call_llm_fn(prompt, system=system, schema=schema)
            parsed = _parse_step(raw, allowed)
        except Exception as e:
            logger.warning("agent loop LLM failed: %s", e)
            return AgentResult(decision="none", reason=str(e)[:240], steps=steps)

        steps.append(parsed)
        action = parsed["action"]
        decision = parsed["decision"]
        reason = parsed["reason"]
        done = parsed["done"]
        extras = parsed.get("extras") or {}

        if done or action == "decide":
            return AgentResult(
                decision=decision, reason=reason, steps=steps, extras=extras,
            )

        tool = tools.get(action)
        if tool is None:
            transcript += f"\n\nTOOL {action}: unknown tool"
            continue
        try:
            result = _invoke_tool(tool, reason=reason, extras=extras)
            if inspect.isawaitable(result):
                result = await result
            text = str(result)
        except Exception as e:
            text = f"error: {e}"
        transcript += f"\n\nTOOL {action}: {text[: budget.result_chars]}"

    return AgentResult(decision="none", reason="step budget exhausted", steps=steps, exhausted=True)


def _parse_step(raw: str, valid_decisions: frozenset[str]) -> dict[str, Any]:
    from .llm import loads_json_object

    data = loads_json_object(raw or "")
    if not isinstance(data, dict):
        raise ValueError("agent step is not an object")
    action = str(data.get("action") or "decide")
    decision = str(data.get("decision") or "none")
    reason = str(data.get("reason") or "")
    done = data.get("done")
    if isinstance(done, str):
        done = done.strip().lower() in {"true", "1", "yes"}
    else:
        done = bool(done)
    extras = {
        str(key): "" if value is None else str(value)
        for key, value in data.items()
        if key not in _CORE_STEP_KEYS
    }
    return {
        "action": action,
        "decision": decision if decision in valid_decisions else "none",
        "reason": reason,
        "done": done,
        "extras": extras,
    }


def _invoke_tool(tool: ToolFn, *, reason: str, extras: dict[str, str]) -> str | Awaitable[str]:
    kwargs: dict[str, str] = {}
    try:
        sig = inspect.signature(tool)
    except (TypeError, ValueError):
        return tool()
    params = sig.parameters
    accepts_var = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    if accepts_var or "reason" in params:
        kwargs["reason"] = reason
    for key, value in extras.items():
        if key == "reason":
            continue
        if accepts_var or key in params:
            kwargs[key] = value
    return tool(**kwargs) if kwargs else tool()
