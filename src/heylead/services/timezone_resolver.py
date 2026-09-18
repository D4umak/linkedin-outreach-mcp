"""Map LinkedIn location strings to IANA zones and DST-aware business hours."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_BUSINESS_START = 8
DEFAULT_BUSINESS_END = 18
DEFAULT_BUSINESS_DAYS = [0, 1, 2, 3, 4]

US_CONTINENTAL = (
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
)

_LOCATION_TO_IANA: list[tuple[list[str], str]] = [
    (["hawaii", "honolulu"], "Pacific/Honolulu"),
    (["alaska", "anchorage"], "America/Anchorage"),
    (["pacific", "san francisco", "bay area", "los angeles", "seattle",
      "portland", "las vegas", "sacramento", "san diego", "silicon valley",
      "california", "oregon", "washington state", "nevada"], "America/Los_Angeles"),
    (["phoenix", "arizona"], "America/Phoenix"),
    (["mountain", "denver", "salt lake", "boise",
      "colorado", "utah", "new mexico", "montana", "idaho", "wyoming"],
     "America/Denver"),
    (["central", "chicago", "dallas", "houston", "austin", "minneapolis",
      "nashville", "kansas city", "milwaukee", "san antonio",
      "texas", "illinois", "wisconsin", "minnesota", "iowa", "missouri",
      "louisiana", "tennessee", "arkansas", "nebraska", "oklahoma"],
     "America/Chicago"),
    (["eastern", "new york", "boston", "miami", "atlanta", "washington dc",
      "philadelphia", "charlotte", "pittsburgh", "detroit", "raleigh",
      "florida", "georgia", "ohio", "michigan", "virginia", "north carolina",
      "south carolina", "maryland", "massachusetts", "connecticut",
      "new jersey", "pennsylvania", "delaware", "district of columbia"],
     "America/New_York"),
    (["toronto", "ottawa", "montreal", "quebec"], "America/Toronto"),
    (["vancouver", "british columbia"], "America/Vancouver"),
    (["calgary", "edmonton", "alberta"], "America/Edmonton"),
    (["winnipeg", "manitoba", "saskatchewan"], "America/Winnipeg"),
    (["halifax", "nova scotia", "new brunswick"], "America/Halifax"),
    (["london", "united kingdom", "england", "scotland", "wales",
      "ireland", "dublin", "belfast", "manchester", "birmingham", "leeds",
      "glasgow", "liverpool", "edinburgh", "bristol", "cambridge", "oxford"],
     "Europe/London"),
    ([" uk", "uk,", "uk "], "Europe/London"),
    (["stockholm", "sweden"], "Europe/Stockholm"),
    (["zurich", "switzerland"], "Europe/Zurich"),
    (["berlin", "munich", "frankfurt", "germany"], "Europe/Berlin"),
    (["paris", "france"], "Europe/Paris"),
    (["amsterdam", "netherlands"], "Europe/Amsterdam"),
    (["brussels", "belgium"], "Europe/Brussels"),
    (["madrid", "barcelona", "spain"], "Europe/Madrid"),
    (["rome", "milan", "italy"], "Europe/Rome"),
    (["vienna", "austria"], "Europe/Vienna"),
    (["copenhagen", "denmark"], "Europe/Copenhagen"),
    (["oslo", "norway"], "Europe/Oslo"),
    (["helsinki", "finland"], "Europe/Helsinki"),
    (["lisbon", "portugal"], "Europe/Lisbon"),
    (["warsaw", "poland"], "Europe/Warsaw"),
    (["prague", "czech"], "Europe/Prague"),
    (["budapest", "hungary"], "Europe/Budapest"),
    (["athens", "greece"], "Europe/Athens"),
    (["kyiv", "ukraine"], "Europe/Kyiv"),
    (["istanbul", "turkey"], "Europe/Istanbul"),
    (["sao paulo", "rio de janeiro", "belo horizonte", "brasil", "brazil"],
     "America/Sao_Paulo"),
    (["mexico city", "guadalajara", "monterrey", "mexico"], "America/Mexico_City"),
    (["tokyo", "osaka", "japan"], "Asia/Tokyo"),
    (["seoul", "south korea", "korea"], "Asia/Seoul"),
    (["sydney", "melbourne", "brisbane", "canberra", "australia"],
     "Australia/Sydney"),
    (["auckland", "wellington", "new zealand"], "Pacific/Auckland"),
    (["mumbai", "delhi", "bangalore", "hyderabad", "chennai", "pune",
      "kolkata", "india"], "Asia/Kolkata"),
    (["singapore"], "Asia/Singapore"),
    (["hong kong"], "Asia/Hong_Kong"),
    (["beijing", "shanghai", "shenzhen", "guangzhou", "china"], "Asia/Shanghai"),
    (["dubai", "abu dhabi", "uae", "united arab emirates"], "Asia/Dubai"),
    (["united states", "usa"], "US"),
]


def resolve_iana(location: str) -> str | None:
    if not (location or "").strip():
        return None
    loc = location.lower().strip()
    for keywords, zone in _LOCATION_TO_IANA:
        for kw in keywords:
            token = kw.strip().strip(",")
            if token == "uk":
                padded = f" {loc} "
                if "ukraine" in loc:
                    continue
                if loc == "uk" or " uk " in padded or loc.startswith("uk,") or loc.endswith(", uk"):
                    return zone
                continue
            if token in loc:
                return zone
    return None


def _in_window(zone: str, now: datetime, start_hour: int, end_hour: int, business_days: list[int]) -> bool:
    local = now.astimezone(ZoneInfo(zone))
    return local.weekday() in business_days and start_hour <= local.hour < end_hour


def _seconds_until_zone(zone: str, now: datetime, start_hour: int, end_hour: int, business_days: list[int]) -> int:
    if _in_window(zone, now, start_hour, end_hour, business_days):
        return 0
    local = now.astimezone(ZoneInfo(zone))
    candidate = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    for _ in range(8):
        if candidate.weekday() in business_days:
            break
        candidate += timedelta(days=1)
    return max(0, int((candidate.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()))


def _seconds_until_us_intersection(now: datetime, start_hour: int, end_hour: int, business_days: list[int]) -> int:
    if all(_in_window(z, now, start_hour, end_hour, business_days) for z in US_CONTINENTAL):
        return 0
    cursor = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(24 * 8):
        if all(_in_window(z, cursor, start_hour, end_hour, business_days) for z in US_CONTINENTAL):
            return max(0, int((cursor - now.astimezone(timezone.utc)).total_seconds()))
        cursor += timedelta(hours=1)
    return 12 * 3600


def seconds_until_business_hours(
    location: str,
    *,
    now: datetime | None = None,
    start_hour: int = DEFAULT_BUSINESS_START,
    end_hour: int = DEFAULT_BUSINESS_END,
    business_days: list[int] | None = None,
) -> int:
    days = list(business_days) if business_days is not None else list(DEFAULT_BUSINESS_DAYS)
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    zone = resolve_iana(location)
    if zone is None:
        return 0
    if zone == "US":
        return _seconds_until_us_intersection(clock, start_hour, end_hour, days)
    return _seconds_until_zone(zone, clock, start_hour, end_hour, days)


def get_prospect_location(prospect: dict) -> str:
    loc = prospect.get("location") or prospect.get("contact_location") or ""
    if isinstance(loc, dict):
        loc = loc.get("name") or loc.get("default") or loc.get("city") or ""
    if not loc:
        profile = prospect.get("profile_json") or prospect.get("contact_profile_json") or {}
        if isinstance(profile, str):
            try:
                import json
                profile = json.loads(profile)
            except (json.JSONDecodeError, TypeError):
                profile = {}
        if isinstance(profile, dict):
            loc = profile.get("location") or ""
            if isinstance(loc, dict):
                loc = loc.get("name") or ""
    return str(loc or "").strip()
