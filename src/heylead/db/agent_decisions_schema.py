"""The local agent decision ledger (heylead-api#1209, SCHEMA_VERSION 16).

Mirrors heylead-api ``app/db/agent_decisions_schema.py`` for a machine that
sends itself: ``agent_decisions`` (what an agent decided and the numbers it
saw), ``agent_decision_outcomes`` (what its rows did in the 48 h after) and
``outreaches.decision_id`` (the decision that last changed the row's plan).
Idempotent, so the migration pass can run it any number of times.
"""

from __future__ import annotations

import sqlite3

STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS agent_decisions (
        id                TEXT PRIMARY KEY,
        actor             TEXT NOT NULL,
        campaign_id       TEXT NOT NULL DEFAULT '',
        kind              TEXT NOT NULL DEFAULT '',
        scope             TEXT NOT NULL DEFAULT 'rows',
        applied           INTEGER NOT NULL DEFAULT 0,
        outreach_ids_json TEXT NOT NULL DEFAULT '[]',
        numbers_json      TEXT NOT NULL DEFAULT '{}',
        created_at        INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS agent_decision_outcomes (
        decision_id    TEXT PRIMARY KEY,
        actor          TEXT NOT NULL,
        campaign_id    TEXT NOT NULL DEFAULT '',
        kind           TEXT NOT NULL DEFAULT '',
        applied        INTEGER NOT NULL DEFAULT 0,
        decided_at     INTEGER NOT NULL,
        scored_at      INTEGER NOT NULL,
        window_seconds INTEGER NOT NULL,
        touched        INTEGER NOT NULL DEFAULT 0,
        accepted_delta INTEGER NOT NULL DEFAULT 0,
        reply_delta    INTEGER NOT NULL DEFAULT 0,
        closed_delta   INTEGER NOT NULL DEFAULT 0,
        meeting_delta  INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_agent_decisions_time ON agent_decisions(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agent_decision_outcomes_actor "
    "ON agent_decision_outcomes(actor, decided_at)",
)


def install(conn: sqlite3.Connection) -> None:
    """Create both tables and add ``outreaches.decision_id``. Idempotent."""
    for statement in STATEMENTS:
        conn.execute(statement)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(outreaches)").fetchall()}
    if "decision_id" not in columns:
        conn.execute("ALTER TABLE outreaches ADD COLUMN decision_id TEXT")
    conn.commit()
