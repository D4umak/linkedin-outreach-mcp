"""Every persona's titles for the ICP research agent, and the guard on what it adds (heylead-api#7290).

The client twin of heylead-api ``app/services/icp_persona_titles.py``. A saved audience with several
personas has one target that names all of them, and each persona is searched on its own. The research
agent judges and revises persona 1 only, but it was shown persona 1 alone (``read_icp`` cut the ICP JSON
at 1,800 characters), so it added to persona 1 titles another persona already carries.

``personas_line`` is the one line that names every persona's titles, in the context and first in
``read_icp``. ``drop_other_persona_titles`` runs before the coordinator reads the decision: it drops each
title a revise adds to persona 1 that ``profile_signals.role_hit`` finds in another persona's titles.
One direction only: a persona 1 title another persona also carries is left alone (api #7294).
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from ..ai.agent_loop import AgentResult

#: Titles named per persona before the line counts the rest.
TITLES_SHOWN = 12

#: The reason when every title the revise added was another persona's (owner words, api #7290).
KEEP_REASON = ("Every title the research suggested is already in another persona of this audience, "
               "so nothing changed.")
#: The reason when the model's named a title the guard dropped: dropped titles are never named.
ASK_REASON = ("The research suggested titles another persona of this audience already has. "
              "I left those out and kept the rest of the change.")

_HEAD = "Personas (each searched on its own; judge and revise persona 1 only): "


def _personas(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    icps = (data or {}).get("icps") or []
    return [p if isinstance(p, dict) else {} for p in icps] if isinstance(icps, list) else []


def persona_titles(persona: dict[str, Any]) -> list[str]:
    """A persona's included job titles: ``{include, exclude}``, a plain list, or comma text."""
    value = persona.get("job_titles")
    if isinstance(value, dict):
        return [str(v).strip() for v in (value.get("include") or []) if str(v).strip()]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        from .job_titles import split_list_text

        return split_list_text(value)
    return []


def personas_line(data: dict[str, Any] | None) -> str:
    """``Personas (...): 1 <name>: t1, t2 | 2 <name>: ...``; says what is missing rather than leaving it out."""
    personas = _personas(data)
    if not personas:
        return "Personas: none saved"
    parts = []
    for n, persona in enumerate(personas, start=1):
        name = str(persona.get("name") or "").strip() or "(no name)"
        titles = persona_titles(persona)
        if not titles:
            shown = "(no job titles)"
        else:
            shown = ", ".join(titles[:TITLES_SHOWN])
            if len(titles) > TITLES_SHOWN:
                shown += f" (+{len(titles) - TITLES_SHOWN} more titles)"
        parts.append(f"{n} {name}: {shown}")
    return _HEAD + " | ".join(parts)


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    return [v for v in values if not (v.casefold() in seen or seen.add(v.casefold()))]


def _changes(data: dict[str, Any], patch: dict[str, Any]) -> bool:
    """Whether the patch would change any of persona 1's lists (case-folded sets, as the hosted ``_changes``)."""
    from .icp_research import _PATCH_MAP, patch_lists

    personas = _personas(data)
    persona = personas[0] if personas else {}
    for key, now in patch_lists(patch, data).items():
        field, side = _PATCH_MAP[key]
        bucket = persona.get(field)
        if isinstance(bucket, dict):
            was = [str(v) for v in bucket.get(side) or []]
        elif field == "job_titles" and side == "include":
            was = persona_titles(persona)
        else:
            was = []
        if {v.casefold() for v in was} != {v.casefold() for v in now}:
            return True
    return False


def drop_other_persona_titles(data: dict[str, Any], result: AgentResult) -> tuple[AgentResult, dict[str, Any]]:
    """(the result without titles another persona carries, the log's record of what the model asked).

    Same rules as the hosted twin: an added title ``role_hit`` finds in another persona is dropped; when
    something was dropped, a cleaned ``titles_include`` that is a strict subset of persona 1's saved list
    drops the whole key, a revise with nothing left to change becomes a keep with ``KEEP_REASON``, and a
    reason that names a dropped title is replaced with ``ASK_REASON``.
    """
    from ..textutil import contains_term
    from .icp_research import patch_lists
    from .profile_signals import role_hit

    personas = _personas(data)
    before = persona_titles(personas[0]) if personas else []
    others = _unique([t for p in personas[1:] for t in persona_titles(p)])
    extras = dict(result.extras or {})
    info: dict[str, Any] = {
        "model_decision": result.decision,
        "model_patch": dict(extras),
        "titles_before": before,
        "other_persona_titles": others,
        "persona_count": len(personas),
        "dropped_titles": [],
    }
    if result.decision != "revise" or not others:
        return result, info
    asked = patch_lists(extras, data).get("titles_include") or []
    had = {t.casefold() for t in before}
    dropped = [t for t in asked if t.casefold() not in had and role_hit(t, others)]
    if not dropped:
        return result, info
    info["dropped_titles"] = dropped
    gone = {t.casefold() for t in dropped}
    kept = [t for t in asked if t.casefold() not in gone]
    if kept and {t.casefold() for t in kept} < had:
        kept = []
    if kept:
        extras["titles_include"] = json.dumps(kept) if isinstance(extras.get("titles_include"), str) else kept
    else:
        extras.pop("titles_include", None)
    if not _changes(data, extras):
        info["proposed_reason"] = result.reason  # the hosted row's key for the model's reason behind a keep
        return replace(result, decision="keep", reason=KEEP_REASON, extras=extras), info
    reason = result.reason
    # Whole words, the plural ("CEOs") and role_hit's aliases ("VP of Asset Management"): never named to the owner.
    if any(contains_term(reason, t) or contains_term(reason, f"{t}s") or role_hit(reason, [t]) for t in dropped):
        info["model_reason"] = reason
        reason = ASK_REASON
    return replace(result, reason=reason, extras=extras), info
