"""Official inspectable scratch for in-process agents.

Sidecar beats after every loop. Tools may leave one short note per run.
Nothing here writes LinkedIn, email, or calendar.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from ..db.schema import get_db

VALID_AGENTS = frozenset({
    "reply", "strategist", "closer", "icp_research", "coordinator", "product",
})
NOTE_MAX_CHARS = 500
BEAT_REASON_MAX = 240
DIGEST_MAX_CHARS = 800
NOTE_TTL_SECONDS = 24 * 3600
STALE_AFTER_SECONDS = 6 * 3600


def beat_is_stale(
    beat: dict[str, Any],
    *,
    mode: str,
    now: int | None = None,
) -> bool:
    if (mode or "").strip().lower() == "off":
        return False
    ts = int(beat.get("created_at") or 0)
    when = int(now if now is not None else time.time())
    return bool(ts) and (when - ts) > STALE_AFTER_SECONDS


def upsert_beat(
    *,
    agent: str,
    campaign_id: str,
    decision: str,
    reason: str,
    timestamp: int | None = None,
) -> None:
    if agent not in VALID_AGENTS:
        return
    cid = campaign_id or ""
    ts = int(timestamp if timestamp is not None else time.time())
    text = (reason or "")[:BEAT_REASON_MAX]
    db = get_db()
    try:
        row = db.execute(
            """SELECT id FROM agent_commons
               WHERE kind = 'beat' AND campaign_id = ? AND agent = ?""",
            (cid, agent),
        ).fetchone()
        if row:
            db.execute(
                """UPDATE agent_commons
                   SET decision = ?, reason = ?, created_at = ?
                   WHERE id = ?""",
                (decision, text, ts, row["id"]),
            )
        else:
            db.execute(
                """INSERT INTO agent_commons
                   (id, kind, agent, campaign_id, decision, reason, created_at)
                   VALUES (?, 'beat', ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), agent, cid, decision, text, ts),
            )
        db.commit()
    finally:
        db.close()


