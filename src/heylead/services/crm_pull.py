"""Read deals back from HubSpot: crm_sync(action='pull').

Until 24 Sep 2026 crm_sync only pushed. A deal a person moved to closed won
in HubSpot, with its amount, never reached HeyLead, so the funnel ended at a
count (heylead-api#1212). For every outreach whose contact is mapped
(``crm_mappings``), this reads the contact's deals, and when one is closed
won it records the amount, the currency and the day, and marks the outreach
won if it was not. It never downgrades: an open or reopened deal changes
nothing. On a hosted workspace the same deal goes to the cloud's
``POST /prospects/{id}/deal``, which applies the same rule.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from .. import config
from ..db.async_bridge import run_db
from . import revenue

logger = logging.getLogger(__name__)


def deal_is_closed_won(props: dict[str, Any]) -> bool:
    flag = str(props.get("hs_is_closed_won") or "").strip().lower()
    if flag in ("true", "false"):
        return flag == "true"
    return str(props.get("dealstage") or "").strip().lower() == "closedwon"


def deal_amount(props: dict[str, Any]) -> float | None:
    try:
        return revenue.parse_deal_value(props.get("amount"))
    except ValueError:
        return None


def deal_closed_at(props: dict[str, Any]) -> int | None:
    raw = str(props.get("closedate") or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        value = int(raw)
        return value // 1000 if value > 10**11 else value
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def pick_deal(deals: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The closed-won deal most recently closed, else None."""
    won = [d for d in deals if deal_is_closed_won(d.get("properties") or {})]
    if not won:
        return None
    return max(won, key=lambda d: deal_closed_at(d.get("properties") or {}) or 0)


async def push_deal_to_cloud(
    outreach_id: str,
    *,
    closed_won: bool,
    deal_value: float | None,
    deal_currency: str,
    closed_at: int | None,
    source: str = "hubspot",
) -> bool:
    """Send one deal to the hosted workspace. False when not hosted or it failed."""
    if not config.is_backend_mode():
        return False
    from .cloud_sync import _TIMEOUT, _base_url, _headers

    payload: dict[str, Any] = {
        "closed_won": closed_won, "deal_value": deal_value,
        "deal_currency": deal_currency, "closed_at": closed_at, "source": source,
    }
    url = f"{_base_url()}/api/v1/prospects/{outreach_id}/deal"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.post(url, json=payload, headers=_headers())
            resp.raise_for_status()
            return True
        except httpx.HTTPError as e:
            logger.warning("Backend deal sync failed for %s: %s", outreach_id, e)
            return False


async def pull_deals(hs: Any, campaign_id: str = "") -> dict[str, Any]:
    """Read HubSpot deals for every mapped outreach and apply them.

    Returns counts plus one row per outreach that changed."""
    rows = await run_db(revenue.mapped_outreaches, campaign_id)
    summary: dict[str, Any] = {
        "checked": 0, "marked_won": 0, "amount_updated": 0, "open": 0,
        "cloud_synced": 0, "errors": [], "changed": [],
    }
    for row in rows:
        summary["checked"] += 1
        name = row.get("name") or "Unknown"
        try:
            deal_ids: list[str] = []
            if row.get("crm_contact_id"):
                deal_ids = await hs.get_contact_deal_ids(row["crm_contact_id"])
            if row.get("crm_deal_id") and row["crm_deal_id"] not in deal_ids:
                deal_ids.append(row["crm_deal_id"])
            deals = await hs.get_deals(deal_ids) if deal_ids else []
        except Exception as e:  # one contact's failure must not stop the rest
            summary["errors"].append(f"{name}: {e}")
            continue
        deal = pick_deal(deals)
        if deal is None:
            summary["open"] += 1
            continue
        props = deal.get("properties") or {}
        amount = deal_amount(props)
        currency = revenue.normalize_currency(props.get("deal_currency_code"))
        closed_at = deal_closed_at(props)
        result = await run_db(
            revenue.apply_crm_deal, row["outreach_id"],
            closed_won=True, deal_value=amount, deal_currency=currency,
            closed_at=closed_at, source="hubspot",
        )
        if result["marked_won"]:
            summary["marked_won"] += 1
        elif result["changed"]:
            summary["amount_updated"] += 1
        if result["changed"]:
            stored = await run_db(revenue.get_deal, row["outreach_id"]) or {}
            summary["changed"].append({
                "name": name,
                "amount": stored.get("deal_value"),
                "currency": stored.get("deal_currency") or "",
                "marked_won": result["marked_won"],
            })
        if await push_deal_to_cloud(
            row["outreach_id"], closed_won=True, deal_value=amount,
            deal_currency=currency, closed_at=closed_at,
        ):
            summary["cloud_synced"] += 1
    return summary
