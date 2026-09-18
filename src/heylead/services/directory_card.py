"""The only fields that may enter the shared contact directory."""

from __future__ import annotations

import json
from typing import Any

_PROFILE_KEEP = (
    "name", "title", "company", "headline", "location",
    "provider_id", "public_id", "is_open_profile", "is_premium",
)

_PRIVATE_PROFILE = frozenset({
    "email", "phone", "emails", "phone_numbers", "contact_info",
    "lookup_failed", "lookup_failed_at",
})


def directory_identity(row: dict[str, Any]) -> str:
    """One key per LinkedIn person. provider_id wins — it is stable across slugs."""
    provider = (row.get("provider_id") or "").strip()
    if not provider:
        raw = row.get("profile_json") or ""
        if raw:
            try:
                blob = json.loads(raw) if isinstance(raw, str) else raw
                provider = (blob.get("provider_id") or "").strip()
            except (json.JSONDecodeError, TypeError, AttributeError):
                provider = ""
    if provider:
        return provider
    return (row.get("linkedin_id") or "").strip()


def public_directory_card(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return the contribute payload, or None if this row has no LinkedIn identity."""
    identity = directory_identity(row)
    if not identity:
        return None
    profile_in: dict[str, Any] = {}
    raw = row.get("profile_json") or ""
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, dict):
                profile_in = parsed
        except (json.JSONDecodeError, TypeError, ValueError):
            profile_in = {}
    profile_out = {
        key: profile_in[key]
        for key in _PROFILE_KEEP
        if key in profile_in and profile_in[key] not in (None, "")
    }
    for banned in _PRIVATE_PROFILE:
        profile_out.pop(banned, None)
    provider_id = (profile_out.get("provider_id") or row.get("provider_id") or "").strip()
    linkedin_id = (row.get("linkedin_id") or "").strip()
    card = {
        "linkedin_id": linkedin_id,
        "provider_id": provider_id,
        "name": (row.get("name") or profile_out.get("name") or "").strip(),
        "title": (row.get("title") or profile_out.get("title") or "").strip(),
        "company": (row.get("company") or profile_out.get("company") or "").strip(),
        "linkedin_url": (row.get("linkedin_url") or "").strip(),
        "location": (row.get("location") or profile_out.get("location") or "").strip(),
        "profile_json": json.dumps(profile_out, separators=(",", ":")),
    }
    if not card["linkedin_id"] and not card["provider_id"]:
        return None
    return card
