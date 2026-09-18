"""Find a prospect email in the shapes Unipile and our stores actually use."""

from __future__ import annotations

import json
from typing import Any


def extract_profile_email(*sources: Any) -> str:
    """First valid address from prospect rows, profile blobs, or Unipile payloads."""
    for source in sources:
        found = _email_from_value(source)
        if found:
            return found
    return ""


def attach_email_to_profile_json(profile_json: str | None, email: str) -> str | None:
    """Flatten ``email`` onto the stored profile blob so every reader sees it."""
    email = extract_profile_email(email)
    if not email:
        return profile_json
    blob = _as_dict(profile_json)
    if blob.get("email") != email:
        blob["email"] = email
    return json.dumps(blob)


def contact_info_from_profile(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Keep Unipile ``contact_info`` (emails/phones) instead of dropping it."""
    if not isinstance(raw, dict):
        return {}
    ci = raw.get("contact_info")
    if not isinstance(ci, dict):
        ci = {}
    emails = _email_list(ci.get("emails") or raw.get("emails"))
    phones = _plain_list(ci.get("phones") or raw.get("phones") or raw.get("phone_numbers"))
    addresses = _plain_list(
        ci.get("addresses") or ci.get("adresses") or raw.get("addresses")
    )
    out: dict[str, Any] = {}
    if emails:
        out["emails"] = emails
    if phones:
        out["phones"] = phones
    if addresses:
        out["addresses"] = addresses
    return out


def _email_from_value(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        parsed = _as_dict(value)
        if parsed:
            return _email_from_mapping(parsed)
        return _clean_email(value)
    if isinstance(value, dict):
        return _email_from_mapping(value)
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _email_from_value(item)
            if found:
                return found
    return ""


def _email_from_mapping(data: dict[str, Any]) -> str:
    for key in ("email", "email_address", "emailAddress", "contact_email"):
        found = _clean_email(data.get(key))
        if found:
            return found

    nested = data.get("profile_json")
    if nested not in (None, ""):
        found = _email_from_value(nested)
        if found:
            return found

    contact_info = data.get("contact_info")
    if isinstance(contact_info, dict):
        found = _email_from_value(contact_info.get("emails"))
        if found:
            return found

    found = _email_from_value(data.get("emails"))
    if found:
        return found

    extra = data.get("contact_data_json")
    if extra not in (None, ""):
        found = _email_from_value(extra)
        if found:
            return found
    return ""


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _email_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        found = _clean_email(value)
        return [found] if found else []
    out: list[str] = []
    for item in value:
        found = _clean_email(item)
        if found and found not in out:
            out.append(found)
    return out


def _plain_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if not isinstance(value, (list, tuple)):
        text = str(value).strip()
        return [text] if text else []
    out: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = str(
                item.get("number") or item.get("phone") or item.get("name")
                or item.get("address") or ""
            ).strip()
        else:
            text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _clean_email(value: Any) -> str:
    if isinstance(value, dict):
        return _email_from_value(value)
    text = str(value or "").strip()
    if text.lower().startswith("mailto:"):
        text = text[7:].strip()
    if "@" in text and "." in text.rsplit("@", 1)[-1]:
        return text
    return ""
