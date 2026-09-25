"""SQLite CRUD helpers for HeyLead."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
import time
import uuid
from collections.abc import Sequence
from typing import Any, Optional

from .schema import get_db
from .message_rows import insert_message_row
from ..ai.copywriter import provenance as copy_provenance
from ..ai.copywriter.provenance import Provenance
from ..constants import (
    ENGAGEMENT_RESERVATION_TTL_SECONDS,
    INMAIL_FALLBACK_MAX_AGE_DAYS,
    SIGNAL_FIT_OVERRIDE,
)

# Shared send-gate: hand-picked CSV rows and a one-invite classified hook
# may sit below min_fit. Keep this fragment in one place so invite/DM/InMail
# and cloud-sync cannot drift.
FIT_SENDABLE_SQL = (
    f"(c.source = 'csv_import' "
    f"OR o.next_action = '{SIGNAL_FIT_OVERRIDE}' "
    f"OR COALESCE(c.fit_score, 0) >= ?)"
)

# Invitation notes are stored as role='sdr'. Tagged rows carry
# format='invite_note'; older notes sit on invited_at. A real opening DM is
# anything else — pickers, orphan rescue and the stall watchdog share this.
_INVITE_NOTE_WINDOW_SECONDS = 120
SDR_REAL_DM_SQL = f"""EXISTS (
    SELECT 1 FROM messages m
    WHERE m.outreach_id = o.id AND m.role = 'sdr'
      AND COALESCE(m.format, '') != 'invite_note'
      AND NOT (
          COALESCE(o.invited_at, 0) > 0
          AND ABS(COALESCE(m.timestamp, 0) - o.invited_at) <= {_INVITE_NOTE_WINDOW_SECONDS}
      )
)"""
SDR_RECENT_SDR_SQL = f"""EXISTS (
    SELECT 1 FROM messages m
    WHERE m.outreach_id = o.id AND m.role = 'sdr'
      AND COALESCE(m.timestamp, 0) > ?
      AND COALESCE(m.format, '') != 'invite_note'
      AND NOT (
          COALESCE(o.invited_at, 0) > 0
          AND ABS(COALESCE(m.timestamp, 0) - o.invited_at) <= {_INVITE_NOTE_WINDOW_SECONDS}
      )
)"""

# Failed first-touch InMail parks a retry of InMail itself, not the invite.
# Same 24h window as get_inmail_fallback_candidates — a 3h cooldown let a
# 422 come back the same night (Tyler R. at +4h on 25 Aug 2026).
_INMAIL_FIRST_TOUCH_FAIL_COOLDOWN = 86400

# Channel-routing 422: Unipile treated the send as a regular chat. Retrying
# after a day burns another job and will 422 again until the hosted proxy
# forwards inmail=true.
_INMAIL_PERMANENT_FAIL_SQL = """
             AND NOT EXISTS (
                 SELECT 1 FROM actions_log al
                 WHERE al.outreach_id = o.id
                   AND (
                       al.action_type = 'inmail_unreachable'
                       OR (
                           al.action_type = 'inmail_sent'
                           AND al.result = 'failed'
                           AND (
                               COALESCE(al.details_json, '') LIKE '%no_connection_with_recipient%'
                               OR COALESCE(al.details_json, '') LIKE '%not to be first degree%'
                           )
                       )
                   )
             )"""

logger = logging.getLogger(__name__)


def _local_day_start() -> int:
    """Unix timestamp of local midnight today.

    Daily digest / dashboard counters share this so "today" is one window.
    UTC midnight (`time() % 86400`) is a different day for anyone west of
    Greenwich — leave that form to the signal-search settings keys that
    already key on it.
    """
    from datetime import date, datetime

    return int(datetime.combine(date.today(), datetime.min.time()).timestamp())


# ──────────────────────────────────────────────
# Settings (key-value store)
# ──────────────────────────────────────────────

def save_setting(key: str, value: Any) -> None:
    """Save a setting (JSON-serialized)."""
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        (key, json.dumps(value), int(time.time())),
    )
    db.commit()
    db.close()


def get_setting_updated_at(key: str) -> int | None:
    """When a setting was last written, or None if it does not exist."""
    db = get_db()
    row = db.execute(
        "SELECT updated_at FROM settings WHERE key = ?", (key,),
    ).fetchone()
    db.close()
    if row is None:
        return None
    try:
        ts = int(row["updated_at"] or 0)
    except (TypeError, ValueError):
        return None
    return ts or None


def get_setting(key: str, default: Any = None) -> Any:
    """Load a setting, returning default if not found."""
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    db.close()
    if row is None:
        return default
    try:
        result = json.loads(row["value"])
        # Handle double-serialized JSON (string containing JSON)
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        return result
    except (json.JSONDecodeError, TypeError):
        return row["value"]


def delete_setting(key: str) -> None:
    db = get_db()
    db.execute("DELETE FROM settings WHERE key = ?", (key,))
    db.commit()
    db.close()


# ──────────────────────────────────────────────
# Campaigns
# ──────────────────────────────────────────────

def create_campaign(
    name: str,
    icp_json: str = "",
    status: str = "draft",
    mode: str = "autopilot",
    config_json: str = "",
    context_json: str = "",
) -> str:
    """Create a new campaign and return its ID."""
    campaign_id = str(uuid.uuid4())
    now = int(time.time())
    db = get_db()
    db.execute(
        """INSERT INTO campaigns (id, name, icp_json, status, mode, config_json, context_json, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (campaign_id, name, icp_json, status, mode, config_json, context_json, now, now),
    )
    db.commit()
    db.close()
    return campaign_id


def get_campaign(campaign_id: str) -> Optional[dict]:
    db = get_db()
    row = db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    db.close()
    return dict(row) if row else None


