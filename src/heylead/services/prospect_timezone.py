"""Infer a prospect's IANA timezone from their LinkedIn location string.

LinkedIn locations are free text: "London, England, United Kingdom",
"San Francisco Bay Area", "Greater Toronto Area, Canada", "Berlin,
Germany", "Texas, United States". No API, no network: a keyword table
covering countries, US states and Canadian provinces, and the cities and
regions that dominate B2B outreach. Anything unrecognised returns None
and the caller falls back to the user's own timezone.

Countries that span several zones (US, Canada, Australia, Brazil, Russia)
are resolved by state/province/city first; the bare country name maps to
its commercial centre so a coarse location still lands within an hour or
two rather than in UTC.
"""

from __future__ import annotations

import re

# Longest / most specific keys must win, so lookups sort by key length.
_CITIES_REGIONS: dict[str, str] = {
    # UK & Ireland
    "london": "Europe/London", "manchester": "Europe/London", "birmingham": "Europe/London",
    "edinburgh": "Europe/London", "glasgow": "Europe/London", "leeds": "Europe/London",
    "bristol": "Europe/London", "cambridge": "Europe/London", "oxford": "Europe/London",
    "dublin": "Europe/Dublin", "belfast": "Europe/London",
    # US
    "new york": "America/New_York", "nyc": "America/New_York", "boston": "America/New_York",
    "washington": "America/New_York", "philadelphia": "America/New_York", "atlanta": "America/New_York",
    "miami": "America/New_York", "charlotte": "America/New_York", "raleigh": "America/New_York",
    "pittsburgh": "America/New_York", "detroit": "America/New_York", "baltimore": "America/New_York",
    "chicago": "America/Chicago", "austin": "America/Chicago", "dallas": "America/Chicago",
    "houston": "America/Chicago", "minneapolis": "America/Chicago", "nashville": "America/Chicago",
    "st. louis": "America/Chicago", "kansas city": "America/Chicago", "new orleans": "America/Chicago",
    "denver": "America/Denver", "salt lake": "America/Denver", "boulder": "America/Denver",
    "phoenix": "America/Phoenix", "scottsdale": "America/Phoenix",
    "san francisco": "America/Los_Angeles", "bay area": "America/Los_Angeles",
    "los angeles": "America/Los_Angeles", "san diego": "America/Los_Angeles",
    "seattle": "America/Los_Angeles", "portland": "America/Los_Angeles", "san jose": "America/Los_Angeles",
    "silicon valley": "America/Los_Angeles", "las vegas": "America/Los_Angeles",
    "palo alto": "America/Los_Angeles", "oakland": "America/Los_Angeles",
    "honolulu": "Pacific/Honolulu", "anchorage": "America/Anchorage",
    # Canada
    "toronto": "America/Toronto", "ottawa": "America/Toronto", "montreal": "America/Toronto",
    "montréal": "America/Toronto", "vancouver": "America/Vancouver", "calgary": "America/Edmonton",
    "edmonton": "America/Edmonton", "winnipeg": "America/Winnipeg", "halifax": "America/Halifax",
    # Europe
    "paris": "Europe/Paris", "lyon": "Europe/Paris", "berlin": "Europe/Berlin", "munich": "Europe/Berlin",
    "münchen": "Europe/Berlin", "hamburg": "Europe/Berlin", "frankfurt": "Europe/Berlin",
    "cologne": "Europe/Berlin", "amsterdam": "Europe/Amsterdam", "rotterdam": "Europe/Amsterdam",
    "brussels": "Europe/Brussels", "zurich": "Europe/Zurich", "zürich": "Europe/Zurich",
    "geneva": "Europe/Zurich", "vienna": "Europe/Vienna", "madrid": "Europe/Madrid",
    "barcelona": "Europe/Madrid", "lisbon": "Europe/Lisbon", "porto": "Europe/Lisbon",
    "milan": "Europe/Rome", "rome": "Europe/Rome", "stockholm": "Europe/Stockholm",
    "oslo": "Europe/Oslo", "copenhagen": "Europe/Copenhagen", "helsinki": "Europe/Helsinki",
    "warsaw": "Europe/Warsaw", "krakow": "Europe/Warsaw", "kraków": "Europe/Warsaw",
    "prague": "Europe/Prague", "budapest": "Europe/Budapest", "bucharest": "Europe/Bucharest",
    "athens": "Europe/Athens", "kyiv": "Europe/Kyiv", "kiev": "Europe/Kyiv", "lviv": "Europe/Kyiv",
    "istanbul": "Europe/Istanbul", "tallinn": "Europe/Tallinn", "riga": "Europe/Riga",
    "vilnius": "Europe/Vilnius", "moscow": "Europe/Moscow",
    # Middle East / Africa
    "dubai": "Asia/Dubai", "abu dhabi": "Asia/Dubai", "riyadh": "Asia/Riyadh", "doha": "Asia/Qatar",
    "tel aviv": "Asia/Jerusalem", "jerusalem": "Asia/Jerusalem", "cairo": "Africa/Cairo",
    "lagos": "Africa/Lagos", "nairobi": "Africa/Nairobi", "johannesburg": "Africa/Johannesburg",
    "cape town": "Africa/Johannesburg",
    # Asia-Pacific
    "mumbai": "Asia/Kolkata", "bangalore": "Asia/Kolkata", "bengaluru": "Asia/Kolkata",
    "delhi": "Asia/Kolkata", "hyderabad": "Asia/Kolkata", "pune": "Asia/Kolkata", "chennai": "Asia/Kolkata",
    "singapore": "Asia/Singapore", "hong kong": "Asia/Hong_Kong", "shanghai": "Asia/Shanghai",
    "beijing": "Asia/Shanghai", "shenzhen": "Asia/Shanghai", "tokyo": "Asia/Tokyo", "osaka": "Asia/Tokyo",
    "seoul": "Asia/Seoul", "taipei": "Asia/Taipei", "bangkok": "Asia/Bangkok", "jakarta": "Asia/Jakarta",
    "kuala lumpur": "Asia/Kuala_Lumpur", "manila": "Asia/Manila", "ho chi minh": "Asia/Ho_Chi_Minh",
    "karachi": "Asia/Karachi", "lahore": "Asia/Karachi",
    "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne", "brisbane": "Australia/Brisbane",
    "perth": "Australia/Perth", "adelaide": "Australia/Adelaide", "canberra": "Australia/Sydney",
    "auckland": "Pacific/Auckland", "wellington": "Pacific/Auckland",
    # Latin America
    "são paulo": "America/Sao_Paulo", "sao paulo": "America/Sao_Paulo", "rio de janeiro": "America/Sao_Paulo",
    "buenos aires": "America/Argentina/Buenos_Aires", "santiago": "America/Santiago",
    "bogota": "America/Bogota", "bogotá": "America/Bogota", "lima": "America/Lima",
    "mexico city": "America/Mexico_City", "ciudad de méxico": "America/Mexico_City",
    "monterrey": "America/Monterrey", "guadalajara": "America/Mexico_City",
}

