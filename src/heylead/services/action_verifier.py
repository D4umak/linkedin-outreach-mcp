"""Post-action verification — async double-check that actions actually landed on LinkedIn.

Runs every 30 min via scheduler. Verifies:
1. Invitation outreaches — checks relations/pending invitations
2. Engagement actions — comments via get_post_comments(), reactions via
   get_post_reactions(), follows via check_follow_status().
   Endorsements and profile views marked trust_api (no verification API).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..constants import (
    ENGAGEMENT_VERIFY_MAX_CHECKS,
    TRUST_API,
    VERIFICATION_MAX_CHECKS,
    VERIFICATION_WINDOW_HOURS,
)
from ..db.async_bridge import run_db

logger = logging.getLogger(__name__)


def _get_provider_id_for_engagement(eng: dict) -> str:
    """Extract provider_id from engagement's joined contact data.

    The get_unverified_engagements query joins through outreach → contact,
    giving us contact_profile_json which contains provider_id (ACoAAA format).
    Falls back to contact_linkedin_id (public_id) if profile_json not available.
    """
    # Try profile_json first (has ACoAAA provider_id)
    profile_json_str = eng.get("contact_profile_json") or ""
    if profile_json_str:
        try:
            profile = json.loads(profile_json_str) if isinstance(profile_json_str, str) else profile_json_str
            pid = profile.get("provider_id", "")
            if pid:
                return str(pid)
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    # Fallback: linkedin_id (may be public_id slug — less reliable for Voyager)
    return str(eng.get("contact_linkedin_id") or "")


async def verify_recent_actions(
    hours: int = VERIFICATION_WINDOW_HOURS,
    max_checks: int = VERIFICATION_MAX_CHECKS,
) -> dict[str, Any]:
    """Verify recent scheduler actions actually happened on LinkedIn.

    1. Queries outreaches with invited_at in the last N hours and verified_at IS NULL
    2. Batch-fetches current relations from LinkedIn
    3. For each unverified outreach, checks if the prospect appears in relations
       or if a conversation exists
    4. Updates verified_at and verified_status on each outreach
    5. Also verifies recent engagements (comments, reactions, etc.)

    Returns: {"checked": N, "confirmed": N, "unconfirmed": N, "errors": N,
              "engagement_checked": N, "engagement_verified": N, ...}
    """
    from ..db.queries import log_action
    from ..db.schema import get_db
    from ..linkedin import get_account_id, get_linkedin_client

    stats = {"checked": 0, "confirmed": 0, "unconfirmed": 0, "errors": 0}

    try:
        account_id = await run_db(get_account_id)
        if not account_id:
            logger.debug("verify_recent_actions: no account_id, skipping")
            return stats

        client = await run_db(get_linkedin_client)
    except Exception as e:
        logger.debug("verify_recent_actions: client init failed: %s", e)
        return stats

    # 1. Find unverified outreaches from the last N hours
    since = int(time.time()) - (hours * 3600)

    def _get_unverified(since_ts: int, limit: int) -> list[dict]:
        db = get_db()
        rows = db.execute(
            """SELECT o.id, o.status, c.linkedin_id, c.name
               FROM outreaches o
               JOIN contacts c ON o.contact_id = c.id
               WHERE o.invited_at >= ?
                 AND o.invited_at IS NOT NULL
                 AND o.verified_at IS NULL
                 AND o.status IN ('invited', 'connected', 'messaged', 'replied', 'hot_lead')
                 AND c.linkedin_id IS NOT NULL AND c.linkedin_id != ''
               ORDER BY o.invited_at DESC
               LIMIT ?""",
            (since_ts, limit),
        ).fetchall()
        db.close()
        return [dict(r) for r in rows]

    unverified = await run_db(_get_unverified, since, max_checks)

    if unverified:
        # 2. Batch-fetch current relations for comparison
        connected_ids: set[str] = set()
        try:
            relations = await client.get_relations(account_id, limit=500)
            connected_ids = {
                r.get("provider_id", "") for r in relations if r.get("provider_id")
            }
        except Exception as e:
            logger.warning("verify_recent_actions: get_relations failed: %s", e)
            # Fall back to individual checks below

        # 3. Verify each outreach
        now = int(time.time())

        def _update_verification(outreach_id: str, status: str) -> None:
            db = get_db()
            db.execute(
                "UPDATE outreaches SET verified_at = ?, verified_status = ? WHERE id = ?",
                (now, status, outreach_id),
            )
            db.commit()
            db.close()

        for outreach in unverified:
            outreach_id = outreach["id"]
            linkedin_id = outreach["linkedin_id"]
            stats["checked"] += 1

            try:
                # Fast path: check batch relations
                if linkedin_id in connected_ids:
                    # Already connected = invitation confirmed (and accepted)
                    await run_db(_update_verification, outreach_id, "confirmed")
                    stats["confirmed"] += 1
                    continue

                # For "invited" status: check if invitation is pending
                if outreach["status"] == "invited":
                    try:
                        relation = await client.check_existing_relation(
                            account_id, linkedin_id,
                        )
                        if relation.get("connected") or relation.get("has_chat"):
                            await run_db(_update_verification, outreach_id, "confirmed")
                            stats["confirmed"] += 1
                        elif relation.get("pending_invite"):
                            # Invitation exists but not yet accepted
                            await run_db(_update_verification, outreach_id, "confirmed")
                            stats["confirmed"] += 1
                        else:
                            # No relation found — invitation may not have landed
                            await run_db(_update_verification, outreach_id, "unconfirmed")
                            stats["unconfirmed"] += 1
                    except Exception as e:
                        logger.debug(
                            "verify_recent_actions: individual check failed for %s: %s",
                            outreach_id[:8], e,
                        )
                        stats["errors"] += 1
                else:
                    # Already connected/messaged/replied = confirmed
                    await run_db(_update_verification, outreach_id, "confirmed")
                    stats["confirmed"] += 1

            except Exception as e:
                logger.debug("verify_recent_actions: error verifying %s: %s", outreach_id[:8], e)
                stats["errors"] += 1
    else:
        logger.debug("verify_recent_actions: no unverified outreaches in last %dh", hours)

    # 5. Verify recent engagements
    engagement_stats = await verify_recent_engagements(
        client, account_id, hours, ENGAGEMENT_VERIFY_MAX_CHECKS,
    )
    stats.update(engagement_stats)

    # 6. Log summary to actions_log
    try:
        await run_db(
            log_action, "verification_batch", result="success",
            details=stats,
        )
    except Exception:
        pass

    summary_log = logger.debug if stats["checked"] == 0 else logger.info
    summary_log(
        "Action verification: outreach checked=%d confirmed=%d unconfirmed=%d errors=%d | "
        "engagement checked=%d verified=%d unverified=%d trust_api=%d",
        stats["checked"], stats["confirmed"], stats["unconfirmed"], stats["errors"],
        stats.get("engagement_checked", 0), stats.get("engagement_verified", 0),
        stats.get("engagement_unverified", 0), stats.get("engagement_trust_api", 0),
    )
    return stats


async def verify_recent_engagements(
    client: Any,
    account_id: str,
    hours: int = VERIFICATION_WINDOW_HOURS,
    max_checks: int = ENGAGEMENT_VERIFY_MAX_CHECKS,
) -> dict[str, Any]:
    """Batch-verify recent engagements that haven't been verified yet.

    Comments are verified via get_post_comments() (matching author_id).
    Reactions, follows, endorsements, and profile views are marked trust_api
    since no verification API exists for them.

    Returns: {"engagement_checked": N, "engagement_verified": N, ...}
    """
    from ..db.queries import get_setting, get_unverified_engagements, update_engagement_verification

    stats = {
        "engagement_checked": 0,
        "engagement_verified": 0,
        "engagement_unverified": 0,
        "engagement_trust_api": 0,
        "engagement_errors": 0,
    }

    since = int(time.time()) - (hours * 3600)
    unverified = await run_db(get_unverified_engagements, since, max_checks)
    if not unverified:
        return stats

    # Get our provider_id for comment author matching
    profile = await run_db(get_setting, "profile", {})
    our_provider_id = profile.get("provider_id", "")

    for eng in unverified:
        eng_id = eng["id"]
        action_type = eng["action_type"]
        stats["engagement_checked"] += 1

        try:
            # Comments and reply_comments: verify via get_post_comments
            if action_type in ("comment", "reply_comment") and eng.get("post_id"):
                from .engagement_verifier import verify_comment

                result = await verify_comment(
                    client, account_id, our_provider_id,
                    eng_id, eng["post_id"], eng.get("text", ""),
                )
                if result.get("verified"):
                    stats["engagement_verified"] += 1
                else:
                    stats["engagement_unverified"] += 1

            # Reactions: verify via get_post_reactions()
            elif action_type == "react" and eng.get("post_id"):
                from .engagement_verifier import verify_reaction

                result = await verify_reaction(
                    client, account_id, our_provider_id,
                    eng_id, eng["post_id"],
                )
                if result.get("verified"):
                    stats["engagement_verified"] += 1
                else:
                    stats["engagement_unverified"] += 1

            # Follows: verify via check_follow_status()
            elif action_type == "follow":
                provider_id = _get_provider_id_for_engagement(eng)
                if provider_id:
                    from .engagement_verifier import verify_follow

                    result = await verify_follow(
                        client, account_id, provider_id, eng_id,
                    )
                    if result.get("verified"):
                        stats["engagement_verified"] += 1
                    else:
                        stats["engagement_unverified"] += 1
                else:
                    # Can't verify without provider_id — fall back to trust_api
                    await run_db(update_engagement_verification, eng_id, TRUST_API)
                    stats["engagement_trust_api"] += 1

            # Endorsements and profile views: no verification API
            elif action_type in ("endorse", "profile_view"):
                await run_db(update_engagement_verification, eng_id, TRUST_API)
                stats["engagement_trust_api"] += 1

            else:
                # Unknown action type — mark trust_api as safe default
                await run_db(update_engagement_verification, eng_id, TRUST_API)
                stats["engagement_trust_api"] += 1

        except Exception as e:
            logger.debug("verify_recent_engagements: error for %s: %s", eng_id[:8], e)
            stats["engagement_errors"] += 1

    return stats
