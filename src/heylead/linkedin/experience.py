"""Current vs past roles on a LinkedIn experience list.

Unipile / LinkedIn often return ended jobs first. Treating ``experience[0]``
as current is how a past venture gets written as something the sender is
still building.
"""

from __future__ import annotations

from typing import Any

_END_KEYS = ("end", "end_date", "endDate", "date_to", "ended_at")
_START_KEYS = ("start", "start_date", "startDate", "date_from", "started_at")
_CURRENT_KEYS = ("current", "is_current", "isCurrent")
_COMPANY_KEYS = ("company", "company_name", "companyName")
_TITLE_KEYS = ("title", "role", "position")


def experience_from_payload(data: dict[str, Any] | None) -> list[Any]:
    """Unipile v1 puts dated roles on ``work_experience``, not ``experience``."""
    if not isinstance(data, dict):
        return []
    raw = (
        data.get("work_experience")
        or data.get("experience")
        or data.get("positions")
        or []
    )
    return raw if isinstance(raw, list) else []


def _first(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _is_present(value: Any) -> bool:
    if value is None or value == "":
        return True
    text = str(value).strip().lower()
    return text in {"present", "now", "current", "ongoing"}


def is_current_experience(item: dict[str, Any]) -> bool:
    for key in _CURRENT_KEYS:
        if key in item:
            return bool(item[key])
    return _is_present(_first(item, _END_KEYS))


def pick_current_experience(experience: list[Any]) -> dict[str, Any] | None:
    rows = [row for row in experience if isinstance(row, dict)]
    current = [row for row in rows if is_current_experience(row)]
    for row in current:
        if _first(row, _COMPANY_KEYS):
            return row
    return current[0] if current else None


def apply_current_role(profile: dict[str, Any]) -> dict[str, Any]:
    """Set title/company from the current role when experience has dates."""
    current = pick_current_experience(profile.get("experience") or [])
    if not current:
        return profile
    title = _first(current, _TITLE_KEYS)
    company = _first(current, _COMPANY_KEYS)
    if title:
        profile["title"] = title
    if company:
        profile["company"] = company
    return profile


def format_experience(experience: list[Any]) -> str:
    if not experience:
        return "No experience data available"
    lines: list[str] = []
    for exp in experience:
        if not isinstance(exp, dict):
            continue
        title = _first(exp, _TITLE_KEYS) or "Unknown"
        company = _first(exp, _COMPANY_KEYS) or "Unknown"
        start = _first(exp, _START_KEYS) or ""
        end = _first(exp, _END_KEYS)
        current = is_current_experience(exp)
        status = "Current" if current else "Past"
        dates = ""
        if start or end:
            end_label = "present" if current else (end or "?")
            dates = f" ({start or '?'} – {end_label})"
        line = f"- {title} at {company} [{status}]{dates}"
        desc = exp.get("description") or ""
        if desc:
            line += f": {str(desc)[:200]}"
        lines.append(line)
    return "\n".join(lines) if lines else "No experience data available"
