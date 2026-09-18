"""SQLite CRUD helpers for the global contact base (master record per person)."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from typing import Any, Optional

from .schema import get_db

logger = logging.getLogger(__name__)

# Lifecycle stage ordering — only promote forward, never demote.
# Higher number = more advanced stage.
_LIFECYCLE_ORDER: dict[str, int] = {
    "prospect": 0,
    "contacted": 1,
    "connected": 2,
    "engaged": 3,
    "lost": 3,  # same level as engaged (can go engaged→lost, not customer→lost)
    "customer": 4,
    "churned": 5,
    "do_not_contact": 6,
}

# Map outreach status → global contact lifecycle stage
_OUTREACH_TO_LIFECYCLE: dict[str, str] = {
    "invited": "contacted",
    "connected": "connected",
    "messaged": "connected",
    "replied": "engaged",
    "hot_lead": "engaged",
    "closed_happy": "customer",
    "closed_unhappy": "lost",
}


# ──────────────────────────────────────────────
# Core CRUD
# ──────────────────────────────────────────────

def _email_key(email: str, profile_json: str = "") -> str:
    """Canonical address for identity match. Blank is not an identity."""
    from ..services.prospect_email import extract_profile_email

    return extract_profile_email(email, profile_json).strip().lower()


def _profile_dict(profile_json: str = "") -> dict[str, Any]:
    if not profile_json:
        return {}
    try:
        parsed = json.loads(profile_json) if isinstance(profile_json, str) else profile_json
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _incoming_provider_id(linkedin_id: str, profile_json: str = "") -> str:
    pid = (_profile_dict(profile_json).get("provider_id") or "").strip()
    if pid:
        return pid
    raw = (linkedin_id or "").strip()
    return raw if raw.startswith("ACoAA") else ""


def _incoming_public_id(linkedin_id: str, profile_json: str = "") -> str:
    blob = _profile_dict(profile_json)
    pub = (blob.get("public_id") or blob.get("public_identifier") or "").strip()
    if pub:
        return pub
    raw = (linkedin_id or "").strip()
    if raw and not raw.startswith("ACoAA"):
        return raw
    return ""


def _is_provider_id(value: str) -> bool:
    return (value or "").startswith("ACoAA")


def keep_public_id(profile_json: str, public_id: str) -> str:
    """Record the slug in the profile, because the column no longer holds it.

    linkedin_id carries the provider id whenever one is known — deliberately,
    so a vanity-URL change cannot split a member in two. The slug it displaces
    then has nowhere else to live: connection_sync upserts with the slug in
    linkedin_id and a profile blob that frequently carries only the provider
    id, so the row was written under ACoAA… and the slug survived nowhere.

    The public_id match above searches profile_json for exactly this value, so
    without it the next slug-only arrival — which is every connection sync —
    matched nothing and inserted a second row for the same person: the very
    duplicate the canonicalisation exists to prevent, arrived at from the
    other side. A public_id the caller supplied is left alone; it is the
    current one, and the displaced slug may be stale.
    """
    if not public_id:
        return profile_json or ""
    blob = _profile_dict(profile_json)
    if blob.get("public_id") or blob.get("public_identifier"):
        return profile_json or ""
    blob["public_id"] = public_id
    return json.dumps(blob)


def _merge_identity_profile(existing_json: str, incoming_json: str) -> str:
    """Keep the richer blob, but always refresh public_id / provider_id."""
    existing = _profile_dict(existing_json)
    incoming = _profile_dict(incoming_json)
    if not incoming:
        return existing_json or ""
    if not existing or not _is_json(existing_json):
        return incoming_json
    merged = dict(existing)
    for key in ("provider_id", "public_id", "public_identifier", "email", "email_address"):
        if incoming.get(key):
            merged[key] = incoming[key]
    if incoming.get("contact_info"):
        merged["contact_info"] = incoming["contact_info"]
    if incoming_json and (
        not existing_json or len(incoming_json) > len(existing_json)
    ):
        richer = dict(incoming)
        richer.update({k: merged[k] for k in merged if merged.get(k)})
        return json.dumps(richer)
    return json.dumps(merged)


def _is_json(value: Any) -> bool:
    """Whether a stored blob is something a reader can actually parse."""
    if not value or not isinstance(value, str):
        return False
    try:
        json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return True


def upsert_global_contact(
    linkedin_id: str,
    name: str,
    title: str = "",
    company: str = "",
    linkedin_url: str = "",
    email: str = "",
    location: str = "",
    profile_json: str = "",
    analysis_json: str = "",
    fit_score: float = 0.0,
    estimated_revenue: float = 0.0,
    source: str = "search",
    source_detail: str = "",
    campaign_id: str = "",
) -> str:
    """Create or update a global contact. Returns global_contact_id.

    If linkedin_id matches an existing record, updates fields using
    "best wins" logic (newer non-empty values win, MAX for scores).
    """
    now = int(time.time())
    db = get_db()

    existing = None

    # The provider id is the member's identity, and it arrives in either the
    # linkedin_id column or inside profile_json depending on who is calling —
    # connection_sync brings the public slug plus a profile, an inbound DM
    # brings the bare provider id and nothing else. Matching one place against
    # the other is what kept the same person from landing twice.
    #
    # Asked before the raw column, because a database still holding a legacy
    # pair — the same member under a slug in one row and under the provider id
    # in another, the shape 612 rows were merged out of by hand on 21 Aug 2026
    # — matched the slug row on the column and then tried to move it onto an id
    # the other row already owns. The unique index refused, and the
    # IntegrityError surfaced from a function nearly every caller wraps in a
    # bare `except Exception: pass`: the row simply stopped updating, for good,
    # and the duplicate stayed. Identity first means the row that already holds
    # the provider id wins, and the stale twin is left to find_duplicates.
    incoming_pid = _incoming_provider_id(linkedin_id, profile_json)
    if incoming_pid:
        existing = db.execute(
            """SELECT * FROM global_contacts
               WHERE linkedin_id = ?
                  OR (profile_json IS NOT NULL AND profile_json != ''
                      AND json_valid(profile_json)
                      AND json_extract(profile_json, '$.provider_id') = ?)
               LIMIT 1""",
            (incoming_pid, incoming_pid),
        ).fetchone()
        if existing:
            logger.info(
                "Matched global contact by provider_id %s (linkedin_id mismatch: %s vs %s)",
                incoming_pid, linkedin_id, dict(existing).get("linkedin_id"),
            )

    if not existing and linkedin_id:
        existing = db.execute(
            "SELECT * FROM global_contacts WHERE linkedin_id = ? LIMIT 1",
            (linkedin_id,),
        ).fetchone()

    if not existing:
        incoming_pub = _incoming_public_id(linkedin_id, profile_json)
        if incoming_pub:
            existing = db.execute(
                """SELECT * FROM global_contacts
                   WHERE linkedin_id = ?
                      OR (profile_json IS NOT NULL AND profile_json != ''
                          AND json_valid(profile_json)
                          AND (json_extract(profile_json, '$.public_id') = ?
                               OR json_extract(profile_json, '$.public_identifier') = ?))
                   LIMIT 1""",
                (incoming_pub, incoming_pub, incoming_pub),
            ).fetchone()
            if existing:
                logger.info(
                    "Matched global contact by public_id %s (linkedin_id incoming=%s existing=%s)",
                    incoming_pub, linkedin_id, dict(existing).get("linkedin_id"),
                )

    if not existing:
        email_key = _email_key(email, profile_json)
        if email_key:
            existing = db.execute(
                """SELECT * FROM global_contacts
                   WHERE lower(trim(COALESCE(email, ''))) = ?
                      OR (profile_json IS NOT NULL AND profile_json != ''
                          AND json_valid(profile_json)
                          AND (lower(trim(COALESCE(json_extract(profile_json, '$.email'), ''))) = ?
                               OR lower(trim(COALESCE(json_extract(profile_json, '$.email_address'), ''))) = ?
                               OR lower(trim(COALESCE(json_extract(profile_json, '$.contact_info.emails[0]'), ''))) = ?
                               OR lower(trim(COALESCE(json_extract(profile_json, '$.contact_info.emails[0].email'), ''))) = ?
                               OR lower(trim(COALESCE(json_extract(profile_json, '$.contact_info.emails[0].address'), ''))) = ?))
                   LIMIT 1""",
                (email_key, email_key, email_key, email_key, email_key, email_key),
            ).fetchone()
            if existing:
                logger.info(
                    "Matched global contact by email %s (linkedin_id incoming=%s existing=%s)",
                    email_key, linkedin_id, dict(existing).get("linkedin_id"),
                )

    if existing:
        existing = dict(existing)
        gid = existing["id"]
        # Update with best available data (non-empty wins, MAX for scores)
        updates: dict[str, Any] = {"updated_at": now}
        incoming_pid = _incoming_provider_id(linkedin_id, profile_json)
        stored_lid = (existing.get("linkedin_id") or "").strip()
        # Belt and braces behind the identity-first match above: never write an
        # id another row already owns. The unique index would refuse, and this
        # function is called from inside enough bare excepts that the refusal
        # would read as "nothing to update" rather than as an error.
        pid_taken = bool(incoming_pid) and bool(db.execute(
            "SELECT 1 FROM global_contacts WHERE linkedin_id = ? AND id != ? LIMIT 1",
            (incoming_pid, gid),
        ).fetchone())
        if incoming_pid and not pid_taken and (
            not stored_lid or (not _is_provider_id(stored_lid) and stored_lid != incoming_pid)
        ):
            updates["linkedin_id"] = incoming_pid
        elif linkedin_id and not stored_lid:
            updates["linkedin_id"] = linkedin_id
        if name and not existing.get("name"):
            updates["name"] = name
        if title and not existing.get("title"):
            updates["title"] = title
        if company and not existing.get("company"):
            updates["company"] = company
        if linkedin_url and linkedin_url != (existing.get("linkedin_url") or ""):
            updates["linkedin_url"] = linkedin_url
        if email and not existing.get("email"):
            updates["email"] = email
        if location and not existing.get("location"):
            updates["location"] = location
        merged_profile = _merge_identity_profile(
            existing.get("profile_json") or "", profile_json,
        ) if profile_json else (existing.get("profile_json") or "")
        # Whenever the column holds a provider id, any slug we know has to land
        # in the profile or nothing can match it again. Keyed on what the column
        # will hold rather than on whether it changed: the steady state is a row
        # already stored under the provider id, and every connection sync after
        # that arrives with the slug — dropping it there leaves the row exactly
        # as unmatchable as dropping it on the first write. Runs even with no
        # incoming profile_json, because ids alone can carry the displacement.
        column_id = updates.get("linkedin_id") or stored_lid
        if _is_provider_id(column_id):
            merged_profile = keep_public_id(
                merged_profile,
                _incoming_public_id(linkedin_id, profile_json) or (
                    stored_lid if not _is_provider_id(stored_lid) else ""
                ),
            )
        if merged_profile and merged_profile != (existing.get("profile_json") or ""):
            updates["profile_json"] = merged_profile
        if analysis_json and not existing.get("analysis_json"):
            updates["analysis_json"] = analysis_json
        if fit_score > (existing.get("fit_score") or 0):
            updates["fit_score"] = fit_score
        if estimated_revenue > (existing.get("estimated_revenue") or 0):
            updates["estimated_revenue"] = estimated_revenue
        # Promote source if new value is more specific than generic 'search'
        if source and source != "search" and existing.get("source") in ("search", "", None):
            updates["source"] = source
        if source_detail and not existing.get("source_detail"):
            updates["source_detail"] = source_detail
        # A campaign attachment is the only thing that counts as a campaign.
        # Lookups/enrichment re-upsert people without a campaign_id and must
        # not inflate the counter — dedup treats it as "already contacted".
        if campaign_id:
            updates["total_campaigns"] = (existing.get("total_campaigns") or 0) + 1
            if not existing.get("first_campaign_id"):
                updates["first_campaign_id"] = campaign_id

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [gid]
        db.execute(f"UPDATE global_contacts SET {set_clause} WHERE id = ?", values)
        db.commit()
        db.close()
        return gid

    # Create new
    gid = str(uuid.uuid4())
    new_pid = _incoming_provider_id(linkedin_id, profile_json)
    if new_pid:
        # Same displacement as the update path: the column is about to hold the
        # provider id, so the slug has to be kept somewhere findable.
        profile_json = keep_public_id(
            profile_json, _incoming_public_id(linkedin_id, profile_json),
        )
    db.execute(
        """INSERT INTO global_contacts
           (id, linkedin_id, linkedin_url, name, title, company, email, location,
            profile_json, analysis_json, fit_score, estimated_revenue,
            lifecycle_stage, source, source_detail, first_campaign_id, total_campaigns,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prospect', ?, ?, ?, ?, ?, ?)""",
        (
            gid, new_pid or linkedin_id or None, linkedin_url, name, title, company,
            email, location, profile_json, analysis_json, fit_score,
            estimated_revenue, source, source_detail, campaign_id or None,
            1 if campaign_id else 0, now, now,
        ),
    )
    db.commit()
    db.close()
    return gid


