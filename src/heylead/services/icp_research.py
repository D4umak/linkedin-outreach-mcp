"""ICP research agent — preview who persona 1 matches, then keep / revise / hold."""

from __future__ import annotations

import copy
import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from ..ai.agent_loop import AgentBudget, AgentResult, run_agent_loop
from ..ai.icp_research import ICP_RESEARCH_SYSTEM, build_icp_research_context
from ..ai.icp_schemas import IcpResult, icp_result_from_dict, icp_result_to_json
from ..ai.schemas import ICP_AGENT_STEP
from .agent_commons import async_commons_tools
from .coordinator import after_sibling_loop
from ..constants import (
    ICP_RESEARCH_MAX_STEPS,
    ICP_RESEARCH_MAX_TOKENS,
    ICP_RESEARCH_PREVIEW_LIMIT,
    ICP_RESEARCH_RESULT_CHARS,
    ICP_RESEARCH_TIMEOUT_SECONDS,
    ICP_RESEARCH_VALID_DECISIONS,
)
from ..db.async_bridge import run_db
from ..db.queries import get_icp, get_setting, log_action, update_icp
from ..flags import flag_enabled

logger = logging.getLogger(__name__)

PreviewFn = Callable[[], str | Awaitable[str]]
EnrichFn = Callable[[IcpResult], Any]

_PATCH_MAP = {
    "titles_include": ("job_titles", "include"),
    "titles_exclude": ("job_titles", "exclude"),
    "locations_include": ("locations", "include"),
    "locations_exclude": ("locations", "exclude"),
    "industries_include": ("industries", "include"),
    "industries_exclude": ("industries", "exclude"),
}


@dataclass
class IcpResearchOutcome:
    decision: str
    reason: str = ""
    applied: bool = False
    summary: str = ""
    result: IcpResult | None = None


def icp_research_mode(config: dict[str, Any] | None) -> Literal["off", "observe", "act"]:
    """Settings / config → off | observe | act. Unset defaults to act."""
    cfg = config or {}
    raw_mode = cfg.get("icp_research_mode")
    if isinstance(raw_mode, str) and raw_mode.strip():
        text = raw_mode.strip().lower()
        if text in {"off", "observe", "act"}:
            return text  # type: ignore[return-value]
        if text in {"on", "true", "yes", "1"}:
            return "act"
        if text in {"false", "no", "0"}:
            return "off"
    if "enable_icp_research_agent" in cfg and cfg.get("enable_icp_research_agent") not in (None, ""):
        return "act" if flag_enabled(cfg, "enable_icp_research_agent", default=False) else "off"
    return "act"


