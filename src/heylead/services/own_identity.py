"""Identify the connected LinkedIn account so outreach never targets it.

Match ids only (provider_id, public slug, profile URL). Never match on
display name — two people can share one.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_ID_KEYS = ("provider_id", "public_id", "public_identifier", "linkedin_id")
_URL_KEYS = ("profile_url", "linkedin_url")


def _slug_from_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    path = (parsed.path or "").lower().strip("/")
    if not path.startswith("in/"):
        return ""
    slug = path.split("/", 1)[1].split("/")[0].strip()
    return slug


def _normalize_token(raw: Any) -> str:
    token = str(raw or "").lower().strip()
    if not token:
        return ""
    if "linkedin.com" in token or "/in/" in token or token.startswith("http"):
        return _slug_from_url(token) or token.rstrip("/")
    return token.rstrip("/")


def _tokens_from_mapping(data: dict[str, Any] | None) -> set[str]:
    tokens: set[str] = set()
    if not isinstance(data, dict):
        return tokens
    for key in _ID_KEYS:
        token = _normalize_token(data.get(key))
        if token:
            tokens.add(token)
    for key in _URL_KEYS:
        token = _normalize_token(data.get(key))
        if token:
            tokens.add(token)
    return tokens


def _profile_blob(prospect: dict[str, Any]) -> dict[str, Any]:
    blob = prospect.get("profile_json")
    if isinstance(blob, dict):
        return blob
    if isinstance(blob, str) and blob.strip():
        try:
            parsed = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def own_identity_tokens() -> set[str]:
    """Lowercased id tokens for the connected account.

    Empty when ``settings.profile`` has no provider_id / public slug / URL —
    callers must fail open (treat as not-own), same as inbound's warning.
    """
    from ..db.queries import get_setting

    profile = get_setting("profile", {}) or {}
    if not isinstance(profile, dict):
        return set()
    return _tokens_from_mapping(profile)


def _prospect_tokens(prospect_or_ids: Any) -> set[str]:
    if prospect_or_ids is None:
        return set()
    if isinstance(prospect_or_ids, str):
        token = _normalize_token(prospect_or_ids)
        return {token} if token else set()
    if not isinstance(prospect_or_ids, dict):
        return set()
    tokens = _tokens_from_mapping(prospect_or_ids)
    tokens |= _tokens_from_mapping(_profile_blob(prospect_or_ids))
    return tokens


def is_own_identity(prospect_or_ids: Any) -> bool:
    """True if any prospect id/slug/URL matches the connected account.

    Never matches on display name. Empty own-profile tokens → False.
    """
    own = own_identity_tokens()
    if not own:
        return False
    return bool(own & _prospect_tokens(prospect_or_ids))


async def refuse_own_account_target(
    prospect: Any,
    *,
    outreach_id: str = "",
    campaign_id: str = "",
) -> str:
    """Skip text if *prospect* is the owner; otherwise empty.

    Parks the outreach with ``operator_skip`` so an already-invited self-row
    can be stopped (the ICP-skip hole does not apply).
    """
    from ..db.async_bridge import run_db

    if not await run_db(is_own_identity, prospect):
        return ""
    name = ""
    if isinstance(prospect, dict):
        name = str(prospect.get("name") or "").strip()
    label = name or "connected account"
    if outreach_id:
        from ..db import aio as adb

        await adb.update_outreach(
            outreach_id, status="skipped", last_attempt_error="operator_skip",
        )
        await adb.log_action(
            "own_account",
            outreach_id=outreach_id,
            campaign_id=campaign_id,
            result="skipped",
            details={"reason": "own_account", "name": label},
        )
    logger.info("own_account: refusing send to %s", label)
    return (
        f"Skipped {label} — this is the connected LinkedIn account, not a prospect."
    )


def park_own_account_outreaches() -> int:
    """Skip existing self-rows and cancel their pending scheduler jobs.

    Returns the number of outreaches parked. Does not withdraw already-sent
    InMail or invitations.
    """
    own = own_identity_tokens()
    if not own:
        return 0

    from ..db.queries import log_action, update_outreach
    from ..db.schema import get_db

    db = get_db()
    try:
        contacts = db.execute(
            "SELECT id, linkedin_id, linkedin_url, profile_json FROM contacts",
        ).fetchall()
        contact_ids = [row["id"] for row in contacts if is_own_identity(dict(row))]
        if not contact_ids:
            return 0
        placeholders = ",".join("?" * len(contact_ids))
        outreaches = db.execute(
            f"""SELECT id, status FROM outreaches
                WHERE contact_id IN ({placeholders})""",
            contact_ids,
        ).fetchall()
        to_park = [row["id"] for row in outreaches if row["status"] != "skipped"]
    finally:
        db.close()

    parked = 0
    for outreach_id in to_park:
        update_outreach(
            outreach_id, status="skipped", last_attempt_error="operator_skip",
        )
        log_action(
            "own_account_parked",
            outreach_id=outreach_id,
            result="skipped",
            details={"reason": "own_account"},
        )
        parked += 1
        logger.info("own_account_parked: outreach %s", outreach_id)
    return parked
