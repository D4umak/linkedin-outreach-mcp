"""The campaign's numbers, in one place, for every campaign agent (heylead-api#1209).

Mirrors heylead-api ``app/services/agent_context.py``: the same dict and the
same ``CAMPAIGN NUMBERS`` block. ``numbers_for`` is what the agents await:

- hosted (``config.is_backend_mode()``): the outreach rows live in the cloud,
  so it reads ``GET /api/v1/analytics/agent-context``; a failed call is an
  empty dict, never this machine's (empty) tables;
- local: ``campaign_numbers`` reads this machine's tables. There is no case
  ledger or outbound log here, so those two sections stay ``None``, and the
  bottleneck is the cloud scorecard's verdict only.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from typing import Any

from ..db.schema import get_db

logger = logging.getLogger(__name__)

NUMBERS_VERSION = 1
WINDOW_7D = 7 * 86400
BLOCK_LABEL = "CAMPAIGN NUMBERS"
SECTIONS = ("scorecard", "open_cases", "journey", "job_failures_7d", "outbound_errors_7d", "track_record")
_TIMEOUT = 10.0


def _rate(n: int, of: int) -> dict[str, Any]:
    return {"n": int(n), "of": int(of), "rate": round(n / of, 4) if of else None}


def _empty(campaign_id: str, when: int) -> dict[str, Any]:
    return {"version": NUMBERS_VERSION, "campaign_id": campaign_id, "computed_at": when,
            **dict.fromkeys(SECTIONS)}


_CONNECTED = ("connected", "messaged", "replied", "hot_lead", "closed_happy", "closed_unhappy",
              "reverse_pitch", "opted_out")
_REPLIED = ("replied", "hot_lead", "closed_happy", "closed_unhappy", "reverse_pitch", "opted_out")


def _scorecard(campaign_id: str, when: int) -> dict[str, Any]:
    """The same statuses as ``queries.get_campaign_stats``; accept counts mature invitations only."""
    conn_in, rep_in = ", ".join("?" * len(_CONNECTED)), ", ".join("?" * len(_REPLIED))
    mature = "status NOT IN ('pending', 'skipped') AND COALESCE(invited_at, updated_at) < ?"
    r = get_db().execute(
        f"SELECT SUM(CASE WHEN status != 'skipped' THEN 1 ELSE 0 END) AS prospects, "
        f"SUM(CASE WHEN {mature} THEN 1 ELSE 0 END) AS mature, "
        f"SUM(CASE WHEN {mature} AND status IN ({conn_in}) THEN 1 ELSE 0 END) AS mature_connected, "
        f"SUM(CASE WHEN status IN ({conn_in}) THEN 1 ELSE 0 END) AS connected, "
        f"SUM(CASE WHEN status IN ({rep_in}) THEN 1 ELSE 0 END) AS replied, "
        "SUM(CASE WHEN status = 'closed_happy' THEN 1 ELSE 0 END) AS won "
        "FROM outreaches WHERE campaign_id = ?",
        (when - WINDOW_7D, when - WINDOW_7D, *_CONNECTED, *_CONNECTED, *_REPLIED, campaign_id),
    ).fetchone()
    n = {k: int(r[k] or 0) for k in r.keys()}
    return {
        "source": "local", "as_of": when,
        "accept": _rate(n["mature_connected"], n["mature"]),
        "reply": _rate(n["replied"], n["connected"]),
        "close": _rate(n["won"], n["replied"]),
        "meetings": _meetings(campaign_id),
        "prospects": n["prospects"],
        "bottleneck": "",
    }


def _meetings(campaign_id: str) -> int:
    try:
        row = get_db().execute(
            "SELECT COUNT(DISTINCT outreach_id) AS n FROM calendar_events WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()
    except Exception:
        return 0
    return int((row["n"] if row else 0) or 0)


def _journey() -> dict[str, Any]:
    db = get_db()
    sent = db.execute("SELECT 1 FROM messages WHERE role = 'sdr' LIMIT 1").fetchone()
    invited = db.execute("SELECT 1 FROM outreaches WHERE invited_at > 0 LIMIT 1").fetchone()
    replied = db.execute("SELECT 1 FROM outreaches WHERE first_reply_at > 0 LIMIT 1").fetchone()
    return {"first_send": bool(sent or invited), "first_reply": bool(replied), "first_meeting": _meetings_any()}


def _meetings_any() -> bool:
    try:
        return get_db().execute("SELECT 1 FROM calendar_events LIMIT 1").fetchone() is not None
    except Exception:
        return False


def _job_failures(when: int) -> dict[str, Any]:
    rows = get_db().execute(
        "SELECT job_type FROM scheduler_jobs WHERE status = 'failed' "
        "AND COALESCE(completed_at, created_at) >= ? LIMIT 2000",
        (when - WINDOW_7D,),
    ).fetchall()
    counts = Counter(str(r["job_type"] or "unknown") for r in rows)
    return {"total": sum(counts.values()), "by_type": dict(counts.most_common(5))}


def _section(name: str, fn: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception:
        logger.warning("agent context: %s unavailable", name, exc_info=True)
        return None


def campaign_numbers(campaign_id: str, now: int | None = None, *, actor: str = "") -> dict[str, Any]:
    """This machine's numbers. Sync: agents await ``numbers_for`` instead."""
    cid = str(campaign_id or "").strip()
    when = int(now if now is not None else time.time())
    numbers = _empty(cid, when)
    if cid:
        numbers["scorecard"] = _section("scorecard", _scorecard, cid, when)
    numbers["journey"] = _section("journey", _journey)
    numbers["job_failures_7d"] = _section("job failures", _job_failures, when)
    if actor:
        from .agent_decisions import track_record

        numbers["actor"] = actor
        numbers["track_record"] = _section("track record", track_record, actor)
    return numbers