def get_global_contact(global_contact_id: str) -> Optional[dict]:
    """Get a global contact by ID."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM global_contacts WHERE id = ?", (global_contact_id,)
    ).fetchone()
    db.close()
    return dict(row) if row else None


def get_global_contacts_by_identifiers(identifiers: list[str]) -> dict[str, dict]:
    """Batch lookup global_contacts by linkedin_id or provider_id (in profile_json).

    Returns mapping of lowercase identifier -> global_contact row.
    Matches on linkedin_id column first, then provider_id inside profile_json.
    Used to merge existing enrichment data into connection-based prospects.
    """
    if not identifiers:
        return {}
    db = get_db()
    result: dict[str, dict] = {}
    # SQLite has a 999-parameter limit — batch in chunks
    chunk_size = 900
    try:
        for i in range(0, len(identifiers), chunk_size):
            chunk = identifiers[i : i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            # Match by linkedin_id
            rows = db.execute(
                f"SELECT * FROM global_contacts WHERE linkedin_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                r = dict(row)
                lid = (r.get("linkedin_id") or "").lower().strip()
                if lid:
                    result[lid] = r
            # Also match by provider_id inside profile_json for identifiers not yet found
            remaining = [ident for ident in chunk if ident.lower().strip() not in result]
            if remaining:
                # One IN() per chunk, not one full-table json_extract per
                # leftover identifier. The per-id loop blocked the single
                # DB worker for tens of seconds on a connections-only
                # create_campaign.
                placeholders = ",".join("?" for _ in remaining)
                rows = db.execute(
                    f"""SELECT *, json_extract(profile_json, '$.provider_id')
                               AS extracted_provider_id
                        FROM global_contacts
                        WHERE profile_json IS NOT NULL AND profile_json != ''
                          AND json_valid(profile_json)
                          AND json_extract(profile_json, '$.provider_id')
                              IN ({placeholders})""",
                    remaining,
                ).fetchall()
                for row in rows:
                    r = dict(row)
                    pid = (r.pop("extracted_provider_id", None) or "").lower().strip()
                    if pid and pid not in result:
                        result[pid] = r
    except Exception as e:
        logger.warning("Batch global contacts lookup failed: %s", e)
    finally:
        db.close()
    return result


def get_global_contact_by_linkedin_id(linkedin_id: str) -> Optional[dict]:
    """Get a global contact by either of the two id forms LinkedIn uses.

    The column holds the provider id whenever one is known, so matching it
    alone answered None for every caller holding the slug — the form
    connection_sync, CSV imports and vanity URLs all speak — for a row that is
    right there. Identity is the same question upsert_global_contact asks, so
    it is asked the same way: the column, or the ids inside profile_json.
    """
    if not linkedin_id:
        return None
    db = get_db()
    row = db.execute(
        """SELECT * FROM global_contacts
           WHERE linkedin_id = ?
              OR (profile_json IS NOT NULL AND profile_json != ''
                  AND json_valid(profile_json)
                  AND (json_extract(profile_json, '$.provider_id') = ?
                       OR json_extract(profile_json, '$.public_id') = ?
                       OR json_extract(profile_json, '$.public_identifier') = ?))
           LIMIT 1""",
        (linkedin_id, linkedin_id, linkedin_id, linkedin_id),
    ).fetchone()
    db.close()
    return dict(row) if row else None


# ──────────────────────────────────────────────
# Search & List
# ──────────────────────────────────────────────

def search_global_contacts(
    query: str = "",
    lifecycle_stage: str = "",
    tag: str = "",
    min_fit_score: float = 0.0,
    limit: int = 50,
    offset: int = 0,
    order_by: str = "updated_at DESC",
) -> list[dict]:
    """Search global contacts with text search, lifecycle filter, tag filter."""
    conditions: list[str] = []
    params: list[Any] = []

    if query:
        conditions.append(
            "(name LIKE ? OR title LIKE ? OR company LIKE ? OR email LIKE ? OR location LIKE ?)"
        )
        q = f"%{query}%"
        params.extend([q, q, q, q, q])

    if lifecycle_stage:
        conditions.append("lifecycle_stage = ?")
        params.append(lifecycle_stage)

    if tag:
        conditions.append("tags_json LIKE ?")
        params.append(f'%"{tag}"%')

    if min_fit_score > 0:
        conditions.append("fit_score >= ?")
        params.append(min_fit_score)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    # Validate order_by to prevent SQL injection
    allowed_orders = {
        "updated_at DESC", "updated_at ASC",
        "created_at DESC", "created_at ASC",
        "fit_score DESC", "fit_score ASC",
        "name ASC", "name DESC",
        "last_interaction_at DESC",
    }
    if order_by not in allowed_orders:
        order_by = "updated_at DESC"

    db = get_db()
    rows = db.execute(
        f"SELECT * FROM global_contacts {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_all_global_linkedin_ids() -> set[str]:
    """Fast set of all linkedin_ids in global_contacts (for dedup)."""
    db = get_db()
    rows = db.execute(
        """SELECT linkedin_id FROM global_contacts
           WHERE linkedin_id IS NOT NULL AND linkedin_id != ''
           UNION
           SELECT linkedin_url FROM global_contacts
           WHERE linkedin_url IS NOT NULL AND linkedin_url != ''"""
    ).fetchall()
    db.close()

    ids: set[str] = set()
    for row in rows:
        val = row[0]
        if val:
            ids.add(val.lower().strip())
    return ids


# ──────────────────────────────────────────────
# Lifecycle
# ──────────────────────────────────────────────

def update_global_contact_lifecycle(
    global_contact_id: str, new_stage: str
) -> None:
    """Update lifecycle stage (only promotes forward, never demotes).

    Uses _LIFECYCLE_ORDER to determine if the new stage is higher.
    """
    if new_stage not in _LIFECYCLE_ORDER:
        return

    db = get_db()
    row = db.execute(
        "SELECT lifecycle_stage FROM global_contacts WHERE id = ?",
        (global_contact_id,),
    ).fetchone()
    if not row:
        db.close()
        return

    current = row["lifecycle_stage"] or "prospect"
    current_order = _LIFECYCLE_ORDER.get(current, 0)
    new_order = _LIFECYCLE_ORDER.get(new_stage, 0)

    if new_order > current_order:
        now = int(time.time())
        db.execute(
            "UPDATE global_contacts SET lifecycle_stage = ?, updated_at = ? WHERE id = ?",
            (new_stage, now, global_contact_id),
        )
        db.commit()
    db.close()


def promote_lifecycle_from_outreach(
    outreach_id: str, outreach_status: str
) -> None:
    """Promote global contact lifecycle based on outreach status change.

    Called from update_outreach(). Non-critical — failures are logged, not raised.
    """
    target = _OUTREACH_TO_LIFECYCLE.get(outreach_status)
    if not target:
        return

    try:
        db = get_db()
        row = db.execute(
            """SELECT c.global_contact_id
               FROM outreaches o
               JOIN contacts c ON o.contact_id = c.id
               WHERE o.id = ?""",
            (outreach_id,),
        ).fetchone()
        db.close()

        if row and row["global_contact_id"]:
            update_global_contact_lifecycle(row["global_contact_id"], target)

            # Update last_interaction_at
            now = int(time.time())
            db2 = get_db()
            db2.execute(
                "UPDATE global_contacts SET last_interaction_at = ?, updated_at = ? WHERE id = ?",
                (now, now, row["global_contact_id"]),
            )
            db2.commit()
            db2.close()

            # If contacted for the first time, set first_contacted_at
            if target == "contacted":
                db3 = get_db()
                db3.execute(
                    """UPDATE global_contacts
                       SET first_contacted_at = ?
                       WHERE id = ? AND first_contacted_at IS NULL""",
                    (now, row["global_contact_id"]),
                )
                db3.commit()
                db3.close()
    except Exception as exc:
        logger.warning("Failed to promote global contact lifecycle: %s", exc)


# ──────────────────────────────────────────────
# Tags & Notes
# ──────────────────────────────────────────────

def add_global_contact_tag(global_contact_id: str, tag: str) -> None:
    """Add a tag to a global contact's tags_json."""
    tag = tag.strip().lower()
    if not tag:
        return
    db = get_db()
    row = db.execute(
        "SELECT tags_json FROM global_contacts WHERE id = ?",
        (global_contact_id,),
    ).fetchone()
    if not row:
        db.close()
        return

    tags = json.loads(row["tags_json"] or "[]")
    if tag not in tags:
        tags.append(tag)
        db.execute(
            "UPDATE global_contacts SET tags_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(tags), int(time.time()), global_contact_id),
        )
        db.commit()
    db.close()