def batch_get_campaigns(campaign_ids: list[str]) -> dict[str, dict]:
    """Load many campaigns at once, keyed by id.

    The signal classifier needs the ICP of every campaign represented in a
    batch of signals; calling get_campaign() per signal would be one query per
    row. Missing ids are simply absent from the result.

    Chunks at 400 to stay under SQLite's 999 variable limit.
    """
    ids = [c for c in dict.fromkeys(campaign_ids) if c]
    if not ids:
        return {}

    out: dict[str, dict] = {}
    db = get_db()
    for i in range(0, len(ids), 400):
        chunk = ids[i : i + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = db.execute(
            f"SELECT * FROM campaigns WHERE id IN ({placeholders})", chunk
        ).fetchall()
        for r in rows:
            row = dict(r)
            out[row["id"]] = row
    db.close()
    return out


def get_campaign_context(campaign_id: str) -> dict:
    """Get parsed campaign context (project_brief, project_facts, offerings, …).

    Returns empty dict if campaign not found or context_json is null.
    """
    campaign = get_campaign(campaign_id)
    if not campaign:
        return {}
    context_raw = campaign.get("context_json", "")
    if not context_raw:
        return {}
    try:
        return json.loads(context_raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def list_campaigns(status: Optional[str] = None) -> list[dict]:
    db = get_db()
    if status:
        rows = db.execute(
            "SELECT * FROM campaigns WHERE status = ? ORDER BY created_at DESC", (status,)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM campaigns ORDER BY created_at DESC").fetchall()
    db.close()
    return [dict(r) for r in rows]


_VALID_CAMPAIGN_COLS = frozenset({
    "name", "icp_json", "status", "mode", "config_json", "context_json", "updated_at",
})


def update_campaign(campaign_id: str, **kwargs: Any) -> None:
    db = get_db()
    kwargs["updated_at"] = int(time.time())
    bad_keys = set(kwargs) - _VALID_CAMPAIGN_COLS
    if bad_keys:
        raise ValueError(f"Invalid campaign columns: {bad_keys}")
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [campaign_id]
    db.execute(f"UPDATE campaigns SET {set_clause} WHERE id = ?", values)
    db.commit()
    db.close()


def _config_json_path(key: str) -> str:
    """A SQLite json path for one top-level key, quoted so a dot or a space in
    the key cannot be read as a path separator."""
    return '$."' + str(key).replace('"', '""') + '"'


def merge_campaign_config(
    campaign_id: str,
    changes: Optional[dict] = None,
    *,
    remove: Sequence[str] = (),
    status: Optional[str] = None,
) -> None:
    """Merge top-level keys into a campaign's config_json in ONE statement.

    Every writer of this column reads it, edits the dict and writes the whole
    document back. The daemon and an MCP session are separate processes on the
    same database, and the refill flow awaits LinkedIn searches between its
    read and its write, so one of them silently reverts the other's keys. The
    hosted side had the same shape and the same symptom: discovery re-fetching
    and re-rejecting the same prospects (heylead-api#482).

    A value REPLACES its key rather than deep-merging, because a cursor
    dropped from a cursor dict has to disappear. Config that will not parse is
    treated as {}, as the read-modify-write it replaces did.

    Flat on purpose: one json_set per key nests the expression as deep as the
    change count and overflows SQLite's parser once a caller sends a few dozen.
    """
    changes = changes or {}
    remove = list(remove)
    if not changes and not remove and status is None:
        return
    params: list[Any] = []
    expr = "CASE WHEN json_valid(config_json) THEN config_json ELSE '{}' END"
    # json_patch is RFC 7396 and MERGES nested objects; these callers mean
    # replacement, so object-valued keys are removed first and patched back.
    drop = [k for k, v in changes.items() if isinstance(v, (dict, list))]
    drop += remove
    if drop:
        placeholders = ", ".join("?" for _ in drop)
        expr = f"json_remove({expr}, {placeholders})"
        params.extend(_config_json_path(k) for k in drop)
    if changes:
        expr = f"json_patch({expr}, json(?))"
        params.append(json.dumps(changes))
    # ``status`` rides along so pause/resume still change the state and its
    # reason in ONE statement; splitting them would let a crash land a paused
    # campaign with no pause_reason.
    status_sql = ", status = ?" if status is not None else ""
    status_params: tuple[Any, ...] = (status,) if status is not None else ()
    db = get_db()
    db.execute(
        f"UPDATE campaigns SET config_json = {expr}{status_sql}, updated_at = ? "
        "WHERE id = ?",
        (*params, *status_params, int(time.time()), campaign_id),
    )
    db.commit()
    db.close()


def merge_campaign_config_delta(
    campaign_id: str,
    before: dict,
    after: dict,
    *,
    status: Optional[str] = None,
) -> None:
    """Write only what changed between a snapshot and the edited dict.

    Diff against the snapshot you READ, never against the row as it stands: a
    key another writer added meanwhile is absent from both sides, so it is
    left alone rather than deleted as a removal. The snapshot must be a deep
    copy, or nested values edited in place are aliased and the diff is empty.
    """
    changes = {
        key: value for key, value in after.items()
        if key not in before or before[key] != value
    }
    remove = [key for key in before if key not in after]
    merge_campaign_config(
        campaign_id, changes, remove=remove, status=status,
    )


def find_active_campaign(campaign_id: str = "") -> tuple[Optional[dict], str]:
    """Find a campaign by ID or return first active. Returns (campaign, error_msg)."""
    if campaign_id:
        campaign = get_campaign(campaign_id)
        if not campaign:
            return None, f"Campaign not found: {campaign_id}"
        return campaign, ""
    campaigns = list_campaigns(status="active")
    if not campaigns:
        # A campaign that exists but hasn't been launched is the common case
        # right after create_campaign. Point at launch, not at creating a
        # second copy of the same campaign.
        drafts = list_campaigns(status="draft")
        if drafts:
            listed = "\n".join(
                f"  {c['name']} — campaign(action='launch', campaign_id='{c['id'][:8]}')"
                for c in drafts
            )
            return None, (
                "No campaign is running yet — these are still drafts:\n\n"
                f"{listed}\n\n"
                "Launch one to start outreach."
            )
        return None, (
            "No active campaigns.\n\n"
            "Create one first: create_campaign(\"your target description\")"
        )
    return campaigns[0], ""


# ──────────────────────────────────────────────
# Contacts
# ──────────────────────────────────────────────

def _normalised_profile_json(value: Any) -> str | None:
    """Blank profile blobs become NULL on the way in; anything else passes.

    '' is a landmine in this column: json_extract raises 'malformed JSON' on
    it and the raise aborts the whole statement rather than skipping the row,
    so one legacy contact blanks every candidate query it touches. NULL says
    the same thing — nothing known — and json_extract returns NULL for it, so
    a reader that forgets to guard degrades instead of crashing.

    NULL rather than '{}' because the choice has to hold on the Python side
    too: '' is falsy and roughly thirty readers branch on
    `if contact.get("profile_json")` or fall back through
    `json.loads(row.get("profile_json") or "{}")`. '{}' is truthy and would
    flip all of them; None keeps every one behaving exactly as it does today.

    Whitespace-only strings are blank as well — json_extract raises on those
    too. The payload itself is stored verbatim: upsert_global_contact keeps
    whichever blob is longer, so trimming here would quietly change which of
    two records wins.
    """
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _campaign_contact_by_email(campaign_id: str, email: str) -> str:
    """Existing campaign contact that already carries this address."""
    from ..services.prospect_email import extract_profile_email

    email_l = extract_profile_email(email).strip().lower()
    if not email_l:
        return ""
    db = get_db()
    rows = db.execute(
        "SELECT id, profile_json FROM contacts WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchall()
    db.close()
    for row in rows:
        stored = extract_profile_email(dict(row)).strip().lower()
        if stored == email_l:
            return row["id"]
    return ""


def _profile_blob(profile_json: str | None) -> dict[str, Any]:
    if not profile_json:
        return {}
    try:
        parsed = json.loads(profile_json) if isinstance(profile_json, str) else profile_json
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _canonical_campaign_linkedin_id(linkedin_id: str, profile_json: str | None) -> str:
    """Prefer provider_id, then public_id, then the incoming slug."""
    blob = _profile_blob(profile_json)
    provider = (blob.get("provider_id") or "").strip()
    if not provider and (linkedin_id or "").startswith("ACoAA"):
        provider = linkedin_id.strip()
    if provider:
        return provider
    public_id = (blob.get("public_id") or blob.get("public_identifier") or "").strip()
    return public_id or (linkedin_id or "").strip()


def _campaign_contact_by_identity(
    campaign_id: str,
    *,
    linkedin_id: str = "",
    email: str = "",
    profile_json: str | None = None,
) -> str:
    """One campaign row per person: column id, email, or profile public/provider id."""
    from ..services.prospect_email import extract_profile_email

    lids = {linkedin_id.strip().lower()} if linkedin_id else set()
    blob = _profile_blob(profile_json)
    for key in ("provider_id", "public_id", "public_identifier"):
        val = (blob.get(key) or "").strip().lower()
        if val:
            lids.add(val)
    lids.discard("")
    email_l = extract_profile_email(email, profile_json).strip().lower()

    db = get_db()
    rows = db.execute(
        "SELECT id, linkedin_id, profile_json FROM contacts WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchall()
    db.close()
    for row in rows:
        stored_lid = (row["linkedin_id"] or "").strip().lower()
        if stored_lid and stored_lid in lids:
            return row["id"]
        if email_l and extract_profile_email(dict(row)).strip().lower() == email_l:
            return row["id"]
        stored_blob = _profile_blob(row["profile_json"])
        for key in ("provider_id", "public_id", "public_identifier"):
            val = (stored_blob.get(key) or "").strip().lower()
            if val and val in lids:
                return row["id"]
    return ""


def _backfill_campaign_contact(
    contact_id: str,
    *,
    linkedin_id: str = "",
    linkedin_url: str = "",
    profile_json: str | None = None,
    title: str = "",
    company: str = "",
) -> None:
    """Fill or refresh identity fields when a later add is the same person."""
    db = get_db()
    row = db.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if not row:
        db.close()
        return
    existing = dict(row)
    updates: dict[str, Any] = {}
    stored_lid = (existing.get("linkedin_id") or "").strip()
    incoming_lid = (linkedin_id or "").strip()
    if incoming_lid:
        if not stored_lid:
            updates["linkedin_id"] = incoming_lid
        elif stored_lid.startswith("ACoAA"):
            pass
        elif incoming_lid.startswith("ACoAA") or incoming_lid != stored_lid:
            updates["linkedin_id"] = incoming_lid
    if linkedin_url and linkedin_url != (existing.get("linkedin_url") or ""):
        updates["linkedin_url"] = linkedin_url
    if title and not (existing.get("title") or "").strip():
        updates["title"] = title
    if company and not (existing.get("company") or "").strip():
        updates["company"] = company
    incoming = _normalised_profile_json(profile_json)
    if incoming and (
        not existing.get("profile_json")
        or len(incoming) > len(existing.get("profile_json") or "")
    ):
        updates["profile_json"] = incoming
    elif incoming:
        from .global_contact_queries import _merge_identity_profile
        merged = _merge_identity_profile(existing.get("profile_json") or "", incoming)
        if merged and merged != (existing.get("profile_json") or ""):
            updates["profile_json"] = merged
    if updates:
        updates["updated_at"] = int(time.time())
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        db.execute(
            f"UPDATE contacts SET {set_clause} WHERE id = ?",
            list(updates.values()) + [contact_id],
        )
        db.commit()
    db.close()


def save_contact(
    campaign_id: str,
    name: str,
    title: str = "",
    company: str = "",
    linkedin_url: str = "",
    linkedin_id: str = "",
    profile_json: str = "",
    fit_score: float = 0.0,
    source: str = "search",
    source_detail: str = "",
    email: str = "",
    contact_id: str = "",
    why_json: str = "",
) -> str:
    """Save a contact. If duplicate (campaign_id + linkedin_id), return existing ID.

    Also maintains the global_contacts master record. An explicit contact_id
    is used only when a NEW row is inserted (cloud adoption keeps the backend
    id so future pushes upsert the same server row); a dedup match still
    returns the existing local id.
    """
    now = int(time.time())
    from ..services.prospect_email import attach_email_to_profile_json, extract_profile_email

    profile_json = _normalised_profile_json(profile_json)
    email = extract_profile_email(email, profile_json)
    if email:
        profile_json = _normalised_profile_json(
            attach_email_to_profile_json(profile_json, email)
        )
    # Canonicalising to the provider id has to keep the slug it displaces, or
    # the person becomes unfindable by the form most callers speak. This layer
    # is where it has to happen: the rewrite below runs before the row is
    # written *and* before profile_json is forwarded to upsert_global_contact,
    # so a slug dropped here is dropped in both tables. Both matchers —
    # _campaign_contact_by_identity here, the public_id branch there — read
    # profile_json for exactly this value.
    canonical_id = _canonical_campaign_linkedin_id(linkedin_id, profile_json)
    if canonical_id != (linkedin_id or "").strip():
        from .global_contact_queries import keep_public_id

        profile_json = _normalised_profile_json(
            keep_public_id(profile_json or "", (linkedin_id or "").strip())
        )
    linkedin_id = canonical_id

    existing_id = _campaign_contact_by_identity(
        campaign_id, linkedin_id=linkedin_id, email=email, profile_json=profile_json,
    )
    if existing_id:
        try:
            from .global_contact_queries import upsert_global_contact
            upsert_global_contact(
                linkedin_id=linkedin_id,
                name=name,
                title=title,
                company=company,
                linkedin_url=linkedin_url,
                email=email,
                profile_json=profile_json,
                fit_score=fit_score,
                source=source,
                source_detail=source_detail,
                campaign_id=campaign_id,
            )
        except Exception:
            pass
        _backfill_campaign_contact(
            existing_id,
            linkedin_id=linkedin_id,
            linkedin_url=linkedin_url,
            profile_json=profile_json,
            title=title,
            company=company,
        )
        return existing_id

    # Upsert global contact (master record per person)
    global_contact_id = None
    try:
        from .global_contact_queries import upsert_global_contact
        global_contact_id = upsert_global_contact(
            linkedin_id=linkedin_id,
            name=name,
            title=title,
            company=company,
            linkedin_url=linkedin_url,
            email=email,
            profile_json=profile_json,
            fit_score=fit_score,
            source=source,
            source_detail=source_detail,
            campaign_id=campaign_id,
        )
    except Exception:
        pass  # Non-critical — don't break contact creation

    contact_id = contact_id or str(uuid.uuid4())
    db = get_db()
    try:
        db.execute(
            """INSERT INTO contacts
               (id, campaign_id, global_contact_id, name, title, company,
                linkedin_url, linkedin_id, profile_json, fit_score,
                source, source_detail, created_at, why_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (contact_id, campaign_id, global_contact_id, name, title, company,
             linkedin_url, linkedin_id, profile_json, fit_score,
             source, source_detail, now, why_json or ""),
        )
        db.commit()
    except sqlite3.IntegrityError:
        # Unique constraint violation — merge into the existing contact
        row = db.execute(
            "SELECT id FROM contacts WHERE campaign_id = ? AND linkedin_id = ? LIMIT 1",
            (campaign_id, linkedin_id),
        ).fetchone()
        db.close()
        if row:
            _backfill_campaign_contact(
                row["id"],
                linkedin_id=linkedin_id,
                linkedin_url=linkedin_url,
                profile_json=profile_json,
                title=title,
                company=company,
            )
            return row["id"]
        # FK failure (empty/unknown campaign_id) used to fall through and
        # return a UUID that was never inserted. enroll_prospect then blew
        # up on create_outreach and inbound_service marked the signal engaged.
        raise
    db.close()
    return contact_id


def get_contacts_for_campaign(campaign_id: str, status: Optional[str] = None) -> list[dict]:
    """Contacts for a campaign, never-scanned first and then least recently scanned.

    Without an ORDER BY this returned insertion order, and the prospect post
    scanner takes the first N eligible rows off the front. With a 4h rescan
    window the head of the list came back round before the tail was ever
    reached, so 648 of 841 contacts on one campaign had never been scanned
    after 48 scan jobs. Same ordering as get_connections_to_scan (666e801).
    """
    order = " ORDER BY (last_scanned_at IS NOT NULL), COALESCE(last_scanned_at, 0) ASC"
    db = get_db()
    if status:
        rows = db.execute(
            "SELECT * FROM contacts WHERE campaign_id = ? AND status = ?" + order,
            (campaign_id, status),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM contacts WHERE campaign_id = ?" + order, (campaign_id,)
        ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def pending_outreach_for_contact(contact_id: str) -> dict | None:
    """Pending outreach row for a campaign contact, if one exists."""
    if not contact_id:
        return None
    db = get_db()
    row = db.execute(
        """SELECT * FROM outreaches
           WHERE contact_id = ? AND status = 'pending'
           LIMIT 1""",
        (contact_id,),
    ).fetchone()
    db.close()
    return dict(row) if row else None


def list_pending_contacts_at_company(
    company: str,
    campaign_id: str | None = None,
) -> list[dict]:
    """Pending campaign people whose company matches *company*.

    Matching is suffix-tolerant and whole-token (see ``companies_match``),
    not exact SQL string equality — ``Citi Inc.`` attaches to ``Citi``.
    """
    if not (company or "").strip():
        return []
    from ..services.company_name import companies_match

    db = get_db()
    if campaign_id:
        rows = db.execute(
            """SELECT c.*, o.id AS outreach_id
               FROM contacts c
               JOIN outreaches o ON o.contact_id = c.id
               WHERE o.status = 'pending' AND o.campaign_id = ?""",
            (campaign_id,),
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT c.*, o.id AS outreach_id
               FROM contacts c
               JOIN outreaches o ON o.contact_id = c.id
               WHERE o.status = 'pending'""",
        ).fetchall()
    db.close()
    return [dict(r) for r in rows if companies_match(company, r["company"] or "")]


_VALID_CONTACT_COLS = frozenset({
    "name", "title", "company", "fit_score", "status", "updated_at",
    "analysis_json", "global_contact_id", "source", "source_detail",
    "profile_json",
})


def update_contact(contact_id: str, **kwargs: Any) -> None:
    """Update contact fields (name, title, company, fit_score, status)."""
    db = get_db()
    kwargs["updated_at"] = int(time.time())
    bad_keys = set(kwargs) - _VALID_CONTACT_COLS
    if bad_keys:
        raise ValueError(f"Invalid contact columns: {bad_keys}")
    if "profile_json" in kwargs:
        kwargs["profile_json"] = _normalised_profile_json(kwargs["profile_json"])
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [contact_id]
    db.execute(f"UPDATE contacts SET {set_clause} WHERE id = ?", values)
    db.commit()
    db.close()


def save_contact_analysis(contact_id: str, analysis: dict[str, Any]) -> None:
    """Write prospect analysis to contacts.analysis_json."""
    db = get_db()
    db.execute(
        "UPDATE contacts SET analysis_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(analysis), int(time.time()), contact_id),
    )
    db.commit()
    db.close()


def get_contact_analysis(contact_id: str) -> dict[str, Any] | None:
    """Load cached prospect analysis from contacts.analysis_json.

    Returns parsed dict or None if not cached.
    """
    db = get_db()
    row = db.execute(
        "SELECT analysis_json FROM contacts WHERE id = ?",
        (contact_id,),
    ).fetchone()
    db.close()
    if not row or not row["analysis_json"]:
        return None
    try:
        return json.loads(row["analysis_json"])
    except (json.JSONDecodeError, TypeError):
        return None


# ──────────────────────────────────────────────
# Outreaches
# ──────────────────────────────────────────────

_SN_MASK_TITLE = re.compile(
    r"^(someone|recruiter|product manager|executive director|"
    r"director|manager|engineer|consultant) at ",
    re.IGNORECASE,
)
_INBOUND_SOURCES = frozenset({
    "inbound", "inbound_dm", "inbound_invitation", "inbound_comment",
    "referral", "hot_lead",
})
_DISCOVERY_SOURCES = frozenset({
    "search", "linkedin_search", "auto_enrichment", "signal_discovery",
})


def _first_degree_refusal(prospect: dict, campaign_id: str) -> bool:
    """True when this campaign excludes people it was already connected to.

    9 Sep 2026. Reads the campaign's own config rather than a global
    setting, and compares the connection's date against the campaign's start:
    someone who accepts *this* campaign's invitation becomes 1st-degree
    mid-flight and must still receive the opener.
    """
    if not campaign_id:
        return False
    try:
        from ..linkedin.unipile import get_account_id
        from ..services.connection_sync import is_preexisting_connection
        from ..services.outreach_channel import exclude_connections_enabled

        campaign = get_campaign(campaign_id)
        if not campaign:
            return False
        try:
            config = json.loads(campaign.get("config_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            config = {}
        if not isinstance(config, dict) or not exclude_connections_enabled(config):
            return False

        account_id = get_account_id()
        if not account_id:
            return False

        blob = _profile_blob(prospect.get("profile_json"))
        provider_id = str(
            prospect.get("provider_id") or blob.get("provider_id") or ""
        ).strip()
        public_id = str(
            prospect.get("public_id")
            or prospect.get("linkedin_id")
            or blob.get("public_id")
            or blob.get("public_identifier")
            or ""
        ).strip()
        return is_preexisting_connection(
            account_id, provider_id, public_id,
            campaign_created_at=campaign.get("created_at"),
        )
    except Exception:
        # A gate that cannot read its inputs must not silently refuse everyone.
        logger.debug("first_degree enrol check failed", exc_info=True)
        return False


def refuse_junk_at_enrol(
    prospect: dict, *, source: str, campaign_id: str = "",
) -> str | None:
    """Return a short skip reason, or None if this person may join a campaign.

    ``campaign_id`` is optional only because five years of callers predate it;
    without it the ``first_degree`` refusal cannot run, since the rule is a
    per-campaign setting compared against that campaign's start date.
    """
    from ..services.dedup_service import is_company_profile
    from ..services.own_identity import is_own_identity
    from ..services.provider_id_resolver import is_sendable_invite_id

    if is_own_identity(prospect):
        return "own_account"

    if _first_degree_refusal(prospect, campaign_id):
        return "first_degree"

    inbound = source in _INBOUND_SOURCES or source.startswith("inbound")
    name = (prospect.get("name") or "").strip()
    ids = [
        str(prospect.get(k) or "").strip()
        for k in ("provider_id", "public_id", "linkedin_id")
    ]
    blob = _profile_blob(prospect.get("profile_json"))
    for key in ("provider_id", "public_id", "public_identifier"):
        ids.append(str(blob.get(key) or "").strip())
    sendable = any(is_sendable_invite_id(i) for i in ids if i)
    has_email = bool(
        (prospect.get("email") or "").strip()
        or (blob.get("email") or "").strip()
    )

    if not inbound and is_company_profile(prospect):
        return "company_page"
    # CSV is a human list: Sales Nav ACw ids and missing slugs still enrol
    # so a later repair can resolve them. Discovery sources must not.
    # A named referral with an email is sendable that same day even when
    # LinkedIn search missed — Chris's Jordan handoff is that case.
    if not sendable and source != "csv_import" and not (
        source == "referral" and has_email
    ):
        return "unsendable_id"
    if not inbound and (
        source in _DISCOVERY_SOURCES or not source
    ) and _is_sn_mask_name(name):
        return "anonymized_sales_nav"
    return None


def _is_sn_mask_name(name: str) -> bool:
    n = (name or "").strip()
    if not n:
        return False
    low = n.lower()
    if low.startswith("someone at "):
        return True
    if _SN_MASK_TITLE.match(n):
        return True
    if " in the " in low and not re.match(r"^[A-Z][a-z]+ [A-Z]", n):
        return True
    return False


def _why_json_of(prospect: dict) -> str:
    """The enrolment `why` (services.enrolment_why) as stored JSON, or ""."""
    why = prospect.get("why")
    if not isinstance(why, dict) or not why:
        return ""
    try:
        return json.dumps(why)
    except (TypeError, ValueError):
        return ""


def enroll_prospect(
    campaign_id: str,
    prospect: dict,
    *,
    source: str,
    source_detail: str = "",
    status: str = "pending",
    variant: str | None = None,
    signal_id: str | None = None,
) -> str | None:
    """Save a contact and create (or reuse) one outreach. None if junk."""
    if not (campaign_id or "").strip():
        return None
    reason = refuse_junk_at_enrol(
        prospect, source=source, campaign_id=campaign_id,
    )
    if reason:
        try:
            from ..ops_log import log_event

            log_event(
                "enrol_rejected",
                campaign_id=campaign_id,
                reason=reason,
                source=source,
            )
        except Exception:
            logger.debug("enrol_rejected log failed", exc_info=True)
        return None

    profile_json = prospect.get("profile_json") or ""
    if isinstance(profile_json, dict):
        blob = dict(profile_json)
        profile_json = ""
    else:
        blob = _profile_blob(profile_json)
    for key in ("provider_id", "public_id"):
        val = (prospect.get(key) or "").strip()
        if val and not blob.get(key):
            blob[key] = val
    if blob and not profile_json:
        profile_json = json.dumps(blob)
    elif blob:
        profile_json = json.dumps(blob)

    linkedin_id = (
        prospect.get("linkedin_id")
        or prospect.get("public_id")
        or prospect.get("provider_id")
        or ""
    )
    contact_id = save_contact(
        campaign_id=campaign_id,
        name=prospect.get("name", ""),
        title=prospect.get("title", ""),
        company=prospect.get("company", ""),
        linkedin_url=prospect.get("linkedin_url", ""),
        linkedin_id=linkedin_id,
        profile_json=profile_json,
        fit_score=float(prospect.get("fit_score") or 0.0),
        source=source,
        source_detail=source_detail,
        email=prospect.get("email") or "",
        why_json=_why_json_of(prospect),
    )
    existing = _campaign_outreach_for_contact_or_identity(
        campaign_id, contact_id, prospect,
    )
    if existing:
        if signal_id:
            update_outreach(existing, signal_id=signal_id)
        return existing
    return create_outreach(
        campaign_id, contact_id,
        status=status, variant=variant, signal_id=signal_id,
    )


def _campaign_outreach_for_contact_or_identity(
    campaign_id: str, contact_id: str, prospect: dict,
) -> str:
    db = get_db()
    row = db.execute(
        "SELECT id FROM outreaches WHERE campaign_id = ? AND contact_id = ?",
        (campaign_id, contact_id),
    ).fetchone()
    if row:
        db.close()
        return row["id"]

    lids = {str(prospect.get(k) or "").strip().lower() for k in (
        "linkedin_id", "public_id", "provider_id",
    )}
    blob = _profile_blob(prospect.get("profile_json"))
    for key in ("provider_id", "public_id", "public_identifier"):
        lids.add(str(blob.get(key) or "").strip().lower())
    lids.discard("")
    if not lids:
        db.close()
        return ""
    rows = db.execute(
        """SELECT o.id, c.linkedin_id, c.profile_json
           FROM outreaches o
           JOIN contacts c ON c.id = o.contact_id
           WHERE o.campaign_id = ?""",
        (campaign_id,),
    ).fetchall()
    db.close()
    for stored in rows:
        stored_lid = (stored["linkedin_id"] or "").strip().lower()
        if stored_lid and stored_lid in lids:
            return stored["id"]
        stored_blob = _profile_blob(stored["profile_json"])
        for key in ("provider_id", "public_id", "public_identifier"):
            val = (stored_blob.get(key) or "").strip().lower()
            if val and val in lids:
                return stored["id"]
    return ""


def create_outreach(
    campaign_id: str,
    contact_id: str,
    status: str = "pending",
    variant: str | None = None,
    signal_id: str | None = None,
    outreach_id: str = "",
) -> str:
    # An explicit id is how cloud-created outreaches keep their backend id
    # locally — the same pull's messages and engagements FK on it.
    outreach_id = outreach_id or str(uuid.uuid4())
    now = int(time.time())
    db = get_db()
    try:
        db.execute(
            """INSERT INTO outreaches (id, campaign_id, contact_id, status, variant, signal_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (outreach_id, campaign_id, contact_id, status, variant, signal_id, now, now),
        )
        db.commit()
    except sqlite3.IntegrityError:
        # Duplicate (campaign_id, contact_id) — return existing outreach ID
        row = db.execute(
            "SELECT id FROM outreaches WHERE campaign_id = ? AND contact_id = ?",
            (campaign_id, contact_id),
        ).fetchone()
        db.close()
        if row:
            logger.debug("Outreach already exists for contact %s in campaign %s", contact_id, campaign_id)
            return row[0]
        raise  # Unexpected IntegrityError — re-raise
    db.close()
    return outreach_id


_VALID_OUTREACH_COLS = frozenset({
    "status", "next_action", "scheduled_at", "followup_count", "updated_at",
    "outcome_json", "memory_json", "variant", "channel", "signal_id",
    "invited_at", "accepted_at", "first_reply_at",
    "invite_attempts", "last_attempt_error",
    "headline_variant", "headline_test_id",
    "chat_id",
    "cloud_status_version", "local_news",
    "decision_id",
})

# Status sets that trigger event timestamps
_INVITED_STATUSES = frozenset({"invited"})
# Statuses meaning "this outreach is over" — no further work should run for it.
# 'expired' is authored by the backend, not here — it arrives on an outreach
# that is finished, and a follow-up queued while it was still 'invited' would
# otherwise outlive it. The 310 rows already at 'expired' carry no jobs only
# because the planner does not select a status it cannot name.
# Statuses after which no queued job may still reach this person. Twin of the
# backend's scheduler_store.TERMINAL_OUTREACH_STATUSES and the dashboard's
# OutreachTable.TERMINAL_STATUSES.
#
# The three closed statuses were missing until 9 Sep 2026, so a local close —
# including an opt_out, the one status that is meant to suppress everywhere —
# nulled next_action and left every already-queued job on the rails.
_TERMINAL_OUTREACH_STATUSES = frozenset({
    "skipped", "error", "unsubscribed", "bounced", "expired",
    "closed_happy", "closed_unhappy", "opted_out",
})

# 'connected' and 'messaged' are deliberately absent: flips into them are
# often discovery or bookkeeping (a pre-check finding an existing 1st-degree,
# a repair reverting a status, a deleted message re-queuing a follow-up) —
# none of which observe an acceptance happening now. Paths that DO observe
# the acceptance pass accepted_at explicitly. A reply, by contrast, proves
# the connection exists, so the reply-class statuses keep the inference.
_ACCEPTED_STATUSES = frozenset({
    "replied", "hot_lead",
    "closed_happy", "closed_unhappy", "reverse_pitch", "opted_out",
})
_REPLIED_STATUSES = frozenset({
    "replied", "hot_lead", "closed_happy", "closed_unhappy",
    "reverse_pitch", "opted_out",
})


def _auto_set_event_timestamps(
    db: Any,
    outreach_id: str,
    new_status: str,
    now: int,
    kwargs: dict[str, Any],
) -> None:
    """Auto-populate invited_at/accepted_at/first_reply_at on first occurrence.

    Only sets a timestamp if the column is currently NULL (only-first semantics).
    Mutates kwargs in-place so timestamps are included in the same UPDATE.
    """
    needs_invited = new_status in _INVITED_STATUSES
    needs_accepted = new_status in _ACCEPTED_STATUSES
    needs_reply = new_status in _REPLIED_STATUSES

    if not (needs_invited or needs_accepted or needs_reply):
        return

    row = db.execute(
        "SELECT invited_at, accepted_at, first_reply_at FROM outreaches WHERE id = ?",
        (outreach_id,),
    ).fetchone()
    if not row:
        return
    current = dict(row)

    # A value the caller passed is an observation — never overwrite it with now.
    if needs_invited and "invited_at" not in kwargs and current.get("invited_at") is None:
        kwargs["invited_at"] = now
    if needs_accepted and "accepted_at" not in kwargs and current.get("accepted_at") is None:
        kwargs["accepted_at"] = now
    if needs_reply and "first_reply_at" not in kwargs and current.get("first_reply_at") is None:
        kwargs["first_reply_at"] = now


def _status_change_source() -> str:
    """``filename:lineno`` of whoever asked for the status change.

    This used to read frame[-2] of a three-frame stack, which is the direct
    caller only when update_outreach is invoked synchronously. Everything
    routed through ``run_db`` executes on the DB thread, whose stack starts at
    the executor callable — so the trail recorded ``async_bridge.py`` for
    95,859 of the 99,553 status changes in the live DB and the caller was
    unrecoverable. Tracing the 310 'expired' outreaches cost a day for exactly
    that reason. run_db now carries its own caller across the thread boundary;
    walk to it rather than reporting the plumbing.
    """
    try:
        frame: Any = sys._getframe(2)  # skip this helper and update_outreach
    except (AttributeError, ValueError):  # pragma: no cover - non-CPython
        return ""
    if frame is None:
        return ""
    name = frame.f_code.co_filename.rsplit("/", 1)[-1]
    if name == "async_bridge.py":
        from .async_bridge import current_call_source

        return current_call_source() or f"{name}:{frame.f_lineno}"
    return f"{name}:{frame.f_lineno}"


# Contacted rows may still be parked when the reason is an operator
# decision or a clear "wrong buyer" verdict. Fit-score parks stay blocked
# so an accepted invite is not rewound by a later rescore.
_OPERATOR_SKIP_REASONS = frozenset({
    "operator_skip",
    "do_not_contact",
    "targeting_mismatch",
    # exclude_connections (9 Sep 2026). Without this, a row that had
    # already reached 'connected' — which is exactly what a pre-existing
    # 1st-degree connection looks like once any guard has seen it — could not
    # be parked, and the campaign kept it in the DM queue for ever.
    "first_degree",
})
_CONTACTED_STATUSES = frozenset({
    "invited", "connected", "messaged", "replied", "hot_lead",
})


def update_outreach(outreach_id: str, **kwargs: Any) -> bool:
    expected_status = kwargs.pop("expected_status", None)
    from_cloud = kwargs.pop("from_cloud", False)
    db = get_db()
    now = int(time.time())
    kwargs["updated_at"] = now
    # Versioned Outreach Sync: a LOCAL change to the funnel tuple is news
    # the cloud has not seen — mark it so the pull defers to it until the
    # push carries it up. Cloud applies never mark (and clear the flag).
    _funnel_touched = bool(
        set(kwargs) & {"status", "invited_at", "accepted_at", "first_reply_at"}
    )
    if from_cloud:
        kwargs.setdefault("local_news", 0)
    elif _funnel_touched:
        kwargs.setdefault("local_news", 1)

    # Auto-populate event timestamps on first status transition
    new_status = kwargs.get("status")

    # Read old status for audit logging (before the update)
    old_status = None
    if new_status or expected_status is not None:
        row = db.execute(
            "SELECT status, invited_at FROM outreaches WHERE id = ?", (outreach_id,)
        ).fetchone()
        old_status = row["status"] if row else None
        invited_at = row["invited_at"] if row else None
        if expected_status is not None and old_status != expected_status:
            db.close()
            return False
        if new_status == "skipped":
            if not kwargs.get("last_attempt_error"):
                kwargs["last_attempt_error"] = "skipped"
            contacted = old_status in _CONTACTED_STATUSES or bool(invited_at)
            if not contacted:
                msg = db.execute(
                    """SELECT 1 FROM messages
                       WHERE outreach_id = ? AND role = 'sdr' LIMIT 1""",
                    (outreach_id,),
                ).fetchone()
                contacted = msg is not None
            if contacted and kwargs.get("last_attempt_error") not in _OPERATOR_SKIP_REASONS:
                db.close()
                return False
        if new_status:
            _auto_set_event_timestamps(db, outreach_id, new_status, now, kwargs)

    bad_keys = set(kwargs) - _VALID_OUTREACH_COLS
    if bad_keys:
        raise ValueError(f"Invalid outreach columns: {bad_keys}")
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    if expected_status is not None:
        cursor = db.execute(
            f"UPDATE outreaches SET {set_clause} WHERE id = ? AND status = ?",
            list(kwargs.values()) + [outreach_id, expected_status],
        )
        if cursor.rowcount == 0:
            db.close()
            return False
    else:
        db.execute(
            f"UPDATE outreaches SET {set_clause} WHERE id = ?",
            list(kwargs.values()) + [outreach_id],
        )

    # An outreach that has been pulled out of the campaign must not leave work
    # behind: a queued job outlives it and would execute against a prospect the
    # user already stopped. Only 'pending' jobs are reaped — a 'running' job is
    # mid-flight and must not be rewritten underneath itself.
    if new_status in _TERMINAL_OUTREACH_STATUSES and new_status != old_status:
        db.execute(
            "UPDATE scheduler_jobs SET status = 'cancelled' "
            "WHERE outreach_id = ? AND status = 'pending'",
            (outreach_id,),
        )

    # When prospect connects/replies, mark SDR messages as read (inferred read receipt)
    if new_status and new_status in _ACCEPTED_STATUSES and new_status != old_status:
        db.execute(
            "UPDATE messages SET read_at = ? WHERE outreach_id = ? AND role = 'sdr' AND read_at IS NULL",
            (now, outreach_id),
        )

    db.commit()
    db.close()

    # Log status transitions for audit trail
    if new_status and new_status != old_status:
        caller = _status_change_source()
        try:
            log_action(
                "outreach_status_change",
                outreach_id=outreach_id,
                result=new_status,
                details={
                    "old_status": old_status,
                    "new_status": new_status,
                    "source": caller,
                    **{k: v for k, v in kwargs.items()
                       if k in ("followup_count", "channel", "last_attempt_error")},
                },
            )
        except Exception:
            pass  # Non-critical — never break outreach updates

    # Promote global contact lifecycle based on outreach status change
    if new_status:
        try:
            from .global_contact_queries import promote_lifecycle_from_outreach
            promote_lifecycle_from_outreach(outreach_id, new_status)
        except Exception:
            pass  # Non-critical — don't break outreach updates

    return True


def skip_pending_outreaches(campaign_id: str) -> int:
    """Mark all pending outreaches for a campaign as skipped. Returns count."""
    db = get_db()
    now = int(time.time())
    cursor = db.execute(
        "UPDATE outreaches SET status = 'skipped', updated_at = ? "
        "WHERE campaign_id = ? AND status = 'pending'",
        (now, campaign_id),
    )
    count = cursor.rowcount
    db.commit()
    db.close()
    return count


def close_withdrawn_invitation(provider_id: str, public_id: str) -> str | None:
    """Close the outreach behind an invitation just withdrawn on LinkedIn.

    `outreaches` carries no invitation_id, so the row is reached through the
    contact. Unipile names the invitee twice on a sent invitation — the ACoAA
    provider id and the public slug — and either form may be the one sitting in
    contacts.linkedin_id, with the provider id also copied inside profile_json.
    All three routes are tried; the same member arriving under two id forms is
    the standing hazard here.

    Without this the row keeps status='invited' after the invitation is gone:
    it can never escalate (the InMail window shuts at 35 days) and every rollup
    keeps counting it as outstanding. 'withdrawn' stays inside the funnel's
    invited bucket, so closing the row does not erase the send.

    The json_valid guard is load-bearing: json_extract raises on '', which 1685
    of 5860 production contacts store, and the raise takes down the whole
    statement rather than skipping the row.
    """
    ids = [v.lower() for v in (provider_id, public_id) if v]
    if not ids:
        return None

    db = get_db()
    slots = ",".join("?" for _ in ids)
    row = db.execute(
        f"""SELECT o.id
            FROM outreaches o
            JOIN contacts c ON c.id = o.contact_id
            WHERE o.status = 'invited'
              AND (
                LOWER(COALESCE(c.linkedin_id, '')) IN ({slots})
                OR (
                  json_valid(c.profile_json)
                  AND LOWER(COALESCE(
                    json_extract(c.profile_json, '$.provider_id'), ''
                  )) IN ({slots})
                )
              )
            ORDER BY o.invited_at
            LIMIT 1""",
        (*ids, *ids),
    ).fetchone()
    db.close()
    if not row:
        return None

    update_outreach(row["id"], status="withdrawn")
    return row["id"]


def find_followup_count_mismatches() -> list[dict]:
    """Find outreaches where followup_count doesn't match actual sdr message count.

    Returns list of dicts with outreach_id, followup_count, actual_msgs, name.
    Used by daily health check to detect and auto-correct data inconsistencies.
    """
    db = get_db()
    rows = db.execute(
        """SELECT o.id as outreach_id, o.followup_count, o.status,
                  c.name,
                  (SELECT COUNT(*) FROM messages m
                   WHERE m.outreach_id = o.id AND m.role = 'sdr') as actual_msgs
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           WHERE o.status NOT IN ('pending', 'skipped')
             AND o.followup_count != (
                 SELECT COUNT(*) FROM messages m
                 WHERE m.outreach_id = o.id AND m.role = 'sdr'
             )
           ORDER BY c.name
           LIMIT 50"""
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


# When we last said something to this person — the clock the follow-up
# cadence counts from. Outbound only: a prospect's reply is not one of our
# touches, which is the one difference from ``_LAST_ACTIVITY_SQL`` below
# (staleness asks when anybody last spoke; cadence asks when *we* did).
#
# NOT ``updated_at``, and not ``accepted_at`` either. ``updated_at`` is a
# row-mutation clock that every sync and every planner pass bumps; keyed on
# it a cooldown never elapses, because planning the follow-up resets the
# clock that decides it is due (hosted, 16 Sep 2026: 67 people accepted and
# 0 follow-ups went out). ``accepted_at`` anchors every step of the schedule
# to the acceptance instead of to the previous message, so the whole drip
# compresses toward it — and it is a batch detection stamp, not the moment
# the person accepted.
_LAST_OUTBOUND_SQL = """(
        SELECT MAX(m.timestamp) FROM messages m
         WHERE m.outreach_id = o.id AND m.role = 'sdr' AND m.deleted_at IS NULL
    )"""

# Longest-unanswered first, for any queue the caller then truncates. The order
# a queue is SERVED has to use the same clock the due-check DECIDES on, or the
# cap silently picks a different set of people than the predicate chose:
# _plan_followups takes 2 rows a tick, find_orphaned_outreaches LIMITs to 10.
# `accepted_at ASC` looks like "longest waiting" and is not — it is longest
# since ACCEPTANCE, so somebody messaged yesterday outranks somebody last
# messaged a week ago, and it is a batch detection stamp besides.
_LAST_TOUCH_ORDER_SQL = f"""COALESCE(
        {_LAST_OUTBOUND_SQL}, o.accepted_at, o.invited_at, o.created_at
    ) ASC"""


def find_orphaned_outreaches(
    campaign_id: str, max_followups: int = 2,
    late_accepters_since: int | None = None,
) -> list[dict]:
    """Find connected/messaged outreaches with no pending send_dm or followup jobs.

    These are prospects that fell through the cracks — accepted a connection
    but have no scheduled work. Returns outreaches that need rescue.
    Includes 'messaged' status for DM-only campaigns that skip 'connected'.

    *late_accepters_since* narrows the search to prospects who accepted at or
    after that epoch and were never opened (`status='connected'`) — the
    completed-campaign case. It belongs in the query rather than in the caller
    because of the LIMIT below: the order is `accepted_at ASC`, so the ten rows
    returned are the ten *oldest*, and filtering the window afterwards discards
    the whole page whenever a campaign holds ten or more stale orphans. Live,
    campaign 6cc658fb holds 95, so that is its normal case rather than an edge
    one, and the recent accepter would never appear in the page to be kept.
    """
    clauses = [
        "o.campaign_id = ?",
        "o.followup_count < ?",
        """o.id NOT IN (
               SELECT sj.outreach_id FROM scheduler_jobs sj
               WHERE sj.outreach_id IS NOT NULL
                 AND sj.job_type IN ('send_dm', 'followup')
                 AND sj.status IN ('pending', 'running')
           )""",
    ]
    params: list[Any] = [campaign_id]
    if late_accepters_since is None:
        # followup_count=0 is "needs the opening DM". A real SDR DM means it
        # already went out — invite notes do not, and they no longer hold the
        # 24h gap either: a fresh acceptance is immediately DM-eligible even
        # when the invitation carried a note. Pending/running still dedup.
        since = int(time.time()) - 86400
        clauses.insert(
            1,
            f"""((o.followup_count = 0 AND o.status = 'connected'
                 AND NOT {SDR_REAL_DM_SQL}
                 AND NOT {SDR_RECENT_SDR_SQL})
                OR (o.followup_count > 0 AND o.status IN ('connected', 'messaged')))""",
        )
        params.append(since)
    params.append(max_followups)
    if late_accepters_since is not None:
        # A NULL accepted_at fails this comparison, which is intended: it is a
        # date we do not have, not a recent one.
        clauses.insert(1, "o.status = 'connected'")
        clauses.append("o.accepted_at >= ?")
        params.append(late_accepters_since)

    db = get_db()
    rows = db.execute(
        f"""SELECT o.id as outreach_id, o.campaign_id, o.followup_count,
                   o.accepted_at, o.invited_at, o.created_at,
                   o.status as outreach_status,
                   o.updated_at as outreach_updated_at,
                   {_LAST_OUTBOUND_SQL} AS last_sdr_message_at,
                   c.name, c.fit_score
            FROM outreaches o
            JOIN contacts c ON o.contact_id = c.id
            WHERE {' AND '.join(clauses)}
            ORDER BY {_LAST_TOUCH_ORDER_SQL}
            LIMIT 10""",
        tuple(params),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def count_due_sendable_work(min_fit_score: float | None = None) -> int:
    """People on active campaigns the planner would try to contact now.

    Pending rows that pass the fit gate, plus connected rows that still need
    an opening DM and whose last SDR row (including an invite note) is older
    than the 24h message gap. Connected-inside-gap is waiting, not stalled.
    """
    from ..constants import MIN_FIT_SCORE_THRESHOLD

    threshold = MIN_FIT_SCORE_THRESHOLD if min_fit_score is None else min_fit_score
    since = int(time.time()) - 86400
    db = get_db()
    row = db.execute(
        f"""SELECT COUNT(*) AS cnt
            FROM outreaches o
            JOIN contacts c ON c.id = o.contact_id
            JOIN campaigns camp ON camp.id = o.campaign_id
            WHERE camp.status = 'active'
              AND (
                  (o.status = 'pending' AND {FIT_SENDABLE_SQL})
                  OR (
                      o.status = 'connected'
                      AND COALESCE(o.followup_count, 0) = 0
                      AND NOT {SDR_REAL_DM_SQL}
                      AND NOT {SDR_RECENT_SDR_SQL}
                  )
              )""",
        (threshold, since),
    ).fetchone()
    db.close()
    return int(row["cnt"] if row else 0)


def count_open_outreaches(campaign_id: str) -> dict[str, int]:
    """Count outreaches by status that still have pending work.

    'pending' is the warm-up / not-yet-invited queue. Leaving it out made
    archive_campaign treat a freshly launched campaign as empty and
    skip_pending_outreaches the whole list.
    """
    db = get_db()
    rows = db.execute(
        """SELECT status, COUNT(*) as cnt FROM outreaches
           WHERE campaign_id = ? AND status IN ('pending', 'connected', 'invited')
           GROUP BY status""",
        (campaign_id,),
    ).fetchall()
    db.close()
    return {r["status"]: r["cnt"] for r in rows}


def get_outreach(outreach_id: str) -> Optional[dict]:
    """Get a single outreach by ID."""
    db = get_db()
    row = db.execute("SELECT * FROM outreaches WHERE id = ?", (outreach_id,)).fetchone()
    db.close()
    return dict(row) if row else None


def get_outreach_memory(outreach_id: str) -> list[dict]:
    """Get structured conversation memory for an outreach.

    Returns a list of per-followup decision records (cta_used, pain_used,
    greeting, structure, etc.) used for anti-repetition in future follow-ups.
    """
    outreach = get_outreach(outreach_id)
    if not outreach:
        return []
    raw = outreach.get("memory_json", "")
    if not raw:
        return []
    try:
        memory = json.loads(raw)
        return memory if isinstance(memory, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def save_outreach_memory(outreach_id: str, memory_entry: dict) -> None:
    """Append a follow-up memory entry to the outreach's memory_json.

    Each entry captures what was used in a specific follow-up
    (CTA, pain angle, greeting, structure, etc.) so future
    follow-ups can avoid repeating the same patterns.
    """
    existing = get_outreach_memory(outreach_id)
    existing.append(memory_entry)
    update_outreach(outreach_id, memory_json=json.dumps(existing))


def get_outreach_with_contact(outreach_id: str) -> Optional[dict]:
    """Get outreach data merged with contact info for tool display."""
    db = get_db()
    row = db.execute(
        """SELECT o.id as outreach_id, o.campaign_id, o.contact_id,
                  o.status, o.followup_count, o.next_action, o.updated_at,
                  o.signal_id, o.chat_id, o.accepted_at, o.first_reply_at,
                  o.last_attempt_error, o.invited_at,
                  c.name, c.title, c.company, c.linkedin_url, c.linkedin_id,
                  c.fit_score, c.profile_json, c.analysis_json,
                  c.source as contact_source
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           WHERE o.id = ?""",
        (outreach_id,),
    ).fetchone()
    db.close()
    return dict(row) if row else None


def count_outreaches_by_status(campaign_id: str, status: str) -> int:
    """Count outreaches with a given status in a campaign."""
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) as cnt FROM outreaches WHERE campaign_id = ? AND status = ?",
        (campaign_id, status),
    ).fetchone()
    db.close()
    return row["cnt"] if row else 0


def get_next_pending_outreach(campaign_id: str, exclude_job_type: str = "") -> Optional[dict]:
    """Get the next pending outreach for a campaign, ordered by fit_score DESC.

    Used by the planner to bind scheduler jobs to specific outreaches,
    preventing duplicate sends when multiple schedulers are active.

    If *exclude_job_type* is given, outreaches that already have a
    pending/running job of that type are skipped so the planner doesn't
    get stuck on the same top-ranked prospect every tick.
    """
    db = get_db()
    if exclude_job_type:
        row = db.execute(
            """SELECT o.id, o.contact_id, c.fit_score
               FROM outreaches o
               JOIN contacts c ON o.contact_id = c.id
               WHERE o.campaign_id = ? AND o.status = 'pending'
                 AND o.id NOT IN (
                     SELECT sj.outreach_id FROM scheduler_jobs sj
                     WHERE sj.outreach_id IS NOT NULL
                       AND sj.job_type = ?
                       AND sj.status IN ('pending', 'running')
                 )
               ORDER BY c.fit_score DESC
               LIMIT 1""",
            (campaign_id, exclude_job_type),
        ).fetchone()
    else:
        row = db.execute(
            """SELECT o.id, o.contact_id, c.fit_score
               FROM outreaches o
               JOIN contacts c ON o.contact_id = c.id
               WHERE o.campaign_id = ? AND o.status = 'pending'
               ORDER BY c.fit_score DESC
               LIMIT 1""",
            (campaign_id,),
        ).fetchone()
    db.close()
    return dict(row) if row else None


def get_next_dm_candidate(
    campaign_id: str,
    exclude_job_type: str = "",
    only_connected: bool = False,
    min_fit_score: float = 0.0,
    *,
    exclude_first_degree: bool = False,
    campaign_created_at: int | None = None,
) -> Optional[dict]:
    """Get next outreach needing a first DM.

    Picks prospects that haven't been messaged yet (followup_count=0, no
    real SDR DM, no recent send_dm jobs). Invitation notes do not count as
    the opening DM. Ordered by fit_score DESC.

    When *only_connected* is True, restricts to 'connected' plus pending
    people already in the local connections table (silent-sync 1st degree).
    A regular DM to a non-connection is a LinkedIn 403. When False (DM-only
    / connections-only campaigns), includes every 'pending' too.
    """
    db = get_db()
    if only_connected:
        status_clause = f"""(
            o.status = 'connected'
            OR (
                o.status = 'pending'
                AND EXISTS (
                    SELECT 1 FROM connections cn
                    WHERE cn.removed_at IS NULL
                      AND (cn.provider_id = json_extract({_SAFE_CONTACT_PROFILE}, '$.provider_id')
                           OR (c.linkedin_id IS NOT NULL AND c.linkedin_id != ''
                               AND LOWER(cn.public_id) = LOWER(c.linkedin_id)))
                )
            )
        )"""
    else:
        status_clause = "o.status IN ('pending', 'connected')"

    # exclude_connections: never queue the DM in the first place. The executor
    # still re-checks — the planner runs minutes ahead of the send — but a job
    # that was never created cannot race a sync.
    exclusion_clause = ""
    exclusion_params: tuple = ()
    if exclude_first_degree:
        exclusion_clause = f" AND NOT {_PREEXISTING_CONNECTION_SQL}"
        exclusion_params = (int(campaign_created_at or 0) or 2**31,)
    # Exclude outreaches that:
    # 1. Have pending/running send_dm jobs (dedup)
    # 2. Already have a real SDR DM (invite notes do not count)
    # 3. Last SDR row — including the invite note — is still inside the 24h gap
    # Connected people already got the invite; do not re-apply the fit gate
    # (Amanda accepted at 0.26 and would otherwise never get the opening DM).
    # Completed skip jobs must not hide them after the note gap lifts.
    since = int(time.time()) - 86400
    row = db.execute(
        f"""SELECT o.id, o.contact_id, c.fit_score
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           WHERE o.campaign_id = ?
             AND {status_clause}
             AND o.followup_count = 0
             AND NOT EXISTS (
                 SELECT 1 FROM scheduler_jobs sj
                 WHERE sj.outreach_id = o.id
                   AND sj.job_type = ?
                   AND sj.status IN ('pending', 'running')
             )
             AND NOT {SDR_REAL_DM_SQL}
             AND NOT {SDR_RECENT_SDR_SQL}
             AND (o.status = 'connected' OR {FIT_SENDABLE_SQL}){exclusion_clause}
           ORDER BY c.fit_score DESC
           LIMIT 1""",
        (campaign_id, exclude_job_type or "send_dm", since, min_fit_score,
         *exclusion_params),
    ).fetchone()
    db.close()
    return dict(row) if row else None


def get_followup_candidates(
    campaign_id: str,
    max_followups: int = 2,
) -> list[dict]:
    """Find outreaches ready for follow-up.

    Returns outreaches with status 'connected' or 'messaged' and followup_count < max_followups,
    joined with contact data. Ordered by updated_at ASC (oldest first = most overdue).
    'messaged' is included because DM-only campaigns skip 'connected' status entirely.
    """
    db = get_db()
    rows = db.execute(
        f"""SELECT o.id as outreach_id, o.campaign_id, o.contact_id, o.status,
                  o.followup_count, o.updated_at as outreach_updated_at,
                  o.accepted_at, o.invited_at, o.created_at, o.chat_id,
                  {_LAST_OUTBOUND_SQL} AS last_sdr_message_at,
                  c.name, c.title, c.company, c.linkedin_url, c.linkedin_id,
                  c.profile_json, c.fit_score
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           WHERE o.campaign_id = ?
             AND o.status IN ('connected', 'messaged')
             AND o.followup_count < ?
           ORDER BY (
               EXISTS (
                   SELECT 1 FROM engagements e
                   WHERE e.outreach_id = o.id AND e.created_at > (strftime('%s', 'now') - 172800)
               )
           ) DESC, {_LAST_TOUCH_ORDER_SQL}""",
        (campaign_id, max_followups),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def count_followup_ready(campaign_id: str, max_followups: int = 2) -> int:
    """Count outreaches ready for follow-up in a campaign."""
    db = get_db()
    row = db.execute(
        """SELECT COUNT(*) as c FROM outreaches
           WHERE campaign_id = ? AND status IN ('connected', 'messaged') AND followup_count < ?""",
        (campaign_id, max_followups),
    ).fetchone()
    db.close()
    return row["c"] if row else 0


def get_followup_breakdown(campaign_id: str, max_followups: int = 2) -> dict:
    """Detailed breakdown of why follow-up candidates are/aren't eligible.

    Returns counts for: total connected/messaged, eligible (under max),
    maxed out, and per-followup_count distribution.
    """
    db = get_db()
    # Total connected + messaged
    row = db.execute(
        """SELECT COUNT(*) as c FROM outreaches
           WHERE campaign_id = ? AND status IN ('connected', 'messaged')""",
        (campaign_id,),
    ).fetchone()
    total = row["c"] if row else 0

    # Under max followups (eligible pool)
    row = db.execute(
        """SELECT COUNT(*) as c FROM outreaches
           WHERE campaign_id = ? AND status IN ('connected', 'messaged')
             AND followup_count < ?""",
        (campaign_id, max_followups),
    ).fetchone()
    eligible = row["c"] if row else 0

    # Maxed out
    maxed = total - eligible

    # Distribution by followup_count
    rows = db.execute(
        """SELECT followup_count, COUNT(*) as c FROM outreaches
           WHERE campaign_id = ? AND status IN ('connected', 'messaged')
           GROUP BY followup_count ORDER BY followup_count""",
        (campaign_id,),
    ).fetchall()
    distribution = {r["followup_count"]: r["c"] for r in rows}

    # By status
    rows = db.execute(
        """SELECT status, COUNT(*) as c FROM outreaches
           WHERE campaign_id = ? AND status IN ('connected', 'messaged')
           GROUP BY status""",
        (campaign_id,),
    ).fetchall()
    by_status = {r["status"]: r["c"] for r in rows}

    db.close()
    return {
        "total": total,
        "eligible": eligible,
        "maxed_out": maxed,
        "max_followups": max_followups,
        "distribution": distribution,
        "by_status": by_status,
    }


def get_reply_candidates(campaign_id: str | None = None) -> list[dict]:
    """Find outreaches needing a reply, prioritized by sentiment urgency.

    Args:
        campaign_id: Filter to a specific campaign. None or "" = inbound (no campaign).

    Returns outreaches where the last message is from the prospect.
    Ordered by sentiment priority: positive > question > neutral > negative.
    """
    db = get_db()
    if campaign_id:
        where = "o.campaign_id = ?"
        params: tuple = (campaign_id,)
    else:
        where = "(o.campaign_id = '' OR o.campaign_id IS NULL)"
        params = ()
    rows = db.execute(
        f"""SELECT o.id as outreach_id, o.campaign_id, o.contact_id,
                  o.status, o.followup_count, o.updated_at as outreach_updated_at,
                  c.name, c.title, c.company, c.linkedin_url, c.linkedin_id,
                  c.profile_json, c.fit_score,
                  c.id as contact_db_id,
                  m.text as last_reply_text,
                  m.sentiment as last_sentiment
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           JOIN messages m ON m.outreach_id = o.id
           WHERE {where}
             AND o.status IN ('hot_lead', 'replied', 'connected', 'messaged')
             AND m.role = 'prospect'
             AND m.id = (
                 SELECT m2.id FROM messages m2
                 WHERE m2.outreach_id = o.id
                 ORDER BY m2.timestamp DESC LIMIT 1
             )
             -- Cross-outreach dedup: when the same person is in multiple
             -- campaigns, skip this outreach if any sibling outreach has
             -- already replied (SDR message) after the prospect's latest
             -- message here. Without this, every campaign's auto-reply job
             -- independently fires into the same LinkedIn chat.
             AND NOT EXISTS (
                 SELECT 1
                 FROM outreaches o2
                 JOIN contacts c2 ON o2.contact_id = c2.id
                 JOIN messages m2 ON m2.outreach_id = o2.id
                 WHERE o2.id != o.id
                   AND m2.role = 'sdr'
                   AND m2.timestamp >= m.timestamp
                   AND (
                        (c.global_contact_id IS NOT NULL
                         AND c2.global_contact_id = c.global_contact_id)
                     OR (c.linkedin_id IS NOT NULL AND c.linkedin_id != ''
                         AND c2.linkedin_id = c.linkedin_id)
                   )
             )
           ORDER BY
             CASE m.sentiment
               WHEN 'positive' THEN 1
               WHEN 'engaged' THEN 2
               WHEN 'question' THEN 3
               WHEN 'neutral' THEN 4
               WHEN 'negative' THEN 5
               ELSE 6
             END,
             -- Longest-unanswered first: when THEY wrote, not when the row was
             -- last written to. o.updated_at is a row-mutation clock that every
             -- sync pull bumps, so it tie-broke this queue arbitrarily and the
             -- person waiting longest for an answer was not served first.
             m.timestamp ASC""",
        params,
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


# Backward-compatible alias for inbound callers
def get_inbound_reply_candidates() -> list[dict]:
    """Find inbound (campaign-less) outreaches needing a reply."""
    return get_reply_candidates(campaign_id=None)


def get_auto_reply_candidates(campaign_id: str | None = None, min_age_seconds: int = 300) -> list[dict]:
    """Find outreaches needing auto-reply, with minimum age since last prospect message.

    Args:
        campaign_id: Filter to a specific campaign. None or "" = inbound (no campaign).
        min_age_seconds: Minimum age of last prospect message (prevents instant replies).

    Also excludes opt_out/out_of_office/negative sentiments and outreaches with
    pending auto_reply jobs (ignores stale 'running' jobs older than 10 min).
    Meeting-intent replies (positive, calendar) are included — leaving a
    booking-link hot lead unanswered is a product bug, not a courtesy.
    Additionally excludes outreaches where an SDR message already exists after the
    last prospect message (prevents duplicate replies from concurrent jobs).
    """
    from ..constants import AUTO_REPLY_MAX_AGE_DAYS

    now = int(time.time())
    cutoff = now - min_age_seconds
    # Messages older than this are abandoned rather than answered late — an
    # outage must not leave a backlog that all fires at once on reconnect.
    max_age_cutoff = now - (AUTO_REPLY_MAX_AGE_DAYS * 86400)
    stale_cutoff = now - 600  # 10 min TTL for running jobs
    recently_completed = now - 1800  # 30 min cooldown for completed jobs
    db = get_db()
    if campaign_id:
        where = "o.campaign_id = ?"
        params: tuple = (campaign_id, cutoff, max_age_cutoff, stale_cutoff, recently_completed)
    else:
        where = "(o.campaign_id = '' OR o.campaign_id IS NULL)"
        params = (cutoff, max_age_cutoff, stale_cutoff, recently_completed)
    rows = db.execute(
        f"""SELECT o.id as outreach_id, o.campaign_id, o.contact_id,
                  o.status, o.followup_count, o.updated_at as outreach_updated_at,
                  c.name, c.title, c.company, c.linkedin_url, c.linkedin_id,
                  c.profile_json, c.fit_score,
                  c.id as contact_db_id,
                  m.text as last_reply_text,
                  m.sentiment as last_sentiment,
                  m.timestamp as last_message_ts
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           JOIN messages m ON m.outreach_id = o.id
           WHERE {where}
             -- Never auto-reply on a campaign that is no longer running.
             -- Campaign-less (inbound) outreaches have no campaign to check.
             AND (o.campaign_id = '' OR o.campaign_id IS NULL OR EXISTS (
                 SELECT 1 FROM campaigns ca
                 WHERE ca.id = o.campaign_id AND ca.status = 'active'
             ))
             AND o.status IN ('hot_lead', 'replied', 'connected', 'messaged')
             AND (
                 COALESCE(json_extract(
                     CASE WHEN json_valid(o.next_action) THEN o.next_action ELSE '{{}}' END,
                     '$.type'
                 ), '') != 'hold_for_operator'
                 OR CAST(COALESCE(json_extract(
                     CASE WHEN json_valid(o.next_action) THEN o.next_action ELSE '{{}}' END,
                     '$.message_ts'
                 ), 0) AS INTEGER) < m.timestamp
             )
             AND m.role = 'prospect'
             -- 'negative' stays excluded here on purpose: the client never
             -- answers a no; the cloud does (heylead-api #749).
             AND m.sentiment NOT IN ('opt_out', 'out_of_office', 'negative')
             AND m.id = (
                 SELECT m2.id FROM messages m2
                 WHERE m2.outreach_id = o.id
                 ORDER BY m2.timestamp DESC LIMIT 1
             )
             AND m.timestamp <= ?
             AND m.timestamp >= ?
             AND o.id NOT IN (
                 SELECT sj.outreach_id FROM scheduler_jobs sj
                 WHERE sj.outreach_id = o.id
                   AND sj.job_type = 'auto_reply'
                   AND (
                       sj.status = 'pending'
                       OR (sj.status = 'running' AND sj.started_at > ?)
                       OR (sj.status = 'completed' AND sj.completed_at > ?)
                   )
             )
             AND (
                 SELECT COUNT(*) FROM scheduler_jobs sj2
                 WHERE sj2.outreach_id = o.id
                   AND sj2.job_type = 'auto_reply'
                   AND sj2.status = 'failed'
             ) < 3
             AND (
                 SELECT COUNT(*) FROM actions_log al
                 WHERE al.outreach_id = o.id
                   AND al.action_type = 'chat_not_found'
             ) < 3
             AND NOT EXISTS (
                 SELECT 1 FROM messages mx
                 WHERE mx.outreach_id = o.id
                   AND mx.role = 'sdr'
                   AND mx.timestamp >= m.timestamp
             )
           ORDER BY
             CASE m.sentiment
               WHEN 'positive' THEN 1
               WHEN 'calendar' THEN 1
               WHEN 'engaged' THEN 2
               WHEN 'question' THEN 3
               WHEN 'neutral' THEN 4
               WHEN 'negative' THEN 5
               ELSE 6
             END,
             -- Longest-unanswered first: when THEY wrote, not when the row was
             -- last written to. o.updated_at is a row-mutation clock that every
             -- sync pull bumps, so it tie-broke this queue arbitrarily and the
             -- person waiting longest for an answer was not served first.
             m.timestamp ASC""",
        params,
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


# Backward-compatible alias for inbound callers
def get_inbound_auto_reply_candidates(min_age_seconds: int = 300) -> list[dict]:
    """Find inbound (campaign-less) outreaches needing auto-reply."""
    return get_auto_reply_candidates(campaign_id=None, min_age_seconds=min_age_seconds)


def _calendar_in_next_action(next_action: str) -> str:
    """Their own booking page, kept on the row's next_action, or ""."""
    if not next_action:
        return ""
    try:
        payload = json.loads(next_action)
    except (json.JSONDecodeError, TypeError):
        return ""
    return str(payload.get("prospect_calendar_url") or "") if isinstance(payload, dict) else ""


def _unanswered_lead_reason(
    text: str,
    sentiment: str,
    next_action: str,
    hours: int,
) -> tuple[str, str]:
    """Return (reason, calendar_url) for an unanswered lead."""
    calendar_url = _calendar_in_next_action(next_action)
    try:
        from ..ai.sentiment import detect_calendar_url
        found = detect_calendar_url(text or "") or ""
        if found:
            calendar_url = found
    except Exception:
        pass
    from ..formatter import format_wait_age
    from ..services.meeting_handoff import handoff_in, operator_action

    # A way to meet that only a person can finish is an ACTION, not a wait:
    # nobody owes them a message. Until 22 Sep 2026 a prospect who sent their
    # number or their own booking page read as "unanswered", and one waited
    # 45 days in a list of people expecting a reply.
    handoff = handoff_in(text or "")
    if not handoff and calendar_url:
        # Their page, found earlier and kept on the row: the same fact and the
        # same action, so it must not read differently for being stored.
        handoff = {"kind": "link", "value": calendar_url}
    if handoff:
        return operator_action(handoff), (calendar_url or handoff.get("value", ""))
    age = format_wait_age(hours)
    if sentiment in ("positive", "calendar"):
        return f"Meeting intent, unanswered {age}", calendar_url
    if sentiment == "engaged":
        return f"Engaged reply, unanswered {age}", calendar_url
    if sentiment == "negative":
        if '"hold_for_operator"' in (next_action or ""):
            return f"Declined, held for you {age}", calendar_url
        return f"Declined, closing reply unsent {age}", calendar_url
    return f"Unanswered reply, {age}", calendar_url


# The cloud's reply lane labels every message it writes ``move:<name>``
# (heylead-api reply_policy.MOVE_PREFIX), and the cloud pull brings the label
# down in ``sentiment``. This table holds no message_type, so the label is how
# a message of ours is told from a person's: without one, a person spoke.
# The backend keys the same rule on message_type (unanswered_leads.
# LANE_MESSAGE_TYPES), which it holds and this client does not.
_INBOUND_ROLES = ("prospect", "them", "inbound")
# Warm rows the lane spoke last on. Generous: the handoff test runs in Python,
# and a cap that cut a real handoff would repeat the defect.
_HANDOFF_CANDIDATES_MAX = 500


def pending_handoff(messages: list[dict]) -> dict | None:
    """The prospect message that handed over a way to meet and still waits on
    a person, or None. ``messages`` oldest first.

    Twin of heylead-api unanswered_leads.pending_handoff: the lane's own
    messages are passed over, and so are the prospect's later ones ("I was
    blaming LinkedIn" does not ring either); any other message of ours means a
    person has spoken, and the first prospect message carrying a number or
    their own booking page is the action. The one reading of a pending
    handoff: until 24 Sep 2026 Needs attention read the newest message instead
    and "Call <number>" became "Engaged reply" when he asked why nobody rang.
    """
    from ..constants import MOVE_PREFIX
    from ..services.meeting_handoff import handoff_in

    for message in reversed(messages):
        role = str(message.get("role") or "")
        if role in _INBOUND_ROLES:
            if handoff_in(str(message.get("text") or "")):
                return message
            continue
        if str(message.get("sentiment") or "").startswith(MOVE_PREFIX):
            continue
        return None
    return None


def _threads(db: Any, ids: list[str]) -> dict[str, list[dict]]:
    """Each outreach's messages, oldest first, in one read."""
    threads: dict[str, list[dict]] = {oid: [] for oid in ids}
    if not ids:
        return threads
    for raw in db.execute(
        "SELECT outreach_id, role, text, sentiment, timestamp FROM messages "
        f"WHERE outreach_id IN ({', '.join('?' for _ in ids)}) ORDER BY timestamp ASC",
        tuple(ids),
    ).fetchall():
        message = dict(raw)
        threads[str(message["outreach_id"])].append(message)
    return threads


def _as_the_action(candidate: dict, pending: dict) -> dict:
    """The row, read from the handoff that waits on a person: its text gives the
    reason and its time how long the person has been waited on."""
    return {
        **candidate,
        "last_reply_text": pending.get("text") or "",
        "last_sentiment": pending.get("sentiment") or "",
        "last_message_ts": int(pending.get("timestamp") or 0),
    }


def _acknowledged_handoffs(db: Any, narrowing: str = "", params: Sequence[Any] = ()) -> list[dict]:
    """Rows whose prospect handed over a way to meet and whose operator has
    not acted, although the lane has answered since.

    get_unanswered_leads' own query needs the prospect's message to be the
    last one, so until 23 Sep 2026 the lane's acknowledgement took "Call
    <number>" off Needs attention. What ends one here is a person acting: a
    message of their own, or closing the prospect. ``narrowing`` and
    ``params`` are narrowing_sql's, the main query's own.
    """
    rows = db.execute(
        f"""SELECT o.id as outreach_id, o.campaign_id, o.status, o.next_action,
                  c.name as contact_name, c.title, c.company, c.linkedin_url,
                  ca.name as campaign_name
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           JOIN messages m ON m.outreach_id = o.id
           LEFT JOIN campaigns ca ON ca.id = o.campaign_id
           WHERE m.id = (
                 SELECT m2.id FROM messages m2
                 WHERE m2.outreach_id = o.id
                 ORDER BY m2.timestamp DESC LIMIT 1
             )
             AND m.role = 'sdr'
             AND COALESCE(m.sentiment, '') LIKE 'move:%'
             AND o.status IN ('hot_lead', 'replied'){narrowing}
           ORDER BY m.timestamp ASC
           LIMIT ?""",
        (*params, _HANDOFF_CANDIDATES_MAX),
    ).fetchall()
    candidates = [dict(r) for r in rows]
    if not candidates:
        return []
    threads = _threads(db, [str(c["outreach_id"]) for c in candidates])
    out: list[dict] = []
    for candidate in candidates:
        pending = pending_handoff(threads.get(str(candidate["outreach_id"]), []))
        if pending is None:
            continue
        out.append(_as_the_action(candidate, pending))
    return out


# Campaign statuses whose replies are no longer anyone's action (twin of
# heylead-api unanswered_leads.STOPPED_CAMPAIGN_STATUSES). Paused is not here
# on purpose: a pause is "not now".
STOPPED_CAMPAIGN_STATUSES = frozenset({"archived", "deleted"})


# The kinds of wait these rows carry; services.waiting_on_you names them for
# every surface (twin of heylead-api unanswered_leads).
REPLY = "reply"
HANDOFF = "handoff"
HOLD = "hold"


def narrowing_sql(campaign_id: str, outreach_id: str, params: list[Any]) -> str:
    """AND-clauses narrowing a read to one campaign or one outreach (alias ``o``).

    A prefix matches too: inspect prints eight characters of an id and a
    person or the assistant passes those back.
    """
    parts: list[str] = []
    if campaign_id:
        parts.append("(o.campaign_id = ? OR o.campaign_id LIKE ?)")
        params.extend([campaign_id, f"{campaign_id}%"])
    if outreach_id:
        parts.append("(o.id = ? OR o.id LIKE ?)")
        params.extend([outreach_id, f"{outreach_id}%"])
    return "".join(f" AND {part}" for part in parts)


def waiting_candidates(
    *,
    now: int,
    min_age_seconds: int | None = None,
    campaign_id: str = "",
    outreach_id: str = "",
    held: list[dict] | None = None,
) -> list[dict]:
    """Every row a person is waiting on, oldest first (twin of heylead-api
    unanswered_leads.waiting_candidates).

    Each row carries ``kind`` (REPLY, HANDOFF or HOLD), ``reason`` (the line
    Needs attention prints) and ``prospect_calendar_url``. Three sources, one
    pass:

    * replies whose last message is theirs, past the grace window, that the
      lane is not about to answer (the query below);
    * ways to meet the lane has acknowledged and a person has not acted on
      (_acknowledged_handoffs);
    * ``held``: fresh operator holds, read by waiting_on_you.fresh_operator_holds.
      Until 25 Sep 2026 a hold on a reply this query did not admit (a polite
      "no" split over two messages, the second one neutral) reached
      inspect(action='holds') and never Needs attention.

    A booking-link hot lead with no SDR reply is the product-loud case:
    Overview and email must name them. Pending auto_reply jobs are excluded
    so a healthy delayed reply stays quiet. Every row, whatever its source,
    is asked pending_handoff first: a handoff is the action however it
    arrived. Callers go through waiting_on_you.who_is_waiting.
    """
    from ..constants import UNANSWERED_LEAD_GRACE_SECONDS
    from ..formatter import format_wait_age

    if min_age_seconds is None:
        min_age_seconds = UNANSWERED_LEAD_GRACE_SECONDS
    cutoff = now - min_age_seconds
    stale_running = now - 600
    narrow_params: list[Any] = []
    narrowing = narrowing_sql(campaign_id, outreach_id, narrow_params)
    db = get_db()
    rows = db.execute(
        f"""SELECT o.id as outreach_id, o.campaign_id, o.status, o.next_action,
                  c.name as contact_name, c.title, c.company, c.linkedin_url,
                  ca.name as campaign_name, ca.status as campaign_status,
                  m.text as last_reply_text,
                  m.sentiment as last_sentiment,
                  m.timestamp as last_message_ts
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           JOIN messages m ON m.outreach_id = o.id
           LEFT JOIN campaigns ca ON ca.id = o.campaign_id
           WHERE m.id = (
                 SELECT m2.id FROM messages m2
                 WHERE m2.outreach_id = o.id
                 ORDER BY m2.timestamp DESC LIMIT 1
             ){narrowing}
             AND m.role = 'prospect'
             AND m.timestamp <= ?
             AND m.sentiment NOT IN ('opt_out', 'out_of_office')
             AND o.status != 'opted_out'
             AND (
                 o.status = 'hot_lead'
                 -- The backend's list (heylead-api unanswered_leads.py). Since
                 -- 18 Sep 2026 a "no" stays 'replied' until the cloud's closing
                 -- reply is sent, so one still open past the grace window is a
                 -- close that could not be sent, or one held for the operator.
                 OR (o.status = 'replied'
                     AND m.sentiment IN ('positive', 'calendar', 'engaged', 'negative'))
             )
             AND NOT EXISTS (
                 SELECT 1 FROM messages mx
                 WHERE mx.outreach_id = o.id
                   AND mx.role = 'sdr'
                   AND mx.timestamp >= m.timestamp
             )
             AND o.id NOT IN (
                 SELECT sj.outreach_id FROM scheduler_jobs sj
                 WHERE sj.outreach_id = o.id
                   AND sj.job_type = 'auto_reply'
                   AND (
                       sj.status = 'pending'
                       OR (sj.status = 'running' AND sj.started_at > ?)
                   )
             )
           ORDER BY m.timestamp ASC
           LIMIT 50""",
        (*narrow_params, cutoff, stale_running),
    ).fetchall()
    # Replies nobody has answered, held replies, and ways to meet the lane
    # acknowledged and a person has still to act on, oldest first. A reply
    # and a hold on one outreach are one row: the hold says why only a
    # person can answer it.
    candidates = [dict(raw) for raw in rows]
    held_by_id = {str(h["outreach_id"]): h for h in (held or [])}
    seen = {str(c["outreach_id"]) for c in candidates}
    held_only = {oid for oid in held_by_id if oid not in seen}
    candidates += [held_by_id[oid] for oid in held_by_id if oid in held_only]
    # A reply that follows a handoff nobody has acted on is still that
    # handoff's: the action is the call, not an answer to "any news?".
    threads = _threads(db, [str(c["outreach_id"]) for c in candidates])
    live: list[dict] = []
    for candidate in candidates:
        oid = str(candidate["outreach_id"])
        pending = pending_handoff(threads.get(oid, []))
        if pending is not None:
            live.append({**_as_the_action(candidate, pending), "kind": HANDOFF})
            continue
        # Twin of heylead-api unanswered_leads (api #1260): an archived
        # campaign sends nothing, so its "closing reply unsent" can never be
        # sent and an engaged reply from a campaign someone stopped months
        # ago is not an action anyone takes. A handoff (above) is a person's
        # call and stays whatever the campaign's status; paused stays too.
        if str(candidate.get("campaign_status") or "") in STOPPED_CAMPAIGN_STATUSES:
            continue
        if _calendar_in_next_action(str(candidate.get("next_action") or "")):
            kind = HANDOFF
        elif oid in held_by_id:
            kind = HOLD
        else:
            kind = REPLY
        live.append({**candidate, "kind": kind})
    live += [
        {**row, "kind": HANDOFF}
        for row in _acknowledged_handoffs(db, narrowing, narrow_params)
    ]
    db.close()
    live.sort(key=lambda r: int(r.get("last_message_ts") or 0))

    for row in live:
        hours = max(0, (now - int(row.get("last_message_ts") or 0)) // 3600)
        sentiment = str(row.get("last_sentiment") or "")
        if row["kind"] == HOLD and str(row["outreach_id"]) in held_only and sentiment != "negative":
            # Held before Needs attention's own query would admit it: say
            # what it is, not a sentiment the lane did not act on.
            row["reason"] = f"Held for you, unanswered {format_wait_age(hours)}"
            row["prospect_calendar_url"] = ""
            continue
        row["reason"], row["prospect_calendar_url"] = _unanswered_lead_reason(
            str(row.get("last_reply_text") or ""), sentiment,
            str(row.get("next_action") or ""), hours,
        )
    return live


def get_unanswered_leads(min_age_seconds: int | None = None) -> list[dict]:
    """Needs attention: the people waiting on the user, as the list's rows.

    A view of services.waiting_on_you.who_is_waiting (25 Sep 2026), so
    show_status, the unanswered-lead alert, the daily digest,
    inspect(action='waiting'), inspect(action='holds') and check_replies
    name the same people. It holds no query of its own
    (tests/test_one_reader_says_who_is_waiting.py).
    """
    from ..services.waiting_on_you import needs_attention

    return needs_attention(min_age_seconds=min_age_seconds)


def was_unanswered_lead_alerted(outreach_id: str, since: int) -> bool:
    """True if we already emailed about this outreach after ``since``."""
    db = get_db()
    row = db.execute(
        """SELECT 1 FROM actions_log
           WHERE outreach_id = ?
             AND action_type = 'unanswered_lead_alerted'
             AND result = 'success'
             AND timestamp >= ?
           LIMIT 1""",
        (outreach_id, since),
    ).fetchone()
    db.close()
    return row is not None


def get_daily_auto_reply_count() -> int:
    """Count auto-replies sent today (via actions_log)."""
    today_start = _local_day_start()
    db = get_db()
    row = db.execute(
        """SELECT COUNT(*) as cnt FROM actions_log
           WHERE action_type = 'auto_reply_sent'
             AND timestamp >= ?""",
        (today_start,),
    ).fetchone()
    db.close()
    return row["cnt"] if row else 0


# ──────────────────────────────────────────────
# Messages
# ──────────────────────────────────────────────

def save_message(
    outreach_id: str,
    role: str,
    text: str,
    sentiment: str = "",
    format: str = "text",
    timestamp: int | None = None,
    external_message_id: str | None = None,
    message_id: str | None = None,
    provenance: Provenance | None = None,
) -> str:
    """Persist a message.

    An outbound row records where its words came from (heylead-api#1210):
    ``provenance`` when the caller knows (the cloud pull mirrors the hosted
    row's), else the draft the task's last message template wrote.

    ``timestamp`` is when the message was actually sent/received on the
    provider, NOT when we ingested it — pass it whenever the provider reports
    one, or a backfilled year-old reply will look like it just arrived.

    ``message_id`` is the row's identity when another store already holds
    it — the hosted scheduler's id for a message the cloud pull is
    mirroring. Minting a fresh id for such a row made the next push present
    it to the backend as a new message, and the backend stored the copy
    (8 Sep 2026: 264 echo rows, four "dm sent" for one note and one DM).
    """
    db = get_db()
    if message_id:
        held = db.execute(
            "SELECT id FROM messages WHERE id = ? LIMIT 1", (message_id,)
        ).fetchone()
        if held:
            db.close()
            return message_id
    # Dedup so concurrent check_replies runs don't double-store. The provider's
    # message id is the only identity that survives a prospect repeating
    # themselves — "ok" in March and "ok" in August are two replies, and
    # collapsing them hides the second one from the whole reply pipeline. Only
    # when the provider gave us no id do we fall back to matching on text.
    if external_message_id:
        existing = db.execute(
            "SELECT id FROM messages WHERE outreach_id = ? AND external_message_id = ? LIMIT 1",
            (outreach_id, external_message_id),
        ).fetchone()
    else:
        existing = db.execute(
            "SELECT id FROM messages WHERE outreach_id = ? AND role = ? AND text = ? LIMIT 1",
            (outreach_id, role, text),
        ).fetchone()
    if existing:
        db.close()
        return existing[0] if isinstance(existing, (tuple, list)) else existing["id"]
    msg_id = message_id or str(uuid.uuid4())
    insert_message_row(
        db, id=msg_id, outreach_id=outreach_id, role=role, text=text,
        sentiment=sentiment, format=format,
        timestamp=timestamp if timestamp is not None else int(time.time()),
        external_message_id=external_message_id,
        provenance=copy_provenance.for_outbound(text, provenance) if role == "sdr" else None,
    )
    db.commit()
    db.close()
    return msg_id


def get_messages_for_outreach(outreach_id: str) -> list[dict]:
    db = get_db()
    rows = db.execute(
        "SELECT * FROM messages WHERE outreach_id = ? ORDER BY timestamp ASC",
        (outreach_id,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def mark_message_read(message_id: str, read_at: int | None = None) -> None:
    """Mark a message as read by the prospect."""
    db = get_db()
    db.execute(
        "UPDATE messages SET read_at = ? WHERE id = ? AND read_at IS NULL",
        (read_at or int(time.time()), message_id),
    )
    db.commit()
    db.close()


def mark_message_deleted(message_id: str, deleted_at: int | None = None) -> None:
    """Mark a local message as deleted on LinkedIn."""
    db = get_db()
    db.execute(
        "UPDATE messages SET deleted_at = ? WHERE id = ?",
        (deleted_at or int(time.time()), message_id),
    )
    db.commit()
    db.close()


def record_outreach_tombstones(
    db: sqlite3.Connection, outreach_ids: list[str], campaign_id: str = "",
) -> None:
    """Remember ids about to be hard-deleted. Runs on the caller's connection
    so it commits or rolls back with the delete itself."""
    now = int(time.time())
    db.executemany(
        "INSERT OR IGNORE INTO outreach_tombstones (outreach_id, campaign_id, deleted_at) "
        "VALUES (?, ?, ?)",
        [(oid, campaign_id or None, now) for oid in outreach_ids if oid],
    )


def hard_delete_outreach_ids(outreach_ids: list[str]) -> int:
    """Delete outreach rows and their children. Does not write or clear tombstones."""
    victims = [oid for oid in outreach_ids if oid]
    if not victims:
        return 0
    db = get_db()
    child_tables = [
        "messages",
        "actions_log",
        "engagements",
        "prospect_daily_plans",
        "scheduler_jobs",
    ]
    tables = {
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "calendar_events" in tables:
        child_tables.append("calendar_events")
    deleted = 0
    try:
        for batch in _id_batches(victims):
            marks = ",".join("?" * len(batch))
            for table in child_tables:
                db.execute(
                    f"DELETE FROM {table} WHERE outreach_id IN ({marks})", batch,
                )
            cur = db.execute(
                f"DELETE FROM outreaches WHERE id IN ({marks})", batch,
            )
            deleted += cur.rowcount or 0
    except sqlite3.IntegrityError:
        db.rollback()
        leftovers = _outreach_child_counts(db, victims)
        logger.warning(
            "hard-delete FOREIGN KEY leftover children: %s",
            leftovers or "none named",
        )
        db.close()
        raise
    db.commit()
    db.close()
    return int(deleted)


def list_outreach_tombstone_ids() -> list[str]:
    """Every id the hosted store has not yet been told about, oldest first."""
    db = get_db()
    rows = db.execute(
        "SELECT outreach_id FROM outreach_tombstones ORDER BY deleted_at, outreach_id"
    ).fetchall()
    db.close()
    return [r[0] for r in rows]


def clear_outreach_tombstones(outreach_ids: list[str]) -> int:
    """Forget ids a successful push carried. Returns how many were cleared."""
    if not outreach_ids:
        return 0
    db = get_db()
    total = 0
    for i in range(0, len(outreach_ids), 500):
        batch = outreach_ids[i:i + 500]
        marks = ",".join("?" * len(batch))
        cur = db.execute(
            f"DELETE FROM outreach_tombstones WHERE outreach_id IN ({marks})", batch,
        )
        total += cur.rowcount or 0
    db.commit()
    db.close()
    return total


def set_message_external_id(message_id: str, external_message_id: str) -> None:
    """Store the Unipile message ID on a local message record."""
    db = get_db()
    db.execute(
        "UPDATE messages SET external_message_id = ? WHERE id = ?",
        (external_message_id, message_id),
    )
    db.commit()
    db.close()


def get_last_sdr_message(outreach_id: str) -> dict | None:
    """Get the most recent SDR message for an outreach (not deleted)."""
    db = get_db()
    row = db.execute(
        """SELECT * FROM messages
           WHERE outreach_id = ? AND role = 'sdr' AND deleted_at IS NULL
           ORDER BY timestamp DESC LIMIT 1""",
        (outreach_id,),
    ).fetchone()
    db.close()
    return dict(row) if row else None


def get_read_rate(campaign_id: str) -> dict:
    """Calculate read rate for SDR messages in a campaign.

    Returns: {"total_sdr_messages": int, "read_messages": int, "read_rate": float}
    """
    db = get_db()
    row = db.execute(
        """SELECT
               COUNT(*) as total,
               SUM(CASE WHEN m.read_at IS NOT NULL THEN 1 ELSE 0 END) as read_count
           FROM messages m
           JOIN outreaches o ON m.outreach_id = o.id
           WHERE o.campaign_id = ? AND m.role = 'sdr'""",
        (campaign_id,),
    ).fetchone()
    db.close()
    total = row["total"] if row else 0
    read_count = row["read_count"] if row else 0
    return {
        "total_sdr_messages": total,
        "read_messages": read_count,
        "read_rate": read_count / total if total > 0 else 0.0,
    }


# ──────────────────────────────────────────────
# Actions Log
# ──────────────────────────────────────────────

def log_action(
    action_type: str,
    outreach_id: str = "",
    result: str = "",
    details: Any = None,
    campaign_id: str = "",
    timestamp: int | None = None,
) -> str:
    action_id = str(uuid.uuid4())
    db = get_db()
    ts = int(timestamp) if timestamp is not None else int(time.time())
    d_json = json.dumps(details) if details else None
    oid = outreach_id or None
    cid = campaign_id or None
    try:
        db.execute(
            """INSERT INTO actions_log (id, outreach_id, action_type, result, details_json, timestamp, campaign_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (action_id, oid, action_type, result, d_json, ts, cid),
        )
        db.commit()
    except sqlite3.IntegrityError:
        # FK constraint violation (e.g. cloud sync from deleted entity) — fallback safely with NULLs
        try:
            db.execute(
                """INSERT INTO actions_log (id, outreach_id, action_type, result, details_json, timestamp, campaign_id)
                   VALUES (?, NULL, ?, ?, ?, ?, NULL)""",
                (action_id, action_type, result, d_json, ts),
            )
            db.commit()
        except Exception:
            pass
    finally:
        db.close()
    return action_id


def get_campaign_status_history(campaign_id: str = "", limit: int = 50) -> list[dict]:
    """Get campaign status change audit log.

    Returns list of dicts with: timestamp, campaign_id, campaign_name,
    old_status, new_status, changed_by, reason.
    """
    db = get_db()
    if campaign_id:
        rows = db.execute(
            """SELECT timestamp, details_json FROM actions_log
               WHERE action_type = 'campaign_status_change'
                 AND details_json LIKE ?
               ORDER BY timestamp DESC LIMIT ?""",
            (f'%"campaign_id": "{campaign_id}"%', limit),
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT timestamp, details_json FROM actions_log
               WHERE action_type = 'campaign_status_change'
               ORDER BY timestamp DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    db.close()
    result = []
    for row in rows:
        details = json.loads(row[1] or "{}")
        details["timestamp"] = row[0]
        result.append(details)
    return result


def get_actions_taken(hours: int = 24, campaign_id: str = "") -> dict[str, dict]:
    """Time-windowed action results from actions_log (excludes skip_ entries).

    Returns: {action_type: {"total": N, "success": N, "error": N, "blocked": N, ...}}
    """
    db = get_db()
    since = int(time.time()) - (hours * 3600)
    sql = """SELECT action_type, result, COUNT(*) as cnt
             FROM actions_log
             WHERE timestamp >= ? AND action_type NOT LIKE 'skip_%'"""
    params: list[Any] = [since]
    if campaign_id:
        sql += " AND campaign_id = ?"
        params.append(campaign_id)
    sql += " GROUP BY action_type, result"
    rows = db.execute(sql, params).fetchall()
    db.close()

    actions: dict[str, dict] = {}
    for r in rows:
        at = r["action_type"]
        res = r["result"] or "unknown"
        cnt = r["cnt"]
        if at not in actions:
            actions[at] = {"total": 0}
        actions[at]["total"] += cnt
        actions[at][res] = actions[at].get(res, 0) + cnt
    return actions


def get_last_action_details(action_type: str) -> dict[str, Any] | None:
    """The details of the most recent actions_log row of *action_type*."""
    db = get_db()
    row = db.execute(
        """SELECT details_json FROM actions_log
           WHERE action_type = ?
           ORDER BY timestamp DESC, rowid DESC LIMIT 1""",
        (action_type,),
    ).fetchone()
    db.close()
    if row is None:
        return None
    try:
        details = json.loads(row["details_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return details if isinstance(details, dict) else {}


def list_action_details(action_type: str) -> list[dict[str, Any]]:
    """Every details blob for *action_type*, oldest first.

    Brand-post photos need the full history, not only the last row: a photo
    used three posts ago must stay spent.
    """
    db = get_db()
    rows = db.execute(
        """SELECT details_json FROM actions_log
           WHERE action_type = ?
           ORDER BY timestamp ASC, rowid ASC""",
        (action_type,),
    ).fetchall()
    db.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            details = json.loads(row["details_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(details, dict):
            out.append(details)
    return out


def get_actions_skipped(hours: int = 24, campaign_id: str = "") -> dict[str, int]:
    """Time-windowed skip reasons from actions_log (skip_* entries).

    Returns: {reason: count} e.g. {"daily_limit": 23, "no_candidates": 12}
    """
    db = get_db()
    since = int(time.time()) - (hours * 3600)
    sql = """SELECT json_extract(details_json, '$.reason') as reason, COUNT(*) as cnt
             FROM actions_log
             WHERE timestamp >= ? AND action_type LIKE 'skip_%'"""
    params: list[Any] = [since]
    if campaign_id:
        sql += " AND campaign_id = ?"
        params.append(campaign_id)
    sql += " GROUP BY reason ORDER BY cnt DESC"
    rows = db.execute(sql, params).fetchall()
    db.close()

    return {r["reason"] or "unknown": r["cnt"] for r in rows}


def get_actions_skipped_detailed(hours: int = 24, campaign_id: str = "") -> dict[str, dict[str, int]]:
    """Time-windowed skip reasons grouped by action type.

    Returns: {reason: {action_type_suffix: count}} e.g. {"daily_limit": {"follow": 5, "engage": 3}}
    """
    db = get_db()
    since = int(time.time()) - (hours * 3600)
    sql = """SELECT action_type,
                    json_extract(details_json, '$.reason') as reason,
                    COUNT(*) as cnt
             FROM actions_log
             WHERE timestamp >= ? AND action_type LIKE 'skip_%'"""
    params: list[Any] = [since]
    if campaign_id:
        sql += " AND campaign_id = ?"
        params.append(campaign_id)
    sql += " GROUP BY action_type, reason ORDER BY cnt DESC"
    rows = db.execute(sql, params).fetchall()
    db.close()

    result: dict[str, dict[str, int]] = {}
    for r in rows:
        reason = r["reason"] or "unknown"
        # Extract action suffix: "skip_follow" → "follow"
        action = (r["action_type"] or "").removeprefix("skip_")
        if reason not in result:
            result[reason] = {}
        result[reason][action] = r["cnt"]
    return result


def get_outreach_changes(hours: int = 24, campaign_id: str = "") -> dict[str, Any]:
    """Time-windowed real state changes from outreaches/engagements/messages.

    Returns dict with counts of actual LinkedIn results in the time window.
    """
    db = get_db()
    since = int(time.time()) - (hours * 3600)

    campaign_filter = ""
    params_base: list[Any] = [since]
    if campaign_id:
        campaign_filter = " AND campaign_id = ?"
        params_base = [since, campaign_id]

    # Invitations sent (invited_at in window, verified only)
    row = db.execute(
        f"SELECT COUNT(*) as cnt FROM outreaches WHERE invited_at >= ? AND verified_status = 'confirmed'{campaign_filter}",
        params_base,
    ).fetchone()
    invited = row["cnt"] if row else 0

    # Invitations pending verification
    row = db.execute(
        f"SELECT COUNT(*) as cnt FROM outreaches WHERE invited_at >= ? AND (verified_status IS NULL OR verified_status = ''){campaign_filter}",
        params_base,
    ).fetchone()
    invited_pending = row["cnt"] if row else 0

    # Acceptances (accepted_at in window)
    row = db.execute(
        f"SELECT COUNT(*) as cnt FROM outreaches WHERE accepted_at >= ?{campaign_filter}",
        params_base,
    ).fetchone()
    accepted = row["cnt"] if row else 0

    # Replies (first_reply_at in window)
    row = db.execute(
        f"SELECT COUNT(*) as cnt FROM outreaches WHERE first_reply_at >= ?{campaign_filter}",
        params_base,
    ).fetchone()
    replied = row["cnt"] if row else 0

    # Messages sent (role='sdr' in window)
    if campaign_id:
        msg_sql = """SELECT role, COUNT(*) as cnt FROM messages
                     WHERE timestamp >= ? AND outreach_id IN
                       (SELECT id FROM outreaches WHERE campaign_id = ?)
                     GROUP BY role"""
        msg_params: list[Any] = [since, campaign_id]
    else:
        msg_sql = "SELECT role, COUNT(*) as cnt FROM messages WHERE timestamp >= ? GROUP BY role"
        msg_params = [since]
    msg_rows = db.execute(msg_sql, msg_params).fetchall()
    messages_sent = 0
    messages_received = 0
    for r in msg_rows:
        if r["role"] == "sdr":
            messages_sent = r["cnt"]
        elif r["role"] == "prospect":
            messages_received = r["cnt"]

    # Engagements by type in window (verified only)
    if campaign_id:
        eng_sql = """SELECT action_type, COUNT(*) as cnt FROM engagements
                     WHERE created_at >= ? AND verified_status IN ('verified', 'trust_api')
                       AND outreach_id IN
                       (SELECT id FROM outreaches WHERE campaign_id = ?)
                     GROUP BY action_type"""
        eng_params: list[Any] = [since, campaign_id]
    else:
        eng_sql = """SELECT action_type, COUNT(*) as cnt FROM engagements
                     WHERE created_at >= ? AND verified_status IN ('verified', 'trust_api')
                     GROUP BY action_type"""
        eng_params = [since]
    eng_rows = db.execute(eng_sql, eng_params).fetchall()
    engagements = {r["action_type"]: r["cnt"] for r in eng_rows}

    # Engagements pending verification
    if campaign_id:
        eng_pend_sql = """SELECT action_type, COUNT(*) as cnt FROM engagements
                     WHERE created_at >= ? AND (verified_status IS NULL OR verified_status = '')
                       AND outreach_id IN
                       (SELECT id FROM outreaches WHERE campaign_id = ?)
                     GROUP BY action_type"""
        eng_pend_params: list[Any] = [since, campaign_id]
    else:
        eng_pend_sql = """SELECT action_type, COUNT(*) as cnt FROM engagements
                     WHERE created_at >= ? AND (verified_status IS NULL OR verified_status = '')
                     GROUP BY action_type"""
        eng_pend_params = [since]
    eng_pend_rows = db.execute(eng_pend_sql, eng_pend_params).fetchall()
    engagements_pending = {r["action_type"]: r["cnt"] for r in eng_pend_rows}

    db.close()

    return {
        "invited": invited,
        "invited_pending": invited_pending,
        "accepted": accepted,
        "replied": replied,
        "messages_sent": messages_sent,
        "messages_received": messages_received,
        "engagements": engagements,
        "engagements_pending": engagements_pending,
    }


def get_verification_summary(hours: int = 24) -> dict[str, int]:
    """Summary of post-action verification results.

    Returns: {"confirmed": N, "unconfirmed": N, "pending": N}
    """
    db = get_db()
    since = int(time.time()) - (hours * 3600)

    # Verified outreaches in window
    rows = db.execute(
        """SELECT verified_status, COUNT(*) as cnt FROM outreaches
           WHERE verified_at >= ? AND verified_at IS NOT NULL
           GROUP BY verified_status""",
        (since,),
    ).fetchall()
    result = {r["verified_status"]: r["cnt"] for r in rows if r["verified_status"]}

    # Pending verification (invited recently, not yet verified)
    row = db.execute(
        """SELECT COUNT(*) as cnt FROM outreaches
           WHERE invited_at >= ? AND verified_at IS NULL AND status = 'invited'""",
        (since,),
    ).fetchone()
    result["pending"] = row["cnt"] if row else 0

    db.close()
    return result


# ──────────────────────────────────────────────
# Rate Limits
# ──────────────────────────────────────────────

def get_rate_limit_today() -> dict:
    """Get or create today's rate limit record."""
    from datetime import date, timedelta

    from .. import constants as _c

    today = date.today().isoformat()
    db = get_db()
    row = db.execute("SELECT * FROM rate_limits WHERE date = ?", (today,)).fetchone()
    if row is None:
        rid = str(uuid.uuid4())
        # Carry forward yesterday's adaptive limit
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        prev = db.execute(
            "SELECT daily_limit FROM rate_limits WHERE date = ?", (yesterday,)
        ).fetchone()
        # Coerce rather than copy: a 0 written by a pre-v0.10.316 sync would
        # otherwise seed today's row, and tomorrow's from today's, so the bad
        # value outlived the writer fix that stopped producing it.
        from ..services.health_score import coerce_daily_limit

        initial = coerce_daily_limit(prev["daily_limit"] if prev else None)
        db.execute(
            """INSERT OR IGNORE INTO rate_limits (id, date, sent, accepted, daily_limit, blocked, updated_at)
               VALUES (?, ?, 0, 0, ?, 0, ?)""",
            (rid, today, initial, int(time.time())),
        )
        db.commit()
        row = db.execute("SELECT * FROM rate_limits WHERE date = ?", (today,)).fetchone()
    db.close()
    return dict(row)


def increment_sent() -> None:
    from datetime import date
    today = date.today().isoformat()
    db = get_db()
    db.execute(
        "UPDATE rate_limits SET sent = sent + 1, updated_at = ? WHERE date = ?",
        (int(time.time()), today),
    )
    db.commit()
    db.close()


def increment_accepted() -> None:
    from datetime import date
    today = date.today().isoformat()
    db = get_db()
    db.execute(
        "UPDATE rate_limits SET accepted = accepted + 1, updated_at = ? WHERE date = ?",
        (int(time.time()), today),
    )
    db.commit()
    db.close()


def update_daily_limit(new_limit: int) -> None:
    from datetime import date
    today = date.today().isoformat()
    db = get_db()
    db.execute(
        "UPDATE rate_limits SET daily_limit = ?, updated_at = ? WHERE date = ?",
        (new_limit, int(time.time()), today),
    )
    db.commit()
    db.close()


def get_rate_limit_budget() -> dict[str, dict]:
    """Get today's budget remaining per action type.

    Counts completed scheduler_jobs today for warm-up actions, and uses
    rate_limits table for invitations.

    Returns: {action_type: {used_today, limit, remaining, pct_used}}
    """
    from datetime import date

    from .. import constants as _c

    today_start = int(
        __import__("datetime").datetime.combine(
            date.today(), __import__("datetime").time.min
        ).timestamp()
    )

    # Invitation budget from rate_limits table
    rl = get_rate_limit_today()
    inv_sent = rl.get("sent", 0)
    # A hosted account's ceiling is the backend's, not this row's default 15.
    from ..linkedin.rate_limiter import invite_limits_for_display_sync

    _, inv_limit = invite_limits_for_display_sync(rl)

    # Warm-up budgets from scheduler_jobs completed today
    db = get_db()
    rows = db.execute(
        """SELECT job_type, COUNT(*) as cnt
           FROM scheduler_jobs
           WHERE status = 'completed'
             AND completed_at >= ?
           GROUP BY job_type""",
        (today_start,),
    ).fetchall()
    db.close()

    counts: dict[str, int] = {r["job_type"]: r["cnt"] for r in rows}

    limits = {
        "invitations": (15, inv_sent, inv_limit),
        "follows": (None, counts.get("follow", 0), None),
        "engagements": (None, counts.get("engage", 0), None),
        "profile_views": (None, counts.get("profile_view_warmup", 0), None),
        "endorsements": (None, counts.get("endorse", 0), None),
        "followups": (None, counts.get("followup", 0), None),
        "dms": (None, counts.get("send_dm", 0), None),
    }

    budget: dict[str, dict] = {}
    for action, (_default_limit, used, limit) in limits.items():
        if action == "invitations":
            # Use adaptive limit from rate_limits table
            remaining = max(0, inv_limit - inv_sent)
            pct = round(inv_sent / inv_limit * 100, 1) if inv_limit else 0
        elif limit is not None:
            remaining = max(0, limit - used)
            pct = round(used / limit * 100, 1) if limit else 0
        else:
            remaining = None
            pct = None
        budget[action] = {
            "used_today": used,
            "limit": limit,
            "remaining": remaining,
            "pct_used": pct,
        }

    return budget


def get_warmup_effectiveness(campaign_id: str = "") -> dict:
    """Compare acceptance/reply rates for warmed-up vs direct-invite prospects.

    A prospect is "warmed up" if they have engagements created before
    their invitation was sent.

    Returns dict with warmed_up, direct_invite groups and lift metrics.
    """
    db = get_db()
    # Get all outreaches that were invited
    sql = """
        SELECT
            o.id as outreach_id,
            o.invited_at,
            o.accepted_at,
            o.first_reply_at,
            (SELECT COUNT(*) FROM engagements e
             WHERE e.outreach_id = o.id
               AND e.created_at < o.invited_at) as warmup_count
        FROM outreaches o
        WHERE o.invited_at IS NOT NULL
    """
    params: list[Any] = []
    if campaign_id:
        sql += " AND o.campaign_id = ?"
        params.append(campaign_id)

    rows = db.execute(sql, params).fetchall()
    db.close()

    groups: dict[str, dict] = {
        "warmed_up": {"total": 0, "accepted": 0, "replied": 0, "days_sum": 0, "warmup_actions_sum": 0},
        "direct_invite": {"total": 0, "accepted": 0, "replied": 0, "days_sum": 0},
    }

    for r in rows:
        warmup_count = r["warmup_count"] or 0
        group = "warmed_up" if warmup_count > 0 else "direct_invite"
        g = groups[group]
        g["total"] += 1
        if r["accepted_at"]:
            g["accepted"] += 1
            days = (r["accepted_at"] - r["invited_at"]) / 86400
            g["days_sum"] += days
        if r["first_reply_at"]:
            g["replied"] += 1
        if group == "warmed_up":
            g["warmup_actions_sum"] += warmup_count

    result: dict[str, Any] = {}
    for group_name, g in groups.items():
        total = g["total"]
        accepted = g["accepted"]
        replied = g["replied"]
        result[group_name] = {
            "total": total,
            "accepted": accepted,
            "acceptance_rate": round(accepted / total * 100, 1) if total else 0,
            "replied": replied,
            "reply_rate": round(replied / total * 100, 1) if total else 0,
            "avg_days_to_accept": round(g["days_sum"] / accepted, 1) if accepted else None,
        }
        if group_name == "warmed_up":
            result[group_name]["warmup_actions_avg"] = (
                round(g["warmup_actions_sum"] / total, 1) if total else 0
            )

    # Compute lift
    wu = result.get("warmed_up", {})
    di = result.get("direct_invite", {})
    result["lift"] = {
        "acceptance_rate_lift_pp": round(
            wu.get("acceptance_rate", 0) - di.get("acceptance_rate", 0), 1
        ),
        "reply_rate_lift_pp": round(
            wu.get("reply_rate", 0) - di.get("reply_rate", 0), 1
        ),
    }

    return result


def sync_rate_limits_from_outreaches() -> None:
    """Reconcile rate_limits.sent with actual outreach counts.

    Cloud scheduler sends invitations that don't increment the local
    rate_limits.sent counter. This syncs the counter from the outreaches
    table (source of truth) to prevent desync.
    """
    from datetime import date, datetime

    today = date.today().isoformat()
    start_of_day = int(datetime.combine(date.today(), datetime.min.time()).timestamp())

    db = get_db()
    # Count actual invitations sent today from outreaches table
    row = db.execute(
        """SELECT COUNT(*) as cnt FROM outreaches
           WHERE status = 'invited' AND invited_at >= ?""",
        (start_of_day,),
    ).fetchone()
    actual_sent = row["cnt"] if row else 0

    # Update rate_limits if actual count is higher (cloud sent some)
    current = db.execute(
        "SELECT sent FROM rate_limits WHERE date = ?", (today,)
    ).fetchone()
    if current and actual_sent > current["sent"]:
        db.execute(
            "UPDATE rate_limits SET sent = ?, updated_at = ? WHERE date = ?",
            (actual_sent, int(time.time()), today),
        )
        db.commit()
        logger.info(
            "Rate limit sync: sent %d → %d (cloud scheduler catchup)",
            current["sent"], actual_sent,
        )
    db.close()


def get_weekly_invitation_sum() -> int:
    """Sum invitations sent over the last 7 days from rate_limits table."""
    from datetime import date, timedelta
    today = date.today()
    week_ago = (today - timedelta(days=6)).isoformat()
    db = get_db()
    row = db.execute(
        "SELECT COALESCE(SUM(sent), 0) as total FROM rate_limits WHERE date >= ?",
        (week_ago,),
    ).fetchone()
    db.close()
    return row["total"] if row else 0


def get_sending_days_7d() -> int:
    """Count days with at least 1 invitation sent in the last 7 days."""
    from datetime import date, timedelta
    today = date.today()
    week_ago = (today - timedelta(days=6)).isoformat()
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) as days FROM rate_limits WHERE date >= ? AND sent > 0",
        (week_ago,),
    ).fetchone()
    db.close()
    return row["days"] if row else 0


# ──────────────────────────────────────────────
# Email Rate Limits (overflow channel)
# ──────────────────────────────────────────────


def get_email_rate_limit_today() -> dict:
    """Get or create today's email rate limit record."""
    from datetime import date
    import uuid as _uuid

    today = date.today().isoformat()
    db = get_db()
    row = db.execute(
        "SELECT * FROM email_rate_limits WHERE date = ?", (today,)
    ).fetchone()
    if row is None:
        rid = str(_uuid.uuid4())
        db.execute(
            "INSERT OR IGNORE INTO email_rate_limits (id, date, sent, updated_at) VALUES (?, ?, 0, ?)",
            (rid, today, int(time.time())),
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM email_rate_limits WHERE date = ?", (today,)
        ).fetchone()
    db.close()
    return dict(row) if row else {"sent": 0}


def increment_email_sent() -> None:
    """Increment today's email send counter."""
    # Ensure row exists first
    get_email_rate_limit_today()
    from datetime import date

    today = date.today().isoformat()
    db = get_db()
    db.execute(
        "UPDATE email_rate_limits SET sent = sent + 1, updated_at = ? WHERE date = ?",
        (int(time.time()), today),
    )
    db.commit()
    db.close()


def get_weekly_email_sum() -> int:
    """Sum emails sent over the last 7 days."""
    from datetime import date, timedelta

    today = date.today()
    week_ago = (today - timedelta(days=6)).isoformat()
    db = get_db()
    row = db.execute(
        "SELECT COALESCE(SUM(sent), 0) as total FROM email_rate_limits WHERE date >= ?",
        (week_ago,),
    ).fetchone()
    db.close()
    return row["total"] if row else 0


def get_email_eligible_pending_outreaches(campaign_id: str, limit: int = 5) -> list:
    """Get pending outreaches that have email addresses and haven't been contacted.

    Returns prospects who:
    - Have status = 'pending' (not yet contacted on ANY channel)
    - Have an email address in profile_json or linked global_contacts
    - Have NOT been contacted in ANY campaign (cross-campaign anti-double-contact)

    Ordered by fit_score DESC.
    """
    db = get_db()
    rows = db.execute(
        """SELECT o.id as outreach_id, o.contact_id, o.status,
                  c.name, c.title, c.company, c.linkedin_url, c.linkedin_id,
                  c.profile_json, c.fit_score
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           WHERE o.campaign_id = ?
             AND o.status = 'pending'
             AND c.profile_json LIKE '%"email"%'
             AND NOT EXISTS (
                 SELECT 1 FROM outreaches o2
                 WHERE o2.contact_id = o.contact_id
                   AND o2.id != o.id
                   AND o2.status IN ('invited', 'connected', 'messaged', 'replied')
             )
           ORDER BY c.fit_score DESC
           LIMIT ?""",
        (campaign_id, limit),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


# json_extract raises 'malformed JSON' on '' and the raise takes down the whole
# statement rather than skipping the row, so every read of contacts.profile_json
# goes through this instead of touching the column directly. The guard lives
# inside json_extract's argument, not beside it as another WHERE term: two of
# the four reads below sit in an ORDER BY and a correlated subquery where there
# is no conjunct to hide behind, and SQLite may reorder the ones that have one.
# '{}' is the substitute rather than NULL only because it keeps every reader on
# the same expression; both are safe for json_extract.
_SAFE_CONTACT_PROFILE = (
    "CASE WHEN json_valid(c.profile_json) THEN c.profile_json ELSE '{}' END"
)


def get_inmail_fallback_candidates(
    campaign_id: str,
    min_age_days: int,
    limit: int = 5,
    open_profile_only: bool = False,
) -> list:
    """Invited outreaches that stayed quiet long enough to earn an InMail.

    A successful send leaves status='invited' behind (send_inmail keeps the
    invite lifecycle so withdrawal at day 21 still applies), so status alone
    would re-select the same prospect every tick at one metered credit per
    lap — the NOT EXISTS on the actions_log success marker is the loop trap,
    resolved here in SQL rather than in the planner.

    The connections NOT EXISTS is a planner-side pre-filter only; the tool's
    is_first_degree check stays authoritative at send time. provider_id must
    be present: InMail needs the ACo… id, not a public slug.

    open_profile_only restricts to Open Profile members (zero-credit sends —
    the free tier's whole InMail surface). Open-profile candidates rank first
    on both tiers because their sends cost nothing.

    Bounds that keep this from misfiring:
    - Upper age bound: withdrawal never touches the outreach row, so months of
      long-withdrawn invites still sit at status='invited'. Without it, the
      first tick after enabling the feature turns that whole history into a
      metered InMail queue.
    - Pending/running job exclusion: jobs are scheduled 30-55 min out, so a
      campaign-level count alone lets consecutive ticks double-queue the same
      prospect (the exclude_job_type idiom every sibling query uses).
    - 24h attempt window: any recent attempt — success or failure — parks the
      candidate for a day, so a prospect whose copy cannot pass validation
      cannot hold candidates[0] and starve the rest of the campaign.
    """
    now = int(time.time())
    cutoff = now - min_age_days * 86400
    oldest = now - INMAIL_FALLBACK_MAX_AGE_DAYS * 86400
    open_profile_clause = (
        f" AND json_extract({_SAFE_CONTACT_PROFILE}, '$.is_open_profile') = 1"
        if open_profile_only
        else ""
    )
    db = get_db()
    rows = db.execute(
        f"""SELECT o.id as outreach_id, o.contact_id, o.status, o.invited_at,
                  c.name, c.title, c.company, c.linkedin_url, c.linkedin_id,
                  c.profile_json, c.fit_score
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           WHERE o.campaign_id = ?
             AND o.status = 'invited'
             AND o.invited_at IS NOT NULL
             AND o.invited_at < ?
             AND o.invited_at >= ?
             AND COALESCE(json_extract({_SAFE_CONTACT_PROFILE}, '$.provider_id'), '') != ''
             AND o.id NOT IN (
                 SELECT sj.outreach_id FROM scheduler_jobs sj
                 WHERE sj.job_type = 'inmail'
                   AND sj.status IN ('pending', 'running')
                   AND sj.outreach_id IS NOT NULL
             )
             AND NOT EXISTS (
                 SELECT 1 FROM actions_log al
                 WHERE al.outreach_id = o.id
                   AND al.action_type = 'inmail_sent'
                   AND (al.result = 'success' OR al.timestamp > ?)
             )
             {_INMAIL_PERMANENT_FAIL_SQL}
             AND NOT EXISTS (
                 SELECT 1 FROM connections cn
                 WHERE cn.provider_id = json_extract({_SAFE_CONTACT_PROFILE}, '$.provider_id')
                    OR (c.linkedin_id IS NOT NULL AND c.linkedin_id != ''
                        AND LOWER(cn.public_id) = LOWER(c.linkedin_id))
             ){open_profile_clause}
           ORDER BY COALESCE(json_extract({_SAFE_CONTACT_PROFILE}, '$.is_open_profile'), 0) DESC,
                    c.fit_score DESC
           LIMIT ?""",
        (campaign_id, cutoff, oldest, now - 86400, limit),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_inmail_first_touch_candidates(
    campaign_id: str,
    limit: int = 5,
    open_profile_only: bool = False,
    min_fit_score: float = 0.0,
) -> list:
    """Pending non-connections eligible for InMail as the first touch.

    No invite and no 14-day wait. provider_id is required. 1st-degree
    connections are excluded — they get a DM. A successful InMail is
    excluded forever. A failed first-touch parks InMail for 24h so a
    422 is not retried the same night — that person becomes
    invite-eligible immediately (see get_next_invite_candidate).
    """
    now = int(time.time())
    open_profile_clause = (
        f" AND json_extract({_SAFE_CONTACT_PROFILE}, '$.is_open_profile') = 1"
        if open_profile_only
        else ""
    )
    db = get_db()
    rows = db.execute(
        f"""SELECT o.id as outreach_id, o.contact_id, o.status,
                  c.name, c.title, c.company, c.linkedin_url, c.linkedin_id,
                  c.profile_json, c.fit_score
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           WHERE o.campaign_id = ?
             AND o.status = 'pending'
             AND {FIT_SENDABLE_SQL}
             AND COALESCE(json_extract({_SAFE_CONTACT_PROFILE}, '$.provider_id'), '') != ''
             AND o.id NOT IN (
                 SELECT sj.outreach_id FROM scheduler_jobs sj
                 WHERE sj.job_type = 'inmail'
                   AND sj.status IN ('pending', 'running')
                   AND sj.outreach_id IS NOT NULL
             )
             AND NOT EXISTS (
                 SELECT 1 FROM actions_log al
                 WHERE al.outreach_id = o.id
                   AND al.action_type = 'inmail_sent'
                   AND al.result = 'success'
             )
             AND NOT EXISTS (
                 SELECT 1 FROM actions_log al
                 WHERE al.outreach_id = o.id
                   AND al.action_type = 'inmail_sent'
                   AND al.result = 'failed'
                   AND al.timestamp > ?
             )
             {_INMAIL_PERMANENT_FAIL_SQL}
             AND NOT EXISTS (
                 SELECT 1 FROM connections cn
                 WHERE cn.provider_id = json_extract({_SAFE_CONTACT_PROFILE}, '$.provider_id')
                    OR (c.linkedin_id IS NOT NULL AND c.linkedin_id != ''
                        AND LOWER(cn.public_id) = LOWER(c.linkedin_id))
             ){open_profile_clause}
           ORDER BY COALESCE(json_extract({_SAFE_CONTACT_PROFILE}, '$.is_open_profile'), 0) DESC,
                    c.fit_score DESC
           LIMIT ?""",
        (campaign_id, min_fit_score, now - _INMAIL_FIRST_TOUCH_FAIL_COOLDOWN, limit),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def count_first_touch_pool(campaign_id: str, kind: str) -> int:
    """Pre-filter pool size for invite / DM / InMail empty vs none_eligible.

    Invite and InMail: pending outreaches. DM: pending or connected with
    no follow-up yet. Does not apply fit, Open Profile, or job filters.
    """
    db = get_db()
    if kind == "dm":
        row = db.execute(
            """SELECT COUNT(*) AS n FROM outreaches
               WHERE campaign_id = ?
                 AND status IN ('pending', 'connected')
                 AND COALESCE(followup_count, 0) = 0""",
            (campaign_id,),
        ).fetchone()
    else:
        row = db.execute(
            """SELECT COUNT(*) AS n FROM outreaches
               WHERE campaign_id = ? AND status = 'pending'""",
            (campaign_id,),
        ).fetchone()
    db.close()
    return int(row["n"] if row else 0)


# A pre-existing 1st-degree connection, from SQL. 9 Sep 2026.
# `removed_at IS NULL` because the prune is a soft delete now — a tombstoned
# row is not a connection today. `invited_at IS NULL` because someone who
# accepted our own invitation is 1st-degree *because of this campaign* and
# still needs the opener. A NULL connected_at reads as pre-existing: those are
# rows written before this feature, and the promise is "never message an
# existing connection".
_PREEXISTING_CONNECTION_SQL = f"""(
    o.invited_at IS NULL
    AND EXISTS (
        SELECT 1 FROM connections cn
        WHERE cn.removed_at IS NULL
          AND (cn.connected_at IS NULL OR cn.connected_at < ?)
          AND (
              cn.provider_id = json_extract({_SAFE_CONTACT_PROFILE}, '$.provider_id')
              OR (c.linkedin_id IS NOT NULL AND c.linkedin_id != ''
                  AND LOWER(cn.public_id) = LOWER(c.linkedin_id))
          )
    )
)"""


def get_next_invite_candidate(
    campaign_id: str,
    exclude_job_type: str = "",
    *,
    exclude_first_degree: bool = False,
    defer_inmail_first: bool = False,
    can_send_credit_inmail: bool = False,
    min_fit_score: float = 0.0,
) -> Optional[dict]:
    """Next pending outreach that should get an invitation, not InMail or a DM.

    1st-degree pending people get a DM. Open Profile first-touch people are
    skipped when *defer_inmail_first* — credit entitlement no longer parks
    every provider_id for an InMail that will 422.
    """
    db = get_db()
    extras = """
             AND NOT EXISTS (
                 SELECT 1 FROM actions_log al
                 WHERE al.outreach_id = o.id
                   AND al.action_type = 'inmail_sent'
                   AND al.result = 'success'
             )"""
    if exclude_first_degree:
        extras += f"""
             AND NOT EXISTS (
                 SELECT 1 FROM connections cn
                 WHERE cn.removed_at IS NULL
                   AND (cn.provider_id = json_extract({_SAFE_CONTACT_PROFILE}, '$.provider_id')
                        OR (c.linkedin_id IS NOT NULL AND c.linkedin_id != ''
                            AND LOWER(cn.public_id) = LOWER(c.linkedin_id)))
             )"""
    if defer_inmail_first:
        # Only Open Profile first-touch is actually sent locally. Treating
        # every provider_id as InMail-first (the credit-InMail branch) left
        # 263 people uninvited while two unknown-open 422s filled the queue.
        # COALESCE: a missing is_open_profile is unknown, not true.
        # `json_extract(...) = 1` is NULL when the key is absent, and
        # `NOT NULL` is NULL, so the WHERE dropped every directory card
        # that never stored the flag — the live idle after v0.10.266.
        inmail_clause = (
            f"COALESCE(json_extract({_SAFE_CONTACT_PROFILE}, '$.is_open_profile'), 0) = 1 "
            f"AND COALESCE(json_extract({_SAFE_CONTACT_PROFILE}, '$.provider_id'), '') != ''"
        )
        # A failed first-touch InMail must not lock the invite as well.
        extras += f""" AND (
             NOT ({inmail_clause})
             OR (
                 EXISTS (
                     SELECT 1 FROM actions_log al
                     WHERE al.outreach_id = o.id
                       AND (
                           (al.action_type = 'inmail_sent'
                            AND al.result IN ('failed', 'skipped'))
                           OR al.action_type = 'inmail_unreachable'
                       )
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM actions_log al
                     WHERE al.outreach_id = o.id
                       AND al.action_type = 'inmail_sent'
                       AND al.result = 'success'
                 )
             )
         )"""
    job_exclude = ""
    params: list[Any] = [campaign_id]
    if exclude_job_type:
        job_exclude = """
             AND o.id NOT IN (
                 SELECT sj.outreach_id FROM scheduler_jobs sj
                 WHERE sj.outreach_id IS NOT NULL
                   AND sj.job_type = ?
                   AND sj.status IN ('pending', 'running')
             )"""
        params.append(exclude_job_type)
    row = db.execute(
        f"""SELECT o.id, o.contact_id, c.fit_score
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           WHERE o.campaign_id = ? AND o.status = 'pending'
             {job_exclude}
             {extras}
             AND {FIT_SENDABLE_SQL}
           ORDER BY c.fit_score DESC
           LIMIT 1""",
        params + [min_fit_score],
    ).fetchone()
    db.close()
    return dict(row) if row else None


def count_sendable_queue(campaign_id: str, min_fit_score: float = 0.0) -> int:
    """How many pending people the send gate would still accept.

    Stock depth, not "can we send right now": deliberately without the
    InMail/first-degree/job-in-flight predicates get_next_invite_candidate
    carries, because a person parked behind today's InMail is still stock.
    Refill uses this to decide whether a campaign needs topping up.
    """
    db = get_db()
    row = db.execute(
        f"""SELECT COUNT(*) AS n
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           WHERE o.campaign_id = ? AND o.status = 'pending'
             AND {FIT_SENDABLE_SQL}""",
        (campaign_id, min_fit_score),
    ).fetchone()
    db.close()
    return int(row["n"]) if row else 0


def delete_never_contacted_below_threshold(
    campaign_id: str, min_fit_score: float,
) -> int:
    """Drop outreach rows we will never send. Contact rows stay.

    Only never-invited, never-messaged rows below the campaign send gate.
    High-fit pending (Diego) and anyone already touched are left alone.

    Child rows are deleted first. SQLite has no ON DELETE CASCADE here, so a
    pending scheduler_job or daily plan used to abort the whole repair with
    FOREIGN KEY constraint failed (24 times on 25 Aug 2026).
    """
    victim_sql = """
        SELECT id FROM outreaches
        WHERE campaign_id = ?
          AND status IN ('skipped', 'pending')
          AND COALESCE(invited_at, 0) = 0
          AND id NOT IN (
              SELECT outreach_id FROM messages
              WHERE outreach_id IS NOT NULL AND role = 'sdr'
          )
          AND contact_id IN (
              SELECT id FROM contacts
              WHERE campaign_id = ? AND COALESCE(fit_score, 0) < ?
          )
    """
    params = (campaign_id, campaign_id, min_fit_score)
    db = get_db()
    # Tombstones first, on this connection: they commit with the delete below
    # and roll back with it. The 15-minute push sends them as
    # deleted_outreach_ids; without them a locally deleted row lived on in
    # the hosted store and on every dashboard count (577 rows, Aug 2026).
    victims = [r[0] for r in db.execute(victim_sql, params).fetchall()]
    if not victims:
        db.close()
        return 0
    record_outreach_tombstones(db, victims, campaign_id)
    # Every DELETE below is driven by the materialised ids, never by re-running
    # victim_sql: the connection is shared across threads and the SELECT above
    # opened no transaction, so a fit rescore or an invited_at stamp landing in
    # between would make the tombstoned set and the deleted set differ, and a
    # tombstone for a still-live row would later hard-delete a live prospect
    # from the hosted store.
    child_tables = [
        "messages",
        "actions_log",
        "engagements",
        "prospect_daily_plans",
        "scheduler_jobs",
    ]
    tables = {
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "calendar_events" in tables:
        child_tables.append("calendar_events")
    deleted = 0
    try:
        for batch in _id_batches(victims):
            marks = ",".join("?" * len(batch))
            for table in child_tables:
                db.execute(
                    f"DELETE FROM {table} WHERE outreach_id IN ({marks})", batch,
                )
            cur = db.execute(
                f"DELETE FROM outreaches WHERE id IN ({marks})", batch,
            )
            deleted += cur.rowcount or 0
    except sqlite3.IntegrityError:
        db.rollback()
        leftovers = _outreach_child_counts(db, victims)
        logger.warning(
            "sendable-queue repair FOREIGN KEY leftover children: %s",
            leftovers or "none named",
        )
        db.close()
        raise
    db.commit()
    db.close()
    return int(deleted)


def _id_batches(ids: list[str], size: int = 500):
    """Yield ``ids`` in slices small enough for one ``IN (?, ...)`` clause."""
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


def _outreach_child_counts(db, victims: list[str]) -> str:
    """Name tables still holding victim outreach_id rows after child deletes."""
    hits: dict[str, int] = {}
    for (name,) in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'",
    ).fetchall():
        if name == "outreaches":
            continue
        cols = [c[1] for c in db.execute(f"PRAGMA table_info({name})").fetchall()]
        if "outreach_id" not in cols:
            continue
        for batch in _id_batches(victims):
            marks = ",".join("?" * len(batch))
            n = db.execute(
                f"SELECT COUNT(*) FROM {name} WHERE outreach_id IN ({marks})",
                batch,
            ).fetchone()[0]
            if n:
                hits[name] = hits.get(name, 0) + n
    return ", ".join(f"{name}={n}" for name, n in hits.items())


def cancel_stale_first_touch_jobs(
    *,
    lost_credit_inmail: bool = False,
    gained_credit_inmail: bool = False,
    refused: bool = False,
) -> int:
    """Cancel pending jobs that no longer match the first-touch picker."""
    db = get_db()
    now = int(time.time())
    cancelled = 0
    if lost_credit_inmail or refused:
        cur = db.execute(
            """UPDATE scheduler_jobs SET status = 'cancelled', completed_at = ?
               WHERE job_type = 'inmail' AND status = 'pending'
                 AND outreach_id IN (
                     SELECT o.id FROM outreaches o
                     JOIN contacts c ON o.contact_id = c.id
                     WHERE COALESCE(json_extract(
                         CASE WHEN json_valid(c.profile_json) THEN c.profile_json ELSE '{}' END,
                         '$.is_open_profile'
                     ), 0) != 1
                 )""",
            (now,),
        )
        cancelled += int(cur.rowcount or 0)
    if gained_credit_inmail:
        cur = db.execute(
            f"""UPDATE scheduler_jobs SET status = 'cancelled', completed_at = ?
               WHERE job_type = 'invite' AND status = 'pending'
                 AND outreach_id IN (
                     SELECT o.id FROM outreaches o
                     JOIN contacts c ON o.contact_id = c.id
                     WHERE COALESCE(json_extract(
                         {_SAFE_CONTACT_PROFILE}, '$.provider_id'
                     ), '') != ''
                       AND COALESCE(json_extract(
                         {_SAFE_CONTACT_PROFILE}, '$.is_open_profile'
                     ), 0) != 1
                       AND NOT EXISTS (
                           SELECT 1 FROM connections cn
                           WHERE cn.removed_at IS NULL
                             AND cn.provider_id = json_extract(
                               {_SAFE_CONTACT_PROFILE}, '$.provider_id'
                           )
                              OR (c.linkedin_id IS NOT NULL AND c.linkedin_id != ''
                                  AND LOWER(cn.public_id) = LOWER(c.linkedin_id))
                       )
                 )""",
            (now,),
        )
        cancelled += int(cur.rowcount or 0)
    db.commit()
    db.close()
    return cancelled


def get_daily_signal_outreach_count() -> int:
    """Count signal-triggered outreaches created today (non-null signal_id)."""
    today_start = _local_day_start()
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) as cnt FROM outreaches WHERE signal_id IS NOT NULL AND created_at >= ?",
        (today_start,),
    ).fetchone()
    db.close()
    return row["cnt"] if row else 0


# ──────────────────────────────────────────────
# Usage Tracking (Free Tier)
# ──────────────────────────────────────────────

def get_monthly_usage() -> dict:
    """Get or create this month's usage record."""
    from datetime import date
    month = date.today().strftime("%Y-%m")
    db = get_db()
    row = db.execute("SELECT * FROM usage WHERE month = ?", (month,)).fetchone()
    if row is None:
        db.execute(
            "INSERT OR IGNORE INTO usage (month, updated_at) VALUES (?, ?)",
            (month, int(time.time())),
        )
        db.commit()
        row = db.execute("SELECT * FROM usage WHERE month = ?", (month,)).fetchone()
    db.close()
    return dict(row)


_VALID_USAGE_FIELDS = frozenset({
    "invitations_sent",
    "messages_sent",
    "campaigns_created",
    "icps_generated",
    "engagements_sent",
})


def increment_usage(field: str) -> None:
    """Increment a usage counter (invitations_sent, messages_sent, etc.)."""
    if field not in _VALID_USAGE_FIELDS:
        raise ValueError(f"Invalid usage field: {field}. Must be one of {_VALID_USAGE_FIELDS}")
    from datetime import date
    month = date.today().strftime("%Y-%m")
    db = get_db()
    # Ensure row exists
    get_monthly_usage()
    db.execute(
        f"UPDATE usage SET {field} = {field} + 1, updated_at = ? WHERE month = ?",
        (int(time.time()), month),
    )
    db.commit()
    db.close()


def set_monthly_usage(usage: dict) -> None:
    """Overwrite this month's usage with authoritative backend data."""
    from datetime import date
    month = date.today().strftime("%Y-%m")
    # Ensure row exists
    get_monthly_usage()
    db = get_db()
    updates = []
    values: list[Any] = []
    for field in ("invitations_sent", "messages_sent", "engagements_sent"):
        if field in usage:
            updates.append(f"{field} = ?")
            values.append(usage[field])
    if updates:
        values.extend([int(time.time()), month])
        db.execute(
            f"UPDATE usage SET {', '.join(updates)}, updated_at = ? WHERE month = ?",
            tuple(values),
        )
        db.commit()
    db.close()


# ──────────────────────────────────────────────
# Campaign Stats (for show_status)
# ──────────────────────────────────────────────

def get_campaign_stats(campaign_id: str) -> dict:
    """Calculate aggregate stats for a campaign.

    Rates:
    - acceptance_rate: connected / mature_invited (invitations > 7 days old, excl opted_out)
    - raw_acceptance_rate: connected / all_invited (includes pending, for transparency)
    - reply_rate: replied / connected
    """
    import time as _time
    db = get_db()

    seven_days_ago = int(_time.time()) - (7 * 86400)
    row = db.execute(
        """SELECT
               SUM(CASE WHEN status != 'skipped' THEN 1 ELSE 0 END) as total,
               SUM(CASE WHEN status NOT IN ('pending', 'skipped') THEN 1 ELSE 0 END) as invited,
               SUM(CASE WHEN status NOT IN ('pending', 'skipped', 'opted_out') THEN 1 ELSE 0 END) as invited_excl_optout,
               SUM(CASE WHEN status IN ('connected', 'messaged', 'replied', 'hot_lead', 'closed_happy', 'closed_unhappy', 'reverse_pitch', 'opted_out') THEN 1 ELSE 0 END) as connected,
               SUM(CASE WHEN status IN ('replied', 'hot_lead', 'closed_happy', 'closed_unhappy', 'reverse_pitch', 'opted_out') THEN 1 ELSE 0 END) as replied,
               SUM(CASE WHEN status = 'hot_lead' THEN 1 ELSE 0 END) as hot_leads,
               SUM(CASE WHEN status = 'invited' THEN 1 ELSE 0 END) as pending_invitations,
               SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) as skipped,
               SUM(CASE WHEN status = 'closed_happy' THEN 1 ELSE 0 END) as closed_happy,
               SUM(CASE WHEN status = 'closed_unhappy' THEN 1 ELSE 0 END) as closed_unhappy,
               SUM(CASE WHEN status = 'opted_out' THEN 1 ELSE 0 END) as opted_out,
               SUM(CASE WHEN status NOT IN ('pending', 'skipped')
                    AND COALESCE(invited_at, updated_at) < ? THEN 1 ELSE 0 END) as mature_invited,
               SUM(CASE WHEN status = 'pending' AND COALESCE(invite_attempts, 0) > 0 THEN 1 ELSE 0 END) as invite_failed
           FROM outreaches
           WHERE campaign_id = ?""",
        (seven_days_ago, campaign_id),
    ).fetchone()

    db.close()

    r = dict(row) if row else {}
    total = r.get("total", 0) or 0
    invited = r.get("invited", 0) or 0
    invited_excl_optout = r.get("invited_excl_optout", 0) or 0
    connected = r.get("connected", 0) or 0
    replied = r.get("replied", 0) or 0
    mature_invited = r.get("mature_invited", 0) or 0

    # Primary rate: only count invitations that have had time to respond (7d+).
    # Falls back to all invitations if no mature invitations yet.
    # Cap at 1.0 — connected can exceed invited when inbound connections are included.
    if mature_invited > 0:
        acceptance_rate = min(1.0, connected / mature_invited)
    elif invited_excl_optout > 0:
        acceptance_rate = min(1.0, connected / invited_excl_optout)
    else:
        acceptance_rate = 0.0

    raw_acceptance_rate = min(1.0, connected / invited) if invited > 0 else 0.0
    reply_rate = replied / connected if connected > 0 else 0.0

    return {
        "total_prospects": total,
        "invited": invited,
        "connected": connected,
        "replied": replied,
        "hot_leads": r.get("hot_leads", 0) or 0,
        "pending_invitations": r.get("pending_invitations", 0) or 0,
        "skipped": r.get("skipped", 0) or 0,
        "closed_happy": r.get("closed_happy", 0) or 0,
        "closed_unhappy": r.get("closed_unhappy", 0) or 0,
        "opted_out": r.get("opted_out", 0) or 0,
        "invite_failed": r.get("invite_failed", 0) or 0,
        "acceptance_rate": acceptance_rate,
        "raw_acceptance_rate": raw_acceptance_rate,
        "reply_rate": reply_rate,
    }


def get_campaign_outcomes(campaign_id: str) -> dict:
    """Get outcome breakdown for a campaign.

    Returns dict with closed_happy, closed_unhappy, opted_out counts,
    total_closed, conversion_rate, and individual outcome details.
    """
    db = get_db()

    closed_happy = db.execute(
        "SELECT COUNT(*) as c FROM outreaches WHERE campaign_id = ? AND status = 'closed_happy'",
        (campaign_id,),
    ).fetchone()["c"]

    closed_unhappy = db.execute(
        "SELECT COUNT(*) as c FROM outreaches WHERE campaign_id = ? AND status = 'closed_unhappy'",
        (campaign_id,),
    ).fetchone()["c"]

    opted_out = db.execute(
        "SELECT COUNT(*) as c FROM outreaches WHERE campaign_id = ? AND status = 'opted_out'",
        (campaign_id,),
    ).fetchone()["c"]

    # Individual outcome details with contact info
    rows = db.execute(
        """SELECT o.id as outreach_id, o.status, o.outcome_json, o.updated_at,
                  c.name, c.title, c.company, c.fit_score, c.linkedin_url
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           WHERE o.campaign_id = ?
             AND o.status IN ('closed_happy', 'closed_unhappy', 'opted_out')
           ORDER BY o.updated_at DESC""",
        (campaign_id,),
    ).fetchall()
    db.close()

    outcomes = []
    for row in rows:
        r = dict(row)
        outcome_data = {}
        if r.get("outcome_json"):
            try:
                outcome_data = json.loads(r["outcome_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        outcomes.append({
            "outreach_id": r["outreach_id"],
            "status": r["status"],
            "name": r.get("name", "Unknown"),
            "title": r.get("title", ""),
            "company": r.get("company", ""),
            "linkedin_url": r.get("linkedin_url") or "",
            "fit_score": r.get("fit_score", 0),
            "reason": outcome_data.get("reason", ""),
            "meeting_link": outcome_data.get("meeting_link") or outcome_data.get("booking_link", ""),
            "closed_at": outcome_data.get("closed_at", r.get("updated_at", 0)),
        })

    total_closed = closed_happy + closed_unhappy
    conversion_rate = closed_happy / total_closed if total_closed > 0 else 0.0

    return {
        "closed_happy": closed_happy,
        "closed_unhappy": closed_unhappy,
        "opted_out": opted_out,
        "total_closed": total_closed,
        "conversion_rate": conversion_rate,
        "outcomes": outcomes,
    }


# The last time anything really happened to an outreach: the newest message in
# the thread, or failing that the furthest the funnel got.
#
# NOT ``updated_at``. That is a row-mutation clock — it moves whenever any
# column changes, and ``cloud_sync._cloud_outreach_changes`` says so in its own
# docstring ("the cost is that outreaches.updated_at stops recording when the
# row changed"), naming ``_is_followup_due`` and ``show_status`` as the
# consumers it knew about. The stale list was a third one, so a prospect nobody
# had answered in six weeks vanished from it the moment a sync touched the row.
#
# COALESCE down to ``created_at``, which is NOT NULL, so every row keeps a
# clock. A single mostly-NULL column would drop rows silently: ``NULL <
# cutoff`` is NULL, never true, and the filter would look fixed while staying
# dead (the hosted side lost 18 days to exactly that).
_LAST_ACTIVITY_SQL = """COALESCE(
        (SELECT MAX(m.timestamp) FROM messages m
          WHERE m.outreach_id = o.id AND m.deleted_at IS NULL),
        o.first_reply_at, o.accepted_at, o.invited_at, o.created_at
    )"""


def get_stale_outreaches(campaign_id: str, stale_days: int = 14) -> list[dict]:
    """Find outreaches nobody has spoken to for N days.

    Returns outreaches in active states (connected, messaged, hot_lead) whose
    last real activity — the newest message in the thread, else the furthest
    the funnel got — is older than stale_days ago, joined with contact info.
    Ordered by who has been waiting longest.
    """
    import time as _time
    cutoff = int(_time.time()) - (stale_days * 86400)
    now = int(_time.time())

    db = get_db()
    rows = db.execute(
        f"""SELECT o.id as outreach_id, o.status,
                  {_LAST_ACTIVITY_SQL} AS last_activity_at,
                  c.name, c.title, c.company, c.fit_score, c.source, c.linkedin_url
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           WHERE o.campaign_id = ?
             AND o.status IN ('connected', 'messaged', 'hot_lead')
             AND {_LAST_ACTIVITY_SQL} < ?
           ORDER BY last_activity_at ASC""",
        (campaign_id, cutoff),
    ).fetchall()
    db.close()

    results = []
    for row in rows:
        r = dict(row)
        days_stale = (now - (r.get("last_activity_at") or 0)) // 86400
        results.append({
            "outreach_id": r["outreach_id"],
            "status": r["status"],
            "name": r.get("name", "Unknown"),
            "title": r.get("title", ""),
            "company": r.get("company", ""),
            "linkedin_url": r.get("linkedin_url") or "",
            "fit_score": r.get("fit_score", 0),
            "days_stale": days_stale,
            # The last real activity, not the row's mtime. Kept under the old
            # key as well so any reader that has not been updated still gets a
            # timestamp rather than a KeyError — but it is the activity time.
            "last_activity_at": r.get("last_activity_at", 0),
            "updated_at": r.get("last_activity_at", 0),
        })

    return results


def get_campaign_velocity(campaign_id: str) -> dict:
    """Calculate time-based velocity metrics for a campaign.

    Returns avg/min/max time-to-accept, time-to-reply, and per-deal timelines.
    All time values are in seconds. NULL columns are excluded from aggregates.
    """
    db = get_db()

    # Avg/min/max time-to-accept (invited_at → accepted_at)
    accept_row = db.execute(
        """SELECT AVG(accepted_at - invited_at) as avg_tta,
                  MIN(accepted_at - invited_at) as min_tta,
                  MAX(accepted_at - invited_at) as max_tta,
                  COUNT(*) as cnt
           FROM outreaches
           WHERE campaign_id = ?
             AND invited_at IS NOT NULL
             AND accepted_at IS NOT NULL
             AND status IN ('connected', 'messaged', 'replied', 'hot_lead', 'closed_happy', 'closed_unhappy', 'reverse_pitch', 'opted_out')""",
        (campaign_id,),
    ).fetchone()

    # Avg time-to-reply (accepted_at → first_reply_at)
    reply_row = db.execute(
        """SELECT AVG(first_reply_at - accepted_at) as avg_ttr,
                  MIN(first_reply_at - accepted_at) as min_ttr,
                  MAX(first_reply_at - accepted_at) as max_ttr,
                  COUNT(*) as cnt
           FROM outreaches
           WHERE campaign_id = ?
             AND accepted_at IS NOT NULL
             AND first_reply_at IS NOT NULL
             AND status IN ('replied', 'hot_lead', 'closed_happy', 'closed_unhappy', 'reverse_pitch', 'opted_out')""",
        (campaign_id,),
    ).fetchone()

    # Avg invite-to-reply (full funnel)
    full_row = db.execute(
        """SELECT AVG(first_reply_at - invited_at) as avg_full,
                  COUNT(*) as cnt
           FROM outreaches
           WHERE campaign_id = ?
             AND invited_at IS NOT NULL
             AND first_reply_at IS NOT NULL
             AND status IN ('replied', 'hot_lead', 'closed_happy', 'closed_unhappy', 'reverse_pitch', 'opted_out')""",
        (campaign_id,),
    ).fetchone()

    # Per-deal timelines for hot leads and closed deals
    deal_rows = db.execute(
        """SELECT o.invited_at, o.accepted_at, o.first_reply_at,
                  o.status, c.name, c.title, c.company
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           WHERE o.campaign_id = ?
             AND o.status IN ('closed_happy', 'closed_unhappy', 'hot_lead')
             AND o.invited_at IS NOT NULL
           ORDER BY o.updated_at DESC
           LIMIT 10""",
        (campaign_id,),
    ).fetchall()
    db.close()

    ar = dict(accept_row) if accept_row else {}
    rr = dict(reply_row) if reply_row else {}
    fr = dict(full_row) if full_row else {}

    timelines = []
    for row in deal_rows:
        d = dict(row)
        inv = d.get("invited_at")
        acc = d.get("accepted_at")
        rep = d.get("first_reply_at")
        timelines.append({
            "name": d.get("name", "Unknown"),
            "title": d.get("title", ""),
            "company": d.get("company", ""),
            "status": d.get("status", ""),
            "time_to_accept": (acc - inv) if (inv and acc) else None,
            "time_to_reply": (rep - acc) if (acc and rep) else None,
        })

    return {
        "avg_time_to_accept": ar.get("avg_tta"),
        "avg_time_to_reply": rr.get("avg_ttr"),
        "avg_time_invite_to_reply": fr.get("avg_full"),
        "count_accepted": ar.get("cnt", 0),
        "count_replied": rr.get("cnt", 0),
        "fastest_accept": ar.get("min_tta"),
        "slowest_accept": ar.get("max_tta"),
        "fastest_reply": rr.get("min_ttr"),
        "slowest_reply": rr.get("max_ttr"),
        "per_deal_timelines": timelines,
    }


# ──────────────────────────────────────────────
# ICPs (Ideal Customer Profiles)
# ──────────────────────────────────────────────

def save_icp(
    name: str,
    icp_json: str,
    target_desc: str = "",
    source_url: str = "",
    confidence: float = 0.5,
) -> str:
    """Create a new ICP and return its ID."""
    icp_id = str(uuid.uuid4())
    now = int(time.time())
    db = get_db()
    db.execute(
        """INSERT INTO icps (id, name, icp_json, target_desc, source_url, status, confidence, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
        (icp_id, name, icp_json, target_desc, source_url, confidence, now, now),
    )
    db.commit()
    db.close()
    return icp_id


def get_icp(icp_id: str) -> Optional[dict]:
    """Load an ICP by ID."""
    db = get_db()
    row = db.execute("SELECT * FROM icps WHERE id = ?", (icp_id,)).fetchone()
    db.close()
    return dict(row) if row else None


def list_icp_chunk_texts(limit: int = 200) -> list[dict]:
    """ICP chunk text only — never embeddings."""
    db = get_db()
    rows = db.execute(
        "SELECT c.id, s.icp_id, c.source_id, c.text "
        "FROM icp_chunks c "
        "LEFT JOIN icp_sources s ON s.id = c.source_id "
        "ORDER BY c.created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def list_icps(status: Optional[str] = None) -> list[dict]:
    """List all ICPs, optionally filtered by status."""
    db = get_db()
    if status:
        rows = db.execute(
            "SELECT * FROM icps WHERE status = ? ORDER BY created_at DESC",
            (status,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM icps ORDER BY created_at DESC",
        ).fetchall()
    db.close()
    return [dict(r) for r in rows]


_VALID_ICP_COLS = frozenset({
    "name", "icp_json", "target_desc", "source_url", "status",
    "confidence", "updated_at",
})


def update_icp(icp_id: str, **kwargs: Any) -> None:
    """Update an ICP's fields."""
    db = get_db()
    kwargs["updated_at"] = int(time.time())
    bad_keys = set(kwargs) - _VALID_ICP_COLS
    if bad_keys:
        raise ValueError(f"Invalid ICP columns: {bad_keys}")
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [icp_id]
    db.execute(f"UPDATE icps SET {set_clause} WHERE id = ?", values)
    db.commit()
    db.close()


def delete_icp(icp_id: str) -> None:
    """Delete an ICP and its sources/chunks."""
    db = get_db()
    # Delete chunks for this ICP's sources
    db.execute(
        """DELETE FROM icp_chunks WHERE source_id IN
           (SELECT id FROM icp_sources WHERE icp_id = ?)""",
        (icp_id,),
    )
    db.execute("DELETE FROM icp_sources WHERE icp_id = ?", (icp_id,))
    db.execute("DELETE FROM icps WHERE id = ?", (icp_id,))
    db.commit()
    db.close()


# ──────────────────────────────────────────────
# Engagements
# ──────────────────────────────────────────────

def reserve_engagement(
    outreach_id: str,
    action_type: str,
    post_id: str,
    account_id: str,
    campaign_id: str = "",
) -> str | None:
    """Reserve an engagement slot before calling the LinkedIn API.

    Inserts a row with status='pending' to claim the (post_id, account_id) slot
    via the UNIQUE constraint. Returns engagement ID on success, None if already
    reserved/sent (duplicate).
    """
    if not post_id or not account_id:
        return None
    engagement_id = str(uuid.uuid4())
    db = get_db()
    try:
        # Reclaim first, then insert. Both run inside the one implicit
        # transaction sqlite3 opens at the DELETE — the first statement is
        # already a write, so the write lock is held from there to the commit
        # and no other writer can slip between the two.
        #
        # Every in-process outcome resolves a reservation: success finalizes
        # it, each failure branch deletes it. Nothing resolves it if the
        # process itself dies, and then the index makes the corpse permanent —
        # every later attempt on that post gets None and reports it as a
        # duplicate, which is indistinguishable from a comment that landed.
        # Bounded by status and age so a live reservation still wins the race
        # and a delivered engagement is never re-engaged.
        db.execute(
            """DELETE FROM engagements
               WHERE post_id = ? AND account_id = ?
                 AND status = 'pending' AND created_at < ?""",
            (post_id, account_id,
             int(time.time()) - ENGAGEMENT_RESERVATION_TTL_SECONDS),
        )
        db.execute(
            """INSERT INTO engagements
               (id, outreach_id, action_type, post_id, status,
                campaign_id, account_id, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (engagement_id, outreach_id, action_type, post_id,
             campaign_id or None, account_id, int(time.time())),
        )
        db.commit()
    except sqlite3.IntegrityError as e:
        db.rollback()
        db.close()
        # Only the UNIQUE index may answer "already reserved". The same
        # exception also covers the FOREIGN KEY on outreach_id — enforced here,
        # PRAGMA foreign_keys is 1 — so a broken reference used to come back as
        # a routine duplicate and the engagement silently never happened.
        if "UNIQUE constraint failed" in str(e):
            return None
        logger.error(
            "reserve_engagement: %s (outreach_id=%s post_id=%s) — not a "
            "duplicate", e, outreach_id, post_id,
        )
        raise
    db.close()
    return engagement_id


def finalize_engagement(
    engagement_id: str,
    post_text: str = "",
    text: str = "",
    reaction_type: str = "",
    reasoning: str = "",
) -> None:
    """Update a reserved engagement to 'sent' with full details after LinkedIn confirms."""
    db = get_db()
    db.execute(
        """UPDATE engagements
           SET status = 'sent', post_text = ?, text = ?,
               reaction_type = ?, reasoning = ?
           WHERE id = ?""",
        (post_text, text, reaction_type, reasoning, engagement_id),
    )
    db.commit()
    db.close()


def delete_engagement(engagement_id: str) -> None:
    """Remove a reserved engagement that failed to send."""
    db = get_db()
    db.execute("DELETE FROM engagements WHERE id = ? AND status = 'pending'", (engagement_id,))
    db.commit()
    db.close()


def save_engagement(
    outreach_id: str,
    action_type: str,
    post_id: str,
    post_text: str = "",
    text: str = "",
    reaction_type: str = "",
    status: str = "sent",
    reasoning: str = "",
    campaign_id: str = "",
    account_id: str = "",
) -> str | None:
    """Save an engagement action (comment or reaction). Returns engagement ID.

    Returns None if a UNIQUE constraint violation occurs (duplicate post+account),
    which serves as the DB-level safety net against race conditions.
    """
    engagement_id = str(uuid.uuid4())
    db = get_db()
    try:
        db.execute(
            """INSERT INTO engagements
               (id, outreach_id, action_type, post_id, post_text, text,
                reaction_type, status, reasoning, campaign_id, account_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (engagement_id, outreach_id, action_type, post_id, post_text,
             text, reaction_type, status, reasoning, campaign_id or None,
             account_id or None, int(time.time())),
        )
        db.commit()
    except sqlite3.IntegrityError:
        # UNIQUE constraint on (post_id, account_id) — duplicate engagement
        db.close()
        return None
    db.close()
    return engagement_id


def get_unverified_engagements(since_ts: int, limit: int = 20) -> list[dict]:
    """Fetch engagements with verified_at IS NULL from the last N seconds.

    Joins through outreach → contact to include provider_id for follow verification.
    """
    db = get_db()
    rows = db.execute(
        """SELECT e.id, e.action_type, e.post_id, e.text, e.account_id,
                  e.outreach_id, e.created_at,
                  c.linkedin_id AS contact_linkedin_id,
                  c.profile_json AS contact_profile_json
           FROM engagements e
           LEFT JOIN outreaches o ON e.outreach_id = o.id
           LEFT JOIN contacts c ON o.contact_id = c.id
           WHERE e.created_at >= ?
             AND e.verified_at IS NULL
             AND e.status = 'sent'
           ORDER BY e.created_at DESC
           LIMIT ?""",
        (since_ts, limit),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def update_engagement_verification(
    engagement_id: str,
    verified_status: str,
    external_id: str = "",
) -> None:
    """Update verification status and external_id on an engagement."""
    db = get_db()
    now = int(time.time())
    if external_id:
        db.execute(
            "UPDATE engagements SET verified_at = ?, verified_status = ?, external_id = ? WHERE id = ?",
            (now, verified_status, external_id, engagement_id),
        )
    else:
        db.execute(
            "UPDATE engagements SET verified_at = ?, verified_status = ? WHERE id = ?",
            (now, verified_status, engagement_id),
        )
    db.commit()
    db.close()


def get_engagement_verification_stats(campaign_id: str = "") -> dict:
    """Get engagement verification stats, optionally filtered by campaign."""
    db = get_db()
    where = "WHERE campaign_id = ?" if campaign_id else ""
    params: tuple = (campaign_id,) if campaign_id else ()
    rows = db.execute(
        f"""SELECT
            COALESCE(verified_status, 'pending') as status,
            COUNT(*) as cnt
        FROM engagements
        {where}
        GROUP BY COALESCE(verified_status, 'pending')""",
        params,
    ).fetchall()
    db.close()
    result = {"verified": 0, "unverified": 0, "trust_api": 0, "pending": 0}
    for row in rows:
        key = row["status"] if row["status"] in result else "pending"
        result[key] = row["cnt"]
    return result


def get_engagement_stats(campaign_id: str = "") -> dict:
    """Get engagement stats, optionally filtered by campaign.

    Includes engagements linked via outreach→campaign OR directly via campaign_id.
    Excludes unverified engagements (failed to land on LinkedIn).
    """
    db = get_db()
    unverified_filter = "AND (e.verified_status IS NULL OR e.verified_status != 'unverified')"
    if campaign_id:
        comments = db.execute(
            f"""SELECT COUNT(*) as c FROM engagements e
               LEFT JOIN outreaches o ON e.outreach_id = o.id
               WHERE (o.campaign_id = ? OR e.campaign_id = ?)
               AND e.action_type = 'comment'
               AND e.status = 'sent' {unverified_filter}""",
            (campaign_id, campaign_id),
        ).fetchone()["c"]
        reactions = db.execute(
            f"""SELECT COUNT(*) as c FROM engagements e
               LEFT JOIN outreaches o ON e.outreach_id = o.id
               WHERE (o.campaign_id = ? OR e.campaign_id = ?)
               AND e.action_type = 'react'
               AND e.status = 'sent' {unverified_filter}""",
            (campaign_id, campaign_id),
        ).fetchone()["c"]
    else:
        comments = db.execute(
            "SELECT COUNT(*) as c FROM engagements e WHERE action_type = 'comment' AND status = 'sent' "
            "AND (e.verified_status IS NULL OR e.verified_status != 'unverified')"
        ).fetchone()["c"]
        reactions = db.execute(
            "SELECT COUNT(*) as c FROM engagements e WHERE action_type = 'react' AND status = 'sent' "
            "AND (e.verified_status IS NULL OR e.verified_status != 'unverified')"
        ).fetchone()["c"]
    db.close()
    return {"comments": comments, "reactions": reactions, "total": comments + reactions}


def get_recent_engagements(campaign_id: str = "", limit: int = 10) -> list[dict]:
    """Get recent engagement records with prospect details.

    Returns engagement details including prospect name, post text snippet,
    comment text, action type, and post ID for manual verification.
    """
    db = get_db()
    if campaign_id:
        rows = db.execute(
            """SELECT e.action_type, e.post_id, e.post_text, e.text as comment_text,
                      e.reaction_type, e.status, e.created_at,
                      c.name as prospect_name, c.title as prospect_title
               FROM engagements e
               LEFT JOIN outreaches o ON e.outreach_id = o.id
               LEFT JOIN contacts c ON o.contact_id = c.id
               WHERE (o.campaign_id = ? OR e.campaign_id = ?)
               AND e.action_type IN ('comment', 'react')
               ORDER BY e.created_at DESC
               LIMIT ?""",
            (campaign_id, campaign_id, limit),
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT e.action_type, e.post_id, e.post_text, e.text as comment_text,
                      e.reaction_type, e.status, e.created_at,
                      c.name as prospect_name, c.title as prospect_title
               FROM engagements e
               LEFT JOIN outreaches o ON e.outreach_id = o.id
               LEFT JOIN contacts c ON o.contact_id = c.id
               WHERE e.action_type IN ('comment', 'react')
               ORDER BY e.created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    result = [dict(r) for r in rows]
    db.close()
    return result


def get_engagement_candidates(
    campaign_id: str,
    max_per_outreach: int = 3,
) -> list[dict]:
    """Find outreaches ready for post engagement.

    Returns outreaches that haven't exceeded the engagement limit.
    Includes pending/invited/connected/messaged/replied — engaging with
    posts BEFORE connection acceptance is a warm-up tactic.

    Skips outreaches with time-limited cooldowns (next_action JSON with
    ``skip_engagement_until`` timestamp in the future). Expired cooldowns
    are automatically eligible again.
    """
    now = int(time.time())
    db = get_db()
    rows = db.execute(
        """SELECT o.id as outreach_id, o.campaign_id, o.contact_id, o.status,
                  o.followup_count, o.updated_at as outreach_updated_at,
                  o.next_action,
                  c.name, c.title, c.company, c.linkedin_url, c.linkedin_id,
                  c.profile_json, c.fit_score,
                  (SELECT COUNT(*) FROM engagements e WHERE e.outreach_id = o.id) as engagement_count
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           WHERE o.campaign_id = ?
             AND o.status IN ('pending', 'invited', 'connected', 'messaged', 'replied')
             AND (SELECT COUNT(*) FROM engagements e WHERE e.outreach_id = o.id) < ?
             AND COALESCE(o.next_action, '') != 'skip_engagement'
             AND (
               o.next_action IS NULL
               OR o.next_action = ''
               OR json_valid(o.next_action) = 0
               OR json_extract(o.next_action, '$.skip_engagement_until') IS NULL
               OR json_extract(o.next_action, '$.skip_engagement_until') < ?
             )
           ORDER BY COALESCE(
               (SELECT MAX(e.created_at) FROM engagements e
                 WHERE e.outreach_id = o.id),
               o.invited_at, o.created_at
           ) ASC""",
        (campaign_id, max_per_outreach, now),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_follow_candidates(campaign_id: str) -> list[dict]:
    """Find pending outreaches that haven't been followed yet.

    Returns outreaches in 'pending' status that have no 'follow'
    engagement yet. Used by the scheduler to auto-follow prospects
    before engaging with their posts.
    Respects both permanent skip_engagement and time-limited JSON cooldowns.
    """
    now = int(time.time())
    db = get_db()
    rows = db.execute(
        """SELECT o.id as outreach_id, o.campaign_id, o.contact_id, o.status,
                  o.followup_count, o.updated_at as outreach_updated_at,
                  c.name, c.title, c.company, c.linkedin_url, c.linkedin_id,
                  c.profile_json, c.fit_score
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           WHERE o.campaign_id = ?
             AND o.status = 'pending'
             AND COALESCE(o.next_action, '') != 'skip_engagement'
             AND (
               o.next_action IS NULL
               OR o.next_action = ''
               OR json_valid(o.next_action) = 0
               OR json_extract(o.next_action, '$.skip_engagement_until') IS NULL
               OR json_extract(o.next_action, '$.skip_engagement_until') < ?
             )
             AND NOT EXISTS (
                 SELECT 1 FROM engagements e
                 WHERE e.outreach_id = o.id AND e.action_type = 'follow'
             )
           ORDER BY c.fit_score DESC""",
        (campaign_id, now),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_profile_view_candidates(campaign_id: str) -> list[dict]:
    """Find pending outreaches that haven't had a profile view yet.

    Returns outreaches in 'pending' status that have no 'profile_view'
    engagement yet. Used by the scheduler to auto-view prospect profiles
    as the lightest warm-up signal before following.
    Respects both permanent skip_engagement and time-limited JSON cooldowns.
    """
    now = int(time.time())
    db = get_db()
    rows = db.execute(
        """SELECT o.id as outreach_id, o.campaign_id, o.contact_id, o.status,
                  o.followup_count, o.updated_at as outreach_updated_at,
                  c.name, c.title, c.company, c.linkedin_url, c.linkedin_id,
                  c.profile_json, c.fit_score
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           WHERE o.campaign_id = ?
             AND o.status = 'pending'
             AND COALESCE(o.next_action, '') != 'skip_engagement'
             AND (
               o.next_action IS NULL
               OR o.next_action = ''
               OR json_valid(o.next_action) = 0
               OR json_extract(o.next_action, '$.skip_engagement_until') IS NULL
               OR json_extract(o.next_action, '$.skip_engagement_until') < ?
             )
             AND NOT EXISTS (
                 SELECT 1 FROM engagements e
                 WHERE e.outreach_id = o.id AND e.action_type = 'profile_view'
             )
           ORDER BY c.fit_score DESC""",
        (campaign_id, now),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_daily_engagement_count() -> int:
    """Count engagements sent today."""
    import datetime
    today = datetime.date.today()
    today_start = int(time.mktime(today.timetuple()))
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) as c FROM engagements WHERE created_at >= ?",
        (today_start,),
    ).fetchone()
    db.close()
    return row["c"] if row else 0


def get_daily_engagement_count_by_type(action_type: str) -> int:
    """Count engagements of a specific type sent today.

    Enables independent daily limits per action type (comment, react,
    profile_view, follow, endorse).
    """
    import datetime
    today = datetime.date.today()
    today_start = int(time.mktime(today.timetuple()))
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) as c FROM engagements WHERE action_type = ? AND created_at >= ?",
        (action_type, today_start),
    ).fetchone()
    db.close()
    return row["c"] if row else 0


def get_daily_engagement_counts_all() -> dict:
    """Count verified engagements by action_type for today.

    Returns {action_type: count} dict. Only counts engagements with
    verified_status IN ('verified', 'trust_api') — not NULL (pending)
    or 'unverified' (failed).
    """
    import datetime
    today = datetime.date.today()
    today_start = int(time.mktime(today.timetuple()))
    db = get_db()
    rows = db.execute(
        "SELECT action_type, COUNT(*) as c FROM engagements "
        "WHERE created_at >= ? "
        "AND verified_status IN ('verified', 'trust_api') "
        "GROUP BY action_type",
        (today_start,),
    ).fetchall()
    db.close()
    return {row["action_type"]: row["c"] for row in rows}


def get_daily_engagement_counts_pending() -> dict:
    """Count pending-verification engagements by action_type for today.

    Returns {action_type: count} dict for engagements with NULL verified_status.
    """
    import datetime
    today = datetime.date.today()
    today_start = int(time.mktime(today.timetuple()))
    db = get_db()
    rows = db.execute(
        "SELECT action_type, COUNT(*) as c FROM engagements "
        "WHERE created_at >= ? "
        "AND verified_status IS NULL AND status = 'sent' "
        "GROUP BY action_type",
        (today_start,),
    ).fetchall()
    db.close()
    return {row["action_type"]: row["c"] for row in rows}


def get_daily_brand_post_count() -> int:
    """Count published brand posts today (actions_log, not completed jobs)."""
    import datetime

    today = datetime.date.today()
    today_start = int(time.mktime(today.timetuple()))
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) as c FROM actions_log "
        "WHERE action_type = 'brand_post_published' AND timestamp >= ?",
        (today_start,),
    ).fetchone()
    db.close()
    return row["c"] if row else 0


def get_daily_brand_engage_count() -> int:
    """Count completed brand_engage scheduler jobs today."""
    import datetime

    today = datetime.date.today()
    today_start = int(time.mktime(today.timetuple()))
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) as c FROM scheduler_jobs "
        "WHERE job_type = 'brand_engage' AND status = 'completed' AND scheduled_at >= ?",
        (today_start,),
    ).fetchone()
    db.close()
    return row["c"] if row else 0


def get_daily_brand_profile_count() -> int:
    """Count completed brand_profile scheduler jobs today."""
    import datetime

    today = datetime.date.today()
    today_start = int(time.mktime(today.timetuple()))
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) as c FROM scheduler_jobs "
        "WHERE job_type = 'brand_profile' AND status = 'completed' AND scheduled_at >= ?",
        (today_start,),
    ).fetchone()
    db.close()
    return row["c"] if row else 0


def get_weekly_brand_post_published_count() -> int:
    """Count brand_post_published actions_log rows since Monday 00:00 local."""
    import datetime

    today = datetime.date.today()
    week_start = today - datetime.timedelta(days=today.weekday())
    week_start_ts = int(time.mktime(week_start.timetuple()))
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) as c FROM actions_log "
        "WHERE action_type = 'brand_post_published' AND timestamp >= ?",
        (week_start_ts,),
    ).fetchone()
    db.close()
    return row["c"] if row else 0


def count_brand_posts_since(since_ts: int) -> int:
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) as c FROM actions_log "
        "WHERE action_type = 'brand_post_published' AND timestamp >= ?",
        (since_ts,),
    ).fetchone()
    db.close()
    return row["c"] if row else 0


def count_brand_engagements_since(since_ts: int) -> int:
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) as c FROM engagements "
        "WHERE reasoning = 'brand_strategy' AND created_at >= ?",
        (since_ts,),
    ).fetchone()
    db.close()
    return row["c"] if row else 0


def count_brand_profile_changes_since(since_ts: int) -> int:
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) as c FROM profile_changes "
        "WHERE source = 'brand_strategy' AND created_at >= ?",
        (since_ts,),
    ).fetchone()
    db.close()
    return row["c"] if row else 0


def get_engagement_count_for_outreach(outreach_id: str) -> int:
    """Count engagements for a specific outreach."""
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) as c FROM engagements WHERE outreach_id = ?",
        (outreach_id,),
    ).fetchone()
    db.close()
    return row["c"] if row else 0


