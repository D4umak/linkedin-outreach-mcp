"""Infer a prospect's IANA timezone from their LinkedIn location string.

The reading is ``location_zone`` (the same file as heylead-api's, pinned by the
shared ``tests/fixtures/location_zones.json``): country first, every
single-zone country from tzdata, regions only inside the country they belong
to. Until 17 Oct 2026 this module kept its own keyword table, longest key
first, which read "Seattle, Washington" as New York and "Perth, Scotland" as
Australia, while the api's twin read "Eastern Cape" as New York (#3027).

Anything unrecognised returns None and the caller falls back to the user's
own timezone. A bare "United States" is the api's ``US`` sentinel (six
zones); here it keeps the client's old answer, New York, the commercial
centre, so a coarse location still lands within a few hours.
"""

from __future__ import annotations

from .location_zone import US, location_zone

_US_FALLBACK = "America/New_York"


def infer_timezone(location: str | None) -> str | None:
    """IANA zone for a free-text location, or None when unrecognised."""
    zone = location_zone(location)
    return _US_FALLBACK if zone == US else zone


def infer_from_hints(*hints: str | None) -> str | None:
    """First recognised zone across several location strings (contact
    profile, connections row, global directory), or None."""
    for hint in hints:
        tz = infer_timezone(hint)
        if tz:
            return tz
    return None
