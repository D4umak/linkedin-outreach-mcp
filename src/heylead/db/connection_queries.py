"""Query helpers for the local connections table.

Used by the ``my_connections`` contacts action to search 1st-degree
LinkedIn connections that have been synced locally via connection_sync.
All functions are **synchronous** and must be called via ``run_db()``.
"""

from __future__ import annotations

import time
from typing import Any

from .schema import get_db


def _connected_window_sql(
    connected_since: int | None, connected_before: int | None,
) -> tuple[str, list[int]]:
    """SQL + params for the connected_at window filters.

    A NULL connected_at is deliberately excluded from both windows rather than
    swept into one of them: "connected since March" must not answer with rows
    whose date we simply never learned. The exclusion rule reads NULL the other
    way — as pre-existing — because there the safe default is not messaging.
    """
    clauses: list[str] = []
    params: list[int] = []
    if connected_since:
        clauses.append("connected_at IS NOT NULL AND connected_at >= ?")
        params.append(int(connected_since))
    if connected_before:
        clauses.append("connected_at IS NOT NULL AND connected_at < ?")
        params.append(int(connected_before))
    return ("".join(f" AND ({c})" for c in clauses), params)


def search_my_connections(
    query: str,
    account_id: str,
    limit: int = 25,
    connected_since: int | None = None,
    connected_before: int | None = None,
) -> list[dict[str, Any]]:
    """Full-text LIKE search across name, headline, company, location.

    Splits query into keywords and matches connections containing ANY keyword.
    Results are ranked by number of keyword hits and field weight:
    name match (4) > headline (3) > company (2) > location (1).

    Soft-deleted rows (``removed_at``) are never returned: a pruned connection
    is not a connection today, whatever date it still carries.
    """
    db = get_db()
    try:
        keywords = [kw.strip() for kw in query.split() if kw.strip()]
        if not keywords:
            return []

        # Build per-keyword match clauses
        where_parts: list[str] = []
        score_parts: list[str] = []
        # Kept apart rather than interleaved-then-sliced: SQLite binds ? by
        # position, so mixing the two groups makes each keyword's pattern land
        # in the wrong clause.
        where_params: list[str] = []
        score_params: list[str] = []

        for _kw in keywords:
            pat = f"%{_kw}%"
            where_parts.append(
                "(LOWER(name) LIKE LOWER(?) OR LOWER(headline) LIKE LOWER(?)"
                " OR LOWER(company) LIKE LOWER(?) OR LOWER(location) LIKE LOWER(?))"
            )
            where_params.extend([pat, pat, pat, pat])
            score_parts.append(
                "(CASE WHEN LOWER(name) LIKE LOWER(?) THEN 4 ELSE 0 END"
                " + CASE WHEN LOWER(headline) LIKE LOWER(?) THEN 3 ELSE 0 END"
                " + CASE WHEN LOWER(company) LIKE LOWER(?) THEN 2 ELSE 0 END"
                " + CASE WHEN LOWER(location) LIKE LOWER(?) THEN 1 ELSE 0 END)"
            )
            score_params.extend([pat, pat, pat, pat])

        where_clause = " OR ".join(where_parts)
        score_expr = " + ".join(score_parts)

        window_sql, window_params = _connected_window_sql(
            connected_since, connected_before,
        )
        sql = f"""SELECT provider_id, public_id, name, headline,
                         company, location, profile_url, synced_at, connected_at,
                         ({score_expr}) AS relevance
                  FROM connections
                  WHERE account_id = ? AND removed_at IS NULL
                        AND ({where_clause}){window_sql}
                  ORDER BY relevance DESC, name ASC
                  LIMIT ?"""
        all_params = (
            score_params + [account_id] + where_params + window_params + [limit]
        )

        rows = db.execute(sql, all_params).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def list_all_connections(
    account_id: str,
    limit: int = 50,
    offset: int = 0,
    connected_since: int | None = None,
    connected_before: int | None = None,
) -> list[dict[str, Any]]:
    """List all locally-synced (non-pruned) connections ordered by name."""
    db = get_db()
    try:
        window_sql, window_params = _connected_window_sql(
            connected_since, connected_before,
        )
        rows = db.execute(
            f"""SELECT provider_id, public_id, name, headline,
                      company, location, profile_url, synced_at, connected_at
               FROM connections
               WHERE account_id = ? AND removed_at IS NULL{window_sql}
               ORDER BY name ASC
               LIMIT ? OFFSET ?""",
            (account_id, *window_params, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def get_connection_sync_status(account_id: str) -> dict[str, Any]:
    """Return sync status: count, last_synced timestamp, and age in seconds."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT COUNT(*) AS cnt, MAX(synced_at) AS last_synced, "
            "SUM(CASE WHEN connected_at IS NULL THEN 1 ELSE 0 END) AS undated "
            "FROM connections WHERE account_id = ? AND removed_at IS NULL",
            (account_id,),
        ).fetchone()
        count = row["cnt"] if row else 0
        last_synced = row["last_synced"] if row else None
        age = int(time.time()) - last_synced if last_synced else None
        return {
            "count": count,
            "last_synced": last_synced,
            "age_seconds": age,
            "synced": count > 0,
            "undated": (row["undated"] if row else 0) or 0,
        }
    finally:
        db.close()