def remove_global_contact_tag(global_contact_id: str, tag: str) -> None:
    """Remove a tag from a global contact's tags_json."""
    tag = tag.strip().lower()
    if not tag:
        return
    db = get_db()
    row = db.execute(
        "SELECT tags_json FROM global_contacts WHERE id = ?",
        (global_contact_id,),
    ).fetchone()
    if not row:
        db.close()
        return

    tags = json.loads(row["tags_json"] or "[]")
    if tag in tags:
        tags.remove(tag)
        db.execute(
            "UPDATE global_contacts SET tags_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(tags), int(time.time()), global_contact_id),
        )
        db.commit()
    db.close()


def is_excluded_from_automation(global_contact_id: str) -> bool:
    """Check if a global contact should be excluded from all automation.

    Returns True if the contact has the 'do-not-automate' tag or
    lifecycle_stage == 'do_not_contact'.
    """
    if not global_contact_id:
        return False
    db = get_db()
    row = db.execute(
        "SELECT tags_json, lifecycle_stage FROM global_contacts WHERE id = ?",
        (global_contact_id,),
    ).fetchone()
    db.close()
    if not row:
        return False
    if row["lifecycle_stage"] == "do_not_contact":
        return True
    tags = json.loads(row["tags_json"] or "[]")
    return "do-not-automate" in tags


