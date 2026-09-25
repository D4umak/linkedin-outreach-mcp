"""Every local agent decision, and what its rows did in the 48 h after.

Mirrors heylead-api ``app/services/agent_decisions.py`` (heylead-api#1209,
docs/agent-numbers.md there) for a machine that sends itself. Sync functions:
agents call them through ``run_db``. Never raises into an agent.

The client has no ``status_changed_at`` or ``meeting_booked_at``: a close is
dated by ``updated_at`` and a meeting by its ``calendar_events`` row.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Iterable

from ..db.schema import get_db

logger = logging.getLogger(__name__)

SCORE_AFTER_SECONDS = 48 * 3600
SCORE_BATCH = 500
TRACK_RECORD_DAYS = 30
MAX_TOUCHED_IDS = 500
SCOPE_ROWS = "rows"
SCOPE_CAMPAIGN = "campaign"
ACTORS = frozenset({"coordinator", "strategist", "closer", "reply", "daily_strategy"})
_CLOSED_STATUSES = ("closed_happy", "closed_unhappy")


def _now(now: int | None) -> int:
    return int(now if now is not None else time.time())


def record_decision(
    *,
    actor: str,
    kind: str,
    campaign_id: str = "",
    outreach_ids: Iterable[str] = (),
    applied: bool = False,
    numbers: dict[str, Any] | None = None,
    scope: str = SCOPE_ROWS,
    now: int | None = None,
) -> str:
    """Insert one decision. Returns its id, or "" when it could not be written."""
    if actor not in ACTORS:
        return ""
    ids = [str(i) for i in outreach_ids if i][:MAX_TOUCHED_IDS]
    decision_id = str(uuid.uuid4())
    try:
        db = get_db()
        db.execute(
            "INSERT INTO agent_decisions (id, actor, campaign_id, kind, scope, applied, "
            "outreach_ids_json, numbers_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                decision_id, actor, str(campaign_id or ""), str(kind or "")[:60],
                scope if scope in (SCOPE_ROWS, SCOPE_CAMPAIGN) else SCOPE_ROWS,
                1 if applied else 0, json.dumps(ids),
                json.dumps(numbers or {}, default=str, sort_keys=True)[:8000], _now(now),
            ),
        )
        db.commit()
        return decision_id
    except Exception:
        logger.warning("agent decision not recorded (%s)", actor, exc_info=True)
        return ""


def stamp_rows(outreach_ids: Iterable[str], decision_id: str) -> int:
    """Name the decision on each row it changed. Never raises.

    A bare UPDATE, not ``update_outreach``: that bumps ``updated_at``, which is
    what dates a close here, and is not news for the cloud sync."""
    ids = [str(i) for i in outreach_ids if i]
    if not decision_id or not ids:
        return 0
    try:
        db = get_db()
        marks = ", ".join("?" * len(ids))
        cur = db.execute(f"UPDATE outreaches SET decision_id = ? WHERE id IN ({marks})", (decision_id, *ids))
        db.commit()
        return int(cur.rowcount or 0)
    except Exception:
        logger.warning("decision_id not stamped on %d rows", len(ids), exc_info=True)
        return 0


def _touched_rows(db: Any, decision: dict[str, Any]) -> list[dict[str, Any]]:
    cols = "o.id, o.accepted_at, o.first_reply_at, o.status, o.updated_at"
    if decision.get("scope") == SCOPE_CAMPAIGN:
        if not decision.get("campaign_id"):
            return []
        rows = db.execute(
            f"SELECT {cols} FROM outreaches o WHERE o.campaign_id = ?", (decision["campaign_id"],),
        ).fetchall()
        return [dict(r) for r in rows]
    try:
        ids = [str(i) for i in json.loads(decision.get("outreach_ids_json") or "[]") if i]
    except (TypeError, ValueError):
        ids = []
    if not ids:
        return []
    marks = ", ".join("?" * len(ids))
    rows = db.execute(f"SELECT {cols} FROM outreaches o WHERE o.id IN ({marks})", tuple(ids)).fetchall()
    return [dict(r) for r in rows]


def _meetings(db: Any, ids: list[str], start: int, end: int) -> int:
    if not ids:
        return 0
    marks = ", ".join("?" * len(ids))
    try:
        row = db.execute(
            "SELECT COUNT(DISTINCT outreach_id) AS n FROM calendar_events "
            f"WHERE outreach_id IN ({marks}) AND created_at > ? AND created_at <= ?",
            (*ids, start, end),
        ).fetchone()
    except Exception:  # an install that never created calendar_events
        return 0
    return int((row["n"] if row else 0) or 0)


def _counts(db: Any, rows: list[dict[str, Any]], start: int, end: int) -> dict[str, int]:
    def within(value: Any) -> bool:
        try:
            return start < int(value or 0) <= end
        except (TypeError, ValueError):
            return False

    return {
        "touched": len(rows),
        "accepted_delta": sum(1 for r in rows if within(r.get("accepted_at"))),
        "reply_delta": sum(1 for r in rows if within(r.get("first_reply_at"))),
        "closed_delta": sum(
            1 for r in rows if r.get("status") in _CLOSED_STATUSES and within(r.get("updated_at"))
        ),
        "meeting_delta": _meetings(db, [str(r["id"]) for r in rows], start, end),
    }


def score_due(*, now: int | None = None, limit: int = SCORE_BATCH) -> int:
    """Score every unscored decision that is 48 h old. Idempotent."""
    when = _now(now)
    db = get_db()
    due = db.execute(
        "SELECT d.* FROM agent_decisions d "
        "LEFT JOIN agent_decision_outcomes o ON o.decision_id = d.id "
        "WHERE d.created_at <= ? AND o.decision_id IS NULL ORDER BY d.created_at LIMIT ?",
        (when - SCORE_AFTER_SECONDS, max(1, int(limit))),
    ).fetchall()
    scored = 0
    for raw in due:
        decision = dict(raw)
        decided_at = int(decision["created_at"])
        try:
            c = _counts(db, _touched_rows(db, decision), decided_at, decided_at + SCORE_AFTER_SECONDS)
            db.execute(
                "INSERT OR IGNORE INTO agent_decision_outcomes (decision_id, actor, campaign_id, kind, "
                "applied, decided_at, scored_at, window_seconds, touched, accepted_delta, reply_delta, "
                "closed_delta, meeting_delta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision["id"], decision["actor"], decision["campaign_id"], decision["kind"],
                    int(decision["applied"] or 0), decided_at, when, SCORE_AFTER_SECONDS,
                    c["touched"], c["accepted_delta"], c["reply_delta"], c["closed_delta"],
                    c["meeting_delta"],
                ),
            )
            db.commit()
            scored += 1
        except Exception:
            logger.warning("decision %s not scored", str(decision.get("id"))[:8], exc_info=True)
    return scored


def track_record(actor: str, *, now: int | None = None) -> dict[str, Any] | None:
    """The actor's scored decisions over the last 30 days."""
    if not actor:
        return None
    row = get_db().execute(
        "SELECT COUNT(*) AS decisions, COALESCE(SUM(applied), 0) AS applied, "
        "COALESCE(SUM(touched), 0) AS touched, COALESCE(SUM(accepted_delta), 0) AS accepted, "
        "COALESCE(SUM(reply_delta), 0) AS replied, COALESCE(SUM(closed_delta), 0) AS closed, "
        "COALESCE(SUM(meeting_delta), 0) AS meetings "
        "FROM agent_decision_outcomes WHERE actor = ? AND decided_at >= ?",
        (actor, _now(now) - TRACK_RECORD_DAYS * 86400),
    ).fetchone()
    data = {k: int(row[k] or 0) for k in row.keys()} if row else {}
    data["days"] = TRACK_RECORD_DAYS
    return data
