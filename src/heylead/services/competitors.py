"""Exclude people who work at the client's competitors.

11 Sep 2026: a customer asked that campaigns never first-touch people at
competing companies. The campaign setting defaults ON. A missing key excludes.
An empty competitor list excludes nobody — research or the operator must name
the companies first.

Shared verbatim with heylead-api ``app/services/competitors.py``.
"""

from __future__ import annotations

import json
import re
from typing import Any

EXCLUDE_COMPETITORS_KEY = "exclude_competitors"
COMPETITOR_COMPANIES_KEY = "competitor_companies"

_LEGAL_SUFFIX = re.compile(
    r"\b(incorporated|corporation|company|limited|ltda|llc|llp|gmbh|"
    r"plc|corp|inc|ltd|pty|pvt|s\.?a\.?|a\.?g\.?|b\.?v\.?|n\.?v\.?|co)\.?$",
    re.I,
)
_TLD = re.compile(r"\.(io|ai|com|co|dev|app|net|org)$", re.I)
_LEADING_THE = re.compile(r"^the\s+", re.I)
_COMPANY_KEYS = ("company", "contact_company", "prospect_company")
_PROFILE_KEYS = ("profile_json", "contact_profile_json")
_HEADLINE_KEYS = ("headline", "contact_headline", "title", "contact_title")


def exclude_competitors_enabled(config: dict[str, Any] | None) -> bool:
    if not isinstance(config, dict):
        return True
    val = config.get(EXCLUDE_COMPETITORS_KEY, True)
    return val not in (False, "off", "false", "False", 0, "0")


def normalize_company(name: str) -> str:
    text = str(name or "").strip().lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[\"'`]", "", text)
    text = re.sub(r"[^a-z0-9.\s+-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-")
    text = _LEADING_THE.sub("", text)
    text = _TLD.sub("", text)
    while True:
        nxt = _LEGAL_SUFFIX.sub("", text).strip(" .,")
        if nxt == text:
            break
        text = nxt
    return text


def companies_match(employer: str, competitor: str) -> bool:
    """True when *employer* is the same firm as *competitor*.

    Equal after normalize, or one is the other plus a division suffix
    (``Google`` matches ``Google Cloud``). ``Meta`` does not match
    ``Metabase``.
    """
    left = normalize_company(employer)
    right = normalize_company(competitor)
    if not left or not right or min(len(left), len(right)) < 2:
        return False
    if left == right:
        return True
    return left.startswith(right + " ") or right.startswith(left + " ")


def parse_competitor_names(*sources: Any) -> list[str]:
    """Dedupe company names from config strings, ICP lists, and dict rows."""
    names: list[str] = []
    seen: set[str] = set()
    for source in sources:
        for raw in _iter_name_items(source):
            name = str(raw).strip()
            key = normalize_company(name)
            if len(key) < 2 or key in seen:
                continue
            seen.add(key)
            names.append(name)
    return names


def competitor_list_is_set(config: dict[str, Any] | None) -> bool:
    """True once the campaign's own list has been written, even as empty.

    The list in the campaign's config is the setting, and settings are
    authoritative: one a person cleared or trimmed stays that way. The ICP's
    researched names stand in only for a campaign whose list was never
    written. Until 25 Sep 2026 every read took the union of the two, so a
    list cleared in Settings came straight back (D4umak/heylead-api#1428).
    """
    return isinstance(config, dict) and config.get(COMPETITOR_COMPANIES_KEY) is not None


def competitor_names_from(
    config: dict[str, Any] | None,
    icp: dict[str, Any] | None = None,
) -> list[str]:
    config = config if isinstance(config, dict) else {}
    icp = icp if isinstance(icp, dict) else {}
    if competitor_list_is_set(config):
        return parse_competitor_names(config.get(COMPETITOR_COMPANIES_KEY))
    return parse_competitor_names(
        icp.get("competitors"),
        icp.get(COMPETITOR_COMPANIES_KEY),
    )


def format_competitor_companies(names: list[str]) -> str:
    return ", ".join(names)


def person_employer(person: dict[str, Any] | None) -> str:
    if not isinstance(person, dict):
        return ""
    blobs: list[dict[str, Any]] = [person]
    for key in _PROFILE_KEYS:
        profile = _as_dict(person.get(key))
        if profile:
            blobs.append(profile)
    for blob in blobs:
        for key in _COMPANY_KEYS:
            val = blob.get(key)
            if val:
                return str(val).strip()
        experience = blob.get("experience")
        if isinstance(experience, list) and experience:
            first = experience[0]
            if isinstance(first, dict):
                company = first.get("company") or first.get("company_name")
                if company:
                    return str(company).strip()
    for blob in blobs:
        for key in _HEADLINE_KEYS:
            text = str(blob.get(key) or "")
            if " at " in text:
                return text.rsplit(" at ", 1)[-1].strip()
    return ""


def person_works_at_competitor(
    person: dict[str, Any] | None,
    competitors: list[str] | None,
) -> bool:
    if not competitors:
        return False
    employer = person_employer(person)
    if not employer:
        return False
    return any(companies_match(employer, name) for name in competitors)


def drop_competitor_people(
    people: list[dict[str, Any]],
    competitors: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split *people* into (kept, dropped) by current employer."""
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for person in people:
        if person_works_at_competitor(person, competitors):
            dropped.append(person)
        else:
            kept.append(person)
    return kept, dropped


def _iter_name_items(source: Any) -> list[Any]:
    if source is None or source is False:
        return []
    if isinstance(source, str):
        return [part.strip() for part in source.replace(";", ",").split(",") if part.strip()]
    if isinstance(source, list):
        items: list[Any] = []
        for item in source:
            if isinstance(item, dict):
                name = item.get("name") or item.get("company") or ""
                if name:
                    items.append(name)
                for alias in item.get("aliases") or []:
                    if alias:
                        items.append(alias)
            elif item:
                items.append(item)
        return items
    return []


def _as_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}
