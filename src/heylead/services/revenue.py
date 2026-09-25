"""Revenue on this machine: what a won outreach was worth, and "is it won".

Outcome D4umak/heylead-api#1212 (24 Sep 2026). The hosted twin is
heylead-api ``app/services/revenue.py``; the rules are the same:

* ``is_won`` is the one answer to "is this outreach won". Python code that
  decides it reads this, never a literal (tests/test_every_won_check_reads_is_won.py).
* ``deal_value`` / ``deal_currency`` / ``won_at`` live on ``outreaches``. They
  are added here, on first use, rather than by a schema version: the column
  set is additive and only this module reads or writes it, so a database a
  parallel schema bump stamps ahead still gets them.
* a CRM read-back never downgrades: an open or reopened deal leaves a won
  outreach won and writes no amount on an open one.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from typing import Any

from ..constants import OUTREACH_CLOSED_HAPPY

WON_STATUSES: frozenset[str] = frozenset({OUTREACH_CLOSED_HAPPY, "won"})
DEFAULT_CURRENCY = "USD"
_CURRENCY = re.compile(r"^[A-Za-z]{3}$")
_COLUMNS = (
    ("deal_value", "REAL"),
    ("deal_currency", "TEXT NOT NULL DEFAULT ''"),
    ("won_at", "INTEGER"),
)


def is_won(status: Any) -> bool:
    """Whether an outreach status (or a close outcome word) means won."""
    return str(status or "").strip() in WON_STATUSES


def normalize_currency(value: Any) -> str:
    text = str(value or "").strip()
    return text.upper() if _CURRENCY.match(text) else ""


def parse_deal_value(value: Any) -> float | None:
    """A finite, non-negative amount; None when absent; ValueError otherwise."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        amount = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        raise ValueError(f"deal_value must be a number, got {value!r}") from None
    if not math.isfinite(amount) or amount < 0:
        raise ValueError(f"deal_value must be zero or more, got {value!r}")
    return round(amount, 2)


def format_amount(amount: float, currency: str) -> str:
    number = f"{amount:,.0f}" if float(amount).is_integer() else f"{amount:,.2f}"
    return f"{currency} {number}".strip()


def ensure_columns(db: Any) -> None:
    """Add the three deal columns to ``outreaches`` when they are missing."""
    have = {r[1] for r in db.execute("PRAGMA table_info(outreaches)").fetchall()}
    for column, col_type in _COLUMNS:
        if column in have:
            continue
        try:
            db.execute(f"ALTER TABLE outreaches ADD COLUMN {column} {col_type}")
            db.commit()
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise


def _db():
    from ..db.schema import get_db
    db = get_db()
    ensure_columns(db)
    return db


def last_currency() -> str:
    """The currency the last deal was recorded in, else USD."""
    row = _db().execute(
        "SELECT deal_currency FROM outreaches WHERE won_at IS NOT NULL "
        "AND deal_currency <> '' ORDER BY won_at DESC LIMIT 1"
    ).fetchone()
    return (normalize_currency(row[0]) if row else "") or DEFAULT_CURRENCY


def stamp_won(
    outreach_id: str,
    deal_value: float | None,
    deal_currency: str = "",
    won_at: int | None = None,
) -> str:
    """Write the deal columns on a won outreach. ``won_at`` is kept when set.

    Returns the currency stored ('' when no amount was given)."""
    db = _db()
    currency = normalize_currency(deal_currency)
    if deal_value is not None and not currency:
        currency = last_currency()
    db.execute(
        "UPDATE outreaches SET won_at = COALESCE(won_at, ?), "
        "deal_value = COALESCE(?, deal_value), "
        "deal_currency = CASE WHEN ? <> '' THEN ? ELSE deal_currency END "
        "WHERE id = ?",
        (int(won_at or time.time()), deal_value, currency, currency, outreach_id),
    )
    db.commit()
    return currency


def get_deal(outreach_id: str) -> dict[str, Any] | None:
    row = _db().execute(
        "SELECT id, status, deal_value, deal_currency, won_at FROM outreaches WHERE id = ?",
        (outreach_id,),
    ).fetchone()
    return dict(row) if row else None


def apply_crm_deal(
    outreach_id: str,
    *,
    closed_won: bool,
    deal_value: float | None,
    deal_currency: str = "",
    closed_at: int | None = None,
    source: str = "hubspot",
) -> dict[str, Any]:
    """Apply a CRM deal to a local outreach. Never downgrades.

    Returns ``{"changed", "marked_won", "status"}``."""
    current = get_deal(outreach_id)
    if not current:
        return {"changed": False, "marked_won": False, "status": ""}
    status = str(current.get("status") or "")
    if not closed_won:
        return {"changed": False, "marked_won": False, "status": status}
    if not is_won(status):
        db = _db()
        outcome = {
            "outcome": "won",
            "reason": f"{source}: deal closed won",
            "closed_at": int(closed_at or time.time()),
            "previous_status": status,
        }
        db.execute(
            "UPDATE outreaches SET status = ?, outcome_json = ?, next_action = NULL, "
            "updated_at = ? WHERE id = ?",
            (OUTREACH_CLOSED_HAPPY, json.dumps(outcome), int(time.time()), outreach_id),
        )
        db.commit()
        stamp_won(outreach_id, deal_value, deal_currency, won_at=closed_at)
        return {"changed": True, "marked_won": True, "status": OUTREACH_CLOSED_HAPPY}
    before = (current.get("deal_value"), current.get("deal_currency") or "")
    stamp_won(outreach_id, deal_value, deal_currency, won_at=closed_at)
    after = get_deal(outreach_id) or {}
    changed = before != (after.get("deal_value"), after.get("deal_currency") or "")
    return {"changed": changed, "marked_won": False, "status": status}


def mapped_outreaches(campaign_id: str = "") -> list[dict[str, Any]]:
    """Outreaches whose contact has a HubSpot mapping: what a pull can read."""
    sql = (
        "SELECT o.id AS outreach_id, o.campaign_id, o.status, o.deal_value, o.deal_currency, "
        "o.contact_id, c.name, m.crm_contact_id, m.crm_deal_id "
        "FROM outreaches o JOIN contacts c ON c.id = o.contact_id "
        "JOIN crm_mappings m ON m.contact_id = o.contact_id AND m.crm_type = 'hubspot' "
    )
    params: tuple[Any, ...] = ()
    if campaign_id:
        sql += "WHERE o.campaign_id = ? "
        params = (campaign_id,)
    rows = _db().execute(sql + "ORDER BY o.updated_at DESC", params).fetchall()
    return [dict(r) for r in rows]


def revenue_by_currency(campaign_id: str = "") -> dict[str, float]:
    """Sum of won amounts per currency, no conversion."""
    sql = "SELECT status, deal_value, deal_currency FROM outreaches WHERE deal_value IS NOT NULL"
    params: tuple[Any, ...] = ()
    if campaign_id:
        sql += " AND campaign_id = ?"
        params = (campaign_id,)
    out: dict[str, float] = {}
    for row in _db().execute(sql, params).fetchall():
        if not is_won(row["status"]):
            continue
        code = normalize_currency(row["deal_currency"]) or DEFAULT_CURRENCY
        out[code] = round(out.get(code, 0.0) + float(row["deal_value"]), 2)
    return out
