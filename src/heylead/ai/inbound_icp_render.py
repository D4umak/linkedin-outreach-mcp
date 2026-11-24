"""The campaigns the inbound qualifier matches a sender against, as one prompt section (api #2682).

This file is byte-identical in heylead-api (app/services/inbound_icp_render.py)
and the heylead client (src/heylead/ai/inbound_icp_render.py); the shared
fixture tests/fixtures/inbound_icp_render_cases.json, sha256-pinned in both
repos, says what it renders. Pure and stdlib only, so it can be copied.

Campaigns store personas in two shapes: ``icps`` (job_titles, seniority and
industries as ``{"include": [...], "exclude": [...]}``) and ``segments``
(lists, ``seniority_exclude``, keywords often one comma-joined string).
``personas()`` reads both into one shape. ``render_inbound_icp_section()``
names every persona, every title and every excluded title ("not <title>"),
the campaign's goal and its offer, and caps the section at whole campaigns.
"""
from __future__ import annotations

import json
import re
from typing import Any

NO_ICPS = "No active ICPs defined. Qualify based on general B2B sales signals."

_GOAL_LINES = {
    "sell": "Goal: sell (looking for customers)",
    "partner": "Goal: partner (looking for partners or investors, not customers)",
}

SELL_RULE = (
    "Rule for campaigns whose goal is sell: a silent invitation (no message) from a sender "
    "whose headline matches one of that campaign's titles, and none of its 'not' entries, "
    "is a possible customer: recommend ask_purpose. This rule does not apply to campaigns "
    "with any other goal."
)
EXCLUDE_RULE = (
    "Rule: a sender matching a 'not' entry (title, seniority or industry) of a campaign "
    "does not match that campaign."
)


def _line(value: Any) -> str:
    """``value`` as one line: every run of whitespace is one space."""
    return " ".join(str(value).split())


def _items(value: Any) -> list[str]:
    """A list of non-empty single-line strings from a list, a comma-joined string or ``{"name": ...}`` items."""
    if value is None:
        return []
    if isinstance(value, str):
        raw: list[Any] = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("name") or item.get("title") or ""
        text = _line(item) if isinstance(item, (str, int, float)) else ""
        if text:
            out.append(text)
    return out


def _include_exclude(value: Any) -> dict[str, list[str]]:
    if isinstance(value, dict):
        return {"include": _items(value.get("include")), "exclude": _items(value.get("exclude"))}
    return {"include": _items(value), "exclude": []}


def _parse(icp_json: Any) -> dict[str, Any]:
    if isinstance(icp_json, str):
        try:
            icp_json = json.loads(icp_json or "{}")
        except (ValueError, TypeError):
            return {}
    return icp_json if isinstance(icp_json, dict) else {}


def _persona(raw: dict[str, Any], *, name: str = "", description: str = "") -> dict[str, Any]:
    if "job_titles" in raw:
        job_titles = _include_exclude(raw.get("job_titles"))
    else:
        job_titles = _include_exclude(raw.get("titles"))
    seniority = _include_exclude(raw.get("seniority"))
    seniority["exclude"] = seniority["exclude"] + _items(raw.get("seniority_exclude"))
    keywords = raw.get("keywords")
    if isinstance(keywords, dict):
        keywords = keywords.get("include")
    pain_points = raw.get("pain_points")
    if isinstance(pain_points, str):
        pain_points = [pain_points]
    return {
        "name": _line(name or raw.get("name") or ""),
        "description": _line(description or raw.get("description") or ""),
        "pain_points": _items(pain_points),
        "keywords": _items(keywords),
        "job_titles": job_titles,
        "seniority": seniority,
        "industries": _include_exclude(raw.get("industries")),
    }


def personas(icp_json: Any, *, campaign_name: str = "", target: str = "") -> list[dict[str, Any]]:
    """Every persona of a campaign's ICP in one shape, ``icps`` preferred over ``segments``.

    Shape: ``{name, description, pain_points, keywords, job_titles, seniority,
    industries}``, the last three as ``{"include": [...], "exclude": [...]}``.
    A campaign with no personas is one persona read from its top level, named
    after the campaign and described by its target description.
    """
    data = _parse(icp_json)
    for key in ("icps", "segments"):
        listed = data.get(key)
        if isinstance(listed, list):
            found = [_persona(p, name=_line(p.get("name") or campaign_name)) for p in listed if isinstance(p, dict)]
            if found:
                return found
    description = data.get("description") or data.get("target_description") or target
    return [_persona(data, name=campaign_name or _line(data.get("name") or ""), description=_line(description or ""))]


def goal_of(summary: dict[str, Any]) -> str | None:
    """The campaign's goal: ``summary["goal"]``, else ``icp_json.campaign_goal``, lowercased; None when neither says."""
    goal = summary.get("goal") or _parse(summary.get("icp_json")).get("campaign_goal")
    if not isinstance(goal, str) or not goal.strip():
        return None
    return goal.strip().lower()


def _goal_line(goal: str | None) -> str:
    if goal is None:
        return "Goal: not stated"
    return _GOAL_LINES.get(goal, f"Goal: {_line(goal)}")