def is_excluded_by_email(email: str) -> bool:
    """Check exclusion for an email address.

    The send_email tool is given an address and nothing else, but exclusion
    lives on the global contact — so without this lookup a 'do-not-automate'
    tag was honoured on LinkedIn and silently ignored over email.

    An address matching no contact is NOT excluded: unknown is not a denial,
    and refusing to email a stranger the user named explicitly would be the
    wrong side to err on. Matched case-insensitively; addresses are stored as
    the user or LinkedIn supplied them.
    """
    email = (email or "").strip().lower()
    if not email:
        return False
    db = get_db()
    # Look where addresses actually live, not just where the schema says they
    # could. Keying on the `email` column alone made this gate inert: no
    # production path writes it — save_contact has no email parameter, and
    # every upsert_global_contact call site omits it. On the live base that was
    # 0 of 20,399 rows, so a person marked do_not_contact was emailed anyway.
    #
    # channel_selector._extract_email reads profile_json's "email"/"email_address",
    # and that is the address a send actually uses, so the gate has to read the
    # same places or the two can never agree. json_valid guards the 1685 rows
    # with unparseable profile_json, which otherwise kill the whole statement.
    rows = db.execute(
        """SELECT id FROM global_contacts
           WHERE lower(trim(COALESCE(email, ''))) = ?
              OR (profile_json IS NOT NULL AND profile_json != ''
                  AND json_valid(profile_json)
                  AND (lower(trim(COALESCE(json_extract(profile_json, '$.email'), ''))) = ?
                       OR lower(trim(COALESCE(json_extract(profile_json, '$.email_address'), ''))) = ?))""",
        (email, email, email),
    ).fetchall()
    db.close()
    # One address can front more than one row (the id-format split); any
    # excluded match blocks the send.
    return any(is_excluded_from_automation(r["id"]) for r in rows)


