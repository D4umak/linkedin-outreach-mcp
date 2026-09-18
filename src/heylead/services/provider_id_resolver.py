"""Repair non-classic LinkedIn provider ids before prospects become outreaches.

Sales-Navigator-mode searches return prospects whose ``provider_id`` lives in
the Sales Navigator id space (``ACw…``); classic invitations and DMs need the
classic member id (``ACoAA…``). Unipile rejects SN-space ids with 400 "User ID
does not match provider's expected format" — 8 such invitation_failed rows on
21 Aug 2026 all traced back to refill-ingested SN search results. Conversion
requires a get_profile call on the classic api: its response carries the
classic ``provider_id``.

Repair happens at ingestion so a bad id never burns an invite attempt.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..author_identity import normalize_public_slug, slug_from_profile_url

logger = logging.getLogger(__name__)

# Classic member provider ids ("ACoAA…") — the only id shape Unipile's classic
# messaging/invitation endpoints accept besides a public slug. Deliberately
# narrower than author_identity's "^AC…" check, which also matches SN ids.
_CLASSIC_PROVIDER_ID_RE = re.compile(r"^ACo[A-Za-z0-9_-]{10,}$")

# Ingestion batches run inside the scheduler's tick budget; rows past the cap
# are stripped to their slug fallback instead of looked up.
DEFAULT_MAX_LOOKUPS = 50


def is_classic_provider_id(value: Any) -> bool:
    """True when *value* is a classic LinkedIn member provider id (ACoAA…)."""
    if not isinstance(value, str):
        return False
    return bool(_CLASSIC_PROVIDER_ID_RE.match(value.strip()))


def is_sendable_invite_id(value: Any) -> bool:
    """True when Unipile's classic invitation/DM endpoints could accept *value*.

    Accepts a classic member id (ACoAA…) or anything public-slug-shaped.
    Rejects the shapes that draw 400 "User ID does not match provider's
    expected format" and burn the invite attempt: bare numeric ids, SN-space
    ids (``AC…`` that is not classic), and anything containing whitespace —
    contacts from anonymized Sales-Navigator results carry their masked
    display name ("Recruiter at JPMorganChase") in ``linkedin_id``, and the
    send path's provider-id fallback chain reaches it.
    """
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not value or value.isdigit():
        return False
    if any(ch.isspace() for ch in value):
        return False
    if value.startswith("AC") and not is_classic_provider_id(value):
        return False
    return True


def _public_slug(prospect: dict[str, Any]) -> str:
    return (
        normalize_public_slug(prospect.get("public_id"))
        or slug_from_profile_url(prospect.get("linkedin_url"))
        or slug_from_profile_url(prospect.get("profile_url"))
    )


def _has_slug_like_linkedin_id(prospect: dict[str, Any]) -> bool:
    """True when linkedin_id can stand in for the stripped provider_id.

    Non-search collectors put their own keys in ``linkedin_id``; those rows
    must survive a strip even without a public slug. Numeric values are
    company page ids, not send targets.
    """
    lid = str(prospect.get("linkedin_id") or "").strip()
    return bool(lid) and not lid.isdigit()


async def ensure_classic_provider_ids(
    client: Any,
    account_id: str,
    prospects: list[dict[str, Any]],
    max_lookups: int = DEFAULT_MAX_LOOKUPS,
) -> list[dict[str, Any]]:
    """Return only the prospects that are safe to enroll as outreaches.

    Prospects whose provider_id is empty or already classic pass through
    untouched. A non-classic id (SN-space ``ACw…``, bare numeric, …) is
    resolved through a classic-api get_profile on the prospect's public slug;
    on success the classic id replaces it and the original moves to
    ``sales_navigator_provider_id``, otherwise the bad id is stripped so send
    paths fall back to the slug. A prospect left with no usable identifier at
    all is dropped rather than saved — an outreach that can only 400 is worse
    than no outreach.
    """
    kept: list[dict[str, Any]] = []
    lookups = repaired = stripped = rejected = 0

    for prospect in prospects:
        raw_id = str(prospect.get("provider_id") or "").strip()
        if not raw_id or is_classic_provider_id(raw_id):
            kept.append(prospect)
            continue

        slug = _public_slug(prospect)
        resolved = ""
        profile: dict[str, Any] = {}
        if lookups < max_lookups:
            lookups += 1
            identifier = slug or raw_id
            try:
                profile = await client.get_profile(account_id, identifier) or {}
            except Exception as e:
                logger.debug(
                    "Classic profile lookup failed for %s: %s", identifier[:30], e,
                )
                profile = {}
            candidate = str(profile.get("provider_id") or "").strip()
            if is_classic_provider_id(candidate):
                resolved = candidate

        prospect["sales_navigator_provider_id"] = raw_id
        if resolved:
            prospect["provider_id"] = resolved
            if not slug:
                pub = normalize_public_slug(profile.get("public_id"))
                if pub:
                    prospect["public_id"] = pub
            repaired += 1
            kept.append(prospect)
        elif slug or _has_slug_like_linkedin_id(prospect):
            prospect["provider_id"] = ""
            stripped += 1
            kept.append(prospect)
        else:
            rejected += 1

    if repaired or stripped or rejected:
        logger.info(
            "Provider-id repair: %d resolved to classic ids, %d stripped to "
            "slug fallback, %d rejected (no usable identifier), %d lookups",
            repaired, stripped, rejected, lookups,
        )
    return kept