def _field(label: str, values: dict[str, list[str]]) -> str:
    parts = list(values["include"]) + [f"not {x}" for x in values["exclude"]]
    return f"    {label}: {', '.join(parts)}" if parts else ""


def _has_excludes(persona: dict[str, Any]) -> bool:
    return any(persona[k]["exclude"] for k in ("job_titles", "seniority", "industries"))


def _campaign_block(summary: dict[str, Any]) -> tuple[str, str | None, bool]:
    data = _parse(summary.get("icp_json"))
    name = _line(summary.get("name") or data.get("name") or "")
    goal = goal_of(summary)
    head = f"Campaign '{name}'" if name else "Campaign"
    lines = [f"{head} (id={_line(summary.get('id') or '')})", f"  {_goal_line(goal)}"]
    offer = _line(data.get("summary") or "") or _line(data.get("relevance_hook") or "")
    if offer:
        lines.append(f"  Offer: {offer[:200]}")
    found = personas(data, campaign_name=name, target=_line(summary.get("target") or ""))
    for p in found:
        persona_name = p["name"] or name
        lines.append(f"  Persona '{persona_name}':" if persona_name else "  Persona:")
        for label, key in (("Titles", "job_titles"), ("Seniority", "seniority"), ("Industries", "industries")):
            row = _field(label, p[key])
            if row:
                lines.append(row)
        if p["keywords"]:
            lines.append(f"    Keywords: {', '.join(p['keywords'])}")
        if p["pain_points"]:
            lines.append(f"    Pain points: {', '.join(p['pain_points'][:3])}")
        if p["description"]:
            lines.append(f"    Description: {p['description'][:150]}")
    return "\n".join(lines), goal, any(_has_excludes(p) for p in found)


def render_inbound_icp_section(icp_summaries: list[dict[str, Any]], cap: int = 6000) -> str:
    """The prompt section naming each campaign the sender may fit, capped at whole campaigns.

    The first campaign is always shown whole; later ones are shown while the
    blocks stay within ``cap`` characters, then "(N more campaigns not shown)".
    The rules after the blocks are written only for the campaigns shown.
    """
    summaries = [s for s in icp_summaries or [] if isinstance(s, dict)]
    if not summaries:
        return NO_ICPS
    blocks: list[str] = []
    sell = excludes = False
    used = 0
    for summary in summaries:
        block, goal, has_excludes = _campaign_block(summary)
        cost = len(block) + (2 if blocks else 0)
        if blocks and used + cost > cap:
            break
        blocks.append(block)
        used += cost
        sell = sell or goal == "sell"
        excludes = excludes or has_excludes
    hidden = len(summaries) - len(blocks)
    if hidden:
        blocks.append(f"({hidden} more campaign{'s' if hidden != 1 else ''} not shown)")
    rules = ([SELL_RULE] if sell else []) + ([EXCLUDE_RULE] if excludes else [])
    if rules:
        blocks.append("\n".join(rules))
    return "\n\n".join(blocks)


# The eval's vendor keyword rule (VENDOR_RE in the #2682 eval's labels.py), read on the headline only.
VENDOR_HEADLINE_RE = re.compile(r"(?<![a-z])(" + "|".join([
    r"agency", r"agencies", r"i help", r"we help",
    r"helping (?:companies|businesses|founders|brands|b2b|teams|startups|agencies|coaches|saas)",
    r"lead gen(?:eration)?", r"appointment setting", r"outsourc\w*", r"offshore", r"nearshore", r"white[- ]label",
    r"(?:web|app|software|mobile|custom software) development (?:company|services|agency|partner|studio)",
    r"development services", r"dev shop", r"freelanc\w*", r"virtual assistan\w*", r"staff augmentation",
    r"ghostwrit\w*", r"cold email", r"seo (?:services|expert|specialist|agency)", r"marketing agency",
    r"growth partner", r"our services", r"we offer", r"we provide", r"i offer", r"consulting services",
    r"book a call", r"quick call", r"would you be open to", r"done[- ]for[- ]you", r"we build", r"we scale",
    r"i build", r"hire (?:me|us)", r"available for (?:projects|hire|work)",
]) + r")(?![a-z])")

# A first-person buying ask in the sender's message.
BUYING_ASK_RE = re.compile(r"(?<![a-z])(" + "|".join([
    r"pricing", r"prices?", r"quotes?", r"demo", r"trial", r"how much", r"costs?",
    r"interested in (?:your|using|buying|trying)", r"looking for an?", r"can you help", r"do you offer",
    r"we need", r"i need", r"purchas(?:e|ing)", r"buy(?:ing)?",
]) + r")(?![a-z])")


def vendor_engage_carve_out(headline: Any, content: Any, action: str) -> str:
    """``action``, except engage_immediately becomes accept_and_monitor for a vendor headline with no buying ask.

    api #2682, pm-3-vendor-engage: a sender whose headline sells a service and
    whose message asks to buy nothing is not engaged at once. Intent and every
    other verdict stay as the model gave them.
    """
    if action != "engage_immediately":
        return action
    if not VENDOR_HEADLINE_RE.search(str(headline or "").lower()):
        return action
    if BUYING_ASK_RE.search(str(content or "").lower()):
        return action
    return "accept_and_monitor"
