"""Normalize a Unipile / backend user payload into HeyLead's profile shape.

UnipileClient.get_profile already did this. BackendClient.get_profile returned
the raw card (headline, no title/company/experience), so hosted invites
analyzed an empty person. One function, both clients.
"""

from __future__ import annotations

from typing import Any

from .experience import apply_current_role, experience_from_payload


def _as_dict(data: Any) -> dict[str, Any]:
    if isinstance(data, list) and data:
        data = data[0]
    return data if isinstance(data, dict) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _location(data: dict[str, Any]) -> str:
    loc = data.get("location") or ""
    if isinstance(loc, dict):
        loc = loc.get("name") or loc.get("default") or str(loc)
    return str(loc) if loc else ""


def _skills(data: dict[str, Any]) -> list[str]:
    skills_raw = data.get("skills") or []
    skills: list[str] = []
    if not isinstance(skills_raw, list):
        return skills
    for item in skills_raw:
        if isinstance(item, str) and item.strip():
            skills.append(item)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("skill") or ""
            if name:
                skills.append(str(name))
    return skills


def _title_company_from_headline(headline: str) -> tuple[str, str]:
    title = headline
    company = ""
    if " at " in headline:
        parts = headline.rsplit(" at ", 1)
        title = parts[0]
        company = parts[1]
    return title, company


def normalize_linkedin_profile(data: Any) -> dict[str, Any]:
    """Map a Unipile user payload to HeyLead's stored profile dict.

    Empty / non-dict input returns ``{}``. Already-normalized dicts keep
    title, company, experience, and public_id aliases.
    """
    data = _as_dict(data)
    if not data:
        return {}

    from ..services.prospect_email import contact_info_from_profile, extract_profile_email

    first_name = _first_text(data.get("first_name"), data.get("firstName"))
    last_name = _first_text(data.get("last_name"), data.get("lastName"))
    headline = _first_text(data.get("headline"), data.get("occupation"))
    parsed_title, parsed_company = _title_company_from_headline(headline)
    title = _first_text(data.get("title"), parsed_title)
    company = _first_text(data.get("company"), parsed_company)

    experience = experience_from_payload(data)
    pub_id = _first_text(
        data.get("public_id"),
        data.get("public_identifier"),
        data.get("publicIdentifier"),
    )
    prov_id = _first_text(data.get("provider_id"), data.get("id"))

    prof_url = _first_text(
        data.get("profile_url"),
        data.get("public_profile_url"),
        data.get("url"),
    )
    if not prof_url and pub_id:
        prof_url = f"https://www.linkedin.com/in/{pub_id}"

    name = _first_text(data.get("name"), f"{first_name} {last_name}".strip())
    contact_info = contact_info_from_profile(data)
    email = extract_profile_email(data, contact_info)

    profile: dict[str, Any] = {
        "name": name,
        "first_name": first_name,
        "last_name": last_name,
        "headline": headline,
        "title": title,
        "company": company,
        "location": _location(data),
        "summary": _first_text(data.get("summary"), data.get("about")),
        "industry": _first_text(data.get("industry")),
        "public_id": pub_id,
        "public_identifier": pub_id,
        "provider_id": prov_id,
        "profile_url": prof_url,
        "connections": (
            data.get("connections_count")
            or (data.get("network_info") or {}).get("connections_count", 0)
            or data.get("connections")
            or 0
        ),
        "is_relationship": bool(data.get("is_relationship", False)),
        "network_distance": data.get("network_distance", "") or "",
        "skills": _skills(data),
        "experience": experience if isinstance(experience, list) else [],
        "education": data.get("education") or [],
        "certifications": data.get("certifications") or [],
        "publications": data.get("publications") or [],
        "languages": data.get("languages") or [],
        "volunteer": data.get("volunteer_experience") or data.get("volunteer") or [],
        "honors": data.get("honors_awards") or data.get("honors") or [],
        "is_premium": bool(data.get("is_premium", False)),
        "is_open_profile": bool(data.get("is_open_profile", False)),
    }
    if email:
        profile["email"] = email
    if contact_info:
        profile["contact_info"] = contact_info
    return apply_current_role(profile)


_CONTACT_FILL = ("name", "title", "company")
_SCANNER_KEYS = ("_profile_scan_at",)


def _filled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _as_profile_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        import json
        try:
            value = json.loads(value or "{}")
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def merge_prospect_profile(
    stored: Any,
    contact_row: Any = None,
    live: Any = None,
) -> dict[str, Any]:
    """Build the profile analysis/generation should see.

    First non-empty wins per field: live (normalized) > stored profile_json
    > contact columns. Empty live keys never wipe a filled stored or contact
    value. Scanner keys such as ``_profile_scan_at`` stay on the stored card.
    """
    stored = _as_profile_dict(stored)
    contact_row = _as_profile_dict(contact_row)
    live_raw = _as_profile_dict(live)
    live_norm = normalize_linkedin_profile(live_raw) if live_raw else {}

    merged = dict(stored)
    if stored and not merged.get("title") and not merged.get("company"):
        stored_norm = normalize_linkedin_profile(stored)
        for key, value in stored_norm.items():
            if _filled(value) and not _filled(merged.get(key)):
                merged[key] = value

    for key in _CONTACT_FILL:
        if not _filled(merged.get(key)) and _filled(contact_row.get(key)):
            merged[key] = contact_row[key]

    for key, value in live_norm.items():
        if key in _SCANNER_KEYS:
            continue
        if _filled(value):
            merged[key] = value

    for key in _SCANNER_KEYS:
        if key in stored and _filled(stored.get(key)):
            merged[key] = stored[key]

    if not _filled(merged.get("name")):
        name = " ".join(
            p for p in (merged.get("first_name"), merged.get("last_name")) if p
        ).strip()
        if name:
            merged["name"] = name
    return merged


def prospect_data_from_contact(row: Any, live: Any = None) -> dict[str, Any]:
    """Merge stored profile_json, contact columns, and an optional live fetch."""
    row = _as_profile_dict(row)
    return merge_prospect_profile(row.get("profile_json"), row, live)
