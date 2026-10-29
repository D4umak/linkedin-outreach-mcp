"""Where a LinkedIn location string is: its IANA zone, country first.

One file, two repositories: heylead-api ``app/services/location_zone.py`` and
the heylead client ``src/heylead/services/location_zone.py`` are the same
bytes, and ``tests/fixtures/location_zones.json`` (the same file in both)
pins their answers. Edit both or neither (#3027).

How a string is read, and why:

1. The country comes first. The comma parts are read from the last one back,
   and a part that IS a country's name (or a US state, a Canadian province,
   an Australian state) names the country. The old resolver walked one
   keyword table in row order with ``token in loc``: "Eastern Cape, South
   Africa" hit the "eastern" row (New York), "Central Java, Indonesia" the
   "central" row (Chicago), "Tbilisi, Georgia" the US state, "Indianapolis"
   the "india" row, and every African country returned nothing, so an owner
   in Lagos sent on London hours and a prospect in Lahore was messaged at
   any hour of the night (17 Oct 2026, 18 prospects in active campaigns).
2. A country with one zone answers with it: every country in the IANA
   ``iso3166.tab``/``zone.tab`` tables that ``tzdata`` ships, so a country
   is never missing because nobody typed it in.
3. A country with several zones answers from the region or city it names,
   matched as whole words and only among that country's regions; a bare
   name answers with the zone nearly all of it keeps, or None when there is
   none (Canada, Russia, Indonesia). A bare "United States" stays the ``US``
   sentinel: the prospect gate waits for all four continental zones.
4. A string that names no country ("San Francisco Bay Area", "Zürich
   Metropolitan Area") is read against the city table, whole words only.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

US = "US"

# ── Countries the IANA tables give several zones, and the zone a bare name keeps ──
# A bare name keeps a zone only where nearly everyone in the country shares
# its clock (Spain's Canaries, Portugal's Azores, Germany's Büsingen and
# China's Xinjiang aside). Australia, Brazil and Mexico keep the zone the
# old resolver gave them, so no prospect there moves (the #3027 guardrail).
_BARE_MULTI_ZONE: dict[str, str | None] = {
    "AR": "America/Argentina/Buenos_Aires", "AU": "Australia/Sydney", "BR": "America/Sao_Paulo",
    "CA": None, "CD": None, "CL": "America/Santiago", "CN": "Asia/Shanghai", "CY": "Asia/Nicosia",
    "DE": "Europe/Berlin", "EC": "America/Guayaquil", "ES": "Europe/Madrid", "FM": None, "GL": None,
    "ID": None, "KI": None, "KZ": "Asia/Almaty", "MH": "Pacific/Majuro", "MN": "Asia/Ulaanbaatar",
    "MX": "America/Mexico_City", "MY": "Asia/Kuala_Lumpur", "NZ": "Pacific/Auckland",
    "PF": "Pacific/Tahiti", "PG": "Pacific/Port_Moresby", "PS": "Asia/Gaza", "PT": "Europe/Lisbon",
    "RU": None, "UA": "Europe/Kyiv", "UM": None, "US": US, "UZ": "Asia/Tashkent", "AQ": None,
}

# Regions and cities inside a multi-zone country, most specific first.
_REGIONS: dict[str, list[tuple[tuple[str, ...], str]]] = {
    "US": [
        (("honolulu", "hawaii"), "Pacific/Honolulu"),
        (("anchorage", "alaska"), "America/Anchorage"),
        (("washington dc", "washington d.c.", "district of columbia", "dc"), "America/New_York"),
        (("phoenix", "tucson", "scottsdale", "arizona"), "America/Phoenix"),
        (("indianapolis", "indiana", "terre haute"), "America/Indiana/Indianapolis"),
        (("detroit", "michigan"), "America/Detroit"),
        (("boise", "idaho"), "America/Boise"),
        (("san francisco", "san francisco bay", "los angeles", "seattle", "portland", "las vegas",
          "sacramento", "san diego", "san jose", "silicon valley", "california", "oregon",
          "washington", "nevada", "bend"), "America/Los_Angeles"),
        (("denver", "salt lake city", "albuquerque", "santa fe", "colorado", "utah", "new mexico",
          "montana", "wyoming"), "America/Denver"),
        (("knoxville", "chattanooga"), "America/New_York"),
        (("chicago", "dallas", "houston", "austin", "san antonio", "minneapolis", "nashville",
          "kansas city", "milwaukee", "st. louis", "saint louis", "new orleans", "birmingham",
          "memphis", "oklahoma city", "omaha", "little rock", "sioux falls", "waco", "texas",
          "illinois", "wisconsin", "minnesota", "iowa", "missouri", "louisiana", "tennessee",
          "arkansas", "nebraska", "oklahoma", "kansas", "alabama", "mississippi", "north dakota",
          "south dakota"), "America/Chicago"),
        (("new york", "new york city", "nyc", "boston", "miami", "atlanta", "philadelphia",
          "charlotte", "pittsburgh", "raleigh", "durham", "chapel hill", "baltimore", "orlando",
          "tampa", "tampa bay", "lynchburg", "gainesville", "cleveland", "columbus", "cincinnati",
          "erie", "augusta", "charleston", "asheville", "homosassa springs", "florida", "georgia",
          "ohio", "virginia", "west virginia", "north carolina", "south carolina", "maryland",
          "massachusetts", "connecticut", "new jersey", "pennsylvania", "delaware", "rhode island",
          "vermont", "new hampshire", "maine", "kentucky"), "America/New_York"),
    ],
    "CA": [
        (("vancouver", "victoria", "british columbia"), "America/Vancouver"),
        (("calgary", "edmonton", "alberta"), "America/Edmonton"),
        (("regina", "saskatoon", "saskatchewan"), "America/Regina"),
        (("winnipeg", "manitoba"), "America/Winnipeg"),
        (("halifax", "nova scotia", "new brunswick", "prince edward island"), "America/Halifax"),
        (("st. john's", "newfoundland", "newfoundland and labrador"), "America/St_Johns"),
        (("toronto", "ottawa", "montreal", "quebec", "ontario", "mississauga", "waterloo"),
         "America/Toronto"),
    ],
    "AU": [
        (("perth", "western australia"), "Australia/Perth"),
        (("adelaide", "south australia"), "Australia/Adelaide"),
        (("darwin", "northern territory"), "Australia/Darwin"),
        (("brisbane", "gold coast", "queensland"), "Australia/Brisbane"),
        (("hobart", "tasmania"), "Australia/Hobart"),
        (("melbourne", "victoria"), "Australia/Melbourne"),
        (("sydney", "canberra", "new south wales", "australian capital territory"),
         "Australia/Sydney"),
    ],
    "BR": [
        (("manaus", "amazonas"), "America/Manaus"),
        (("recife", "pernambuco"), "America/Recife"),
        (("fortaleza", "ceara"), "America/Fortaleza"),
        (("sao paulo", "rio de janeiro", "belo horizonte", "brasilia", "curitiba", "porto alegre"),
         "America/Sao_Paulo"),
    ],
    "MX": [
        (("cancun", "quintana roo"), "America/Cancun"),
        (("tijuana", "baja california"), "America/Tijuana"),
        (("mexico city", "guadalajara", "monterrey", "puebla"), "America/Mexico_City"),
    ],
    "RU": [
        (("moscow", "saint petersburg", "st. petersburg", "kazan"), "Europe/Moscow"),
        (("yekaterinburg",), "Asia/Yekaterinburg"),
        (("novosibirsk",), "Asia/Novosibirsk"),
        (("vladivostok",), "Asia/Vladivostok"),
        (("kaliningrad",), "Europe/Kaliningrad"),
    ],
    "ID": [
        (("bali", "sulawesi", "makassar", "denpasar", "west nusa tenggara", "lombok"),
         "Asia/Makassar"),
        (("papua", "jayapura", "maluku"), "Asia/Jayapura"),
        (("jakarta", "java", "bandung", "surabaya", "yogyakarta", "sumatra", "medan", "banten",
          "tangerang"), "Asia/Jakarta"),
    ],
    "ES": [(("canary islands", "tenerife", "las palmas", "gran canaria"), "Atlantic/Canary")],
    "PT": [(("azores",), "Atlantic/Azores")],
    "KZ": [(("almaty", "astana"), "Asia/Almaty")],
    "CD": [(("kinshasa",), "Africa/Kinshasa"), (("lubumbashi",), "Africa/Lubumbashi")],
}

# A state, province or Australian state seen as a whole comma part names its
# country, so "Austin, Texas" is in the United States without saying so.
# Georgia is left out: as a whole part it is the country unless the string
# names a US city or the United States as well.
_REGION_COUNTRY: dict[str, str] = {}
for _country, _rows in _REGIONS.items():
    if _country not in ("US", "CA", "AU"):
        continue
    for _names, _zone in _rows:
        for _name in _names:
            if " " in _name or _name in (
                "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
                "delaware", "florida", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas",
                "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota",
                "mississippi", "missouri", "montana", "nebraska", "nevada", "ohio", "oklahoma",
                "oregon", "pennsylvania", "tennessee", "texas", "utah", "vermont", "virginia",
                "wisconsin", "wyoming", "alberta", "ontario", "quebec", "manitoba", "saskatchewan",
                "queensland", "tasmania",
            ):
                _REGION_COUNTRY.setdefault(_name, _country)

_US_STATE_CODES: dict[str, str] = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california",
    "co": "colorado", "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia",
    "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa",
    "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada", "nh": "new hampshire",
    "nj": "new jersey", "nm": "new mexico", "ny": "new york", "nc": "north carolina",
    "nd": "north dakota", "oh": "ohio", "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania",
    "ri": "rhode island", "sc": "south carolina", "sd": "south dakota", "tn": "tennessee",
    "tx": "texas", "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington",
    "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
}
_CA_PROVINCE_CODES: dict[str, str] = {
    "ab": "alberta", "bc": "british columbia", "mb": "manitoba", "nb": "new brunswick",
    "nl": "newfoundland and labrador", "ns": "nova scotia", "on": "ontario",
    "pe": "prince edward island", "qc": "quebec", "sk": "saskatchewan",
}

# Names LinkedIn and people write that the IANA table spells otherwise.
_COUNTRY_ALIASES: dict[str, str] = {
    "uk": "GB", "u.k.": "GB", "united kingdom": "GB", "great britain": "GB", "britain": "GB",
    "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "usa": "US", "u.s.": "US", "u.s.a.": "US", "us": "US", "united states of america": "US",
    "uae": "AE", "czechia": "CZ", "south korea": "KR", "republic of korea": "KR", "korea": "KR",
    "north korea": "KP", "russian federation": "RU", "turkiye": "TR", "viet nam": "VN",
    "the netherlands": "NL", "holland": "NL", "ivory coast": "CI", "cote d'ivoire": "CI",
    "democratic republic of the congo": "CD", "dr congo": "CD", "drc": "CD",
    "republic of the congo": "CG", "congo": "CG", "myanmar": "MM", "burma": "MM",
    "eswatini": "SZ", "swaziland": "SZ", "cabo verde": "CV", "timor-leste": "TL",
    "macedonia": "MK", "bosnia and herzegovina": "BA", "trinidad and tobago": "TT",
    "hong kong sar": "HK", "macao": "MO", "macau sar": "MO", "palestinian territories": "PS",
    "vatican": "VA", "holy see": "VA", "samoa": "WS", "mainland china": "CN",
    "people's republic of china": "CN", "taiwan, province of china": "TW", "kyrgyz republic": "KG",
    "lao pdr": "LA", "brunei darussalam": "BN", "saint kitts and nevis": "KN",
    "saint vincent and the grenadines": "VC", "saint lucia": "LC", "sao tome and principe": "ST",
    "the bahamas": "BS", "the gambia": "GM", "micronesia": "FM", "kosovo": "RS",
}

_UK_METRO_TOWNS: tuple[str, ...] = (
    "cambridge", "oxford", "reading", "portsmouth", "chester", "brighton", "preston", "leicester",
    "belfast", "salisbury", "newport", "aberdeen",
)

# Cities read when no part names a country, whole words only. A city here is
# unambiguous enough to stand alone; "Birmingham" and "Cambridge" are not
# (England or Alabama, England or Massachusetts) and wait for a country.
_CITIES: list[tuple[tuple[str, ...], str]] = [
    (("london", "manchester", "edinburgh", "glasgow", "leeds", "liverpool", "bristol",
      "bournemouth", "cardiff", "sheffield", "colchester", "tyneside", "coventry", "plymouth",
      "derby", "nottingham", "ipswich", "southampton", "stoke-on-trent", "cheltenham", "hove",
      "harrow", "sidcup", "brentwood", "welwyn garden city", "stansted mountfitchet", "harlow",
      "bradford", "slough", "southwark", "tonbridge", "lichfield", "claygate", "kingsley",
      "buckhurst hill"), "Europe/London"),
    (("dublin",), "Europe/Dublin"),
    (("zurich", "zuerich", "geneva", "basel", "bern"), "Europe/Zurich"),
    (("berlin", "munich", "frankfurt", "hamburg", "stuttgart", "rhein-neckar", "kiel", "ruhr"),
     "Europe/Berlin"),
    (("paris", "rennes", "bordeaux", "lille", "strasbourg", "amiens", "angers", "lyon"),
     "Europe/Paris"),
    (("amsterdam",), "Europe/Amsterdam"),
    (("brussels", "ghent"), "Europe/Brussels"),
    (("madrid", "barcelona", "valladolid", "pamplona", "algeciras", "torrevieja", "malaga"),
     "Europe/Madrid"),
    (("milan", "rome"), "Europe/Rome"), (("vienna",), "Europe/Vienna"),
    (("stockholm", "gothenburg", "uppsala", "malmo"), "Europe/Stockholm"),
    (("copenhagen",), "Europe/Copenhagen"),
    (("oslo",), "Europe/Oslo"), (("helsinki",), "Europe/Helsinki"),
    (("tallinn",), "Europe/Tallinn"), (("lisbon", "porto", "braga"), "Europe/Lisbon"),
    (("warsaw", "wroclaw", "poznan", "cracow", "krakow"), "Europe/Warsaw"),
    (("prague",), "Europe/Prague"), (("bratislava",), "Europe/Bratislava"),
    (("budapest",), "Europe/Budapest"), (("ljubljana",), "Europe/Ljubljana"),
    (("athens",), "Europe/Athens"), (("bucharest", "timisoara"), "Europe/Bucharest"),
    (("kyiv", "kiev", "dnipro"), "Europe/Kyiv"),
    (("istanbul",), "Europe/Istanbul"), (("tel aviv",), "Asia/Jerusalem"),
    (("dubai", "abu dhabi"), "Asia/Dubai"), (("riyadh",), "Asia/Riyadh"),
    (("lagos",), "Africa/Lagos"), (("nairobi",), "Africa/Nairobi"),
    (("cape town", "johannesburg"), "Africa/Johannesburg"), (("cairo",), "Africa/Cairo"),
    (("karachi", "lahore"), "Asia/Karachi"),
    (("ahmedabad", "jaipur", "mumbai", "delhi", "new delhi", "bangalore", "bengaluru", "hyderabad",
      "chennai", "pune", "kolkata"), "Asia/Kolkata"),
    (("singapore",), "Asia/Singapore"), (("hong kong",), "Asia/Hong_Kong"),
    (("kuala lumpur",), "Asia/Kuala_Lumpur"),
    (("beijing", "shanghai", "shenzhen", "guangzhou"), "Asia/Shanghai"),
    (("tokyo", "osaka"), "Asia/Tokyo"), (("seoul",), "Asia/Seoul"),
    (("manila", "davao"), "Asia/Manila"),
    (("sydney",), "Australia/Sydney"), (("melbourne",), "Australia/Melbourne"),
    (("brisbane",), "Australia/Brisbane"),
    (("auckland",), "Pacific/Auckland"), (("sao paulo",), "America/Sao_Paulo"),
    (("santiago",), "America/Santiago"),
    (("mexico city",), "America/Mexico_City"),
    (("toronto", "montreal", "ottawa"), "America/Toronto"),
    (("vancouver",), "America/Vancouver"), (("calgary",), "America/Edmonton"),
    (("new york", "new york city", "nyc", "charlotte", "pittsburgh", "raleigh", "durham",
      "chapel hill", "orlando", "tampa", "tampa bay", "cincinnati", "knoxville", "chattanooga",
      "lynchburg", "gainesville", "erie", "augusta", "charleston", "asheville",
      "homosassa springs"), "America/New_York"),
    (("san francisco", "san francisco bay", "silicon valley", "los angeles", "sacramento",
      "bend"), "America/Los_Angeles"),
    (("chicago", "dallas", "houston", "austin", "minneapolis", "milwaukee", "little rock",
      "sioux falls", "waco"), "America/Chicago"),
    (("boston", "atlanta", "miami", "washington dc", "philadelphia"), "America/New_York"),
    (("seattle",), "America/Los_Angeles"), (("denver", "albuquerque", "santa fe"), "America/Denver"),
    (("phoenix", "tucson", "scottsdale"), "America/Phoenix"),
    (("indianapolis", "terre haute"), "America/Indiana/Indianapolis"),
    (("detroit",), "America/Detroit"), (("boise",), "America/Boise"),
]

_DECOR = re.compile(r"\b(greater|metropolitan|metro|area|region|city of)\b")


def _norm(text: str) -> str:
    """Lower case, accents off, '&' as 'and', single spaces."""
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    text = text.lower().replace("&", " and ").replace("’", "'")
    return re.sub(r"\s+", " ", text).strip()


def _has(text: str, term: str) -> bool:
    """``term`` occurs in ``text`` as whole words.

    ``findall``, not ``search``: heylead-api's async-dependency guard walks the
    call graph by name, and an app function called ``search`` binds a
    workspace (tests/test_workspace_dependencies_are_async.py).
    """
    return bool(re.findall(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text))


@lru_cache(maxsize=1)
def _tables() -> tuple[dict[str, str], dict[str, list[str]]]:
    """Country name -> ISO code, ISO code -> its zones, from the IANA tables."""
    from importlib import resources

    root = resources.files("tzdata").joinpath("zoneinfo")
    names: dict[str, str] = {}
    for line in root.joinpath("iso3166.tab").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        code, name = line.split("\t", 1)
        full = _norm(name)
        names[full] = code
        # "Korea (South)" -> "korea south"; "Britain (UK)" -> "britain"; "Bosnia & Herzegovina".
        bare = re.sub(r"\s*\(.*?\)", "", full).strip()
        if bare and bare not in names:
            names[bare] = code
        names.setdefault(full.replace("(", "").replace(")", ""), code)
        names.setdefault(full.replace("st ", "saint ", 1) if full.startswith("st ") else full, code)
    for alias, code in _COUNTRY_ALIASES.items():
        names[_norm(alias)] = code
    zones: dict[str, list[str]] = {}
    for line in root.joinpath("zone.tab").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        for code in parts[0].split(","):
            zones.setdefault(code, []).append(parts[2])
    return names, zones


def _country_zone(code: str, text: str) -> str | None:
    """The zone of ``code``, from a region named in ``text`` when it has several."""
    for names, zone in _REGIONS.get(code, []):
        if any(_has(text, n) for n in names):
            return zone
    _, zones = _tables()
    own = zones.get(code) or []
    if len(own) == 1:
        return own[0]
    return _BARE_MULTI_ZONE.get(code)


def _names_a_us_place(text: str) -> bool:
    return any(_has(text, n) for names, _ in _REGIONS["US"] for n in names if n != "georgia") or any(
        _has(text, n) for n in ("united states", "usa", "u.s.")
    )


@lru_cache(maxsize=4096)
def location_zone(location: str | None) -> str | None:
    """IANA zone of a LinkedIn location, ``US`` for a bare United States, else None."""
    text = _norm(location or "")
    if not text:
        return None
    names, _ = _tables()
    parts = [_DECOR.sub(" ", p).strip() for p in text.split(",")]
    parts = [re.sub(r"\s+", " ", p) for p in parts if p.strip()]
    for i in range(len(parts) - 1, -1, -1):
        part = parts[i]
        if part == "georgia":
            code = "US" if _names_a_us_place(text.replace("georgia", " ")) else "GE"
            return _country_zone(code, text)
        if part in names:
            return _country_zone(names[part], text)
        if part in _REGION_COUNTRY:
            return _country_zone(_REGION_COUNTRY[part], text)
        if i > 0:
            if part in _US_STATE_CODES:
                return _country_zone("US", f"{text} {_US_STATE_CODES[part]}")
            if part in _CA_PROVINCE_CODES:
                return _country_zone("CA", f"{text} {_CA_PROVINCE_CODES[part]}")
    flat = " ".join(parts)
    if _DECOR.findall(text) and any(_has(flat, n) for n in _UK_METRO_TOWNS):
        return "Europe/London"
    for city_names, zone in _CITIES:
        if any(_has(flat, n) for n in city_names):
            return zone
    # A country named inside a part ("Lagos State Nigeria", "Remote - Kenya").
    for name in sorted(names, key=len, reverse=True):
        if len(name) > 3 and _has(flat, name):
            return _country_zone(names[name], text)
    return None


def names_a_single_zone_country(location: str | None) -> bool:
    """Whether some comma part of ``location`` is the name of a one-zone country."""
    names, zones = _tables()
    for part in _norm(location or "").split(","):
        code = names.get(re.sub(r"\s+", " ", _DECOR.sub(" ", part)).strip())
        if code and len(zones.get(code) or []) == 1:
            return True
    return False