_US_STATES: dict[str, str] = {
    "alabama": "America/Chicago", "alaska": "America/Anchorage", "arizona": "America/Phoenix",
    "arkansas": "America/Chicago", "california": "America/Los_Angeles", "colorado": "America/Denver",
    "connecticut": "America/New_York", "delaware": "America/New_York", "florida": "America/New_York",
    "georgia": "America/New_York", "hawaii": "Pacific/Honolulu", "idaho": "America/Boise",
    "illinois": "America/Chicago", "indiana": "America/Indiana/Indianapolis", "iowa": "America/Chicago",
    "kansas": "America/Chicago", "kentucky": "America/New_York", "louisiana": "America/Chicago",
    "maine": "America/New_York", "maryland": "America/New_York", "massachusetts": "America/New_York",
    "michigan": "America/Detroit", "minnesota": "America/Chicago", "mississippi": "America/Chicago",
    "missouri": "America/Chicago", "montana": "America/Denver", "nebraska": "America/Chicago",
    "nevada": "America/Los_Angeles", "new hampshire": "America/New_York", "new jersey": "America/New_York",
    "new mexico": "America/Denver", "north carolina": "America/New_York", "north dakota": "America/Chicago",
    "ohio": "America/New_York", "oklahoma": "America/Chicago", "oregon": "America/Los_Angeles",
    "pennsylvania": "America/New_York", "rhode island": "America/New_York",
    "south carolina": "America/New_York", "south dakota": "America/Chicago", "tennessee": "America/Chicago",
    "texas": "America/Chicago", "utah": "America/Denver", "vermont": "America/New_York",
    "virginia": "America/New_York", "wisconsin": "America/Chicago", "wyoming": "America/Denver",
    "district of columbia": "America/New_York",
}

_CA_PROVINCES: dict[str, str] = {
    "ontario": "America/Toronto", "quebec": "America/Toronto", "québec": "America/Toronto",
    "british columbia": "America/Vancouver", "alberta": "America/Edmonton",
    "manitoba": "America/Winnipeg", "saskatchewan": "America/Regina", "nova scotia": "America/Halifax",
    "new brunswick": "America/Halifax", "newfoundland": "America/St_Johns",
}

