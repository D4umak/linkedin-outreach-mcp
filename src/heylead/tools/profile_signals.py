"""Tool: profile_signals — compile and preview profile-signal targeting."""

from __future__ import annotations

import json
from typing import Any

from ..services.profile_signals import (
    compile_profile_signals,
    score_profile_evidence,
)

VALID_ACTIONS = ("compile", "preview", "schools")


def run_profile_signals(
    action: str = "compile",
    request: str = "",
    titles: str = "",
    location_codes: str = "",
    profiles_json: str = "",
) -> str:
    """Compile a targeting sentence into recall queries + evidence rules.

    preview can score caller-supplied profile JSON (no LinkedIn write).
    """
    act = (action or "compile").strip().lower()
    if act not in VALID_ACTIONS:
        return f"Unknown action '{action}'. Use compile, preview, or schools."
    if act == "schools":
        return _format_schools(request)
    if not (request or "").strip():
        return "Pass request= e.g. 'Ukrainians in the US' or 'car lovers'."

    title_list = [t.strip() for t in titles.split(",") if t.strip()]
    loc_list = [c.strip() for c in location_codes.split(",") if c.strip()]
    spec = compile_profile_signals(
        request, titles=title_list or None, location_codes=loc_list or None,
    )
    if not spec:
        return (
            f"No profile-signal spec for: {request!r}\n"
            "Country/diaspora needs a gazetteer match. Interests need a "
            "distinctive hobby/identity phrase."
        )

    lines = [
        f"kind: {spec['kind']}",
        f"label: {spec.get('label')}",
        f"keep_rule: {spec.get('keep_rule')}",
        f"compiler_version: {spec.get('compiler_version', 1)}",
        "",
        "Lists (one token each; never identity+title+geo):",
    ]
    for q in spec.get("lists") or spec.get("recall_queries") or []:
        lines.append(
            f"  - layer={q.get('layer') or q.get('why')} keywords={q.get('keywords')!r} "
            f"title={q.get('title_or') or '(none)'} location={q.get('location_codes')}"
        )
    lines.append("")
    lines.append(
        "Filter stages: card geo → retrieve → identity evidence → "
        "profile geo → role keep → min_fit (identity keywords skipped in fit)"
    )
    lines.append(
        "Recall: campaign identity lists run with sibling role_geo every pass; "
        "catalog schools only if identity_geo cards are thin."
    )
    ev = spec.get("evidence") or {}
    lines.append("")
    lines.append("Evidence:")
    for key in ("schools", "languages", "experience_places", "about_terms"):
        vals = ev.get(key) or []
        if vals:
            lines.append(f"  {key}: " + ", ".join(str(v) for v in vals[:12]))
    for note in spec.get("explain") or []:
        lines.append(note)

    if act == "preview" and profiles_json.strip():
        try:
            profiles = json.loads(profiles_json)
        except json.JSONDecodeError as e:
            return f"profiles_json is not valid JSON: {e}"
        if isinstance(profiles, dict):
            profiles = [profiles]
        lines.append("")
        lines.append("Preview scores:")
        for i, prof in enumerate(profiles[:5], 1):
            if not isinstance(prof, dict):
                continue
            scored = score_profile_evidence(prof, spec)
            lines.append(
                f"  {i}. keep={scored['keep']} hits={scored['hits']} "
                f"explain={scored['explain']}"
            )
    elif act == "preview":
        lines.append("")
        lines.append(
            "Pass profiles_json with up to 5 full profiles (education, "
            "languages, experience, headline) to score them. Nothing was "
            "sent to LinkedIn."
        )
    return "\n".join(lines)


def _format_schools(request: str) -> str:
    from ..services.profile_signals import (
        compile_profile_signals,
        load_country_gazetteer,
        list_country_gazetteers,
    )

    text = (request or "ukraine").strip()
    spec = compile_profile_signals(text)
    code = (spec or {}).get("code") if spec and spec.get("kind") == "country_tie" else ""
    if not code:
        blob = text.lower().replace(" ", "_")
        code = blob if blob in list_country_gazetteers() else "ukraine"
    gaz = load_country_gazetteer(code)
    if not gaz:
        return f"No gazetteer for {code!r}. Known: {', '.join(list_country_gazetteers())}"
    lines = [
        f"Schools for {gaz.get('label')} (exact LinkedIn names to search on Premium/SN):",
        "Cloud harvest adds real profile strings + SN parameter ids after retrieve.",
        "",
    ]
    for school in gaz.get("schools") or []:
        stem = school.get("stem") or ""
        aliases = ", ".join(school.get("aliases") or [])
        lines.append(f"  - {stem}" + (f"  (also: {aliases})" if aliases else ""))
    return "\n".join(lines)
