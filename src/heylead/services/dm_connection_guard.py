"""Single DM eligibility check used by generate_send, follow-up, and the executor.

Acceptance evidence (accepted_at, connected/messaged, or a logged accept)
beats a stale local connections cache. Cache miss is not proof of a stranger.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..author_identity import looks_like_provider_id
from ..db.async_bridge import run_db
from ..db.schema import get_db
from . import connection_sync as _cs

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DmGuardResult:
    allowed: bool
    outcome: str  # ok | defer | skip | error
    message: str
    provider_id: str = ""
    public_id: str = ""


def resolve_connection_ids(prospect: dict[str, Any], profile: dict[str, Any]) -> tuple[str, str]:
    """Return (provider_id, public_id) using the same ACoAA swap as the executor."""
    provider_id = str(profile.get("provider_id") or "").strip()
    public_id = str(
        prospect.get("linkedin_id") or profile.get("public_id") or ""
    ).strip()
    if not provider_id and looks_like_provider_id(public_id):
        provider_id = public_id
        public_id = str(profile.get("public_id") or "").strip()
    return provider_id, public_id


def _sync_has_logged_accept(oid: str) -> bool:
    db = get_db()
    try:
        row = db.execute(
            """SELECT 1 FROM actions_log
               WHERE outreach_id = ? AND action_type IN
                     ('connection_accepted', 'silent_connection_detected')
               LIMIT 1""",
            (oid,),
        ).fetchone()
        return bool(row)
    finally:
        db.close()


async def _has_acceptance_evidence(outreach: dict[str, Any]) -> bool:
    if outreach.get("accepted_at"):
        return True
    if (outreach.get("status") or "") in ("connected", "messaged", "replied"):
        return True
    oid = outreach.get("id") or outreach.get("outreach_id")
    if not oid:
        return False
    return await run_db(_sync_has_logged_accept, oid)


async def verify_dm_eligible(
    account_id: str,
    prospect: dict[str, Any],
    outreach: dict[str, Any],
    *,
    client: Any = None,
    profile: dict[str, Any] | None = None,
) -> DmGuardResult:
    """Decide whether a DM may proceed. Never marks outreach status itself."""
    profile = profile if profile is not None else {}
    provider_id, public_id = resolve_connection_ids(prospect, profile)
    name = prospect.get("name") or outreach.get("name") or "prospect"

    if not provider_id and not public_id:
        return DmGuardResult(
            False, "error",
            f"No identifiers for {name} — cannot verify 1st-degree connection",
        )

    try:
        sync_age = await run_db(_cs.get_sync_age, account_id)
        if (
            (sync_age is None or sync_age > 1800)
            and _cs.should_run_precheck_sync()
            and client is not None
        ):
            await _cs.sync_connections(
                client, account_id, time_budget=_cs.PRECHECK_SYNC_BUDGET_SECONDS,
            )
    except Exception as e:
        logger.warning("Connection re-sync before DM failed: %s", e)

    if provider_id and await run_db(_cs.is_first_degree, account_id, provider_id):
        return DmGuardResult(True, "ok", "", provider_id, public_id)
    if public_id and await run_db(_cs.is_first_degree_by_public_id, account_id, public_id):
        return DmGuardResult(True, "ok", "", provider_id, public_id)

    live_ok = False
    live_error: Exception | None = None
    if client is not None:
        try:
            profile_live = await client.get_profile(account_id, provider_id or public_id)
            distance = str((profile_live or {}).get("network_distance") or "").upper()
            live_ok = distance in ("FIRST_DEGREE", "DISTANCE_1", "1")
            if not live_ok:
                check = getattr(client, "check_existing_relation", None)
                if check and provider_id:
                    relation = await check(account_id, provider_id)
                    live_ok = bool(
                        (relation or {}).get("connected") or (relation or {}).get("has_chat")
                    )
        except Exception as e:
            live_error = e
            logger.warning("DM live connection check failed for %s: %s", name, e)

    if live_ok:
        if provider_id:
            await run_db(
                _cs.mark_connected, account_id, provider_id, name, public_id, "",
            )
        _cs.log_stale_cache_once(
            logger,
            "DM guard: %s absent from connections cache but LinkedIn says "
            "1st-degree — proceeding",
            name,
        )
        return DmGuardResult(True, "ok", "", provider_id, public_id)

    if live_error is not None or await _has_acceptance_evidence(outreach):
        try:
            import time
            from ..constants import JOB_SYNC_CONNECTIONS
            from ..db.queries import create_scheduler_job, get_pending_job_count

            def _enqueue() -> None:
                if get_pending_job_count(None, JOB_SYNC_CONNECTIONS) == 0:
                    create_scheduler_job(None, JOB_SYNC_CONNECTIONS, int(time.time()))

            await run_db(_enqueue)
        except Exception as e:
            logger.debug("Could not enqueue sync_connections after DM defer: %s", e)
        return DmGuardResult(
            False, "defer",
            f"Connection cache miss for {name} — will retry after sync",
            provider_id, public_id,
        )

    return DmGuardResult(
        False, "error",
        f"Not in local 1st-degree connections (provider_id={provider_id}, public_id={public_id})",
        provider_id, public_id,
    )