def is_excluded_by_contact_id(contact_id: str) -> bool:
    """Check exclusion using a campaign-level contact_id (resolves to global)."""
    if not contact_id:
        return False
    db = get_db()
    row = db.execute(
        "SELECT global_contact_id FROM contacts WHERE id = ?",
        (contact_id,),
    ).fetchone()
    db.close()
    if not row or not row["global_contact_id"]:
        return False
    return is_excluded_from_automation(row["global_contact_id"])


def add_global_contact_note(global_contact_id: str, text: str) -> None:
    """Append a note to a global contact's notes_json."""
    text = text.strip()
    if not text:
        return
    db = get_db()
    row = db.execute(
        "SELECT notes_json FROM global_contacts WHERE id = ?",
        (global_contact_id,),
    ).fetchone()
    if not row:
        db.close()
        return

    notes = json.loads(row["notes_json"] or "[]")
    notes.append({"text": text, "created_at": int(time.time())})
    db.execute(
        "UPDATE global_contacts SET notes_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(notes), int(time.time()), global_contact_id),
    )
    db.commit()
    db.close()


# ──────────────────────────────────────────────
# Cross-Campaign History
# ──────────────────────────────────────────────

def get_cross_campaign_history(global_contact_id: str) -> Optional[dict]:
    """Get full cross-campaign interaction history for a global contact.

    Returns:
        {
            "global_contact": {...},
            "campaigns": [
                {
                    "campaign_id": "...",
                    "campaign_name": "...",
                    "contact_id": "...",
                    "fit_score": 0.85,
                    "outreach": {...},
                    "messages": [...],
                    "engagements": [...],
                }
            ],
            "signals": [...],
        }
    """
    gc = get_global_contact(global_contact_id)
    if not gc:
        return None

    db = get_db()

    # Get all campaign contacts linked to this global contact
    contacts = db.execute(
        """SELECT c.*, camp.name as campaign_name, camp.status as campaign_status
           FROM contacts c
           LEFT JOIN campaigns camp ON c.campaign_id = camp.id
           WHERE c.global_contact_id = ?
           ORDER BY c.created_at DESC""",
        (global_contact_id,),
    ).fetchall()

    campaigns: list[dict] = []
    for contact in contacts:
        c = dict(contact)
        contact_id = c["id"]
        campaign_id = c["campaign_id"]

        # Get outreach for this contact
        outreach_row = db.execute(
            "SELECT * FROM outreaches WHERE contact_id = ? AND campaign_id = ? LIMIT 1",
            (contact_id, campaign_id),
        ).fetchone()
        outreach = dict(outreach_row) if outreach_row else None

        # Get messages
        messages: list[dict] = []
        if outreach:
            msg_rows = db.execute(
                "SELECT * FROM messages WHERE outreach_id = ? ORDER BY timestamp ASC",
                (outreach["id"],),
            ).fetchall()
            messages = [dict(m) for m in msg_rows]

        # Get engagements
        engagements: list[dict] = []
        if outreach:
            eng_rows = db.execute(
                "SELECT * FROM engagements WHERE outreach_id = ? ORDER BY created_at ASC",
                (outreach["id"],),
            ).fetchall()
            engagements = [dict(e) for e in eng_rows]

        campaigns.append({
            "campaign_id": campaign_id,
            "campaign_name": c.get("campaign_name") or "Unknown",
            "campaign_status": c.get("campaign_status") or "unknown",
            "contact_id": contact_id,
            "fit_score": c.get("fit_score") or 0.0,
            "outreach": outreach,
            "messages": messages,
            "engagements": engagements,
        })

    # Get signals linked to this person's linkedin_id
    signals: list[dict] = []
    linkedin_id = gc.get("linkedin_id")
    if linkedin_id:
        sig_rows = db.execute(
            """SELECT * FROM signals WHERE linkedin_id = ?
               ORDER BY detected_at DESC LIMIT 20""",
            (linkedin_id,),
        ).fetchall()
        signals = [dict(s) for s in sig_rows]

    db.close()

    return {
        "global_contact": gc,
        "campaigns": campaigns,
        "signals": signals,
    }


# ──────────────────────────────────────────────
# Stats
# ──────────────────────────────────────────────

