"""Tool: profile — View and restore LinkedIn profile change history.

Actions:
  history  — List recent profile changes (optionally filtered by field)
  restore  — Revert a specific change by ID
  current  — Show current cached profile snapshot
"""

from __future__ import annotations

import base64
import json
import logging
import time

from ..db.async_bridge import run_db
from ..db import aio as db
from .profile_editor import apply_profile_change

logger = logging.getLogger(__name__)


def _ts(epoch: int | None) -> str:
    """Format epoch seconds as human-readable date."""
    if not epoch:
        return "?"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


async def run_profile(
    action: str = "history",
    field: str = "",
    change_id: str = "",
    limit: int = 20,
) -> str:
    action = action.lower().strip()

    if action == "history":
        return await _show_history(field or None, limit)

    if action == "restore":
        return await _restore_change(change_id)

    if action == "current":
        return await _show_current()

    return (
        f"Unknown action: '{action}'. Use:\n"
        "  'history'  — list recent changes\n"
        "  'restore'  — revert a change by ID\n"
        "  'current'  — show current profile"
    )


async def _show_history(field: str | None, limit: int) -> str:
    changes = await db.get_profile_changes(field=field, limit=limit)
    if not changes:
        hint = f" for field '{field}'" if field else ""
        return f"No profile changes recorded{hint}."

    lines = ["Profile Change History", "=" * 60, ""]
    for c in changes:
        old = c.get("old_value") or "(unknown)"
        new = c.get("new_value", "")
        status_tag = " [REVERTED]" if c["status"] == "reverted" else ""
        lines.append(
            f"  ID: {c['id']}  |  {c['field']}  |  {c['source']}  |  "
            f"{_ts(c.get('created_at'))}{status_tag}"
        )
        if c["field"] not in ("photo", "cover_photo"):
            old_trunc = old[:80] + ("..." if len(old) > 80 else "")
            new_trunc = new[:80] + ("..." if len(new) > 80 else "")
            lines.append(f"    Old: {old_trunc}")
            lines.append(f"    New: {new_trunc}")
        else:
            # Show human-readable size info for photo fields
            for label, val in [("Old", old), ("New", new)]:
                if val and val != "(unknown)":
                    try:
                        photo_data = json.loads(val)
                        raw_len = len(base64.b64decode(photo_data["base64"]))
                        size_kb = raw_len / 1024
                        ct = photo_data.get("content_type", "image/jpeg")
                        lines.append(f"    {label}: [Photo: {size_kb:.1f} KB, {ct}]")
                    except (json.JSONDecodeError, KeyError, Exception):
                        # Legacy placeholder or corrupted data
                        lines.append(f"    {label}: {val}")
                else:
                    lines.append(f"    {label}: (none)")
        lines.append("")

    lines.append(f"Total: {len(changes)} change(s)")
    if field:
        lines.append(f"Filtered by: {field}")
    lines.append("\nTo restore a change: profile(action='restore', change_id='<id>')")
    return "\n".join(lines)


async def _restore_change(change_id: str) -> str:
    if not change_id:
        return "Please provide a change_id to restore."

    record = await db.get_profile_change(change_id)
    if not record:
        return f"No change found with ID '{change_id}'."

    if record["status"] == "reverted":
        return f"Change {change_id} has already been reverted."

    old_value = record.get("old_value")
    if not old_value:
        return (
            f"Cannot restore change {change_id}: the previous value was not recorded.\n"
            "You can manually set the value using brand_strategy or direct API."
        )

    field = record["field"]

    # Apply the restore
    from ..linkedin import get_account_id, get_linkedin_client

    profile = await db.get_setting("profile", {})
    provider_id = profile.get("provider_id", "")

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected. Please run setup_profile first."

    client = get_linkedin_client()

    if field in ("photo", "cover_photo"):
        # Decode stored base64 photo data
        label = "cover photo" if field == "cover_photo" else "photo"
        try:
            photo_data = json.loads(old_value)
            image_bytes = base64.b64decode(photo_data["base64"])
            ct = photo_data.get("content_type", "image/jpeg")
        except (json.JSONDecodeError, KeyError, TypeError):
            await client.close()
            return (
                f"Cannot restore {label}: stored data is not in the expected format.\n"
                f"This may be a legacy record. Please re-upload your {label} manually."
            )

        result = await apply_profile_change(
            client, account_id, provider_id, field, "restored",
            source="restore",
            image_bytes=image_bytes, content_type=ct,
        )
        await client.close()

        if result.get("success"):
            await db.mark_profile_change_reverted(change_id)
            size_kb = len(image_bytes) / 1024
            return (
                f"Restored {label} from saved image ({size_kb:.1f} KB, {ct}).\n\n"
                f"  Change {change_id} marked as reverted.\n"
                f"  New restore point: {result.get('change_id')}\n\n"
                "LinkedIn may take a few minutes to display the restored image."
            )
        return f"Photo restore failed: {result.get('error', 'unknown error')}"

    # Text field restore
    result = await apply_profile_change(
        client, account_id, provider_id, field, old_value, source="restore",
    )
    await client.close()

    if result.get("success"):
        await db.mark_profile_change_reverted(change_id)
        return (
            f"Restored {field} to previous value.\n\n"
            f"  Field: {field}\n"
            f"  Restored to: {old_value[:120]}\n"
            f"  Change {change_id} marked as reverted.\n"
            f"  New restore point: {result.get('change_id')}"
        )

    return f"Restore failed: {result.get('error', 'unknown error')}"


async def _show_current() -> str:
    profile = await db.get_setting("profile", {})
    if not profile:
        return "No profile cached yet. Run brand_strategy(action='analyze') first."

    lines = ["Current LinkedIn Profile (cached)", "=" * 60, ""]
    lines.append(f"  Name:     {profile.get('first_name', '')} {profile.get('last_name', '')}")
    lines.append(f"  Headline: {profile.get('headline', '(not set)')}")
    summary = profile.get("summary", "(not set)")
    if len(summary) > 200:
        summary = summary[:200] + "..."
    lines.append(f"  Summary:  {summary}")
    lines.append(f"  Location: {profile.get('location_id', '(not set)')}")
    link = profile.get("custom_link")
    if link:
        import json as _json
        link_str = _json.dumps(link) if isinstance(link, dict) else str(link)
        lines.append(f"  Link:     {link_str}")
    else:
        lines.append(f"  Link:     (not set)")
    skills = profile.get("skills", [])
    lines.append(f"  Skills:   {', '.join(skills) if skills else '(not set)'}")
    lines.append(f"  Public:   {profile.get('public_id', '(unknown)')}")
    lines.append("")
    lines.append("Note: This is the locally cached version. Use brand_strategy(action='analyze')")
    lines.append("to refresh from LinkedIn.")
    return "\n".join(lines)
