"""Reset only the error rows a first DM would actually send.

``run_retry_failed`` has no scheduled caller, and reset-all would also
re-queue permanently unsendable rows and not-connected people. This pass
uses the same facts generate_and_send uses at send time: a classic DM id
and a local 1st-degree connection. Not-connected rows stay at error —
flipping them to pending would start an invitation, which is a different
action. Nothing here sends.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..constants import STATUS_ACTIVE
from ..db.queries import (
    get_campaign,
    get_error_outreaches,
    get_outreach_with_contact,
    update_outreach,
)
from ..linkedin import get_account_id
from ..tools.generate_send import _resolve_dm_provider_id
from .connection_sync import is_first_degree, is_first_degree_by_public_id

logger = logging.getLogger(__name__)


def retry_recoverable_errors() -> dict[str, Any]:
    """Move first-degree, addressable error rows back to ``connected``."""
    account_id = get_account_id() or ""
    report: dict[str, Any] = {"reset_to_connected": [], "left_at_error": []}

    for row in get_error_outreaches():
        campaign = get_campaign(row["campaign_id"])
        if not campaign or campaign.get("status") != STATUS_ACTIVE:
            continue

        full = get_outreach_with_contact(row["outreach_id"])
        if not full:
            continue

        try:
            profile = json.loads(full.get("profile_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            profile = {}
        if not isinstance(profile, dict):
            profile = {}

        prospect = {
            "linkedin_url": full.get("linkedin_url") or "",
            "linkedin_id": full.get("linkedin_id") or "",
        }
        provider_id = _resolve_dm_provider_id(prospect, profile)
        public_id = (
            str(profile.get("public_id") or prospect["linkedin_id"] or "")
        ).strip()

        first = False
        if account_id and provider_id and is_first_degree(account_id, provider_id):
            first = True
        elif account_id and public_id and is_first_degree_by_public_id(account_id, public_id):
            first = True

        entry = {
            "outreach_id": row["outreach_id"],
            "campaign_id": row["campaign_id"],
            "name": full.get("name", ""),
        }
        cache_miss_error = "Not in local 1st-degree" in str(
            full.get("last_attempt_error") or row.get("last_attempt_error") or ""
        )
        accepted = bool(full.get("accepted_at"))
        if accepted and cache_miss_error:
            first = True
        if not provider_id or not first:
            report["left_at_error"].append(entry)
            continue

        update_outreach(
            row["outreach_id"],
            status="connected",
            last_attempt_error=None,
        )
        report["reset_to_connected"].append(entry)

    # Also recover stuck 'sending' outreaches older than 30 minutes
    _recover_stuck_sending()

    logger.info(
        "Retry recoverable errors: reset=%d left=%d",
        len(report["reset_to_connected"]),
        len(report["left_at_error"]),
    )
    return report


def _recover_stuck_sending() -> int:
    """Recover outreaches stranded in 'sending' status (>30 min with no running job)."""
    from ..db.schema import get_db
    import time

    now = int(time.time())
    cutoff = now - 1800
    db = get_db()
    recovered = 0
    try:
        rows = db.execute(
            """SELECT o.id, o.campaign_id, o.contact_id, o.invited_at, o.accepted_at,
                      (SELECT COUNT(*) FROM messages m WHERE m.outreach_id = o.id AND m.role = 'sdr') as sdr_msg_count
               FROM outreaches o
               WHERE o.status = 'sending' AND o.updated_at < ?""",
            (cutoff,),
        ).fetchall()

        for r in rows:
            oid = r[0]
            sdr_count = r[5]
            accepted_at = r[4]
            invited_at = r[3]

            new_status = "messaged" if sdr_count > 0 else ("connected" if accepted_at else ("invited" if invited_at else "pending"))
            db.execute(
                "UPDATE outreaches SET status = ?, updated_at = ?, last_attempt_error = NULL WHERE id = ?",
                (new_status, now, oid),
            )
            recovered += 1
            logger.info("Recovered stuck 'sending' outreach %s -> %s", oid[:8], new_status)

        if recovered:
            db.commit()
    except Exception as e:
        logger.warning("Failed to recover stuck sending outreaches: %s", e)
    finally:
        db.close()
    return recovered