async def _cloud_numbers(campaign_id: str, actor: str) -> dict[str, Any]:
    import httpx

    from .cloud_sync import _base_url, _headers

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{_base_url()}/api/v1/analytics/agent-context",
                params={"campaign_id": campaign_id, "actor": actor}, headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("agent context: the cloud did not answer (%r)", e)
        return _empty(campaign_id, int(time.time()))
    return data if isinstance(data, dict) else _empty(campaign_id, int(time.time()))


async def numbers_for(campaign_id: str, *, actor: str = "") -> dict[str, Any]:
    """The numbers an agent is shown: the cloud's when hosted, this machine's otherwise."""
    from .. import config
    from ..db.async_bridge import run_db

    if not campaign_id:
        return _empty("", int(time.time()))
    if config.is_backend_mode():
        return await _cloud_numbers(campaign_id, actor)
    return await run_db(campaign_numbers, campaign_id, actor=actor)


def _pct(block: dict[str, Any] | None) -> str:
    block = block or {}
    n, of = int(block.get("n") or 0), int(block.get("of") or 0)
    return f"{n}/{of} ({round(100 * n / of)}%)" if of else f"{n}/0 (no denominator yet)"


def numbers_block(numbers: dict[str, Any] | None) -> str:
    """The one labelled evidence block every campaign agent's prompt carries."""
    data = numbers or {}
    lines = [f"{BLOCK_LABEL} (counts from agent_context; quote them, do not re-derive):"]
    card = data.get("scorecard")
    if card:
        lines.append(
            f"- accept {_pct(card.get('accept'))} · reply after accept {_pct(card.get('reply'))}"
            f" · close {_pct(card.get('close'))} · meetings {int(card.get('meetings') or 0)}"
            f" · bottleneck: {card.get('bottleneck') or 'none'}"
        )
    else:
        lines.append("- scorecard: not available")
    cases = data.get("open_cases")
    if cases is None:
        lines.append("- open cases: not available")
    else:
        shown = ", ".join(
            f"{c['type']} {c['severity']} {c['age_hours']}h" + (" (this campaign)" if c.get("this_campaign") else "")
            for c in cases.get("items") or []
        )
        lines.append(f"- open cases: {int(cases.get('n') or 0)}" + (f" ({shown})" if shown else ""))
    journey = data.get("journey")
    if journey:
        seen = " · ".join(
            f"{label} {'yes' if journey.get(key) else 'no'}"
            for key, label in (("first_send", "first send"), ("first_reply", "first reply"),
                               ("first_meeting", "first meeting"))
        )
        lines.append(f"- workspace journey: {seen}")
    else:
        lines.append("- workspace journey: not available")
    failures = data.get("job_failures_7d")
    if failures is not None:
        by_type = ", ".join(f"{k} {v}" for k, v in (failures.get("by_type") or {}).items()) or "none"
        lines.append(f"- job failures 7d: {int(failures.get('total') or 0)} ({by_type})")
    errors = data.get("outbound_errors_7d")
    if errors is not None:
        lines.append(f"- outbound call errors 7d: {_pct(errors)}")
    record = data.get("track_record")
    if record is not None:
        lines.append(
            f"- your track record {record.get('days', 30)}d: {int(record.get('decisions') or 0)} decisions scored"
            f" · of {int(record.get('touched') or 0)} people touched,"
            f" {int(record.get('accepted') or 0)} accepted, {int(record.get('replied') or 0)} replied,"
            f" {int(record.get('meetings') or 0)} booked, {int(record.get('closed') or 0)} closed within 48h"
        )
    return "\n".join(lines)