def get_engaged_post_ids(outreach_id: str) -> set[str]:
    """Return post_ids already engaged for this outreach."""
    db = get_db()
    rows = db.execute(
        "SELECT DISTINCT post_id FROM engagements WHERE outreach_id = ?",
        (outreach_id,),
    ).fetchall()
    db.close()
    return {row["post_id"] for row in rows if row["post_id"]}


def get_account_engaged_post_ids(account_id: str) -> set[str]:
    """Return all post_ids already engaged by this account across ALL outreaches.

    This is the global dedup check — prevents the same LinkedIn account from
    commenting on the same post twice, regardless of which outreach triggers it.
    """
    if not account_id:
        return set()
    db = get_db()
    rows = db.execute(
        """SELECT DISTINCT post_id FROM engagements
           WHERE account_id = ? AND post_id IS NOT NULL AND post_id != ''""",
        (account_id,),
    ).fetchall()
    db.close()
    return {row["post_id"] for row in rows}


def is_post_already_engaged(post_id: str, account_id: str = "") -> bool:
    """Check if this post has already been engaged by this account (any outreach).

    Lightweight single-post check used as a pre-send race condition guard.
    """
    if not post_id or not account_id:
        return False
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM engagements WHERE post_id = ? AND account_id = ? LIMIT 1",
        (post_id, account_id),
    ).fetchone()
    db.close()
    return row is not None


