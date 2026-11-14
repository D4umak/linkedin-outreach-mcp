"""Author identity parsing for LinkedIn post-search results (issue #64).

Upstream post search sends no ``provider_id``; the ``id`` on the author
object is a numeric internal id that is NOT a per-author identity (one
numeric value was shared by 19 different author names on the live DB, and
some are company ids). The old parser wrote that numeric into
``signals.linkedin_id``, where it could never match ``contacts.linkedin_id``
(an ACoAA provider id), so watchlist signals never matched campaign contacts.

This module classifies every identity field the author object may carry:

* ``provider_id`` — a real LinkedIn member provider id (``ACoAA…``). Preferred
  value for ``signals.linkedin_id``.
* ``public_id`` — the public profile slug (the ``/in/<slug>`` path segment).
  Also a valid ``signals.linkedin_id`` so activation can match the person
  when search only returned a slug.
* ``numeric_id`` — the ambiguous numeric id, kept for metadata only. Never
  written to ``signals.linkedin_id`` (one numeric was shared by 19 authors).

Stdlib-only on purpose: it is imported from both ``linkedin`` clients and
``db.signal_queries`` and must never create an import cycle.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote

# LinkedIn member provider ids look like "ACoAABt_IO4BrjSB0emJu95f…" — base64ish,
# always starting with "AC". Nothing legitimate that short matches, and the
# numeric ids the search feed sends never do.
# 14 Nov 2026: Include ACwAA and ADoAA prefixes.
_PROVIDER_ID_RE = re.compile(r"^A[CD][owO]AA[A-Za-z0-9_-]{7,}$")

# Slugs that mean "we don't actually have one" (mirrors backend_client._BAD_SLUGS).
_BAD_SLUGS = frozenset({"undefined", "null", "none", "unknown", ""})


def looks_like_provider_id(value: Any) -> bool:
    """True when *value* is a real LinkedIn member provider id (ACoAA…)."""
    if not isinstance(value, str):
        return False
    val = value.strip()
    return bool(_PROVIDER_ID_RE.match(val))


def provider_id_from(linkedin_id: Any, profile_json: Any = "") -> str:
    """The member id (``ACoAA…``) a stored row carries, from either place.

    A contact keeps that id in ``profile_json["provider_id"]`` when a profile
    blob was stored and in the ``linkedin_id`` column when it was not, and the
    caller does not get to know which. Reading one place alone makes an
    id-carrying row look like a row with no id -- see
    docs/incidents/2026-09-22-a-provider-id-was-read-from-one-of-the-two-places-it-lives.md.

    ``linkedin_id`` also holds vanity slugs, so the column counts only when it
    actually carries a member id. Deliberately the narrow ``ACoAA/ACwAA/ADoAA``
    test and not ``looks_like_provider_id`` above: the callers pass this
    straight to Unipile's classic endpoints, which reject a Sales Navigator
    ``AC…`` id.

    Pure and stdlib-only, like the rest of this module, so it is callable from
    the event-loop thread -- the reason it does not live beside the row
    queries that use it.
    """
    blob = profile_json
    if isinstance(blob, str):
        try:
            blob = json.loads(blob) if blob else {}
        except (ValueError, TypeError):
            blob = {}
    if isinstance(blob, dict):
        pid = str(blob.get("provider_id") or "").strip()
        if pid:
            return pid
    raw = str(linkedin_id or "").strip()
    if raw.startswith(("ACoAA", "ACwAA", "ADoAA")):
        return raw
    return ""


def contact_provider_id(contact: dict[str, Any] | None) -> str:
    """``provider_id_from`` for a whole stored contact row."""
    row = contact or {}
    return provider_id_from(row.get("linkedin_id"), row.get("profile_json") or "")


def _last_urn_segment(value: str) -> str:
    """'urn:li:member:123' → '123'; plain values pass through unchanged."""
    if ":" in value:
        return value.rsplit(":", 1)[-1]
    return value


def normalize_public_slug(value: Any) -> str:
    """Normalise a public-profile slug for joining: lowercase, no query/junk.

    Accepts a bare slug ("Jane-Doe"), a percent-encoded one, or anything with
    query-string/fragment/trailing-slash residue. Returns "" for junk
    placeholders ("undefined", "null", …), for anything carrying whitespace,
    for provider-id shapes (ACoAA...), and for non-strings.

    Whitespace is what separates a slug from a description. LinkedIn does not
    name an anonymous profile viewer, it labels them — "Someone at ExampleBank" —
    and profile_view_collector passed that label here as a pseudo-ID. Lower-
    casing it produced "someone at examplebank", which is not in _BAD_SLUGS and
    carries no "/", so it qualified as an identity: save_signal stored it and
    the hosted backend built `/in/Someone at ExampleBank` from it. No slug can
    hold a space — it would not survive the URL. Checked after unquote, so an
    encoded space cannot walk through either. Measured over the 7,563
    identifiers this workspace has stored, this rejects 52, every one a label,
    and changes no real slug (they carry "&", "’" and percent escapes, which
    is why the rule is whitespace and not a charset).
    """
    if not isinstance(value, str):
        return ""
    slug = unquote(value).strip()
    slug = slug.split("?")[0].split("#")[0].strip("/").strip().lower()
    if slug in _BAD_SLUGS or "/" in slug or any(ch.isspace() for ch in slug):
        return ""
    if slug.startswith(("acoaa", "acwaa", "adoaa")):
        return ""
    return slug


def slug_from_profile_url(url: Any) -> str:
    """Raw ``/in/<slug>`` segment of a LinkedIn profile URL, or "".

    ``https://www.linkedin.com/in/Jane-Doe?miniProfileUrn=…`` → ``Jane-Doe``.
    """
    if not isinstance(url, str) or "/in/" not in url:
        return ""
    return unquote(url).strip().split("/in/", 1)[1].split("?")[0].split("#")[0].strip("/")


def parse_author_identity(author: dict[str, Any] | None) -> dict[str, str]:
    """Classify every identity field a post-search author object may carry.

    Returns ``{"provider_id": …, "public_id": …, "numeric_id": …}`` with ""
    for anything absent. Never raises on malformed input — an unparseable
    author simply yields empty fields (signals still get saved, unmatched).
    """
    provider_id = ""
    public_id = ""
    numeric_id = ""

    if not isinstance(author, dict):
        return {"provider_id": "", "public_id": "", "numeric_id": ""}

    # Id-shaped fields: classify, never trust position. provider_id first so a
    # real provider id wins over a numeric in "id".
    for key in ("provider_id", "id", "member_id", "author_id"):
        raw = author.get(key)
        if raw is None or isinstance(raw, (dict, list)):
            continue
        value = _last_urn_segment(str(raw).strip())
        if not value:
            continue
        if looks_like_provider_id(value):
            if not provider_id:
                provider_id = value
        elif value.isdigit():
            if not numeric_id:
                numeric_id = value

    # Public slug: explicit fields first, then any profile-URL variant.
    for key in ("public_identifier", "public_id"):
        raw = author.get(key)
        candidate = normalize_public_slug(raw) or normalize_public_slug(slug_from_profile_url(raw))
        if candidate:
            public_id = candidate
            break
    if not public_id:
        for key in ("profile_url", "public_profile_url", "url", "public_url", "linkedin_url"):
            candidate = normalize_public_slug(slug_from_profile_url(author.get(key)))
            if candidate:
                public_id = candidate
                break

    return {"provider_id": provider_id, "public_id": public_id, "numeric_id": numeric_id}


PRIVATE_KEYS = frozenset({"content", "comment_text", "email", "phone", "contact_info", "messages"})


def is_private_key(key: str) -> bool:
    """True when *key* names a field that carries private PII / context."""
    return key.lower() in PRIVATE_KEYS


def public_profile(payload: Any) -> dict[str, Any]:
    """Recursively strip private keys from a profile / signal metadata payload."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
    if not isinstance(payload, dict):
        return {}

    cleaned = {}
    for k, v in payload.items():
        if is_private_key(k):
            continue
        if isinstance(v, dict):
            cleaned[k] = public_profile(v)
        elif isinstance(v, list):
            cleaned[k] = [
                public_profile(item) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            cleaned[k] = v
    return cleaned


def author_payload_identity(payload: Mapping[str, Any]) -> tuple[str, str, str]:
    """Extract (provider_id, public_id, linkedin_url) from an author payload.

    Provider IDs are preserved case-sensitively and never used to fabricate
    /in/ URLs. Public IDs are normalized (lowercase).
    """
    prov_id = ""
    pub_id = ""
    url = ""

    # 1. Collect all candidates
    id_candidates = []
    for key in ("provider_id", "id", "member_id", "author_id", "linkedin_id", "sender_id"):
        val = _last_urn_segment(str(payload.get(key) or "").strip())
        if val:
            id_candidates.append(val)

    slug_candidates = []
    for key in ("public_id", "public_identifier", "publicIdentifier"):
        val = str(payload.get(key) or "").strip()
        if val:
            slug_candidates.append(val)

    url_candidates = []
    for key in ("profile_url", "public_profile_url", "url", "linkedin_url"):
        val = str(payload.get(key) or "").strip()
        if val:
            url_candidates.append(val)

    # 2. Find provider_id (case-sensitive)
    for val in id_candidates + slug_candidates:
        if looks_like_provider_id(val):
            prov_id = val
            break

    if not prov_id:
        for val in url_candidates:
            seg = slug_from_profile_url(val)
            if looks_like_provider_id(seg):
                prov_id = seg
                break

    # 3. Find public_id (normalized)
    for val in id_candidates + slug_candidates:
        norm = normalize_public_slug(val)
        if norm:
            pub_id = norm
            break

    if not pub_id:
        for val in url_candidates:
            norm = normalize_public_slug(slug_from_profile_url(val))
            if norm:
                pub_id = norm
                break

    # 4. Find linkedin_url
    for val in url_candidates:
        if "/in/" in val.lower():
            seg = slug_from_profile_url(val)
            # Must be a real vanity URL, not a provider ID in disguise
            if not looks_like_provider_id(seg) and normalize_public_slug(seg):
                url = val
                break

    # 5. Cross-pollinate
    if url and not pub_id:
        pub_id = normalize_public_slug(slug_from_profile_url(url))
    if not url and pub_id:
        url = f"https://www.linkedin.com/in/{pub_id}"

    return prov_id, pub_id, url


def sendable_person_id(*, provider_id: str = "", public_id: str = "") -> str:
    """Id we can store on a person signal and later activate.

    Provider id wins. A public slug is enough for matching and activation.
    Pure numerics never qualify — they are not a per-author identity.
    """
    if looks_like_provider_id(provider_id):
        return provider_id.strip()
    for raw in (public_id, provider_id):
        slug = normalize_public_slug(raw)
        if slug and not slug.isdigit():
            return slug
    return ""
