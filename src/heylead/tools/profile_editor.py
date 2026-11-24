"""Shared wrapper for LinkedIn profile edits with change tracking.

Every profile mutation goes through ``apply_profile_change`` so we
always log the before/after value in the ``profile_changes`` table.

On a hosted account (the only mode supported), profile edits (except
headline and About) are not available through the client.
"""

from __future__ import annotations

from typing import Any

from ..hosted_writes import LaptopWriteRefused, ensure_laptop_may_write, generic_refusal


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
    ensure_laptop_may_write("profile edit")
    raise LaptopWriteRefused(f"{generic_refusal()} (profile edit)")
