"""Keep the stored sender profile aligned with live LinkedIn.

``setup_profile`` writes a snapshot and nothing refreshes it. Headlines and
current companies change; outreach then introduces the sender with facts that
LinkedIn itself no longer shows.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..db.async_bridge import run_db
from ..db.queries import get_setting, get_setting_updated_at, save_setting
from ..linkedin import get_account_id, get_linkedin_client
from ..linkedin.experience import apply_current_role

logger = logging.getLogger(__name__)

PROFILE_MAX_AGE_SECONDS = 24 * 3600

_REFRESH_KEYS = (
    "headline",
    "title",
    "company",
    "summary",
    "location",
    "industry",
    "experience",
    "skills",
    "profile_picture_url",
    "connections",
)


async def refresh_own_profile() -> dict[str, Any]:
    """Replace stale identity fields from LinkedIn. Leaves expertise_map alone.

    When the cloud owns sending this returns the stored profile and writes
    nothing. The cache is ONE setting shared by every workspace on the
    machine, while ``GET /profile`` answers for the active workspace's seat
    (heylead-api #464) -- so a single write inside a client workspace is what
    the next sync push sends up as the owner's own profile (heylead#350).
    Nothing reaches here once the send tools refuse; this is what keeps it
    that way. A machine that does own sending still refreshes, or its
    outreach goes out with a stale headline.
    """
    from .cloud_sync import local_scheduler_engine_enabled

    stored = await run_db(get_setting, "profile", {}) or {}
    if not local_scheduler_engine_enabled():
        return stored
    try:
        account_id = await run_db(get_account_id)
    except RuntimeError:
        account_id = None
    if not account_id:
        account_id = await run_db(get_setting, "unipile_account_id", None)
    if not account_id:
        return stored

    client = get_linkedin_client()
    try:
        live = await client.get_own_profile(account_id)
    finally:
        await client.close()

    if not isinstance(live, dict) or not live.get("name"):
        return stored

    merged = dict(stored)
    for key in _REFRESH_KEYS:
        value = live.get(key)
        if value not in (None, "", []):
            merged[key] = value
    if live.get("headline"):
        merged["headline"] = live["headline"]
        merged["title"] = live.get("title") or live["headline"]
    if live.get("name"):
        merged["name"] = live["name"]
    apply_current_role(merged)
    await run_db(save_setting, "profile", merged)
    logger.info(
        "Refreshed own profile headline=%r company=%r",
        merged.get("headline", "")[:80],
        merged.get("company", ""),
    )
    return merged


async def refresh_own_profile_if_stale(
    max_age_seconds: int = PROFILE_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Fetch LinkedIn only when the snapshot is older than *max_age_seconds*."""
    updated_at = await run_db(get_setting_updated_at, "profile")
    if updated_at and (time.time() - updated_at) < max_age_seconds:
        return await run_db(get_setting, "profile", {}) or {}
    try:
        return await refresh_own_profile()
    except Exception as e:
        logger.warning("own profile refresh failed: %s", e)
        return await run_db(get_setting, "profile", {}) or {}