def insert_note(
    *,
    agent: str,
    campaign_id: str,
    body: str,
    outreach_id: str = "",
    now: int | None = None,
) -> str:
    if agent not in VALID_AGENTS:
        return "refused: unknown agent"
    text = (body or "").strip()
    if not text:
        return "refused: empty note"
    if len(text) > NOTE_MAX_CHARS:
        return f"refused: note longer than {NOTE_MAX_CHARS} characters"
    created = int(now if now is not None else time.time())
    db = get_db()
    try:
        db.execute(
            """INSERT INTO agent_commons
               (id, kind, agent, campaign_id, outreach_id, body, created_at, expires_at)
               VALUES (?, 'note', ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                agent,
                campaign_id or "",
                outreach_id or None,
                text,
                created,
                created + NOTE_TTL_SECONDS,
            ),
        )
        db.commit()
    finally:
        db.close()
    return "ok"


def list_beats(*, campaign_id: str = "") -> list[dict[str, Any]]:
    db = get_db()
    try:
        if campaign_id:
            rows = db.execute(
                """SELECT * FROM agent_commons
                   WHERE kind = 'beat' AND (campaign_id = ? OR campaign_id = '')
                   ORDER BY created_at DESC""",
                (campaign_id,),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT * FROM agent_commons
                   WHERE kind = 'beat'
                   ORDER BY created_at DESC""",
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def list_live_notes(
    *,
    campaign_id: str = "",
    outreach_id: str = "",
    now: int | None = None,
) -> list[dict[str, Any]]:
    when = int(now if now is not None else time.time())
    db = get_db()
    try:
        params: list[Any] = [when]
        where = ["kind = 'note'", "(expires_at IS NULL OR expires_at > ?)"]
        if campaign_id:
            where.append("(campaign_id = ? OR campaign_id = '')")
            params.append(campaign_id)
        if outreach_id:
            where.append("(outreach_id = ? OR outreach_id IS NULL)")
            params.append(outreach_id)
        rows = db.execute(
            f"""SELECT * FROM agent_commons
                WHERE {' AND '.join(where)}
                ORDER BY created_at DESC""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def upsert_campaign_row(
    *,
    kind: str,
    campaign_id: str,
    body: str = "",
    reason: str = "",
    now: int | None = None,
) -> None:
    if kind not in {"digest", "hold"}:
        return
    if kind == "hold" and not (campaign_id or "").strip():
        return
    cid = campaign_id or ""
    ts = int(now if now is not None else time.time())
    text_body = (body or "")[:DIGEST_MAX_CHARS]
    text_reason = (reason or "")[:BEAT_REASON_MAX]
    db = get_db()
    try:
        row = db.execute(
            """SELECT id FROM agent_commons
               WHERE kind = ? AND campaign_id = ?""",
            (kind, cid),
        ).fetchone()
        if row:
            db.execute(
                """UPDATE agent_commons
                   SET body = ?, reason = ?, created_at = ?
                   WHERE id = ?""",
                (text_body, text_reason, ts, row["id"]),
            )
        else:
            db.execute(
                """INSERT INTO agent_commons
                   (id, kind, agent, campaign_id, body, reason, created_at)
                   VALUES (?, ?, 'coordinator', ?, ?, ?, ?)""",
                (str(uuid.uuid4()), kind, cid, text_body, text_reason, ts),
            )
        db.commit()
    finally:
        db.close()


def _get_campaign_row(kind: str, campaign_id: str) -> dict[str, Any] | None:
    db = get_db()
    try:
        row = db.execute(
            """SELECT * FROM agent_commons
               WHERE kind = ? AND campaign_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (kind, campaign_id or ""),
        ).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def get_digest(campaign_id: str) -> dict[str, Any] | None:
    return _get_campaign_row("digest", campaign_id)


def get_hold(campaign_id: str) -> dict[str, Any] | None:
    return _get_campaign_row("hold", campaign_id)


def clear_hold(campaign_id: str) -> None:
    db = get_db()
    try:
        db.execute(
            "DELETE FROM agent_commons WHERE kind = 'hold' AND campaign_id = ?",
            (campaign_id or "",),
        )
        db.commit()
    finally:
        db.close()


def list_coordinator_holds(*, campaign_id: str = "") -> list[dict[str, Any]]:
    db = get_db()
    try:
        if campaign_id:
            rows = db.execute(
                """SELECT * FROM agent_commons
                   WHERE kind = 'hold' AND campaign_id = ?
                   ORDER BY created_at DESC""",
                (campaign_id,),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT * FROM agent_commons
                   WHERE kind = 'hold'
                   ORDER BY created_at DESC""",
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def read_commons_text(
    *,
    campaign_id: str = "",
    outreach_id: str = "",
    now: int | None = None,
) -> str:
    beats = list_beats(campaign_id=campaign_id)
    notes = list_live_notes(campaign_id=campaign_id, outreach_id=outreach_id, now=now)
    lines = ["beats:"]
    if beats:
        for beat in beats:
            lines.append(
                f"- {beat.get('agent')}: {beat.get('decision') or '?'} "
                f"— {(beat.get('reason') or '')[:120]}"
            )
    else:
        lines.append("- (none)")
    lines.append("notes:")
    if notes:
        for note in notes:
            lines.append(f"- {note.get('agent')}: {note.get('body') or ''}")
    else:
        lines.append("- (none)")
    return "\n".join(lines)


def commons_tools(
    *,
    agent: str,
    campaign_id: str,
    outreach_id: str = "",
) -> dict[str, Callable[..., str]]:
    wrote = {"n": 0}

    def read_commons() -> str:
        return read_commons_text(campaign_id=campaign_id, outreach_id=outreach_id)

    def write_commons(reason: str = "", note: str = "") -> str:
        if wrote["n"]:
            return "refused: one note per run"
        result = insert_note(
            agent=agent,
            campaign_id=campaign_id,
            outreach_id=outreach_id,
            body=(note or reason),
        )
        if result == "ok":
            wrote["n"] += 1
        return result

    return {"read_commons": read_commons, "write_commons": write_commons}


def async_commons_tools(
    *,
    agent: str,
    campaign_id: str,
    outreach_id: str = "",
) -> dict[str, Callable[..., Any]]:
    """Same tools as commons_tools, safe to call from the asyncio loop."""
    from ..db.async_bridge import run_db

    sync = commons_tools(
        agent=agent, campaign_id=campaign_id, outreach_id=outreach_id,
    )

    async def read_commons() -> str:
        return await run_db(sync["read_commons"])

    async def write_commons(reason: str = "", note: str = "") -> str:
        return await run_db(sync["write_commons"], reason, note)

    return {"read_commons": read_commons, "write_commons": write_commons}


def record_agent_beat(
    *,
    agent: str,
    campaign_id: str,
    decision: str,
    reason: str,
) -> None:
    upsert_beat(
        agent=agent,
        campaign_id=campaign_id,
        decision=decision,
        reason=reason,
    )
