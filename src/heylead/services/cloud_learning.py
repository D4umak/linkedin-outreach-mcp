"""Read the hosted learning loops from the backend (heylead-api#1211).

A hosted account's local engine is off (``local_scheduler_engine_enabled()``
is False when the cloud sends), so the local ``strategy_actions``,
``ab_tests`` and ``optimization_history`` tables are never written for it.
The backend runs those loops on its tick and serves the results at
``GET /api/v1/analytics/learning``. show_status and signals(strategy|report|
optimize_history) read that when ``is_backend_mode()``; the local tables stay
the source for a self-hosted sender.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from .. import config

logger = logging.getLogger(__name__)

LEARNING_PATH = "/api/v1/analytics/learning"


async def fetch_learning(limit: int = 20) -> dict[str, Any] | None:
    """The workspace's learning loops, or None when the backend cannot answer."""
    if not config.is_backend_mode():
        return None
    from .cloud_sync import _TIMEOUT, _base_url, _headers

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.get(
                f"{_base_url()}{LEARNING_PATH}",
                params={"limit": int(limit)},
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            # show_status reports an expired sign-in from its own stats call;
            # this section only goes quiet.
            logger.warning("Learning loops HTTP error: %s", e.response.status_code)
            return None
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("Learning loops fetch failed: %s", e)
            return None
    return data if isinstance(data, dict) else None


def _when(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%d %b %H:%M UTC")
    except (TypeError, ValueError, OSError):
        return "?"


def _mode_line(data: dict[str, Any]) -> str:
    mode = data.get("mode") or "observe"
    if mode == "act":
        return "Mode: act. Winners and signal changes are applied."
    if mode == "off":
        return "Mode: off. The loops are paused."
    return "Mode: observe. Outcomes are measured; nothing is applied yet."


def strategy_lines(data: dict[str, Any], limit: int = 7) -> list[str]:
    strategy = data.get("strategy") or {}
    summary = strategy.get("summary") or {}
    actions = strategy.get("actions") or []
    lines = [
        f"Strategy actions: {summary.get('total_actions', 0)} "
        f"({summary.get('measured_actions', 0)} measured, "
        f"{summary.get('validated_actions', 0)} validated, "
        f"{summary.get('rolled_back_actions', 0)} rolled back)",
    ]
    for a in actions[:limit]:
        outcome = a.get("outcome") or {}
        result = outcome.get("reason") or "waiting 48 h to measure"
        lines.append(f"  - {a.get('action_type', '?')} [{a.get('status', '?')}] "
                     f"{_when(a.get('created_at'))}: {result}")
    return lines


def ab_lines(data: dict[str, Any], limit: int = 5) -> list[str]:
    tests = data.get("ab_tests") or []
    if not tests:
        return ["A/B tests: none running."]
    lines = [f"A/B tests: {len(tests)}"]
    for t in tests[:limit]:
        winner = t.get("winner") or t.get("observed_winner")
        verdict = (
            f"winner {winner}" if t.get("status") == "completed" and winner
            else f"leading: {winner}" if winner else "collecting data (15 invited per variant)"
        )
        reason = (t.get("result") or {}).get("reason") or ""
        lines.append(f"  - {t.get('name', '?')} [{t.get('status', '?')}]: {verdict}"
                     + (f". {reason}" if reason else ""))
    return lines


def history_lines(data: dict[str, Any], limit: int = 10) -> list[str]:
    entries = data.get("optimization_history") or []
    if not entries:
        return ["Signal changes: none yet (the optimiser runs once a day)."]
    lines = [f"Signal changes: {len(entries)}"]
    for e in entries[:limit]:
        lines.append(
            f"  - {e.get('optimization_type', '?')} {e.get('target', '?')}: "
            f"{e.get('before_value', '')} -> {e.get('after_value', '')} "
            f"[{e.get('status', '?')}] {_when(e.get('applied_at'))}"
        )
        if e.get("reason"):
            lines.append(f"    {e['reason']}")
    return lines


def status_section(data: dict[str, Any]) -> list[str]:
    """The show_status block for a hosted account."""
    return ["Learning loops (cloud):", _mode_line(data), *strategy_lines(data, limit=3),
            *ab_lines(data, limit=3), *history_lines(data, limit=3), ""]


def format_strategy(data: dict[str, Any]) -> str:
    return "\n".join([
        "Strategy engine (cloud)", _mode_line(data), "",
        *strategy_lines(data), "", *ab_lines(data), "",
        "It measures every 4 hours and judges A/B tests every hour.",
    ])


def format_history(data: dict[str, Any], limit: int = 30) -> str:
    return "\n".join(["Signal optimisation (cloud)", _mode_line(data), "",
                      *history_lines(data, limit=limit)])


UNREACHABLE = "The cloud's learning loops could not be read just now. Try again in a minute."