def get_global_contact_stats() -> dict:
    """Dashboard stats: total contacts, by lifecycle, by source, top tags."""
    db = get_db()

    total = db.execute("SELECT COUNT(*) as cnt FROM global_contacts").fetchone()["cnt"]

    # By lifecycle
    lc_rows = db.execute(
        """SELECT lifecycle_stage, COUNT(*) as cnt
           FROM global_contacts GROUP BY lifecycle_stage ORDER BY cnt DESC"""
    ).fetchall()
    by_lifecycle = {r["lifecycle_stage"]: r["cnt"] for r in lc_rows}

    # By source
    src_rows = db.execute(
        """SELECT source, COUNT(*) as cnt
           FROM global_contacts GROUP BY source ORDER BY cnt DESC"""
    ).fetchall()
    by_source = {r["source"]: r["cnt"] for r in src_rows}

    # Top tags (flatten tags_json across all contacts)
    tag_rows = db.execute(
        "SELECT tags_json FROM global_contacts WHERE tags_json != '[]'"
    ).fetchall()
    tag_counts: dict[str, int] = {}
    for row in tag_rows:
        try:
            tags = json.loads(row["tags_json"] or "[]")
            for t in tags:
                tag_counts[t] = tag_counts.get(t, 0) + 1
        except (json.JSONDecodeError, TypeError):
            pass
    top_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    db.close()

    return {
        "total": total,
        "by_lifecycle": by_lifecycle,
        "by_source": by_source,
        "top_tags": top_tags,
    }


# ──────────────────────────────────────────────
# Duplicate detection & merging
# ──────────────────────────────────────────────

def find_duplicate_global_contacts() -> list[dict]:
    """Find global contacts that share the same provider_id but have different IDs.

    Returns list of dicts with keys: keep_id, merge_id, provider_id, keep_name, merge_name.
    """
    db = get_db()
    try:
        # Strategy 1: Match by provider_id in profile_json
        rows = db.execute(
            """WITH pids AS (
                 SELECT id, name, total_campaigns, fit_score,
                        json_extract(profile_json, '$.provider_id') AS pid
                 FROM global_contacts
                 WHERE profile_json IS NOT NULL AND profile_json != ''
                   AND json_valid(profile_json)
                   AND json_extract(profile_json, '$.provider_id') IS NOT NULL
                   AND json_extract(profile_json, '$.provider_id') != ''
               )
               SELECT p1.id AS id1, p2.id AS id2, p1.pid AS pid,
                      p1.name AS name1, p2.name AS name2,
                      p1.total_campaigns AS tc1, p2.total_campaigns AS tc2,
                      p1.fit_score AS fs1, p2.fit_score AS fs2
               FROM pids p1
               JOIN pids p2 ON p1.pid = p2.pid AND p1.id < p2.id"""
        ).fetchall()

        # Strategy 2: one record keyed by provider_id (ACoAA…), another by the
        # public slug — the id-format split.
        #
        # A NAME IS NOT AN IDENTIFIER. This used to join on LOWER(TRIM(name))
        # alone, so two unrelated John Smiths — one stored with a provider id,
        # one with a slug — were emitted as a merge candidate, complete with a
        # provider_id line. Acting on that repoints one person's campaign rows
        # at the other's record, combines tags and notes, and deletes the loser;
        # 612 rows were bulk-merged off this report this month, so "a human
        # reviews it" is not the safeguard it sounds like.
        #
        # A second, NON-EMPTY field must agree. Absent on both sides is not
        # agreement — it is absence, the same distinction the rest of this
        # codebase now keeps everywhere.
        #
        # Strategy 1 above is untouched: a shared provider_id IS the identity,
        # and needs no corroboration.
        rows2 = db.execute(
            """SELECT gc1.id AS id1, gc2.id AS id2,
                      CASE WHEN gc1.linkedin_id LIKE 'ACoAA%' THEN gc1.linkedin_id
                           ELSE gc2.linkedin_id END AS pid,
                      gc1.name AS name1, gc2.name AS name2,
                      gc1.total_campaigns AS tc1, gc2.total_campaigns AS tc2,
                      gc1.fit_score AS fs1, gc2.fit_score AS fs2
               FROM global_contacts gc1
               JOIN global_contacts gc2
                 ON LOWER(TRIM(gc1.name)) = LOWER(TRIM(gc2.name))
                AND gc1.id < gc2.id
               WHERE gc1.name IS NOT NULL AND gc1.name != ''
                 AND gc1.linkedin_id IS NOT NULL AND gc1.linkedin_id != ''
                 AND gc2.linkedin_id IS NOT NULL AND gc2.linkedin_id != ''
                 AND (
                   (gc1.linkedin_id LIKE 'ACoAA%' AND gc2.linkedin_id NOT LIKE 'ACoAA%')
                   OR
                   (gc2.linkedin_id LIKE 'ACoAA%' AND gc1.linkedin_id NOT LIKE 'ACoAA%')
                 )
                 AND (
                   (COALESCE(TRIM(gc1.company), '') != ''
                    AND LOWER(TRIM(gc1.company)) = LOWER(TRIM(gc2.company)))
                   OR
                   (COALESCE(TRIM(gc1.linkedin_url), '') != ''
                    AND LOWER(TRIM(gc1.linkedin_url)) = LOWER(TRIM(gc2.linkedin_url)))
                 )"""
        ).fetchall()
        rows = list(rows) + list(rows2)
    except Exception as e:
        logger.warning("find_duplicate_global_contacts failed: %s", e)
        db.close()
        return []

    db.close()

    duplicates = []
    seen_pairs: set[tuple[str, str]] = set()
    for row in rows:
        row = dict(row)
        # Deduplicate pairs (same pair may appear from multiple strategies)
        pair_key = tuple(sorted((row["id1"], row["id2"])))
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        # Keep the record with more campaigns (or higher fit_score as tiebreaker)
        tc1, tc2 = row["tc1"] or 0, row["tc2"] or 0
        fs1, fs2 = row["fs1"] or 0, row["fs2"] or 0
        if tc1 > tc2 or (tc1 == tc2 and fs1 >= fs2):
            keep_id, merge_id = row["id1"], row["id2"]
            keep_name, merge_name = row["name1"], row["name2"]
        else:
            keep_id, merge_id = row["id2"], row["id1"]
            keep_name, merge_name = row["name2"], row["name1"]

        duplicates.append({
            "keep_id": keep_id,
            "merge_id": merge_id,
            "provider_id": row["pid"],
            "keep_name": keep_name,
            "merge_name": merge_name,
        })

    return duplicates