def apply_persona_patch(data: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Apply flat include/exclude strings to persona 1. Empty values leave the field."""
    out = copy.deepcopy(data)
    icps = out.get("icps") or []
    if not icps or not isinstance(icps[0], dict):
        return out
    persona = icps[0]
    changed = False
    for key, (field, side) in _PATCH_MAP.items():
        raw = patch.get(key)
        if raw is None or not str(raw).strip():
            continue
        values = [part.strip() for part in str(raw).split(",") if part.strip()]
        if not values:
            continue
        bucket = persona.get(field)
        if not isinstance(bucket, dict):
            bucket = {"include": [], "exclude": []}
            persona[field] = bucket
        bucket[side] = values
        changed = True
    if changed:
        persona["linkedin_enriched_params"] = None
    return out


async def maybe_run_icp_research(
    icp_id: str,
    target_description: str = "",
    *,
    config: dict[str, Any] | None = None,
    call_llm_fn: Any = None,
    preview_fn: PreviewFn | None = None,
    enrich_fn: EnrichFn | None = None,
) -> IcpResearchOutcome:
    """Run the short loop against a saved ICP. Failure → keep, never raise to generate_icp."""
    try:
        cfg = config if config is not None else await _load_config()
        mode = icp_research_mode(cfg)
        if mode == "off":
            return IcpResearchOutcome(decision="keep", reason="agent off")

        record = await run_db(get_icp, icp_id)
        if not record:
            return IcpResearchOutcome(decision="keep", reason="icp missing", summary="keep — ICP not found")

        context = build_icp_research_context(
            target_description=target_description or record.get("target_desc") or "",
            icp_name=record.get("name") or "",
        )
        tools = _build_tools(icp_id, record, preview_fn)
        tools.update(async_commons_tools(agent="icp_research", campaign_id=""))
        budget = AgentBudget(
            max_steps=ICP_RESEARCH_MAX_STEPS,
            max_tokens=ICP_RESEARCH_MAX_TOKENS,
            timeout_seconds=float(ICP_RESEARCH_TIMEOUT_SECONDS),
            result_chars=ICP_RESEARCH_RESULT_CHARS,
        )
        result = await run_agent_loop(
            system=ICP_RESEARCH_SYSTEM,
            context=context,
            tools=tools,
            schema=ICP_AGENT_STEP,
            budget=budget,
            call_llm_fn=call_llm_fn,
            valid_decisions=ICP_RESEARCH_VALID_DECISIONS,
        )
        await after_sibling_loop(
            agent="icp_research",
            campaign_id="",
            decision=result.decision,
            reason=result.reason,
            config=cfg,
        )
        return await _apply_decision(
            icp_id, result, mode=mode, enrich_fn=enrich_fn,
        )
    except Exception as e:
        logger.warning("ICP research agent failed: %s", e)
        return IcpResearchOutcome(
            decision="keep",
            reason=str(e)[:240],
            summary="keep — research could not run",
        )


async def _load_config() -> dict[str, Any]:
    return {
        "icp_research_mode": await run_db(get_setting, "icp_research_mode", "") or "",
        "enable_icp_research_agent": await run_db(get_setting, "enable_icp_research_agent", "") or "",
    }


def _build_tools(
    icp_id: str,
    record: dict[str, Any],
    preview_fn: PreviewFn | None,
) -> dict[str, Callable[..., Any]]:
    used = {"preview": False}

    async def read_icp() -> str:
        return _format_saved_icp(record)

    async def read_filters() -> str:
        return _format_filters(record)

    async def preview_search() -> str:
        if used["preview"]:
            return "preview_search already used this run"
        used["preview"] = True
        if preview_fn is not None:
            raw = preview_fn()
            if inspect.isawaitable(raw):
                raw = await raw
            return str(raw)
        return await _default_preview(icp_id)

    return {
        "read_icp": read_icp,
        "read_filters": read_filters,
        "preview_search": preview_search,
    }


def _format_saved_icp(record: dict[str, Any]) -> str:
    blob = (record.get("icp_json") or "")[:1800]
    target = record.get("target_desc") or ""
    return f"target: {target}\nname: {record.get('name') or ''}\nicp: {blob or '(none)'}"


def _format_filters(record: dict[str, Any]) -> str:
    from ..services.icp_search import SALES_NAV_ONLY_FILTERS, build_segment_query
    from ..tools.create_campaign import _icp_result_to_legacy

    try:
        result = icp_result_from_dict(json.loads(record.get("icp_json") or "{}"))
    except Exception as e:
        return f"filters unreadable: {e}"
    if not result.icps:
        return "no personas"
    persona = result.icps[0]
    target = record.get("target_desc") or result.summary or ""
    legacy = _icp_result_to_legacy(result, target)
    segments = legacy.get("segments") or []
    segment = segments[0] if segments else {}
    keywords, filters = build_segment_query(segment, use_sales_nav=True)
    classic_kw, classic_filters = build_segment_query(segment, use_sales_nav=False)
    dropped = [name for name in SALES_NAV_ONLY_FILTERS if name in filters and name not in classic_filters]
    titles = persona.job_titles
    locations = persona.locations
    industries = persona.industries
    return (
        f"titles include: {', '.join(titles.include or [])}\n"
        f"titles exclude: {', '.join(titles.exclude or [])}\n"
        f"locations include: {', '.join(locations.include or [])}\n"
        f"locations exclude: {', '.join(locations.exclude or [])}\n"
        f"industries include: {', '.join(industries.include or [])}\n"
        f"industries exclude: {', '.join(industries.exclude or [])}\n"
        f"keywords: {keywords or classic_kw or '(none)'}\n"
        f"classic drops: {', '.join(dropped) or '(none)'}"
    )


async def _default_preview(icp_id: str) -> str:
    """One page of persona-1 matches. Never creates campaign/contact/outreach rows."""
    from ..linkedin import UnipileError, get_account_id, get_linkedin_client
    from ..services.icp_search import build_segment_query, page_size, resolve_icp_record
    from ..tools.create_campaign import _icp_result_to_legacy

    record = await run_db(resolve_icp_record, icp_id)
    if not record:
        return "preview failed: ICP not found"
    try:
        result = icp_result_from_dict(json.loads(record["icp_json"]))
    except Exception as e:
        return f"preview failed: {e}"
    if not result.icps:
        return "preview failed: no personas"
    target = record.get("target_desc") or result.summary or ""
    legacy = _icp_result_to_legacy(result, target)
    segments = legacy.get("segments") or []
    if not segments:
        return "preview failed: no search segment"
    account_id = await run_db(get_account_id)
    if not account_id:
        return "preview failed: no LinkedIn account"
    try:
        client = get_linkedin_client()
    except UnipileError as e:
        return f"preview failed: {e}"

    search_acct_id = account_id
    use_sales_nav = False
    try:
        from ..services.search_account_resolver import resolve_search_account
        search_acct_id, use_sales_nav = await resolve_search_account(
            client=client, sending_account_id=account_id,
        )
    except Exception as e:
        logger.debug("ICP research search-account resolve failed: %s", e)

    keywords, filters = build_segment_query(segments[0], use_sales_nav)
    if not keywords and not filters:
        return "preview failed: empty query"
    override = search_acct_id if search_acct_id != account_id else None
    try:
        profiles, _cursor = await client.search_people(
            account_id=search_acct_id,
            keywords=keywords,
            count=page_size(use_sales_nav),
            use_sales_navigator=use_sales_nav,
            search_account_id=override,
            raise_on_error=True,
            **filters,
        )
    except Exception as e:
        return f"preview failed: {e}"

    lines = []
    for i, profile in enumerate(list(profiles)[:ICP_RESEARCH_PREVIEW_LIMIT], 1):
        name = profile.get("name") or "Unknown"
        title = profile.get("title") or profile.get("headline") or ""
        company = profile.get("company") or ""
        if company and title:
            lines.append(f"{i}. {name} — {title} at {company}")
        elif title:
            lines.append(f"{i}. {name} — {title}")
        else:
            lines.append(f"{i}. {name}")
    if not lines:
        return "preview: 0 profiles on the first page"
    return "\n".join(lines)


async def _apply_decision(
    icp_id: str,
    result: AgentResult,
    *,
    mode: str,
    enrich_fn: EnrichFn | None,
) -> IcpResearchOutcome:
    decision = result.decision if result.decision in {"keep", "revise", "hold"} else "keep"
    reason = result.reason or ("research could not decide" if result.decision == "none" else "")

    for step in result.steps:
        await run_db(
            log_action, "icp_research_step",
            result=str(step.get("action") or ""),
            details=step,
        )
    await run_db(
        log_action, "icp_research_decision",
        result=decision,
        details={
            "reason": reason,
            "mode": mode,
            "exhausted": result.exhausted,
            "patch": result.extras,
        },
    )

    applied = False
    updated: IcpResult | None = None
    if decision == "revise" and mode == "act":
        updated = await _persist_revise(icp_id, result.extras, enrich_fn)
        applied = True
        summary = f"revised — {reason}" if reason else "revised"
    elif decision == "revise":
        summary = f"revise recommended (observe) — {reason}" if reason else "revise recommended (observe)"
    elif decision == "hold":
        summary = f"hold — {reason}" if reason else "hold"
    else:
        summary = f"keep — {reason}" if reason else "keep"

    return IcpResearchOutcome(
        decision=decision,
        reason=reason,
        applied=applied,
        summary=summary,
        result=updated,
    )


async def _persist_revise(
    icp_id: str,
    extras: dict[str, str],
    enrich_fn: EnrichFn | None,
) -> IcpResult:
    record = await run_db(get_icp, icp_id)
    if not record:
        raise ValueError("ICP not found")
    data = json.loads(record.get("icp_json") or "{}")
    patched = apply_persona_patch(data, extras)
    result = icp_result_from_dict(patched)
    if enrich_fn is not None:
        maybe = enrich_fn(result)
        if inspect.isawaitable(maybe):
            await maybe
    else:
        try:
            from ..ai.icp_generator_v2 import _enrich_result
            await _enrich_result(result)
        except Exception as e:
            logger.debug("ICP research enrich failed: %s", e)
    await run_db(update_icp, icp_id, icp_json=icp_result_to_json(result))
    return result
