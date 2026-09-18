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

import re
from typing import Any
from urllib.parse import unquote

# LinkedIn member provider ids look like "ACoAABt_IO4BrjSB0emJu95f…" — base64ish,
# always starting with "AC". Nothing legitimate that short matches, and the
# numeric ids the search feed sends never do.
_PROVIDER_ID_RE = re.compile(r"^AC[A-Za-z0-9_-]{10,}$")

# Slugs that mean "we don't actually have one" (mirrors backend_client._BAD_SLUGS).
_BAD_SLUGS = frozenset({"undefined", "null", "none", "unknown", ""})


def looks_like_provider_id(value: Any) -> bool:
    """True when *value* is a real LinkedIn member provider id (ACoAA…)."""
    if not isinstance(value, str):
        return False
    return bool(_PROVIDER_ID_RE.match(value.strip()))


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
    and for non-strings.

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
    return slug


def slug_from_profile_url(url: Any) -> str:
    """Normalised ``/in/<slug>`` segment of a LinkedIn profile URL, or "".

    ``https://www.linkedin.com/in/Jane-Doe?miniProfileUrn=…`` → ``jane-doe``.
    """
    if not isinstance(url, str) or "/in/" not in url:
        return ""
    return normalize_public_slug(url.split("/in/", 1)[1].split("/")[0])


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
        candidate = normalize_public_slug(raw) or slug_from_profile_url(raw)
        if candidate:
            public_id = candidate
            break
    if not public_id:
        for key in ("profile_url", "public_profile_url", "url", "public_url", "linkedin_url"):
            candidate = slug_from_profile_url(author.get(key))
            if candidate:
                public_id = candidate
                break

    return {"provider_id": provider_id, "public_id": public_id, "numeric_id": numeric_id}


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