def merge_global_contacts(keep_id: str, merge_id: str) -> bool:
    """Merge two global contact records. Keeps keep_id, deletes merge_id.

    - Applies "best wins" field merging
    - Reassigns all contacts.global_contact_id FKs from merge_id to keep_id
    - Combines tags and notes
    - Sums total_campaigns
    """
    db = get_db()
    try:
        keep_row = db.execute(
            "SELECT * FROM global_contacts WHERE id = ?", (keep_id,)
        ).fetchone()
        merge_row = db.execute(
            "SELECT * FROM global_contacts WHERE id = ?", (merge_id,)
        ).fetchone()

        if not keep_row or not merge_row:
            logger.warning("merge_global_contacts: one or both records not found")
            db.close()
            return False

        keep = dict(keep_row)
        merge = dict(merge_row)
        now = int(time.time())

        # Best-wins merge
        updates: dict[str, Any] = {"updated_at": now}

        # Fill empty fields from merge record
        for field in ("name", "title", "company", "linkedin_id", "linkedin_url",
                      "email", "location", "source_detail"):
            if not keep.get(field) and merge.get(field):
                updates[field] = merge[field]

        # MAX for scores
        if (merge.get("fit_score") or 0) > (keep.get("fit_score") or 0):
            updates["fit_score"] = merge["fit_score"]
        if (merge.get("estimated_revenue") or 0) > (keep.get("estimated_revenue") or 0):
            updates["estimated_revenue"] = merge["estimated_revenue"]

        # Longer profile_json wins
        if merge.get("profile_json") and len(merge["profile_json"]) > len(keep.get("profile_json") or ""):
            updates["profile_json"] = merge["profile_json"]
        if merge.get("analysis_json") and not keep.get("analysis_json"):
            updates["analysis_json"] = merge["analysis_json"]

        # Combine tags
        keep_tags = set()
        merge_tags = set()
        try:
            keep_tags = set(json.loads(keep.get("tags_json") or "[]"))
        except (json.JSONDecodeError, TypeError):
            pass
        try:
            merge_tags = set(json.loads(merge.get("tags_json") or "[]"))
        except (json.JSONDecodeError, TypeError):
            pass
        combined_tags = sorted(keep_tags | merge_tags)
        if combined_tags:
            updates["tags_json"] = json.dumps(combined_tags)

        # Combine notes
        keep_notes = []
        merge_notes = []
        try:
            keep_notes = json.loads(keep.get("notes_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            pass
        try:
            merge_notes = json.loads(merge.get("notes_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            pass
        combined_notes = keep_notes + merge_notes
        if combined_notes:
            updates["notes_json"] = json.dumps(combined_notes)

        # Sum campaigns
        updates["total_campaigns"] = (keep.get("total_campaigns") or 0) + (merge.get("total_campaigns") or 0)

        # Promote lifecycle stage (keep higher)
        keep_stage = keep.get("lifecycle_stage") or "prospect"
        merge_stage = merge.get("lifecycle_stage") or "prospect"
        if _LIFECYCLE_ORDER.get(merge_stage, 0) > _LIFECYCLE_ORDER.get(keep_stage, 0):
            updates["lifecycle_stage"] = merge_stage

        # Reassign campaign contacts FK
        db.execute(
            "UPDATE contacts SET global_contact_id = ? WHERE global_contact_id = ?",
            (keep_id, merge_id),
        )

        # The merged record goes first: linkedin_id carries a unique partial
        # index, so adopting the loser's id while its row still exists is an
        # IntegrityError — the common shape when duplicates were found by
        # provider_id and the survivor's linkedin_id column is empty.
        db.execute("DELETE FROM global_contacts WHERE id = ?", (merge_id,))

        # Apply updates to keep record
        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [keep_id]
            db.execute(f"UPDATE global_contacts SET {set_clause} WHERE id = ?", values)

        db.commit()
        logger.info("Merged global contact %s into %s", merge_id, keep_id)
        db.close()
        return True

    except Exception as e:
        logger.error("merge_global_contacts failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        db.close()
        return False


# ──────────────────────────────────────────────
# Linking global contacts back onto campaign rows
# ──────────────────────────────────────────────

def normalise_contact_name(name: str) -> str:
    """Fold a display name to the key used for matching.

    Case and surrounding/internal whitespace only. No initials, no nickname
    table, no fuzzy distance: whatever this matches gets a LinkedIn id written
    onto a campaign row, and the next send goes to whoever owns that id.
    """
    return " ".join((name or "").split()).casefold()


def _candidate_rank(row: dict[str, Any]) -> tuple[int, int, int, str]:
    """Order global records that share one LinkedIn id: richest first.

    ``idx_global_contacts_linkedin`` is unique, so this list is normally one
    element long. It is not assumed to be: that index is created by a migration
    that already had to clean duplicates out once, and an arbitrary pick would
    make the dry run show one record and the apply write another. The trailing
    id keeps the order stable when everything else ties.
    """
    return (
        1 if row.get("profile_json") else 0,
        1 if row.get("linkedin_url") else 0,
        len(row.get("title") or ""),
        row.get("id") or "",
    )


def plan_contact_links(campaign_id: str) -> dict[str, Any]:
    """Work out which campaign contacts the global contact base can resolve.

    Read-only — the dry run and the apply both call this, so what a dry run
    shows is what an apply acts on.

    Only rows with an EMPTY ``linkedin_id`` are candidates. ``global_contact_id``
    is deliberately not used as the "unlinked" test: ``save_contact`` is the only
    INSERT into ``contacts`` and it always sets ``global_contact_id`` — for a
    contact with no LinkedIn id it creates a fresh name-only global record — so
    that column is never NULL in production and a clause keyed on it would match
    nothing.
    """
    conn = get_db()
    try:
        rows = [
            dict(r) for r in conn.execute(
                """SELECT id, name, title, company, linkedin_url, linkedin_id,
                          global_contact_id
                   FROM contacts
                   WHERE campaign_id = ?
                   -- rowid, not id: contacts.id is a uuid, so ordering by it
                   -- would hand the LinkedIn id to a random one of two rows
                   -- with the same name. rowid is insertion order, so the row
                   -- that was imported first claims the match and the later
                   -- duplicate is reported as the merge candidate — the same
                   -- way round every time this is run.
                   ORDER BY created_at ASC, rowid ASC""",
                (campaign_id,),
            ).fetchall()
        ]
        candidates = [
            dict(r) for r in conn.execute(
                """SELECT id, linkedin_id, name, title, company, linkedin_url,
                          source, source_detail, profile_json
                   FROM global_contacts
                   WHERE linkedin_id IS NOT NULL AND linkedin_id != ''
                     AND name IS NOT NULL AND TRIM(name) != ''"""
            ).fetchall()
        ]
    finally:
        conn.close()

    by_name: dict[str, list[dict[str, Any]]] = {}
    for cand in candidates:
        by_name.setdefault(normalise_contact_name(cand["name"]), []).append(cand)

    # LinkedIn ids this campaign already carries. UNIQUE(campaign_id,
    # linkedin_id) is a partial index over non-empty ids, so writing one of
    # these onto a second row raises IntegrityError.
    taken: dict[str, dict[str, str]] = {}
    already_linked = 0
    for row in rows:
        lid = (row.get("linkedin_id") or "").strip()
        if lid:
            already_linked += 1
            taken.setdefault(lid, {"contact_id": row["id"], "name": row.get("name") or ""})

    plan: dict[str, Any] = {
        "campaign_id": campaign_id,
        "total": len(rows),
        "already_linked": already_linked,
        "nameless": 0,
        "links": [],
        "collisions": [],
        "ambiguous": [],
        "unmatched": [],
    }

    for row in rows:
        if (row.get("linkedin_id") or "").strip():
            continue
        name = (row.get("name") or "").strip()
        if not name:
            plan["nameless"] += 1
            continue

        cands = by_name.get(normalise_contact_name(name), [])
        if not cands:
            plan["unmatched"].append({
                "contact_id": row["id"],
                "contact_name": name,
                "contact_company": row.get("company") or "",
            })
            continue

        distinct_ids = {c["linkedin_id"] for c in cands}
        if len(distinct_ids) > 1:
            plan["ambiguous"].append({
                "contact_id": row["id"],
                "contact_name": name,
                "contact_company": row.get("company") or "",
                "candidates": sorted(
                    (
                        {
                            "global_contact_id": c["id"],
                            "linkedin_id": c["linkedin_id"],
                            "name": c.get("name") or "",
                            "title": c.get("title") or "",
                            "company": c.get("company") or "",
                            "linkedin_url": c.get("linkedin_url") or "",
                        }
                        for c in cands
                    ),
                    key=lambda c: (c["company"], c["linkedin_id"]),
                ),
            })
            continue

        best = sorted(cands, key=_candidate_rank, reverse=True)[0]
        lid = best["linkedin_id"]
        if lid in taken:
            other = taken[lid]
            plan["collisions"].append({
                "contact_id": row["id"],
                "contact_name": name,
                "contact_company": row.get("company") or "",
                "linkedin_id": lid,
                "global_contact_id": best["id"],
                "global_name": best.get("name") or "",
                "other_contact_id": other["contact_id"],
                "other_contact_name": other["name"],
            })
            continue

        plan["links"].append({
            "contact_id": row["id"],
            "contact_name": name,
            "contact_title": row.get("title") or "",
            "contact_company": row.get("company") or "",
            "global_contact_id": best["id"],
            "linkedin_id": lid,
            "global_name": best.get("name") or "",
            "global_title": best.get("title") or "",
            "global_company": best.get("company") or "",
            "linkedin_url": best.get("linkedin_url") or "",
            "source": best.get("source") or "",
            "source_detail": best.get("source_detail") or "",
        })
        # Claim the id so a second row with the same name is reported as a
        # collision here rather than blowing up as an IntegrityError on write.
        taken[lid] = {"contact_id": row["id"], "name": name}

    return plan


def apply_contact_link(
    contact_id: str,
    global_contact_id: str,
    linkedin_id: str,
    linkedin_url: str = "",
) -> str:
    """Write one planned resolution onto a campaign contact row.

    Returns the disposition:
      "linked"    — the row now carries the LinkedIn id and points at the
                    resolved global contact.
      "collision" — UNIQUE(campaign_id, linkedin_id) refused it; another row in
                    the campaign already carries that id. Nothing was written.
      "skipped"   — the row already had a LinkedIn id by the time we wrote, so
                    it was left alone.
    """
    now = int(time.time())
    conn = get_db()
    try:
        cur = conn.execute(
            """UPDATE contacts
               SET linkedin_id = ?,
                   linkedin_url = CASE
                       WHEN linkedin_url IS NULL OR linkedin_url = '' THEN ?
                       ELSE linkedin_url END,
                   global_contact_id = ?,
                   updated_at = ?
               WHERE id = ? AND (linkedin_id IS NULL OR linkedin_id = '')""",
            (linkedin_id, linkedin_url, global_contact_id, now, contact_id),
        )
        conn.commit()
        return "linked" if cur.rowcount else "skipped"
    except sqlite3.IntegrityError:
        # The plan can go stale between reading and writing. Roll the empty
        # transaction back and report it as what it is instead of letting a raw
        # IntegrityError out of the tool.
        conn.rollback()
        logger.info(
            "Link refused by unique index: contact=%s linkedin_id=%s",
            contact_id, linkedin_id,
        )
        return "collision"
    finally:
        conn.close()
