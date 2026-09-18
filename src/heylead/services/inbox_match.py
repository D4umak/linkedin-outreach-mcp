"""Pick the outreach an inbound LinkedIn message actually belongs to.

The same person can exist as several contacts/outreaches (refill duplicates,
cross-campaign copies, provider_id stored as linkedin_id). Indexing them by
provider_id with last-write-wins attaches replies to a skipped row that was
never touched, and `_answers_our_outreach` then drops a real reply.

Prefer the row we actually sent on — invited_at or a live funnel status —
then active campaigns, then the most recent invite.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..db.schema import get_db

logger = logging.getLogger(__name__)

_STATUS_SCORE = {
    "hot_lead": 80,
    "replied": 70,
    "messaged": 60,
    "connected": 50,
    "invited": 40,
    "sending": 20,
    "sending_followup": 20,
    "pending": 10,
    "error": 3,
    "skipped": 1,
    "opted_out": 0,
}

_CAMPAIGN_SCORE = {
    "active": 20,
    "paused": 5,
    "draft": 2,
}


def _match_key(row: dict[str, Any]) -> tuple:
    invited_at = int(row.get("invited_at") or 0)
    has_touch = 1 if invited_at > 0 else 0
    status_score = _STATUS_SCORE.get(row.get("status") or "", 5)
    camp_score = _CAMPAIGN_SCORE.get(row.get("campaign_status") or "", 0)
    return (has_touch, status_score, camp_score, invited_at)


def prefer_inbox_contact(
    current: dict[str, Any] | None,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Return the better of two outreach-contact rows for the same person."""
    if current is None:
        return candidate
    return candidate if _match_key(candidate) > _match_key(current) else current


def index_contacts_for_inbox(
    rows: list[Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any] | None]]:
    """Build provider_id / linkedin_id lookup, preferring the touched outreach.

    Name fallback stays disabled when two *different* rows share a name —
    "John Smith" collisions must not steal replies.
    """
    contact_lookup: dict[str, dict[str, Any]] = {}
    name_lookup: dict[str, dict[str, Any] | None] = {}
    for row in rows:
        r = dict(row)
        keys: list[str] = []
        linkedin_id = r.get("linkedin_id") or ""
        if linkedin_id:
            keys.append(linkedin_id)
        pj = r.pop("profile_json", None)
        if pj:
            try:
                profile = json.loads(pj) if isinstance(pj, str) else pj
                provider_id = (profile or {}).get("provider_id") or ""
                if provider_id:
                    keys.append(provider_id)
            except Exception:
                logger.debug("inbox_match: unreadable profile_json", exc_info=True)
        for key in keys:
            contact_lookup[key] = prefer_inbox_contact(contact_lookup.get(key), r)
        name_key = (r.get("name") or "").strip().lower()
        if not name_key:
            continue
        existing = name_lookup.get(name_key)
        if name_key in name_lookup and existing is not None:
            if existing.get("outreach_id") != r.get("outreach_id"):
                name_lookup[name_key] = None
        elif name_key not in name_lookup:
            name_lookup[name_key] = r
    return contact_lookup, name_lookup


def find_best_inbox_match(sender_id: str) -> dict[str, Any] | None:
    """Best outreach for a LinkedIn sender_id (provider_id or public slug)."""
    if not sender_id:
        return None
    db = get_db()
    rows = db.execute(
        """SELECT o.id as outreach_id, o.contact_id, o.status, o.invited_at,
                  o.campaign_id, camp.status as campaign_status,
                  c.id, c.linkedin_id, c.name, c.title, c.company, c.fit_score,
                  c.profile_json
           FROM contacts c
           JOIN outreaches o ON o.contact_id = c.id
           LEFT JOIN campaigns camp ON camp.id = o.campaign_id
           WHERE o.status NOT IN ('opted_out')
             AND (
               c.linkedin_id = ?
               OR json_extract(
                    CASE WHEN json_valid(c.profile_json) THEN c.profile_json ELSE '{}' END,
                    '$.provider_id'
                  ) = ?
             )""",
        (sender_id, sender_id),
    ).fetchall()
    db.close()
    best: dict[str, Any] | None = None
    for row in rows:
        best = prefer_inbox_contact(best, dict(row))
    if best:
        best.pop("profile_json", None)
    return best
