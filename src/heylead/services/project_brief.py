"""Campaign project brief — what the model sees before it writes.

Stored on campaigns.context_json as:
  project_brief  (required string — the operator paste)
  project_facts  (optional object: product, go_live, volume, must_confirm)

offerings stays the short product line. The gate is project_brief only;
offerings is never a substitute.
"""

from __future__ import annotations

import json
import re
from typing import Any

MISSING_PROJECT_BRIEF = (
    "This campaign has no project_brief. Set it via edit_campaign(project_brief=...)."
)

_URL_ONLY_RE = re.compile(r"^https?://\S+$", re.I)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def is_real_project_brief(text: str) -> bool:
    """False for empty, a homepage URL, or a one-line company blurb under 80 chars."""
    s = (text or "").strip()
    if not s:
        return False
    if _URL_ONLY_RE.fullmatch(s):
        return False
    if "\n" not in s and len(s) < 80:
        return False
    return True


def first_sentence(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    parts = _SENTENCE_SPLIT_RE.split(s, maxsplit=1)
    return (parts[0] if parts else s).strip()


def parse_campaign_context(campaign: dict[str, Any] | None) -> dict[str, Any]:
    """Return context_json as a dict, or the dict itself if already unpacked."""
    if not campaign:
        return {}
    raw = campaign.get("context_json", None)
    if raw is None and ("project_brief" in campaign or "offerings" in campaign):
        return campaign
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def read_project_brief(campaign: dict[str, Any] | None) -> str:
    """Return the stored project_brief, or empty string. No offerings fallback."""
    return str(parse_campaign_context(campaign).get("project_brief") or "").strip()


def campaign_has_project_brief(campaign: dict[str, Any] | None) -> bool:
    """True only when project_brief is a real paste, not a URL or one-liner."""
    return is_real_project_brief(read_project_brief(campaign))


def read_project_facts(campaign: dict[str, Any] | None) -> dict[str, Any]:
    facts = parse_campaign_context(campaign).get("project_facts")
    return facts if isinstance(facts, dict) else {}


def refuse_without_project_brief(campaign: dict[str, Any] | None) -> str:
    """One-line refusal used by launch, resume, and auto-send."""
    if campaign_has_project_brief(campaign):
        return ""
    return MISSING_PROJECT_BRIEF


def parse_must_confirm(raw: str | list[str] | None) -> list[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except (json.JSONDecodeError, TypeError):
            pass
    return [p.strip() for p in re.split(r"[\n,;]+", text) if p.strip()]


def build_context_payload(
    *,
    company_context: str = "",
    project_brief: str = "",
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge create-time paste into context_json.

    company_context always becomes offerings. It is copied into project_brief
    only when it is a real paste (not a homepage URL or one-liner).
    """
    ctx = dict(existing or {})
    company = (company_context or "").strip()
    brief = (project_brief or "").strip()
    if not is_real_project_brief(brief):
        brief = company if is_real_project_brief(company) else ""
    if company:
        ctx["offerings"] = company
        ctx["company_context_raw"] = company
    if brief:
        ctx["project_brief"] = brief
    return ctx


def merge_project_facts(
    existing: dict[str, Any],
    *,
    product: str = "",
    go_live: str = "",
    volume: str = "",
    must_confirm: str | list[str] | None = None,
) -> dict[str, Any]:
    """Write optional structured facts into context_json.project_facts."""
    ctx = dict(existing or {})
    facts = dict(ctx.get("project_facts") or {}) if isinstance(ctx.get("project_facts"), dict) else {}
    if product.strip():
        facts["product"] = product.strip()
    if go_live.strip():
        facts["go_live"] = go_live.strip()
    if volume.strip():
        facts["volume"] = volume.strip()
    confirms = parse_must_confirm(must_confirm)
    if confirms:
        facts["must_confirm"] = confirms
    if facts:
        ctx["project_facts"] = facts
    return ctx


def format_project_brief_block(
    brief: str,
    facts: dict[str, Any] | None = None,
) -> str:
    """PROJECT BRIEF prompt block plus optional facts. Empty when unset."""
    text = (brief or "").strip()
    if not text:
        return ""
    lines = ["PROJECT BRIEF", text]
    facts = facts if isinstance(facts, dict) else {}
    product = str(facts.get("product") or "").strip()
    go_live = str(facts.get("go_live") or "").strip()
    volume = str(facts.get("volume") or "").strip()
    confirms = parse_must_confirm(facts.get("must_confirm"))
    extras: list[str] = []
    if product:
        extras.append(f"Product: {product}")
    if go_live:
        extras.append(f"Go-live: {go_live}")
    if volume:
        extras.append(f"Volume: {volume}")
    if confirms:
        extras.append("Must confirm: " + "; ".join(confirms))
    if extras:
        lines.append("")
        lines.extend(extras)
    return "\n".join(lines) + "\n"


def short_offering_line(ctx: dict[str, Any] | None) -> str:
    """Short product line for buy templates. Never invent when nothing is stored."""
    ctx = ctx or {}
    offerings = str(ctx.get("offerings") or "").strip()
    if offerings:
        return offerings
    facts = ctx.get("project_facts") if isinstance(ctx.get("project_facts"), dict) else {}
    product = str((facts or {}).get("product") or "").strip()
    if product:
        return product
    return first_sentence(str(ctx.get("project_brief") or ""))
