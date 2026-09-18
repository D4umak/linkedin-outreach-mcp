"""Repair stored invite targets that can only draw Unipile's format-400.

Companion to provider_id_resolver, which repairs ids at ingestion. Rows
created before that fix are already outreaches, and two shapes of them burn
an invite attempt on 400 "User ID does not match provider's expected format"
every time they are retried (retry_failed resets 'error' rows to pending, so
'error' is not a terminal state):

* contacts whose profile_json still carries an SN-space ``ACw…`` provider id;
* contacts with EMPTY profile_json whose ``linkedin_id`` holds the masked
  Sales-Navigator display name ("Recruiter at JPMorganChase") — the shape
  behind all 8 invitation_failed rows of 21 Aug 2026.

The repair resolves a classic id through the classic-api get_profile where a
usable slug exists and resets the outreach for retry; where no usable
identifier exists it parks the row as 'skipped' so nothing retries it again.

Run against the live DB (dry run first — it writes nothing and calls no API):

    PYTHONPATH=src python -m heylead.services.provider_id_repair
    PYTHONPATH=src python -m heylead.services.provider_id_repair --apply
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..author_identity import normalize_public_slug, slug_from_profile_url
from ..db.async_bridge import run_db
from .provider_id_resolver import is_classic_provider_id, is_sendable_invite_id

logger = logging.getLogger(__name__)

_FORMAT_400_MARKER = "does not match provider's expected format"
_PARK_REASON = (
    "No usable LinkedIn identifier (anonymized Sales Navigator result) — "
    "parked by provider-id repair"
)


def _find_candidate_rows() -> list[dict[str, Any]]:
    """Outreaches that already failed on the format-400, rows a previous
    apply parked (then something moved off skipped), plus not-yet-failed
    rows whose contact still carries an SN-space provider id."""
    from ..db.schema import get_db

    db = get_db()
    rows = db.execute(
        """SELECT o.id AS outreach_id, o.status, o.last_attempt_error,
                  c.id AS contact_id, c.name, c.linkedin_id, c.linkedin_url,
                  c.profile_json
           FROM outreaches o JOIN contacts c ON o.contact_id = c.id
           WHERE o.last_attempt_error LIKE '%' || ? || '%'
              OR (o.status != 'skipped'
                  AND o.last_attempt_error LIKE '%' || ? || '%')
              OR (o.status IN ('pending', 'error')
                  AND c.profile_json LIKE '%"provider_id": "AC%'
                  AND c.profile_json NOT LIKE '%"provider_id": "ACo%')""",
        (_FORMAT_400_MARKER, _PARK_REASON),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def _find_unsendable_queued_rows() -> list[dict[str, Any]]:
    """Pending outreaches whose contact offers no sendable identifier at all.

    These have not failed yet, but they can only fail: the send guard rejects
    them — after the LLM pipeline has already run, once per retry. Selecting
    them here parks the whole burn up front. Rows with any sendable
    identifier are left strictly alone; this sweep never repairs, only parks.
    """
    from ..db.schema import get_db

    db = get_db()
    rows = db.execute(
        """SELECT o.id AS outreach_id, o.status, o.last_attempt_error,
                  c.id AS contact_id, c.name, c.linkedin_id, c.linkedin_url,
                  c.profile_json
           FROM outreaches o JOIN contacts c ON o.contact_id = c.id
           WHERE o.status = 'pending'""",
    ).fetchall()
    db.close()
    return [dict(r) for r in rows if _classify(dict(r))[0] == "park"]


def _parse_profile(profile_json: Any) -> dict[str, Any]:
    try:
        profile = json.loads(profile_json) if profile_json else {}
    except (json.JSONDecodeError, TypeError):
        profile = {}
    return profile if isinstance(profile, dict) else {}


def _classify(row: dict[str, Any]) -> tuple[str, str]:
    """Return (action, identifier): 'reset' when the contact already carries a
    classic id (someone repaired it since the failure), 'lookup' with the
    first identifier the classic api could resolve, else 'park'."""
    profile = _parse_profile(row.get("profile_json"))
    pid = str(profile.get("provider_id") or "").strip()
    if is_classic_provider_id(pid):
        return "reset", pid
    for candidate in (
        normalize_public_slug(profile.get("public_id")),
        slug_from_profile_url(profile.get("linkedin_url")),
        slug_from_profile_url(row.get("linkedin_url")),
        str(row.get("linkedin_id") or "").strip(),
        # A slug-shaped provider_id is itself resolvable — and sendable, so
        # the queued sweep must never classify such a row as 'park'.
        pid,
    ):
        if candidate and is_sendable_invite_id(candidate):
            return "lookup", candidate
    return "park", ""


def _reset_outreach(outreach_id: str) -> None:
    from ..db.queries import update_outreach

    update_outreach(
        outreach_id, status="pending", invite_attempts=0, last_attempt_error=None,
    )


def _park_outreach(outreach_id: str) -> None:
    from ..db.queries import update_outreach

    update_outreach(outreach_id, status="skipped", last_attempt_error=_PARK_REASON)


def _apply_contact_fix(
    contact_id: str, profile_json: Any, classic_id: str, public_id: str,
) -> None:
    from ..db.queries import update_contact

    profile = _parse_profile(profile_json)
    old = str(profile.get("provider_id") or "").strip()
    if old and old != classic_id:
        profile.setdefault("sales_navigator_provider_id", old)
    profile["provider_id"] = classic_id
    if public_id and not profile.get("public_id"):
        profile["public_id"] = public_id
    update_contact(contact_id, profile_json=json.dumps(profile))


async def repair_unsendable_invite_targets(
    client: Any,
    account_id: str,
    *,
    dry_run: bool = True,
    max_lookups: int = 50,
) -> dict[str, Any]:
    """Repair or park every stored invite target that can only 400.

    A dry run classifies without a single write or API call. Applying resolves
    'lookup' rows through the classic api: a classic id in the response
    repairs the contact and resets the outreach; anything else parks it.
    Queued (not-yet-failed) rows with no sendable identifier at all join the
    set pre-classified as 'park'.
    """
    rows = await run_db(_find_candidate_rows)
    seen = {row["outreach_id"] for row in rows}
    rows += [
        row for row in await run_db(_find_unsendable_queued_rows)
        if row["outreach_id"] not in seen
    ]

    if dry_run:
        report: dict[str, Any] = {
            "dry_run": True, "would_lookup": [], "would_park": [], "would_reset": [],
        }
    else:
        report = {"dry_run": False, "repaired": [], "parked": [], "reset": [], "deferred": []}

    lookups = 0
    for row in rows:
        action, identifier = _classify(row)
        entry = {
            "outreach_id": row["outreach_id"],
            "contact_id": row["contact_id"],
            "name": row.get("name", ""),
            "identifier": identifier,
        }
        if dry_run:
            report[f"would_{action}"].append(entry)
            continue

        if action == "reset":
            await run_db(_reset_outreach, row["outreach_id"])
            report["reset"].append(entry)
        elif action == "park":
            await run_db(_park_outreach, row["outreach_id"])
            report["parked"].append(entry)
        elif lookups >= max_lookups:
            report["deferred"].append(entry)
        else:
            lookups += 1
            try:
                resp = await client.get_profile(account_id, identifier) or {}
            except Exception as e:
                logger.warning(
                    "Classic profile lookup failed for %s: %s", identifier[:40], e,
                )
                resp = {}
            classic = str(resp.get("provider_id") or "").strip()
            if is_classic_provider_id(classic):
                await run_db(
                    _apply_contact_fix,
                    row["contact_id"],
                    row.get("profile_json"),
                    classic,
                    normalize_public_slug(resp.get("public_id")),
                )
                await run_db(_reset_outreach, row["outreach_id"])
                report["repaired"].append(entry)
            else:
                await run_db(_park_outreach, row["outreach_id"])
                report["parked"].append(entry)

    logger.info("Provider-id repair report: %s", {
        k: (len(v) if isinstance(v, list) else v) for k, v in report.items()
    })
    return report


async def _main(apply: bool) -> None:
    from ..linkedin import get_account_id, get_linkedin_client

    account_id = await run_db(get_account_id)
    if not account_id:
        raise SystemExit("No LinkedIn account connected")
    client = await run_db(get_linkedin_client)
    try:
        report = await repair_unsendable_invite_targets(
            client, account_id, dry_run=not apply,
        )
    finally:
        await client.close()
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(
        description="Repair stored invite targets that can only 400",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="write changes; without it, classify and print only",
    )
    asyncio.run(_main(parser.parse_args().apply))