# ──────────────────────────────────────────────
# Engagement Anomaly Detection
# ──────────────────────────────────────────────


def get_duplicate_post_engagements(hours: int = 24) -> list[dict]:
    """Find post_ids with multiple engagements by the same account in the last N hours.

    Returns list of dicts with post_id, count, outreach_ids, action_types.
    """
    db = get_db()
    since = int(time.time()) - (hours * 3600)
    rows = db.execute(
        """SELECT post_id, account_id,
                  COUNT(*) as cnt,
                  GROUP_CONCAT(DISTINCT outreach_id) as outreach_ids,
                  GROUP_CONCAT(DISTINCT action_type) as action_types,
                  MIN(created_at) as first_at,
                  MAX(created_at) as last_at
           FROM engagements
           WHERE created_at >= ?
             AND post_id IS NOT NULL AND post_id != ''
             AND account_id IS NOT NULL AND account_id != ''
           GROUP BY post_id, account_id
           HAVING COUNT(*) > 1
           ORDER BY cnt DESC""",
        (since,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_engagement_burst_count(minutes: int = 30) -> int:
    """Count engagements in the last N minutes (burst detection)."""
    db = get_db()
    since = int(time.time()) - (minutes * 60)
    row = db.execute(
        "SELECT COUNT(*) as c FROM engagements WHERE created_at >= ?",
        (since,),
    ).fetchone()
    db.close()
    return row["c"] if row else 0


def get_engagement_burst_count_by_type(minutes: int = 30, action_type: str = "react") -> int:
    """Count engagements of a specific action_type in the last N minutes."""
    db = get_db()
    since = int(time.time()) - (minutes * 60)
    row = db.execute(
        "SELECT COUNT(*) as c FROM engagements WHERE created_at >= ? AND action_type = ?",
        (since, action_type),
    ).fetchone()
    db.close()
    return row["c"] if row else 0


def get_same_company_engagements(hours: int = 24) -> list[dict]:
    """Find cases where many comments were made on posts related to the same company.

    Groups by prospect company to detect same-company-post clustering.
    """
    db = get_db()
    since = int(time.time()) - (hours * 3600)
    rows = db.execute(
        """SELECT c.company, c.name as prospect_name,
                  COUNT(DISTINCT e.post_id) as post_count,
                  COUNT(*) as engagement_count,
                  GROUP_CONCAT(DISTINCT e.post_id) as post_ids
           FROM engagements e
           JOIN outreaches o ON e.outreach_id = o.id
           JOIN contacts c ON o.contact_id = c.id
           WHERE e.created_at >= ?
             AND e.action_type = 'comment'
             AND c.company IS NOT NULL AND c.company != ''
           GROUP BY c.company
           HAVING COUNT(*) > 2
           ORDER BY engagement_count DESC""",
        (since,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_failed_engagement_count(hours: int = 24) -> int:
    """Count failed engagements in the last N hours."""
    db = get_db()
    since = int(time.time()) - (hours * 3600)
    row = db.execute(
        "SELECT COUNT(*) as c FROM engagements WHERE status = 'failed' AND created_at >= ?",
        (since,),
    ).fetchone()
    db.close()
    return row["c"] if row else 0


def get_last_activity_timestamp(outreach_id: str) -> int:
    """Get the most recent activity timestamp for an outreach.

    Checks messages, engagements, and outreach updated_at.
    Returns Unix timestamp (0 if no activity).
    """
    db = get_db()
    # Latest message
    msg = db.execute(
        "SELECT MAX(timestamp) as t FROM messages WHERE outreach_id = ?",
        (outreach_id,),
    ).fetchone()
    # Latest engagement
    eng = db.execute(
        "SELECT MAX(created_at) as t FROM engagements WHERE outreach_id = ?",
        (outreach_id,),
    ).fetchone()
    # Outreach updated_at
    out = db.execute(
        "SELECT updated_at FROM outreaches WHERE id = ?",
        (outreach_id,),
    ).fetchone()
    db.close()

    timestamps = [
        msg["t"] if msg and msg["t"] else 0,
        eng["t"] if eng and eng["t"] else 0,
        out["updated_at"] if out else 0,
    ]
    return max(timestamps)


def get_error_outreaches(campaign_id: str = "") -> list[dict]:
    """Find outreaches with status 'error', optionally filtered by campaign."""
    db = get_db()
    if campaign_id:
        rows = db.execute(
            """SELECT o.id as outreach_id, o.campaign_id, o.contact_id,
                      o.status, o.followup_count, o.updated_at,
                      o.invite_attempts,
                      c.name, c.title, c.company, c.linkedin_url, c.fit_score
               FROM outreaches o
               JOIN contacts c ON o.contact_id = c.id
               WHERE o.campaign_id = ? AND o.status = 'error'
               ORDER BY o.updated_at DESC""",
            (campaign_id,),
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT o.id as outreach_id, o.campaign_id, o.contact_id,
                      o.status, o.followup_count, o.updated_at,
                      o.invite_attempts,
                      c.name, c.title, c.company, c.linkedin_url, c.fit_score
               FROM outreaches o
               JOIN contacts c ON o.contact_id = c.id
               WHERE o.status = 'error'
               ORDER BY o.updated_at DESC"""
        ).fetchall()
    db.close()
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────
# Scheduler Jobs (Sprint 17)
# ──────────────────────────────────────────────

def cancel_pending_jobs_of_type(job_type: str) -> int:
    """Cancel leftover pending jobs once today's cap for that type is spent."""
    if not job_type:
        return 0
    db = get_db()
    cur = db.execute(
        """UPDATE scheduler_jobs SET status = 'cancelled', completed_at = ?
           WHERE job_type = ? AND status = 'pending'""",
        (int(time.time()), job_type),
    )
    db.commit()
    n = cur.rowcount
    db.close()
    return int(n or 0)


def create_scheduler_job(
    campaign_id: Optional[str],
    job_type: str,
    scheduled_at: int,
    outreach_id: Optional[str] = None,
) -> str:
    """Create a new scheduler job and return its ID.

    Arguments are validated up front. Callers routinely wrap this in a broad
    `except Exception: logger.debug(...)`, so an invalid call would otherwise
    disable a whole periodic subsystem without anything surfacing.
    """
    if not job_type or not isinstance(job_type, str):
        raise ValueError(
            f"create_scheduler_job: job_type must be a non-empty string, got "
            f"{job_type!r}. Signature is (campaign_id, job_type, scheduled_at)."
        )
    if isinstance(scheduled_at, bool) or not isinstance(scheduled_at, (int, float)):
        raise ValueError(
            f"create_scheduler_job: scheduled_at must be a unix timestamp, got "
            f"{scheduled_at!r}. Signature is (campaign_id, job_type, scheduled_at)."
        )
    scheduled_at = int(scheduled_at)

    job_id = str(uuid.uuid4())
    db = get_db()
    db.execute(
        """INSERT INTO scheduler_jobs (id, campaign_id, outreach_id, job_type, status, scheduled_at)
           VALUES (?, ?, ?, ?, 'pending', ?)""",
        (job_id, campaign_id, outreach_id, job_type, scheduled_at),
    )
    db.commit()
    db.close()
    return job_id


def get_ready_jobs(limit: int = 10) -> list[dict]:
    """Get jobs that are ready to execute (pending and scheduled_at <= now).

    Core outreach jobs (invite, send_dm, followup, auto_reply) are prioritised
    over warm-up jobs (engage, follow, endorse, profile_view) so that warm-up
    never blocks outreach.
    """
    now = int(time.time())
    db = get_db()
    rows = db.execute(
        """SELECT * FROM scheduler_jobs
           WHERE status = 'pending' AND scheduled_at <= ?
           ORDER BY
               CASE job_type
                   WHEN 'invite' THEN 1
                   WHEN 'send_dm' THEN 1
                   WHEN 'followup' THEN 1
                   WHEN 'auto_reply' THEN 1
                   WHEN 'discover' THEN 2
                   WHEN 'email_fallback' THEN 2
                   WHEN 'engage' THEN 3
                   WHEN 'follow' THEN 4
                   WHEN 'endorse' THEN 4
                   WHEN 'profile_view' THEN 5
                   ELSE 3
               END,
               scheduled_at ASC
           LIMIT ?""",
        (now, limit),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def count_stale_ready_jobs(staleness_seconds: int = 600) -> int:
    """Count pending jobs whose scheduled_at is more than staleness_seconds in the past."""
    now = int(time.time())
    cutoff = now - staleness_seconds
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) as cnt FROM scheduler_jobs WHERE status = 'pending' AND scheduled_at <= ?",
        (cutoff,),
    ).fetchone()
    db.close()
    return dict(row).get("cnt", 0) if row else 0


def restagger_stale_jobs(staleness_seconds: int = 600, spread_seconds: int = 30) -> int:
    """Re-stagger stale pending jobs, spreading them out from now.

    Jobs are ordered by priority (core outreach first, warm-up last) then by
    original scheduled_at. Each job gets scheduled_at = now + (index * spread_seconds).

    Returns the number of re-staggered jobs.
    """
    now = int(time.time())
    cutoff = now - staleness_seconds
    db = get_db()

    # Fetch stale jobs in priority order (same as get_ready_jobs)
    rows = db.execute(
        """SELECT id FROM scheduler_jobs
           WHERE status = 'pending' AND scheduled_at <= ?
           ORDER BY
               CASE job_type
                   WHEN 'invite' THEN 1
                   WHEN 'send_dm' THEN 1
                   WHEN 'followup' THEN 1
                   WHEN 'auto_reply' THEN 1
                   WHEN 'discover' THEN 2
                   WHEN 'email_fallback' THEN 2
                   WHEN 'engage' THEN 3
                   WHEN 'follow' THEN 4
                   WHEN 'endorse' THEN 4
                   WHEN 'profile_view' THEN 5
                   ELSE 3
               END,
               scheduled_at ASC""",
        (cutoff,),
    ).fetchall()

    if not rows:
        db.close()
        return 0

    for idx, row in enumerate(rows):
        new_time = now + (idx * spread_seconds)
        db.execute(
            "UPDATE scheduler_jobs SET scheduled_at = ? WHERE id = ? AND status = 'pending'",
            (new_time, row["id"]),
        )

    db.commit()
    count = len(rows)
    db.close()
    return count


def claim_job(job_id: str) -> bool:
    """Atomically claim a pending job (set to running). Returns True if claimed."""
    now = int(time.time())
    db = get_db()
    cursor = db.execute(
        """UPDATE scheduler_jobs SET status = 'running', started_at = ?
           WHERE id = ? AND status = 'pending'""",
        (now, job_id),
    )
    db.commit()
    changed = cursor.rowcount > 0
    db.close()
    return changed


def complete_job(
    job_id: str,
    error: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """Mark a job as completed or failed, optionally recording execution duration.

    Failure is decided by ``error is not None``, never by truthiness: an
    exception whose message is empty (``asyncio.wait_for`` raises a bare
    TimeoutError, whose str() is "") must not be filed as a success. Inferring
    the outcome from the truthiness of a human-readable string is what turned a
    4-day collection blackout into a table full of 'completed' rows.
    """
    now = int(time.time())
    failed = error is not None
    status = "failed" if failed else "completed"
    db = get_db()
    if failed:
        db.execute(
            """UPDATE scheduler_jobs
               SET status = ?, completed_at = ?, error = ?, retry_count = retry_count + 1,
                   duration_ms = ?
               WHERE id = ?""",
            (status, now, error, duration_ms, job_id),
        )
    else:
        # Clear any error left by a previous attempt — the column means "why the
        # last attempt failed", and a stale value would make a green job look red.
        db.execute(
            """UPDATE scheduler_jobs
               SET status = ?, completed_at = ?, duration_ms = ?, error = NULL
               WHERE id = ?""",
            (status, now, duration_ms, job_id),
        )
    db.commit()
    db.close()


def reschedule_job(job_id: str, scheduled_at: int) -> None:
    """Put a running job back on the queue for later — not a failure retry.

    Business-hours and cap deferrals must stay pending so they are not counted
    as completed sends. retry_count is left alone.
    """
    db = get_db()
    db.execute(
        """UPDATE scheduler_jobs SET status = 'pending', scheduled_at = ?,
           started_at = NULL, completed_at = NULL
           WHERE id = ?""",
        (int(scheduled_at), job_id),
    )
    db.commit()
    db.close()


def retry_job(job_id: str, new_scheduled_at: int) -> None:
    """Reset a failed job to pending with a new scheduled time.

    ``error`` is deliberately preserved. Blanking it here is why scheduler_jobs
    held only the two watchdog strings across all history: every real exception
    was written by complete_job() and erased ~5 min later by the retry, so the
    only failures that survived were the watchdog's — which never retry. A
    4-day collection blackout was therefore invisible in the job table.
    """
    db = get_db()
    db.execute(
        """UPDATE scheduler_jobs SET status = 'pending', scheduled_at = ?,
           started_at = NULL, completed_at = NULL
           WHERE id = ?""",
        (new_scheduled_at, job_id),
    )
    db.commit()
    db.close()


def recover_stuck_jobs(stuck_minutes: int = 30) -> int:
    """Mark stuck jobs as failed so they can be retried.

    Recovers:
    - 'running' jobs with started_at older than *stuck_minutes* (likely crashed)
    - 'pending' jobs with scheduled_at older than *stuck_minutes* (never picked up)

    Returns the number of jobs recovered.
    """
    now = int(time.time())
    cutoff = now - (stuck_minutes * 60)
    db = get_db()

    # Recover stuck running jobs
    c1 = db.execute(
        """UPDATE scheduler_jobs
           SET status = 'failed', error = 'stuck_job_recovered',
               completed_at = ?
           WHERE status = 'running' AND started_at < ?""",
        (now, cutoff),
    )

    # Recover stale pending jobs (scheduled long ago but never claimed)
    c2 = db.execute(
        """UPDATE scheduler_jobs
           SET status = 'failed', error = 'stale_pending_recovered',
               completed_at = ?
           WHERE status = 'pending' AND scheduled_at < ?""",
        (now, cutoff),
    )

    db.commit()
    count = c1.rowcount + c2.rowcount
    db.close()
    return count



def get_pending_job_count(campaign_id: Optional[str], job_type: str) -> int:
    """Count pending/running jobs of a given type for a campaign (dedup check).

    Ignores 'running' jobs older than 10 minutes — these are assumed stuck
    (e.g. from a crashed process or stale session). Without this TTL, a single
    stuck job can block all new scheduling for the same job type indefinitely.
    """
    stale_cutoff = int(time.time()) - 600  # 10 min TTL for running jobs
    db = get_db()
    if campaign_id is None:
        row = db.execute(
            """SELECT COUNT(*) as cnt FROM scheduler_jobs
               WHERE campaign_id IS NULL AND job_type = ?
               AND (
                   (status = 'pending')
                   OR (status = 'running' AND started_at > ?)
               )""",
            (job_type, stale_cutoff),
        ).fetchone()
    else:
        row = db.execute(
            """SELECT COUNT(*) as cnt FROM scheduler_jobs
               WHERE campaign_id = ? AND job_type = ?
               AND (
                   (status = 'pending')
                   OR (status = 'running' AND started_at > ?)
               )""",
            (campaign_id, job_type, stale_cutoff),
        ).fetchone()
    db.close()
    return row["cnt"] if row else 0


def get_pending_outreach_job(outreach_id: str, job_type: str) -> Optional[dict]:
    """Check if a specific outreach already has a pending/running job of this type.

    Ignores 'running' jobs older than 10 minutes (assumed stuck).
    """
    stale_cutoff = int(time.time()) - 600  # 10 min TTL for running jobs
    db = get_db()
    row = db.execute(
        """SELECT * FROM scheduler_jobs
           WHERE outreach_id = ? AND job_type = ?
           AND (
               (status = 'pending')
               OR (status = 'running' AND started_at > ?)
           )
           LIMIT 1""",
        (outreach_id, job_type, stale_cutoff),
    ).fetchone()
    db.close()
    return dict(row) if row else None


def cleanup_old_jobs(days: int = 7) -> int:
    """Purge completed/failed jobs older than N days.

    Also purges failed jobs with max retries that are older than 1 day
    (these won't be retried and just waste queue space).
    Returns total count deleted.
    """
    cutoff = int(time.time()) - (days * 86400)
    cutoff_failed = int(time.time()) - 86400  # 1 day for exhausted failures
    db = get_db()
    cursor = db.execute(
        """DELETE FROM scheduler_jobs
           WHERE (status IN ('completed', 'failed') AND created_at < ?)
              OR (status = 'failed' AND retry_count >= 3 AND created_at < ?)""",
        (cutoff, cutoff_failed),
    )
    db.commit()
    deleted = cursor.rowcount
    db.close()
    return deleted


def cleanup_failed_engage_jobs() -> dict[str, int]:
    """Clean up failed engage jobs and set time-limited cooldown on their outreaches.

    Returns dict with 'cleaned' (jobs deleted) and 'marked' (outreaches cooldown-set).
    """
    import json as _json

    db = get_db()
    rows = db.execute(
        """SELECT j.id, j.outreach_id
           FROM scheduler_jobs j
           WHERE j.job_type = 'engage' AND j.status = 'failed'""",
    ).fetchall()
    jobs = [dict(r) for r in rows]

    if not jobs:
        db.close()
        return {"cleaned": 0, "marked": 0}

    cooldown_until = int(time.time()) + 24 * 3600  # 24h cooldown
    cooldown_json = _json.dumps({"skip_engagement_until": cooldown_until})

    marked = 0
    for j in jobs:
        outreach_id = j.get("outreach_id")
        if outreach_id:
            db.execute(
                "UPDATE outreaches SET next_action = ? WHERE id = ?",
                (cooldown_json, outreach_id),
            )
            marked += 1
        db.execute("DELETE FROM scheduler_jobs WHERE id = ?", (j["id"],))

    db.commit()
    db.close()
    return {"cleaned": len(jobs), "marked": marked}


def cleanup_failed_invite_jobs() -> dict[str, int]:
    """Clean up failed invite jobs. Returns dict with 'cleaned' count."""
    db = get_db()
    rows = db.execute(
        """SELECT j.id FROM scheduler_jobs j
           WHERE j.job_type = 'invite' AND j.status = 'failed'""",
    ).fetchall()
    jobs = [dict(r) for r in rows]

    if not jobs:
        db.close()
        return {"cleaned": 0}

    for j in jobs:
        db.execute("DELETE FROM scheduler_jobs WHERE id = ?", (j["id"],))
    db.commit()
    db.close()
    return {"cleaned": len(jobs)}


def recover_stuck_running_jobs(timeout_seconds: int = 600) -> int:
    """Reset jobs stuck in 'running' state for longer than timeout.

    If a job has been 'running' for more than timeout_seconds (default 10 min),
    it's assumed the process crashed. Reset to 'pending' for retry if under
    max retries, or mark 'failed' if retries exhausted.

    Returns total number of recovered/failed jobs.
    """
    cutoff = int(time.time()) - timeout_seconds
    db = get_db()
    # Reset to pending if under retry limit
    cursor = db.execute(
        """UPDATE scheduler_jobs
           SET status = 'pending', started_at = NULL
           WHERE status = 'running' AND started_at < ? AND retry_count < 3""",
        (cutoff,),
    )
    recovered = cursor.rowcount
    # Fail if over retry limit
    cursor2 = db.execute(
        """UPDATE scheduler_jobs
           SET status = 'failed', completed_at = ?, error = 'Stuck running job timed out'
           WHERE status = 'running' AND started_at < ? AND retry_count >= 3""",
        (int(time.time()), cutoff),
    )
    timed_out = cursor2.rowcount
    db.commit()
    db.close()
    return recovered + timed_out


# How far an untagged sdr message may sit from invited_at and still be read as
# the invitation note. Both writes happen in the same branch of the invite path
# — update_outreach stamps invited_at, save_message follows within a second —
# so this is generous; it only has to survive clock skew and a slow DB hop.


def _is_invite_note(msg: Any, invited_at: int | None, accepted_at: int | None) -> bool:
    """Is this sdr message an invitation note rather than a delivered DM?

    Notes written by the current invite path carry format='invite_note' and
    answer for themselves. Older notes are untagged: a message sitting on
    invited_at is the note even after accepted_at is set — the opening DM
    is a later, separate send.
    """
    if (msg["format"] or "") == "invite_note":
        return True
    if not invited_at:
        return False
    return abs((msg["timestamp"] or 0) - invited_at) <= _INVITE_NOTE_WINDOW_SECONDS


def outreach_has_invite_note(outreach_id: str) -> bool:
    """Was this person invited WITH a note?

    LinkedIn delivers the note as the first message of the thread when the
    invitation is accepted, so a note-carrying outreach already has an
    opener in the prospect's inbox — the next message continues it. Same
    rule as the hosted scheduler's outreach_has_invite_note.
    """
    db = get_db()
    row = db.execute(
        "SELECT invited_at, accepted_at FROM outreaches WHERE id = ?", (outreach_id,),
    ).fetchone()
    if not row:
        db.close()
        return False
    msgs = db.execute(
        "SELECT text, format, timestamp FROM messages "
        "WHERE outreach_id = ? AND role = 'sdr' AND deleted_at IS NULL",
        (outreach_id,),
    ).fetchall()
    db.close()
    return any(
        (m["text"] or "").strip()
        and _is_invite_note(m, row["invited_at"], row["accepted_at"])
        for m in msgs
    )


def has_real_sdr_message(outreach_id: str) -> bool:
    """Has anything beyond the invitation note gone out on this thread?"""
    return last_real_sdr_message_ts(outreach_id) > 0


def last_real_sdr_message_ts(outreach_id: str) -> int:
    """Latest SDR timestamp that is not an invitation note, or 0.

    Invitation notes ride the connection request and land in the thread
    only when the prospect accepts. They are not a chat DM, so they must
    not start the 24h conversation clock — same rule as send_followup.
    """
    if not outreach_id:
        return 0
    db = get_db()
    row = db.execute(
        "SELECT invited_at, accepted_at FROM outreaches WHERE id = ?", (outreach_id,),
    ).fetchone()
    if not row:
        db.close()
        return 0
    msgs = db.execute(
        "SELECT text, format, timestamp FROM messages "
        "WHERE outreach_id = ? AND role = 'sdr' AND deleted_at IS NULL",
        (outreach_id,),
    ).fetchall()
    db.close()
    real = [
        int(m["timestamp"] or 0)
        for m in msgs
        if m["timestamp"] and not _is_invite_note(m, row["invited_at"], row["accepted_at"])
    ]
    return max(real) if real else 0


def count_real_sdr_messages(outreach_id: str) -> int:
    """Count chat DMs, excluding invitation notes."""
    if not outreach_id:
        return 0
    db = get_db()
    row = db.execute(
        "SELECT invited_at, accepted_at FROM outreaches WHERE id = ?", (outreach_id,),
    ).fetchone()
    if not row:
        db.close()
        return 0
    msgs = db.execute(
        "SELECT format, timestamp FROM messages "
        "WHERE outreach_id = ? AND role = 'sdr' AND deleted_at IS NULL",
        (outreach_id,),
    ).fetchall()
    db.close()
    return sum(
        1 for m in msgs
        if not _is_invite_note(m, row["invited_at"], row["accepted_at"])
    )


def recover_stuck_sending_outreaches(timeout_seconds: int = 600) -> int:
    """Recover outreaches stuck in 'sending' or 'sending_followup' state.

    If an outreach has been in a transitional send state for longer than
    timeout_seconds (default 10 min), it's assumed the send process crashed.
    Transitions to 'messaged' if a message was actually sent; to 'connected'
    only on acceptance evidence — accepted_at set, or a verified row in the
    connections table (accepted_at is honestly NULL for discovered
    connections, and demoting those to 'pending' re-invites someone who
    already accepted); otherwise back to 'pending' so the send is retried.
    A message-less, never-accepted claim is an invitation that never went
    out — rewriting it as 'connected' fabricates a connection, blocks every
    retried invite at the pre-exec status guard, and routes the planner
    into DMs that fail the 1st-degree pre-check.

    The invitation note is itself stored as a role='sdr' message, so "any sdr
    message" is not evidence of a DM: a row claimed from 'invited' (InMail
    escalation claims those) that crashed came back as 'messaged', skipping
    'connected' and hiding the row from pending-invitation logic keyed on
    'invited'. Invite notes are excluded here — see _is_invite_note.

    Returns total number of recovered outreaches.
    """
    cutoff = int(time.time()) - timeout_seconds
    db = get_db()
    # Find stuck outreaches
    stuck = db.execute(
        """SELECT o.id, o.status, o.invited_at, o.accepted_at,
                  c.linkedin_id, c.linkedin_url, c.profile_json
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           WHERE o.status IN ('sending', 'sending_followup')
             AND o.updated_at < ?""",
        (cutoff,),
    ).fetchall()
    if not stuck:
        db.close()
        return 0

    recovered = 0
    now = int(time.time())
    for row in stuck:
        oid = row["id"]
        # Check if a message was actually sent — invitation notes don't count
        sdr_msgs = db.execute(
            "SELECT format, timestamp FROM messages WHERE outreach_id = ? AND role = 'sdr'",
            (oid,),
        ).fetchall()
        notes = [m for m in sdr_msgs if _is_invite_note(m, row["invited_at"], row["accepted_at"])]
        has_dm = len(notes) < len(sdr_msgs)
        invited_sent = db.execute(
            """SELECT 1 FROM actions_log
               WHERE outreach_id = ? AND action_type = 'invitation_sent'
               LIMIT 1""",
            (oid,),
        ).fetchone()
        if has_dm:
            new_status = "messaged"
        elif row["accepted_at"] or _has_verified_connection(db, row):
            new_status = "connected"
        elif notes or row["invited_at"] or invited_sent:
            # The invitation went out; only the follow-on send died. 'pending'
            # would re-invite someone who already has a live invitation.
            new_status = "invited"
        else:
            new_status = "pending"
        db.execute(
            "UPDATE outreaches SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, now, oid),
        )
        recovered += 1
    db.commit()
    db.close()
    return recovered


def _has_verified_connection(db: Any, contact: Any) -> bool:
    """Is this contact actually in the synced connections table?

    Matches the same three identifiers _sync_silent_connections uses:
    provider_id from profile_json, the linkedin_id slug, and the public_id
    embedded in linkedin_url.
    """
    candidates: set[str] = set()
    if contact["linkedin_id"]:
        candidates.add(str(contact["linkedin_id"]))
    url = contact["linkedin_url"] or ""
    if "/in/" in url:
        candidates.add(url.split("/in/")[-1].strip("/"))
    try:
        profile = json.loads(contact["profile_json"] or "{}")
        provider_id = str(profile.get("provider_id") or "")
        if provider_id:
            candidates.add(provider_id)
    except (json.JSONDecodeError, TypeError):
        pass
    if not candidates:
        return False
    placeholders = ",".join("?" for _ in candidates)
    row = db.execute(
        f"""SELECT 1 FROM connections
            WHERE removed_at IS NULL
              AND ((provider_id != '' AND provider_id IN ({placeholders}))
                   OR (public_id != '' AND public_id IN ({placeholders})))
            LIMIT 1""",
        (*candidates, *candidates),
    ).fetchone()
    return row is not None


def repair_phantom_replies() -> int:
    """Revert 'replied'/'hot_lead' outreaches whose reply answers nothing we sent.

    A reply is only real if a prospect message arrived after our first
    outbound touch (invited_at, an invitation_sent action, or an sdr
    message). Rows that fail that test are phantoms: old inbox threads that
    reply detection swept up (the 18 Aug 2026 refill incident).

    The revert target is decided from evidence, never assumed — the one-off
    repair that put phantoms back to 'connected' sent the planner's
    only_connected DM pass after 13 never-invited prospects, and every send
    died on the 1st-degree pre-check. An sdr message on file → 'messaged';
    a verified row in connections → 'connected'; an invitation_sent action
    → 'invited'; no outbound touch of any kind → 'pending'. Timestamps that
    only the phantom pipeline could have written are cleared; a NULL means
    it did not happen.

    Returns the number of reverted outreaches.
    """
    db = get_db()
    now = int(time.time())
    rows = db.execute(
        """SELECT o.id, o.status, o.invited_at, o.accepted_at,
                  c.linkedin_id, c.linkedin_url, c.profile_json
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           WHERE o.status IN ('replied', 'hot_lead')"""
    ).fetchall()

    repaired = 0
    for row in rows:
        oid = row["id"]
        first_invite = db.execute(
            """SELECT MIN(timestamp) AS ts FROM actions_log
               WHERE outreach_id = ? AND action_type = 'invitation_sent'""",
            (oid,),
        ).fetchone()["ts"]
        first_sdr = db.execute(
            """SELECT MIN(timestamp) AS ts FROM messages
               WHERE outreach_id = ? AND role = 'sdr' AND deleted_at IS NULL""",
            (oid,),
        ).fetchone()["ts"]

        touches = [t for t in (row["invited_at"], first_invite, first_sdr) if t]
        if touches:
            answered = db.execute(
                """SELECT 1 FROM messages
                   WHERE outreach_id = ? AND role = 'prospect'
                     AND deleted_at IS NULL AND timestamp >= ?
                   LIMIT 1""",
                (oid, min(touches)),
            ).fetchone()
            if answered:
                continue

        # Acceptance evidence: a row in the synced connections table, or an
        # acceptance we observed and logged. The table alone is not enough —
        # sync can miss or prune on partial API responses, and a logged
        # connection_accepted is no less real for having aged out of it.
        connected = _has_verified_connection(db, row) or db.execute(
            """SELECT 1 FROM actions_log
               WHERE outreach_id = ? AND action_type IN
                     ('connection_accepted', 'silent_connection_detected')
               LIMIT 1""",
            (oid,),
        ).fetchone() is not None
        if first_sdr:
            new_status = "messaged"
        elif connected:
            new_status = "connected"
        elif first_invite:
            new_status = "invited"
        else:
            new_status = "pending"
        invited_at = (row["invited_at"] or first_invite) if first_invite else None
        accepted_at = row["accepted_at"] if connected else None
        db.execute(
            """UPDATE outreaches
               SET status = ?, invited_at = ?, accepted_at = ?,
                   first_reply_at = NULL, updated_at = ?
               WHERE id = ?""",
            (new_status, invited_at, accepted_at, now, oid),
        )
        db.execute(
            """INSERT INTO actions_log (id, outreach_id, action_type, result, details_json, timestamp)
               VALUES (?, ?, 'phantom_reply_repair', 'reverted', ?, ?)""",
            (str(uuid.uuid4()), oid,
             json.dumps({
                 "from_status": row["status"],
                 "to_status": new_status,
                 "reason": "no prospect message answers any outbound touch",
                 "evidence": {
                     "invitation_sent": bool(first_invite),
                     "sdr_message": bool(first_sdr),
                     "verified_connection": connected,
                 },
             }),
             now),
        )
        repaired += 1
    db.commit()
    db.close()
    return repaired


def get_scheduler_stats() -> dict:
    """Get scheduler job stats for the dashboard."""
    db = get_db()
    rows = db.execute(
        """SELECT status, job_type, COUNT(*) as cnt
           FROM scheduler_jobs
           GROUP BY status, job_type"""
    ).fetchall()

    # Next scheduled jobs
    next_jobs = db.execute(
        """SELECT job_type, scheduled_at, outreach_id
           FROM scheduler_jobs
           WHERE status = 'pending'
           ORDER BY scheduled_at ASC
           LIMIT 5"""
    ).fetchall()

    # Recent completed/failed
    recent = db.execute(
        """SELECT job_type, status, completed_at, error
           FROM scheduler_jobs
           WHERE status IN ('completed', 'failed')
           ORDER BY completed_at DESC
           LIMIT 10"""
    ).fetchall()

    db.close()
    return {
        "counts": [dict(r) for r in rows],
        "next_jobs": [dict(r) for r in next_jobs],
        "recent": [dict(r) for r in recent],
    }


# ──────────────────────────────────────────────
# Scheduler Events (structured observability log)
# ──────────────────────────────────────────────


def log_scheduler_event(
    event_type: str,
    *,
    campaign_id: Optional[str] = None,
    outreach_id: Optional[str] = None,
    job_id: Optional[str] = None,
    context: Optional[dict] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """Fire-and-forget INSERT into scheduler_events. Never raises."""
    try:
        db = get_db()
        db.execute(
            """INSERT INTO scheduler_events
               (id, event_type, campaign_id, outreach_id, job_id, context, duration_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                event_type,
                campaign_id,
                outreach_id,
                job_id,
                json.dumps(context or {}),
                duration_ms,
                int(time.time()),
            ),
        )
        db.commit()
        db.close()
    except Exception as e:
        logger.warning("log_scheduler_event FAILED for %s: %s", event_type, e)


def get_recent_scheduler_events(
    hours: int = 24,
    event_type: str = "",
    campaign_id: str = "",
    limit: int = 100,
) -> list[dict]:
    """Query recent scheduler events, newest first."""
    db = get_db()
    since = int(time.time()) - (hours * 3600)
    sql = "SELECT * FROM scheduler_events WHERE created_at >= ?"
    params: list[Any] = [since]

    if event_type:
        sql += " AND event_type = ?"
        params.append(event_type)
    if campaign_id:
        sql += " AND campaign_id = ?"
        params.append(campaign_id)

    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(min(limit, 500))

    rows = db.execute(sql, params).fetchall()
    db.close()

    events = []
    for r in rows:
        evt = dict(r)
        ctx = evt.get("context", "{}")
        if isinstance(ctx, str):
            try:
                evt["context"] = json.loads(ctx)
            except (json.JSONDecodeError, TypeError):
                pass
        events.append(evt)
    return events


def get_scheduler_event_summary(hours: int = 24) -> dict[str, int]:
    """GROUP BY event_type -> {type: count} for the last N hours."""
    db = get_db()
    since = int(time.time()) - (hours * 3600)
    rows = db.execute(
        """SELECT event_type, COUNT(*) as cnt FROM scheduler_events
           WHERE created_at >= ?
           GROUP BY event_type ORDER BY cnt DESC""",
        (since,),
    ).fetchall()
    db.close()
    return {dict(r)["event_type"]: dict(r)["cnt"] for r in rows}


def get_job_metrics(hours: int = 24) -> dict[str, dict]:
    """Rolling window metrics per job type from scheduler_events.

    Returns {job_type: {total, success, skipped, deferred, permanent_failure,
    failed, success_rate, avg_duration_ms}}.

    Event types:
    - job_completed  → actual success (action was performed)
    - job_skipped    → pre-check blocked it (status changed, not connected, etc.)
    - job_deferred   → budget exhausted / will retry later
    - job_permanent_failure → action failed and won't be retried
    - job_failed     → transient error (exception), may be retried
    """
    db = get_db()
    since = int(time.time()) - (hours * 3600)
    rows = db.execute(
        """SELECT
               json_extract(context, '$.job_type') as jtype,
               event_type,
               COUNT(*) as cnt,
               AVG(duration_ms) as avg_dur
           FROM scheduler_events
           WHERE event_type IN ('job_completed', 'job_failed',
                                'job_skipped', 'job_deferred', 'job_permanent_failure')
             AND created_at >= ?
           GROUP BY jtype, event_type""",
        (since,),
    ).fetchall()
    db.close()

    _EMPTY = {"total": 0, "success": 0, "skipped": 0, "deferred": 0,
              "permanent_failure": 0, "failed": 0, "avg_duration_ms": 0}
    metrics: dict[str, dict] = {}
    for r in rows:
        row = dict(r)
        jtype = row.get("jtype") or "unknown"
        if jtype not in metrics:
            metrics[jtype] = dict(_EMPTY)
        cnt = row["cnt"]
        evt = row["event_type"]
        if evt == "job_completed":
            metrics[jtype]["success"] += cnt
            metrics[jtype]["avg_duration_ms"] = int(row["avg_dur"] or 0)
        elif evt == "job_skipped":
            metrics[jtype]["skipped"] += cnt
        elif evt == "job_deferred":
            metrics[jtype]["deferred"] += cnt
        elif evt == "job_permanent_failure":
            metrics[jtype]["permanent_failure"] += cnt
        else:
            metrics[jtype]["failed"] += cnt
        metrics[jtype]["total"] += cnt

    # Compute success rates (success / total that actually attempted)
    for m in metrics.values():
        attempted = m["total"] - m["skipped"] - m["deferred"]
        if attempted > 0:
            m["success_rate"] = round(m["success"] / attempted * 100, 1)
        else:
            m["success_rate"] = 0.0

    return metrics


def cleanup_scheduler_events(days: int = 7) -> int:
    """Delete events older than retention period. Returns count deleted."""
    db = get_db()
    cutoff = int(time.time()) - (days * 86400)
    cursor = db.execute(
        "DELETE FROM scheduler_events WHERE created_at < ?", (cutoff,),
    )
    db.commit()
    deleted = cursor.rowcount
    db.close()
    return deleted


# ──────────────────────────────────────────────
# Experiments (PM hypothesis tracking)
# ──────────────────────────────────────────────


def save_experiment(
    snapshot: str, result_json: str, campaign_ids: str = "",
) -> str:
    """Save an experiment analysis result. Returns experiment ID."""
    exp_id = str(uuid.uuid4())
    db = get_db()
    db.execute(
        """INSERT INTO experiments (id, snapshot, result_json, campaign_ids, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (exp_id, snapshot, result_json, campaign_ids, int(time.time())),
    )
    db.commit()
    db.close()
    return exp_id


def list_experiments(limit: int = 5) -> list[dict]:
    """List recent experiments, newest first."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM experiments ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def update_experiment_status(exp_id: str, status: str) -> None:
    """Update experiment status: pending/tested/validated/dismissed."""
    db = get_db()
    db.execute(
        "UPDATE experiments SET status = ? WHERE id = ?",
        (status, exp_id),
    )
    db.commit()
    db.close()


def get_deal_profiles(
    campaign_id: str, status: str, limit: int = 3,
) -> list[dict]:
    """Get contact profiles for won or lost deals.

    Args:
        campaign_id: Campaign to query.
        status: 'closed_happy' or 'closed_unhappy'.
        limit: Max profiles to return.
    """
    db = get_db()
    rows = db.execute(
        """SELECT c.name, c.title, c.company, c.fit_score, o.status
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           WHERE o.campaign_id = ? AND o.status = ?
           ORDER BY o.updated_at DESC LIMIT ?""",
        (campaign_id, status, limit),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────
# A/B Tests
# ──────────────────────────────────────────────


def create_ab_test(
    campaign_id: str,
    name: str,
    variant_a: str,
    variant_b: str,
    hypothesis: str = "",
    test_type: str = "message",
) -> str:
    """Create a new A/B test for a campaign. Returns test ID.

    ``test_type`` is ``"message"`` (default) or ``"headline"``.
    """
    test_id = str(uuid.uuid4())
    db = get_db()
    db.execute(
        """INSERT INTO ab_tests (id, campaign_id, name, hypothesis, variant_a, variant_b, test_type)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (test_id, campaign_id, name, hypothesis, variant_a, variant_b, test_type),
    )
    db.commit()
    db.close()
    return test_id


def get_ab_test(test_id: str) -> dict | None:
    """Get an A/B test by ID."""
    db = get_db()
    row = db.execute("SELECT * FROM ab_tests WHERE id = ?", (test_id,)).fetchone()
    db.close()
    return dict(row) if row else None


def list_ab_tests(campaign_id: str = "", status: str = "") -> list[dict]:
    """List A/B tests, optionally filtered by campaign and/or status."""
    db = get_db()
    q = "SELECT * FROM ab_tests"
    params: list = []
    clauses: list[str] = []
    if campaign_id:
        clauses.append("campaign_id = ?")
        params.append(campaign_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY created_at DESC"
    rows = db.execute(q, params).fetchall()
    db.close()
    return [dict(r) for r in rows]


def complete_ab_test(
    test_id: str,
    winner: str,
    result_json: str = "",
) -> None:
    """Mark an A/B test as completed with a winner."""
    db = get_db()
    db.execute(
        """UPDATE ab_tests
           SET status = 'completed', winner = ?, result_json = ?,
               completed_at = strftime('%s', 'now')
           WHERE id = ?""",
        (winner, result_json, test_id),
    )
    db.commit()
    db.close()


def get_variant_stats(campaign_id: str) -> dict:
    """Get per-variant funnel stats for A/B testing.

    Returns {"A": {invited, connected, replied, ...}, "B": {...}, None: {...}}.
    """
    db = get_db()
    rows = db.execute(
        """SELECT variant,
                  COUNT(*) as total,
                  SUM(CASE WHEN status NOT IN ('pending', 'skipped') THEN 1 ELSE 0 END) as invited,
                  SUM(CASE WHEN status IN ('connected','messaged','replied','hot_lead',
                       'closed_happy','closed_unhappy') THEN 1 ELSE 0 END) as connected,
                  SUM(CASE WHEN status IN ('replied','hot_lead',
                       'closed_happy','closed_unhappy') THEN 1 ELSE 0 END) as replied,
                  SUM(CASE WHEN status = 'hot_lead' THEN 1 ELSE 0 END) as hot_leads,
                  SUM(CASE WHEN status = 'closed_happy' THEN 1 ELSE 0 END) as won,
                  SUM(CASE WHEN status = 'closed_unhappy' THEN 1 ELSE 0 END) as lost
           FROM outreaches
           WHERE campaign_id = ?
           GROUP BY variant""",
        (campaign_id,),
    ).fetchall()
    db.close()
    result = {}
    for r in rows:
        d = dict(r)
        variant = d.pop("variant")
        invited = d.get("invited", 0)
        connected = d.get("connected", 0)
        d["acceptance_rate"] = round(connected / invited * 100, 1) if invited else 0
        d["reply_rate"] = round(d.get("replied", 0) / connected * 100, 1) if connected else 0
        result[variant] = d
    return result


def get_headline_variant_stats(campaign_id: str) -> dict:
    """Per-headline-variant funnel stats. Does not read outreaches.variant."""
    db = get_db()
    rows = db.execute(
        """SELECT headline_variant,
                  COUNT(*) as total,
                  SUM(CASE WHEN status NOT IN ('pending', 'skipped') THEN 1 ELSE 0 END) as invited,
                  SUM(CASE WHEN status IN ('connected','messaged','replied','hot_lead',
                       'closed_happy','closed_unhappy') THEN 1 ELSE 0 END) as connected,
                  SUM(CASE WHEN status IN ('replied','hot_lead',
                       'closed_happy','closed_unhappy') THEN 1 ELSE 0 END) as replied,
                  SUM(CASE WHEN status = 'hot_lead' THEN 1 ELSE 0 END) as hot_leads,
                  SUM(CASE WHEN status = 'closed_happy' THEN 1 ELSE 0 END) as won,
                  SUM(CASE WHEN status = 'closed_unhappy' THEN 1 ELSE 0 END) as lost
           FROM outreaches
           WHERE campaign_id = ? AND headline_variant IS NOT NULL
           GROUP BY headline_variant""",
        (campaign_id,),
    ).fetchall()
    db.close()
    result = {}
    for r in rows:
        d = dict(r)
        variant = d.pop("headline_variant")
        invited = d.get("invited", 0)
        connected = d.get("connected", 0)
        d["acceptance_rate"] = round(connected / invited * 100, 1) if invited else 0
        d["reply_rate"] = round(d.get("replied", 0) / connected * 100, 1) if connected else 0
        result[variant] = d
    return result


def has_running_message_ab_test(campaign_id: str) -> bool:
    """True only when a running *message* A/B test exists (not headline)."""
    tests = list_ab_tests(campaign_id, status="running")
    return any((t.get("test_type") or "message") != "headline" for t in tests)


def has_running_headline_test(campaign_id: str = "") -> bool:
    tests = list_ab_tests(campaign_id, status="running") if campaign_id else list_ab_tests(status="running")
    return any(t.get("test_type") == "headline" for t in tests)


def cancel_ab_test(test_id: str) -> None:
    db = get_db()
    db.execute(
        """UPDATE ab_tests
           SET status = 'cancelled', completed_at = strftime('%s', 'now')
           WHERE id = ? AND status = 'running'""",
        (test_id,),
    )
    db.commit()
    db.close()


def assign_variant(campaign_id: str) -> str:
    """Assign the next prospect to variant A or B (round-robin).

    Counts current variant distribution and assigns to the underrepresented one.
    Returns 'A' or 'B'.
    """
    db = get_db()
    rows = db.execute(
        """SELECT variant, COUNT(*) as cnt
           FROM outreaches
           WHERE campaign_id = ? AND variant IS NOT NULL
           GROUP BY variant""",
        (campaign_id,),
    ).fetchall()
    db.close()
    counts = {dict(r)["variant"]: dict(r)["cnt"] for r in rows}
    a_count = counts.get("A", 0)
    b_count = counts.get("B", 0)
    return "A" if a_count <= b_count else "B"


# ──────────────────────────────────────────────
# Cohort Analysis (Feature 4.3)
# ──────────────────────────────────────────────


def get_cohort_analysis(campaign_id: str) -> list[dict]:
    """Group outreaches by invitation week and compute per-cohort funnel stats.

    Returns a list of cohort dicts sorted by week:
    [{"cohort": "2026-W08", "invited": 15, "connected": 8, ...}]
    """
    db = get_db()
    rows = db.execute(
        """SELECT
               CASE
                   WHEN invited_at IS NOT NULL
                   THEN strftime('%Y-W%W', invited_at, 'unixepoch')
                   ELSE 'Not invited'
               END as cohort,
               COUNT(*) as total,
               SUM(CASE WHEN status IN ('invited','connected','replied','hot_lead',
                    'messaged','closed_happy','closed_unhappy','opted_out','reverse_pitch')
                    THEN 1 ELSE 0 END) as invited,
               SUM(CASE WHEN status IN ('connected','replied','hot_lead',
                    'messaged','closed_happy','closed_unhappy','reverse_pitch','opted_out')
                    THEN 1 ELSE 0 END) as connected,
               SUM(CASE WHEN status IN ('replied','hot_lead','closed_happy','closed_unhappy','reverse_pitch','opted_out')
                    THEN 1 ELSE 0 END) as replied,
               SUM(CASE WHEN status = 'hot_lead' THEN 1 ELSE 0 END) as hot_lead,
               SUM(CASE WHEN status = 'closed_happy' THEN 1 ELSE 0 END) as won,
               SUM(CASE WHEN status = 'closed_unhappy' THEN 1 ELSE 0 END) as lost
           FROM outreaches
           WHERE campaign_id = ?
           GROUP BY cohort
           ORDER BY cohort""",
        (campaign_id,),
    ).fetchall()
    db.close()

    cohorts = []
    for r in rows:
        d = dict(r)
        invited = d.get("invited", 0)
        connected = d.get("connected", 0)
        d["acceptance_rate"] = round(connected / invited * 100, 1) if invited else 0
        d["reply_rate"] = round(d.get("replied", 0) / connected * 100, 1) if connected else 0
        cohorts.append(d)
    return cohorts


def get_time_series_stats(campaign_id: str) -> list[dict]:
    """Get daily activity counts for a campaign (last 30 days).

    Returns: [{"date": "2026-02-24", "invites": 3, "replies": 1, "connections": 2}]
    """
    db = get_db()
    cutoff = int(time.time()) - (30 * 86400)

    rows = db.execute(
        """SELECT
               date(created_at, 'unixepoch') as date,
               SUM(CASE WHEN status NOT IN ('pending', 'skipped') THEN 1 ELSE 0 END) as invites,
               SUM(CASE WHEN status IN ('connected','replied','hot_lead',
                    'messaged','closed_happy','closed_unhappy','reverse_pitch','opted_out')
                    THEN 1 ELSE 0 END) as connections,
               SUM(CASE WHEN status IN ('replied','hot_lead','closed_happy','closed_unhappy','reverse_pitch','opted_out')
                    THEN 1 ELSE 0 END) as replies
           FROM outreaches
           WHERE campaign_id = ? AND created_at > ?
           GROUP BY date
           ORDER BY date""",
        (campaign_id, cutoff),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_full_campaign_export(campaign_id: str) -> list[dict]:
    """Get all contacts with outreach data for export.

    Returns a list of dicts with contact + outreach fields for CSV/JSON export.
    """
    db = get_db()
    rows = db.execute(
        """SELECT
               c.name, c.title, c.company, c.linkedin_url, c.linkedin_id,
               c.fit_score,
               o.status, o.channel, o.followup_count, o.variant,
               o.invited_at, o.accepted_at, o.first_reply_at,
               o.outcome_json, o.created_at as outreach_created_at
           FROM outreaches o
           JOIN contacts c ON c.id = o.contact_id
           WHERE o.campaign_id = ?
           ORDER BY o.created_at""",
        (campaign_id,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────
# CRM Mappings (Feature 4.4)
# ──────────────────────────────────────────────


def get_crm_mapping(contact_id: str, crm_type: str = "hubspot") -> dict | None:
    """Get an existing CRM mapping for a contact."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM crm_mappings WHERE contact_id = ? AND crm_type = ?",
        (contact_id, crm_type),
    ).fetchone()
    db.close()
    return dict(row) if row else None


def save_crm_mapping(
    contact_id: str,
    crm_type: str = "hubspot",
    crm_contact_id: str = "",
    crm_deal_id: str = "",
) -> str:
    """Create or update a CRM mapping."""
    now = int(time.time())
    existing = get_crm_mapping(contact_id, crm_type)
    db = get_db()
    if existing:
        updates = []
        params: list[Any] = []
        if crm_contact_id:
            updates.append("crm_contact_id = ?")
            params.append(crm_contact_id)
        if crm_deal_id:
            updates.append("crm_deal_id = ?")
            params.append(crm_deal_id)
        updates.append("synced_at = ?")
        params.append(now)
        params.append(existing["id"])
        db.execute(f"UPDATE crm_mappings SET {', '.join(updates)} WHERE id = ?", params)
        db.commit()
        db.close()
        return existing["id"]
    else:
        mapping_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO crm_mappings (id, contact_id, crm_type, crm_contact_id, crm_deal_id, synced_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (mapping_id, contact_id, crm_type, crm_contact_id, crm_deal_id, now, now),
        )
        db.commit()
        db.close()
        return mapping_id


def get_unsynced_won_outreaches(campaign_id: str) -> list[dict]:
    """Get won outreaches that haven't been synced to CRM yet."""
    db = get_db()
    rows = db.execute(
        """SELECT o.id as outreach_id, o.contact_id, o.outcome_json,
                  c.name, c.title, c.company, c.linkedin_url, c.linkedin_id
           FROM outreaches o
           JOIN contacts c ON c.id = o.contact_id
           LEFT JOIN crm_mappings m ON m.contact_id = c.id AND m.crm_type = 'hubspot'
           WHERE o.campaign_id = ? AND o.status = 'closed_happy'
             AND m.id IS NULL
           ORDER BY o.updated_at DESC""",
        (campaign_id,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_hot_lead_outreaches(campaign_id: str) -> list[dict]:
    """Get hot lead outreaches for CRM sync."""
    db = get_db()
    rows = db.execute(
        """SELECT o.id as outreach_id, o.contact_id, o.outcome_json,
                  c.name, c.title, c.company, c.linkedin_url, c.linkedin_id
           FROM outreaches o
           JOIN contacts c ON c.id = o.contact_id
           WHERE o.campaign_id = ? AND o.status = 'hot_lead'
           ORDER BY o.updated_at DESC""",
        (campaign_id,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────
# Inbound Signals (Pipeline)
# ──────────────────────────────────────────────


def save_inbound_signal(
    signal_type: str,
    sender_name: str = "",
    sender_id: str = "",
    sender_headline: str = "",
    sender_company: str = "",
    sender_url: str = "",
    content: str = "",
    post_id: str = "",
    profile_json: str = "",
    invitation_id: str = "",
    message_id: str = "",
    sent_at: int | None = None,
) -> str:
    """Save a new inbound signal (invitation, message, or comment). Returns signal ID.

    ``sent_at`` is when the provider says it was SENT -- pass it whenever the
    provider gives one. ``created_at`` is when we noticed it, and a message
    stored at that time looks as fresh as the day we read it (timeutil.signal_sent_at).
    """
    signal_id = str(uuid.uuid4())
    db = get_db()
    db.execute(
        """INSERT INTO inbound_signals
           (id, signal_type, sender_name, sender_id, sender_headline,
            sender_company, sender_url, content, post_id, profile_json,
            invitation_id, message_id, created_at, sent_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (signal_id, signal_type, sender_name, sender_id, sender_headline,
         sender_company, sender_url, content, post_id, profile_json,
         invitation_id or None, message_id or None, int(time.time()),
         int(sent_at) if sent_at else None),
    )
    db.commit()
    db.close()
    return signal_id


def get_inbound_signal(signal_id: str) -> Optional[dict]:
    """Get an inbound signal by ID."""
    db = get_db()
    row = db.execute("SELECT * FROM inbound_signals WHERE id = ?", (signal_id,)).fetchone()
    db.close()
    return dict(row) if row else None


def list_inbound_signals(
    status: str = "",
    signal_type: str = "",
    limit: int = 50,
) -> list[dict]:
    """List inbound signals, optionally filtered by status and/or type."""
    db = get_db()
    q = "SELECT * FROM inbound_signals"
    params: list[Any] = []
    clauses: list[str] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if signal_type:
        clauses.append("signal_type = ?")
        params.append(signal_type)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = db.execute(q, params).fetchall()
    db.close()
    return [dict(r) for r in rows]


_VALID_INBOUND_SIGNAL_COLS = frozenset({
    "signal_type", "sender_name", "sender_id", "sender_headline",
    "sender_company", "sender_url", "content", "post_id", "profile_json",
    "intent", "matched_icp_id", "confidence", "recommended_action",
    "reasoning", "status", "campaign_id", "qualified_at", "outreach_id",
    "dm_attempts",
    # Inbound Pipeline v2:
    "invitation_id", "actioned_at", "decline_reason", "message_id",
    "reaction_sent",
})


def update_inbound_signal(signal_id: str, **kwargs: Any) -> None:
    """Update an inbound signal's fields."""
    bad_keys = set(kwargs) - _VALID_INBOUND_SIGNAL_COLS
    if bad_keys:
        raise ValueError(f"Invalid inbound_signal columns: {bad_keys}")
    if not kwargs:
        return
    db = get_db()
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [signal_id]
    db.execute(f"UPDATE inbound_signals SET {set_clause} WHERE id = ?", values)
    db.commit()
    db.close()


def get_inbound_signal_by_sender(
    sender_id: str,
    signal_type: str = "",
) -> Optional[dict]:
    """Find an existing inbound signal by sender_id (dedup check)."""
    db = get_db()
    if signal_type:
        row = db.execute(
            "SELECT * FROM inbound_signals WHERE sender_id = ? AND signal_type = ? ORDER BY created_at DESC LIMIT 1",
            (sender_id, signal_type),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT * FROM inbound_signals WHERE sender_id = ? ORDER BY created_at DESC LIMIT 1",
            (sender_id,),
        ).fetchone()
    db.close()
    return dict(row) if row else None


def count_inbound_signals(status: str = "", signal_type: str = "") -> int:
    """Count inbound signals, optionally filtered."""
    db = get_db()
    q = "SELECT COUNT(*) as c FROM inbound_signals"
    params: list[Any] = []
    clauses: list[str] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if signal_type:
        clauses.append("signal_type = ?")
        params.append(signal_type)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    row = db.execute(q, params).fetchone()
    db.close()
    return row["c"] if row else 0


def count_inbound_dms_today() -> int:
    """Count discovery DMs sent today using the actions_log."""
    today_start = _local_day_start()
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) as c FROM actions_log "
        "WHERE action_type = 'inbound_discovery_dm_sent' AND timestamp >= ?",
        (today_start,),
    ).fetchone()
    db.close()
    return row["c"] if row else 0


def get_inbound_funnel_stats() -> dict:
    """Get inbound pipeline funnel stats grouped by status and intent."""
    db = get_db()
    status_rows = db.execute(
        "SELECT status, COUNT(*) as c FROM inbound_signals GROUP BY status"
    ).fetchall()
    intent_rows = db.execute(
        "SELECT intent, COUNT(*) as c FROM inbound_signals WHERE intent IS NOT NULL GROUP BY intent"
    ).fetchall()
    type_rows = db.execute(
        "SELECT signal_type, COUNT(*) as c FROM inbound_signals GROUP BY signal_type"
    ).fetchall()
    db.close()
    return {
        "by_status": {r["status"]: r["c"] for r in status_rows},
        "by_intent": {r["intent"]: r["c"] for r in intent_rows},
        "by_type": {r["signal_type"]: r["c"] for r in type_rows},
        "total": sum(r["c"] for r in status_rows),
    }


# ──────────────────────────────────────────────
# Published Posts (for inbound comment monitoring)
# ──────────────────────────────────────────────


def save_published_post(
    post_id: str,
    text: str = "",
    topic: str = "",
) -> str:
    """Save a published post for comment monitoring. Returns record ID."""
    record_id = str(uuid.uuid4())
    db = get_db()
    db.execute(
        """INSERT INTO published_posts (id, post_id, text, topic, published_at)
           VALUES (?, ?, ?, ?, ?)""",
        (record_id, post_id, text, topic, int(time.time())),
    )
    db.commit()
    db.close()
    return record_id


def get_published_post_ids() -> set[str]:
    """Every post id we published ourselves.

    Few-shotting the writer on its own output teaches it to imitate itself,
    so the voice examples subtract this set.
    """
    db = get_db()
    rows = db.execute("SELECT post_id FROM published_posts").fetchall()
    db.close()
    return {r["post_id"] for r in rows if r["post_id"]}


def list_published_posts(days: int = 7) -> list[dict]:
    """List recently published posts that need comment monitoring."""
    cutoff = int(time.time()) - (days * 86400)
    db = get_db()
    rows = db.execute(
        "SELECT * FROM published_posts WHERE published_at >= ? ORDER BY published_at DESC",
        (cutoff,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_voice_memo_stats(campaign_id: str = "") -> dict:
    """Get voice memo statistics for analytics.

    Returns: {voice_sent, text_sent, voice_reply_rate, text_reply_rate}
    """
    db = get_db()
    if campaign_id:
        row = db.execute(
            """SELECT
                   SUM(CASE WHEN m.format = 'voice' THEN 1 ELSE 0 END) as voice_sent,
                   SUM(CASE WHEN m.format != 'voice' THEN 1 ELSE 0 END) as text_sent
               FROM messages m
               JOIN outreaches o ON m.outreach_id = o.id
               WHERE o.campaign_id = ? AND m.role = 'sdr'""",
            (campaign_id,),
        ).fetchone()
        # Reply rates per format
        voice_reply_row = db.execute(
            """SELECT COUNT(DISTINCT o.id) as cnt
               FROM outreaches o
               JOIN messages m ON m.outreach_id = o.id
               WHERE o.campaign_id = ?
                 AND o.status IN ('replied', 'hot_lead', 'closed_happy', 'closed_unhappy', 'reverse_pitch', 'opted_out')
                 AND m.format = 'voice' AND m.role = 'sdr'""",
            (campaign_id,),
        ).fetchone()
        text_reply_row = db.execute(
            """SELECT COUNT(DISTINCT o.id) as cnt
               FROM outreaches o
               JOIN messages m ON m.outreach_id = o.id
               WHERE o.campaign_id = ?
                 AND o.status IN ('replied', 'hot_lead', 'closed_happy', 'closed_unhappy', 'reverse_pitch', 'opted_out')
                 AND m.format != 'voice' AND m.role = 'sdr'""",
            (campaign_id,),
        ).fetchone()
        # Total outreaches that received voice vs text
        voice_total_row = db.execute(
            """SELECT COUNT(DISTINCT o.id) as cnt
               FROM outreaches o
               JOIN messages m ON m.outreach_id = o.id
               WHERE o.campaign_id = ? AND m.format = 'voice' AND m.role = 'sdr'""",
            (campaign_id,),
        ).fetchone()
        text_total_row = db.execute(
            """SELECT COUNT(DISTINCT o.id) as cnt
               FROM outreaches o
               JOIN messages m ON m.outreach_id = o.id
               WHERE o.campaign_id = ? AND m.format != 'voice' AND m.role = 'sdr'""",
            (campaign_id,),
        ).fetchone()
    else:
        row = db.execute(
            """SELECT
                   SUM(CASE WHEN format = 'voice' THEN 1 ELSE 0 END) as voice_sent,
                   SUM(CASE WHEN format != 'voice' THEN 1 ELSE 0 END) as text_sent
               FROM messages WHERE role = 'sdr'"""
        ).fetchone()
        voice_reply_row = db.execute(
            """SELECT COUNT(DISTINCT o.id) as cnt
               FROM outreaches o
               JOIN messages m ON m.outreach_id = o.id
               WHERE o.status IN ('replied', 'hot_lead', 'closed_happy', 'closed_unhappy', 'reverse_pitch', 'opted_out')
                 AND m.format = 'voice' AND m.role = 'sdr'"""
        ).fetchone()
        text_reply_row = db.execute(
            """SELECT COUNT(DISTINCT o.id) as cnt
               FROM outreaches o
               JOIN messages m ON m.outreach_id = o.id
               WHERE o.status IN ('replied', 'hot_lead', 'closed_happy', 'closed_unhappy', 'reverse_pitch', 'opted_out')
                 AND m.format != 'voice' AND m.role = 'sdr'"""
        ).fetchone()
        voice_total_row = db.execute(
            """SELECT COUNT(DISTINCT o.id) as cnt
               FROM outreaches o
               JOIN messages m ON m.outreach_id = o.id
               WHERE m.format = 'voice' AND m.role = 'sdr'"""
        ).fetchone()
        text_total_row = db.execute(
            """SELECT COUNT(DISTINCT o.id) as cnt
               FROM outreaches o
               JOIN messages m ON m.outreach_id = o.id
               WHERE m.format != 'voice' AND m.role = 'sdr'"""
        ).fetchone()
    db.close()

    voice_sent = (row["voice_sent"] if row and row["voice_sent"] else 0)
    text_sent = (row["text_sent"] if row and row["text_sent"] else 0)
    voice_replied = voice_reply_row["cnt"] if voice_reply_row else 0
    text_replied = text_reply_row["cnt"] if text_reply_row else 0
    voice_total = voice_total_row["cnt"] if voice_total_row else 0
    text_total = text_total_row["cnt"] if text_total_row else 0

    return {
        "voice_sent": voice_sent,
        "text_sent": text_sent,
        "voice_reply_rate": voice_replied / voice_total if voice_total > 0 else 0.0,
        "text_reply_rate": text_replied / text_total if text_total > 0 else 0.0,
        "voice_replied": voice_replied,
        "text_replied": text_replied,
        "voice_total_outreaches": voice_total,
        "text_total_outreaches": text_total,
    }


def get_daily_voice_memo_count() -> int:
    """Count voice memos sent today for rate limiting."""
    import datetime
    today = datetime.date.today()
    today_start = int(time.mktime(today.timetuple()))
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) as c FROM messages WHERE format = 'voice' AND timestamp >= ?",
        (today_start,),
    ).fetchone()
    db.close()
    return row["c"] if row else 0


def update_published_post(post_id: str, last_checked: int, comment_count: int) -> None:
    """Update a published post's monitoring state."""
    db = get_db()
    db.execute(
        "UPDATE published_posts SET last_checked = ?, comment_count = ? WHERE post_id = ?",
        (last_checked, comment_count, post_id),
    )
    db.commit()
    db.close()


# ──────────────────────────────────────────────
# Partner Follow-Up Tracking
# ──────────────────────────────────────────────


def create_partner_followup(
    name: str,
    company: str = "",
    email: str = "",
    context: str = "",
    next_followup_ts: int | None = None,
) -> str:
    """Create a new partner follow-up record. Returns the new ID."""
    import uuid

    partner_id = str(uuid.uuid4())[:8]
    now = int(time.time())
    if next_followup_ts is None:
        next_followup_ts = now + 86400  # default: tomorrow
    db = get_db()
    db.execute(
        """INSERT INTO partner_followups
           (id, name, company, email, context, status,
            followup_count, next_followup, last_contacted, notes,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'active', 0, ?, ?, '[]', ?, ?)""",
        (partner_id, name, company, email, context,
         next_followup_ts, now, now, now),
    )
    db.commit()
    db.close()
    return partner_id


def get_partner_followups(status: str = "active") -> list[dict]:
    """Return all partner follow-ups with the given status."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM partner_followups WHERE status = ? ORDER BY next_followup ASC",
        (status,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_partner_followup(partner_id: str) -> dict | None:
    """Return a single partner follow-up by ID."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM partner_followups WHERE id = ?",
        (partner_id,),
    ).fetchone()
    db.close()
    return dict(row) if row else None


def update_partner_followup(partner_id: str, **kwargs: Any) -> None:
    """Update one or more fields on a partner follow-up."""
    _valid = frozenset({
        "name", "company", "email", "context", "status",
        "followup_count", "next_followup", "last_contacted", "notes",
    })
    updates = {k: v for k, v in kwargs.items() if k in _valid}
    if not updates:
        return
    updates["updated_at"] = int(time.time())
    cols = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [partner_id]
    db = get_db()
    db.execute(f"UPDATE partner_followups SET {cols} WHERE id = ?", vals)
    db.commit()
    db.close()


def get_due_partner_reminders() -> list[dict]:
    """Return all active partner follow-ups whose next_followup <= now."""
    now = int(time.time())
    db = get_db()
    rows = db.execute(
        """SELECT * FROM partner_followups
           WHERE status = 'active' AND next_followup IS NOT NULL AND next_followup <= ?
           ORDER BY next_followup ASC""",
        (now,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def advance_partner_followup(partner_id: str) -> int | None:
    """Increment followup_count and compute the next follow-up date.

    Uses PARTNER_DEFAULT_SCHEDULE_DAYS for escalating cadence.
    Returns the new next_followup timestamp, or None if max reached.
    """
    from ..constants import PARTNER_DEFAULT_SCHEDULE_DAYS, PARTNER_MAX_AUTO_FOLLOWUPS

    record = get_partner_followup(partner_id)
    if not record:
        return None

    new_count = record["followup_count"] + 1
    now = int(time.time())

    if new_count >= PARTNER_MAX_AUTO_FOLLOWUPS:
        update_partner_followup(
            partner_id,
            followup_count=new_count,
            last_contacted=now,
            status="paused",
        )
        return None

    schedule_idx = min(new_count, len(PARTNER_DEFAULT_SCHEDULE_DAYS) - 1)
    next_days = PARTNER_DEFAULT_SCHEDULE_DAYS[schedule_idx]
    next_ts = now + next_days * 86400

    update_partner_followup(
        partner_id,
        followup_count=new_count,
        last_contacted=now,
        next_followup=next_ts,
    )
    return next_ts


# ──────────────────────────────────────────────
# Profile Change History
# ──────────────────────────────────────────────


def log_profile_change(
    field: str,
    old_value: str | None,
    new_value: str,
    source: str = "manual",
) -> str:
    """Log a LinkedIn profile change. Returns the change ID (8-char uuid)."""
    change_id = str(uuid.uuid4())[:8]
    db = get_db()
    db.execute(
        """INSERT INTO profile_changes (id, field, old_value, new_value, source, status, created_at)
           VALUES (?, ?, ?, ?, ?, 'applied', ?)""",
        (change_id, field, old_value, new_value, source, int(time.time())),
    )
    db.commit()
    db.close()
    return change_id


def get_profile_changes(field: str | None = None, limit: int = 20) -> list[dict]:
    """Return recent profile changes, optionally filtered by field."""
    db = get_db()
    if field:
        rows = db.execute(
            "SELECT * FROM profile_changes WHERE field = ? ORDER BY created_at DESC LIMIT ?",
            (field, limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM profile_changes ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_profile_change(change_id: str) -> dict | None:
    """Return a single profile change by ID."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM profile_changes WHERE id = ?",
        (change_id,),
    ).fetchone()
    db.close()
    return dict(row) if row else None


def mark_profile_change_reverted(change_id: str) -> None:
    """Mark a profile change as reverted."""
    db = get_db()
    db.execute(
        "UPDATE profile_changes SET status = 'reverted' WHERE id = ?",
        (change_id,),
    )
    db.commit()
    db.close()


# ──────────────────────────────────────────────
# Prospect Journey Timeline
# ──────────────────────────────────────────────

def get_prospect_timeline(outreach_id: str, days: int = 30) -> list[dict]:
    """Get chronological timeline of all events for an outreach.

    Merges: actions_log, engagements, scheduler_jobs, prospect_daily_plans.
    Returns sorted list of {event_type, action, timestamp, details, source}.
    """
    since = int(time.time()) - (days * 86400)
    db = get_db()

    # 1. Actions log
    actions = db.execute(
        """SELECT action_type, result, details_json, timestamp
           FROM actions_log
           WHERE outreach_id = ? AND timestamp >= ?
           ORDER BY timestamp""",
        (outreach_id, since),
    ).fetchall()

    # 2. Engagements
    engagements = db.execute(
        """SELECT action_type, status, text, post_id, verified_status, created_at
           FROM engagements
           WHERE outreach_id = ? AND created_at >= ?
           ORDER BY created_at""",
        (outreach_id, since),
    ).fetchall()

    # 3. Scheduler jobs
    jobs = db.execute(
        """SELECT job_type, status, scheduled_at, completed_at, duration_ms, error
           FROM scheduler_jobs
           WHERE outreach_id = ? AND scheduled_at >= ?
           ORDER BY scheduled_at""",
        (outreach_id, since),
    ).fetchall()

    # 4. Outreach status history (from outreaches table itself)
    outreach = db.execute(
        """SELECT status, invited_at, accepted_at, first_reply_at, created_at
           FROM outreaches WHERE id = ?""",
        (outreach_id,),
    ).fetchone()

    db.close()

    timeline: list[dict] = []

    # Add actions
    for a in actions:
        details = {}
        if a["details_json"]:
            try:
                details = json.loads(a["details_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        timeline.append({
            "event_type": "action",
            "action": a["action_type"],
            "timestamp": a["timestamp"],
            "details": f"{a['result'] or ''} {details.get('reason', '')}".strip(),
            "source": "actions_log",
        })

    # Add engagements
    for e in engagements:
        detail_parts = [e["action_type"]]
        if e["text"]:
            detail_parts.append(f'"{e["text"][:60]}..."' if len(e["text"] or "") > 60 else f'"{e["text"]}"')
        if e["verified_status"]:
            detail_parts.append(f"[{e['verified_status']}]")
        timeline.append({
            "event_type": "engagement",
            "action": e["action_type"],
            "timestamp": e["created_at"],
            "details": " ".join(detail_parts),
            "source": "engagements",
        })

    # Add jobs
    for j in jobs:
        status = j["status"]
        detail = f"{status}"
        if j["duration_ms"]:
            detail += f" ({j['duration_ms']}ms)"
        if j["error"]:
            detail += f" — {j['error'][:80]}"
        timeline.append({
            "event_type": "job",
            "action": j["job_type"],
            "timestamp": j["completed_at"] or j["scheduled_at"],
            "details": detail,
            "source": "scheduler_jobs",
        })

    # Add outreach milestones
    if outreach:
        o = dict(outreach)
        if o.get("created_at") and o["created_at"] >= since:
            timeline.append({
                "event_type": "milestone",
                "action": "prospect_added",
                "timestamp": o["created_at"],
                "details": f"Status: {o['status']}",
                "source": "outreaches",
            })
        if o.get("invited_at") and o["invited_at"] >= since:
            timeline.append({
                "event_type": "milestone",
                "action": "invited",
                "timestamp": o["invited_at"],
                "details": "Connection invitation sent",
                "source": "outreaches",
            })
        if o.get("accepted_at") and o["accepted_at"] >= since:
            timeline.append({
                "event_type": "milestone",
                "action": "accepted",
                "timestamp": o["accepted_at"],
                "details": "Connection accepted",
                "source": "outreaches",
            })
        if o.get("first_reply_at") and o["first_reply_at"] >= since:
            timeline.append({
                "event_type": "milestone",
                "action": "first_reply",
                "timestamp": o["first_reply_at"],
                "details": "Prospect replied for the first time",
                "source": "outreaches",
            })

    # Sort by timestamp
    timeline.sort(key=lambda x: x["timestamp"] or 0)
    return timeline


# ──────────────────────────────────────────────
# Metric Trend Data (for anomaly detection)
# ──────────────────────────────────────────────

def get_metric_trend_data(days: int = 14) -> dict[str, list[dict]]:
    """Get daily metric values for trend analysis.

    Returns per-day values for: acceptance_rate, reply_rate,
    avg_job_duration_ms, engagement_success_rate.
    """
    from datetime import date, timedelta

    db = get_db()
    result: dict[str, list[dict]] = {
        "acceptance_rate": [],
        "reply_rate": [],
        "avg_job_duration_ms": [],
        "engagement_success_rate": [],
    }

    for offset in range(days):
        d = date.today() - timedelta(days=days - 1 - offset)
        d_str = d.isoformat()
        day_start = int(
            __import__("datetime").datetime.combine(d, __import__("datetime").time.min).timestamp()
        )
        day_end = day_start + 86400

        # Acceptance rate: accepted today / invited in last 7 days
        accepted_today = db.execute(
            "SELECT COUNT(*) as c FROM outreaches WHERE accepted_at >= ? AND accepted_at < ?",
            (day_start, day_end),
        ).fetchone()["c"]
        # Mature invites: sent 7+ days before this day
        mature_cutoff = day_start - (7 * 86400)
        mature_invited = db.execute(
            "SELECT COUNT(*) as c FROM outreaches WHERE invited_at IS NOT NULL AND invited_at < ?",
            (mature_cutoff,),
        ).fetchone()["c"]
        acc_rate = round(accepted_today / mature_invited, 4) if mature_invited else None
        result["acceptance_rate"].append({"date": d_str, "value": acc_rate})

        # Reply rate: first_reply_at set today / connected prospects
        replied_today = db.execute(
            "SELECT COUNT(*) as c FROM outreaches WHERE first_reply_at >= ? AND first_reply_at < ?",
            (day_start, day_end),
        ).fetchone()["c"]
        connected_total = db.execute(
            "SELECT COUNT(*) as c FROM outreaches WHERE accepted_at IS NOT NULL AND accepted_at < ?",
            (day_start,),
        ).fetchone()["c"]
        rep_rate = round(replied_today / connected_total, 4) if connected_total else None
        result["reply_rate"].append({"date": d_str, "value": rep_rate})

        # Avg job duration
        avg_dur = db.execute(
            """SELECT AVG(duration_ms) as avg_ms FROM scheduler_jobs
               WHERE status = 'completed' AND completed_at >= ? AND completed_at < ?
               AND duration_ms IS NOT NULL""",
            (day_start, day_end),
        ).fetchone()["avg_ms"]
        result["avg_job_duration_ms"].append({
            "date": d_str,
            "value": round(avg_dur) if avg_dur else None,
        })

        # Engagement success rate
        total_eng = db.execute(
            "SELECT COUNT(*) as c FROM engagements WHERE created_at >= ? AND created_at < ?",
            (day_start, day_end),
        ).fetchone()["c"]
        verified_eng = db.execute(
            """SELECT COUNT(*) as c FROM engagements
               WHERE created_at >= ? AND created_at < ?
               AND verified_status IN ('verified', 'trust_api')""",
            (day_start, day_end),
        ).fetchone()["c"]
        eng_rate = round(verified_eng / total_eng, 4) if total_eng else None
        result["engagement_success_rate"].append({"date": d_str, "value": eng_rate})

    db.close()
    return result


# ──────────────────────────────────────────────
# Network post scanning — watching connections, not just campaign contacts
# ──────────────────────────────────────────────

# Headline fragments that mark someone as able to hire, refer, or open a door.
# A job search cares about who can create or fill a role, which is a different
# population from a sales campaign's ICP.
NETWORK_WATCH_KEYWORDS = (
    "founder", "co-founder", "cofounder", "ceo", "cto", "coo", "chief",
    "head of", "vp ", "vp,", "vice president", "director",
    "talent", "recruit", "hiring", "people ops", "partner",
)


def get_connections_to_scan(
    limit: int = 25,
    keywords: "list[str] | tuple[str, ...] | None" = None,
) -> list[dict]:
    """Pick which 1st-degree connections to scan for new posts.

    Scanning 16k connections per cycle is not affordable, so selection is
    prioritised by headline (people who can hire or refer) and rotated by
    last_scanned_at so coverage is even rather than repeatedly re-reading the
    same few.
    """
    words = [w.lower() for w in (keywords or NETWORK_WATCH_KEYWORDS)]
    if not words:
        return []

    clause = " OR ".join("LOWER(headline) LIKE ?" for _ in words)
    params: list = [f"%{w}%" for w in words]
    params.append(limit)

    db = get_db()
    rows = db.execute(
        f"""SELECT id, provider_id, public_id, name, headline, company, last_scanned_at,
                   COALESCE(watch_priority, 0) AS watch_priority
            FROM connections
            WHERE removed_at IS NULL
              AND provider_id IS NOT NULL AND provider_id != ''
              -- Being on the tier-1 watch list is sufficient on its own: several
              -- real targets ("Enterprise GTM", "Consulting | Ex-...") match no
              -- hiring keyword and were silently excluded before priority applied.
              AND (COALESCE(watch_priority, 0) = 1 OR ({clause}))
            ORDER BY COALESCE(watch_priority, 0) DESC,
                     (last_scanned_at IS NOT NULL),
                     COALESCE(last_scanned_at, 0) ASC
            LIMIT ?""",
        params,
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def mark_connection_scanned(provider_id: str, when: int | None = None) -> None:
    """Record that a connection's posts were just scanned, so rotation advances."""
    db = get_db()
    db.execute(
        "UPDATE connections SET last_scanned_at = ? WHERE provider_id = ?",
        (when if when is not None else int(time.time()), provider_id),
    )
    db.commit()
    db.close()


def set_connection_watch_priority(provider_id: str, priority: bool) -> None:
    """Put a connection on the tier-1 watch list, or take them off it.

    Tier-1 connections are scanned every cycle instead of rotating, so a
    time-sensitive post from someone who matters is seen within the hour.
    """
    db = get_db()
    db.execute(
        "UPDATE connections SET watch_priority = ? WHERE provider_id = ?",
        (1 if priority else 0, provider_id),
    )
    db.commit()
    db.close()