_COUNTRIES: dict[str, str] = {
    "united kingdom": "Europe/London", "england": "Europe/London", "scotland": "Europe/London",
    "wales": "Europe/London", "northern ireland": "Europe/London", "ireland": "Europe/Dublin",
    "united states": "America/New_York", "usa": "America/New_York", "canada": "America/Toronto",
    "mexico": "America/Mexico_City", "brazil": "America/Sao_Paulo", "argentina": "America/Argentina/Buenos_Aires",
    "chile": "America/Santiago", "colombia": "America/Bogota", "peru": "America/Lima",
    "france": "Europe/Paris", "germany": "Europe/Berlin", "netherlands": "Europe/Amsterdam",
    "belgium": "Europe/Brussels", "switzerland": "Europe/Zurich", "austria": "Europe/Vienna",
    "spain": "Europe/Madrid", "portugal": "Europe/Lisbon", "italy": "Europe/Rome",
    "sweden": "Europe/Stockholm", "norway": "Europe/Oslo", "denmark": "Europe/Copenhagen",
    "finland": "Europe/Helsinki", "poland": "Europe/Warsaw", "czechia": "Europe/Prague",
    "czech republic": "Europe/Prague", "hungary": "Europe/Budapest", "romania": "Europe/Bucharest",
    "greece": "Europe/Athens", "ukraine": "Europe/Kyiv", "turkey": "Europe/Istanbul",
    "türkiye": "Europe/Istanbul", "estonia": "Europe/Tallinn", "latvia": "Europe/Riga",
    "lithuania": "Europe/Vilnius", "russia": "Europe/Moscow", "bulgaria": "Europe/Sofia",
    "croatia": "Europe/Zagreb", "serbia": "Europe/Belgrade", "slovakia": "Europe/Bratislava",
    "slovenia": "Europe/Ljubljana", "luxembourg": "Europe/Luxembourg", "cyprus": "Asia/Nicosia",
    "malta": "Europe/Malta", "iceland": "Atlantic/Reykjavik",
    "united arab emirates": "Asia/Dubai", "uae": "Asia/Dubai", "saudi arabia": "Asia/Riyadh",
    "qatar": "Asia/Qatar", "israel": "Asia/Jerusalem", "egypt": "Africa/Cairo",
    "nigeria": "Africa/Lagos", "kenya": "Africa/Nairobi", "south africa": "Africa/Johannesburg",
    "india": "Asia/Kolkata", "pakistan": "Asia/Karachi", "singapore": "Asia/Singapore",
    "hong kong": "Asia/Hong_Kong", "china": "Asia/Shanghai", "japan": "Asia/Tokyo",
    "south korea": "Asia/Seoul", "korea": "Asia/Seoul", "taiwan": "Asia/Taipei",
    "thailand": "Asia/Bangkok", "vietnam": "Asia/Ho_Chi_Minh", "indonesia": "Asia/Jakarta",
    "malaysia": "Asia/Kuala_Lumpur", "philippines": "Asia/Manila",
    "australia": "Australia/Sydney", "new zealand": "Pacific/Auckland",
}

# Checked most-specific first: city/region beats state beats country.
_TABLES: tuple[dict[str, str], ...] = (_CITIES_REGIONS, _US_STATES, _CA_PROVINCES, _COUNTRIES)
_ABBREV = {
    "uk": "united kingdom", "u.k.": "united kingdom", "us": "united states", "u.s.": "united states",
    "sf": "san francisco", "la": "los angeles", "dc": "washington", "nz": "new zealand",
}
_TOKEN_RE = re.compile(r"[a-zà-ÿ][a-zà-ÿ.\-]*(?:\s+[a-zà-ÿ][a-zà-ÿ.\-]*)*", re.IGNORECASE)


def _normalise(location: str) -> str:
    text = " ".join(location.lower().replace(",", " , ").split())
    words = []
    for w in text.split():
        words.append(_ABBREV.get(w, w))
    return " ".join(words)


def infer_timezone(location: str | None) -> str | None:
    """IANA zone for a free-text location, or None when unrecognised."""
    if not location or not location.strip():
        return None
    text = _normalise(location)
    for table in _TABLES:
        # longest key first so "new york" beats "york" and "south carolina" beats "carolina"
        for key in sorted(table, key=len, reverse=True):
            if re.search(rf"(?<![a-zà-ÿ]){re.escape(key)}(?![a-zà-ÿ])", text):
                return table[key]
    return None


def infer_from_hints(*hints: str | None) -> str | None:
    """First recognised zone across several location strings (contact
    profile, connections row, global directory), or None."""
    for hint in hints:
        tz = infer_timezone(hint)
        if tz:
            return tz
    return None
