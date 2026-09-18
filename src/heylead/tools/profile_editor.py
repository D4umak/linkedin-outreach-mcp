"""Shared wrapper for LinkedIn profile edits with change tracking.

Every profile mutation goes through ``apply_profile_change`` so we
always log the before/after value in the ``profile_changes`` table.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from ..db import aio as db

logger = logging.getLogger(__name__)


async def _fetch_current_photo_b64(client: Any, account_id: str) -> str | None:
    """Best-effort: fetch the current profile photo as a base64 JSON blob.

    Returns ``'{"base64": "...", "content_type": "image/..."}'`` or *None*
    if the photo cannot be fetched.
    """
    try:
        profile_data = await client.get_own_profile(account_id)
        photo_url = profile_data.get("profile_picture_url", "")
        if not photo_url:
            return None

        import httpx

        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as http:
            resp = await http.get(photo_url)
            if resp.status_code != 200:
                return None
            ct = resp.headers.get("content-type", "image/jpeg")
            content_type = "image/png" if "png" in ct else "image/jpeg"
            return json.dumps({
                "base64": base64.b64encode(resp.content).decode(),
                "content_type": content_type,
            })
    except Exception:
        # Best-effort — upload proceeds regardless
        return None


async def apply_profile_change(
    client: Any,
    account_id: str,
    provider_id: str,
    field: str,
    new_value: str,
    source: str = "manual",
    *,
    image_bytes: bytes | None = None,
    content_type: str = "image/jpeg",
) -> dict[str, Any]:
    """Apply a profile change and log it to the history table.

    For text fields *new_value* is the literal text (or JSON for complex fields).
    For binary uploads pass *image_bytes* and *content_type*; the image is
    stored as base64 JSON in the change log for future restore.

    Returns ``{"success": bool, "error": str, "change_id": str | None}``.
    """
    profile = await db.get_setting("profile", {})
    old_value: str | None = None

    if field == "headline":
        old_value = profile.get("headline")
        result = await client.update_profile_headline(
            account_id, provider_id, new_value,
        )
    elif field == "summary":
        old_value = profile.get("summary")
        result = await client.update_profile_summary(
            account_id, provider_id, new_value,
        )
    elif field == "photo":
        if image_bytes is None:
            return {"success": False, "error": "image_bytes required for photo", "change_id": None}
        # Capture current photo for rollback (best-effort)
        old_value = await _fetch_current_photo_b64(client, account_id)
        # Store the new photo as base64 JSON for future restore
        new_value = json.dumps({
            "base64": base64.b64encode(image_bytes).decode(),
            "content_type": content_type,
        })
        result = await client.upload_profile_photo(
            account_id, provider_id, image_bytes, content_type,
        )
    elif field == "cover_photo":
        if image_bytes is None:
            return {"success": False, "error": "image_bytes required for cover_photo", "change_id": None}
        # No API to fetch current cover photo — old_value stays None
        old_value = None
        # Store the new cover photo as base64 JSON for future restore
        new_value = json.dumps({
            "base64": base64.b64encode(image_bytes).decode(),
            "content_type": content_type,
        })
        result = await client.upload_cover_photo(
            account_id, provider_id, image_bytes, content_type,
        )
    elif field == "custom_link":
        old_link = profile.get("custom_link")
        old_value = json.dumps(old_link) if old_link else None
        try:
            link_data = json.loads(new_value) if isinstance(new_value, str) else new_value
        except (json.JSONDecodeError, TypeError):
            return {"success": False, "error": 'custom_link must be JSON: {"category": "...", "url": "..."}', "change_id": None}
        result = await client.update_custom_link(
            account_id, provider_id, link_data["category"], link_data["url"],
        )
    elif field == "location":
        old_value = profile.get("location_id")
        result = await client.update_location(
            account_id, provider_id, new_value,
        )
    elif field == "skills":
        old_skills = profile.get("skills", [])
        old_value = json.dumps(old_skills) if old_skills else None
        try:
            skills_list = json.loads(new_value) if isinstance(new_value, str) else new_value
        except (json.JSONDecodeError, TypeError):
            return {"success": False, "error": "skills must be a JSON array", "change_id": None}
        result = await client.update_skills(
            account_id, provider_id, skills_list,
        )
    elif field == "experience":
        old_value = None  # Experience is additive, no simple "previous"
        try:
            exp_data = json.loads(new_value) if isinstance(new_value, str) else new_value
        except (json.JSONDecodeError, TypeError):
            return {"success": False, "error": "experience must be a JSON object", "change_id": None}
        result = await client.update_experience(
            account_id, provider_id, exp_data,
        )
    elif field == "education":
        old_value = json.dumps(profile.get("education")) if profile.get("education") else None
        try:
            edu_data = json.loads(new_value) if isinstance(new_value, str) else new_value
        except (json.JSONDecodeError, TypeError):
            return {"success": False, "error": "education must be a JSON object", "change_id": None}
        result = await client.update_education(
            account_id, provider_id, edu_data,
        )
    elif field == "picture_settings":
        old_value = json.dumps(profile.get("picture_settings")) if profile.get("picture_settings") else None
        try:
            settings_data = json.loads(new_value) if isinstance(new_value, str) else new_value
        except (json.JSONDecodeError, TypeError):
            return {"success": False, "error": "picture_settings must be a JSON object", "change_id": None}
        result = await client.update_picture_settings(
            account_id, provider_id, settings_data,
        )
    elif field == "cover_picture_settings":
        old_value = json.dumps(profile.get("cover_picture_settings")) if profile.get("cover_picture_settings") else None
        try:
            settings_data = json.loads(new_value) if isinstance(new_value, str) else new_value
        except (json.JSONDecodeError, TypeError):
            return {"success": False, "error": "cover_picture_settings must be a JSON object", "change_id": None}
        result = await client.update_cover_picture_settings(
            account_id, provider_id, settings_data,
        )
    elif field == "open_to_work":
        old_value = json.dumps(profile.get("open_to_work")) if profile.get("open_to_work") else None
        try:
            otw_data = json.loads(new_value) if isinstance(new_value, str) else new_value
        except (json.JSONDecodeError, TypeError):
            return {"success": False, "error": "open_to_work must be a JSON object", "change_id": None}
        result = await client.update_open_to_work(
            account_id, provider_id, otw_data,
        )
    elif field == "skills_follow":
        old_value = str(profile.get("skills_follow", False)).lower()
        if isinstance(new_value, str):
            skills_follow_bool = new_value.lower() in ("true", "1", "yes")
        else:
            skills_follow_bool = bool(new_value)
        result = await client.update_skills_follow(
            account_id, provider_id, skills_follow_bool,
        )
    else:
        return {"success": False, "error": f"Unsupported field: {field}", "change_id": None}

    if result.get("success"):
        change_id = await db.log_profile_change(field, old_value, new_value, source)
        # Update cache for text fields
        if field in ("headline", "summary"):
            profile[field] = new_value
            await db.save_setting("profile", profile)
        elif field == "location":
            profile["location_id"] = new_value
            await db.save_setting("profile", profile)
        elif field == "custom_link":
            profile["custom_link"] = new_value
            await db.save_setting("profile", profile)
        elif field == "skills":
            profile["skills"] = json.loads(new_value) if isinstance(new_value, str) else new_value
            await db.save_setting("profile", profile)
        elif field in ("education", "picture_settings", "cover_picture_settings", "open_to_work", "skills_follow"):
            profile[field] = json.loads(new_value) if isinstance(new_value, str) else new_value
            await db.save_setting("profile", profile)
        logger.info("Profile change logged: %s id=%s source=%s", field, change_id, source)
        return {"success": True, "error": "", "change_id": change_id}

    return {"success": False, "error": result.get("error", "unknown error"), "change_id": None}
