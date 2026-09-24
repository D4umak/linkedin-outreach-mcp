"""Tool: icp — inspect a saved ICP against LinkedIn without creating anything.

``generate_icp`` produces structured targeting (industry codes, title codes,
seniority, geography, headcount) and until now the only consumer was
``create_campaign(icp_id=...)``, which also writes a campaign, a contact row and
an outreach record per prospect. Checking whether an ICP was any good therefore
cost a campaign that then had to be deleted.

``icp(action='preview')`` runs the search create_campaign would run — same
builder, same page size, same client call — and prints what came back, plus
where the filters are hurting. It creates no campaign, no outreach, no contact
and no global_contacts row.

The one write it can cause is disclosed in the output: resolving the search
account caches ``premium_search_account_id`` / ``premium_search_detected_at`` in
the settings table, exactly as create_campaign does.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..ai.icp_schemas import IcpResult, SingleIcp, icp_result_from_dict
from ..constants import HOSTED_MIN_FIT_SCORE, MIN_FIT_SCORE_THRESHOLD
from ..db.async_bridge import run_db
from ..db.queries import get_setting, list_icps
from ..formatter import person_line
from ..linkedin import (
    UnipileAuthError,
    UnipileError,
    UnipileResultFormatError,
    get_account_id,
    get_linkedin_client,
)
from ..services.icp_search import (
    SALES_NAV_ONLY_FILTERS,
    build_segment_query,
    max_pages,
    page_size,
    resolve_icp_record,
)

logger = logging.getLogger(__name__)

VALID_ACTIONS = ("preview", "goal_match")

# Human labels for the enriched-code lists, so a filter can be read against what
# the ICP asked for. A LinkedIn code on its own tells the user nothing.
_CODE_SOURCE = {
    "industry_codes": ("industries", "Industries"),
    "location_codes": ("locations", "Locations"),
    "role_codes": ("job_titles", "Job titles"),
    "department_codes": ("departments", "Departments"),
}

_DIMENSION_WEIGHTS = (
    ("title", "Title", 0.30),
    ("industry", "Industry", 0.20),
    ("seniority", "Seniority", 0.15),
    ("company_size", "Company size", 0.15),
    ("location", "Location", 0.10),
    ("keywords", "Keywords", 0.10),
)


async def run_icp(
    action: str = "preview",
    icp_id: str = "",
    persona: int = 1,
    limit: int = 10,
    campaign_id: str = "",
    target_description: str = "",
    goal: str = "",
) -> str:
    """Dispatch an icp() action."""
    action = (action or "preview").strip().lower()
    if action not in VALID_ACTIONS:
        return (
            f"Unknown icp action: '{action}'.\n\n"
            f"icp() supports {len(VALID_ACTIONS)} actions: "
            + ", ".join(VALID_ACTIONS)
            + ".\n"
            "Usage: icp(action='preview', icp_id='<id from generate_icp>')"
        )
    if action == "goal_match":
        return await _goal_match(
            icp_id=icp_id,
            campaign_id=campaign_id,
            target_description=target_description,
            goal=goal,
        )
    return await _preview(icp_id=icp_id, persona=persona, limit=limit)


# ──────────────────────────────────────────────
# goal_match
# ──────────────────────────────────────────────

async def _goal_match(
    icp_id: str, campaign_id: str, target_description: str, goal: str = "",
) -> str:
    """Audit a saved ICP against a campaign goal.

    9 Sep 2026: a campaign targeted an audience defined by
    nationality. Nothing asked whether that audience could buy what the
    campaign sold until replies started coming back wrong. Read-only — no
    campaign, no contact, no outreach.

    The question asked follows the campaign's goal (#1153): read from the
    campaign when campaign_id is given, else the `goal` argument (sell).
    """
    from .. import goals
    from ..ai.goal_match import format_verdict, judge_goal_match

    goal_key = goals.normalize_goal(goal)
    if goal_key is None:
        return f"❌ Unknown goal '{goal}'. Valid values: {', '.join(goals.VALID_GOALS)}."

    if not icp_id:
        return (
            "icp(action='goal_match') needs an icp_id.\n\n"
            "Run icp(action='preview') with no icp_id to list your saved ICPs."
        )

    record = await run_db(resolve_icp_record, icp_id)
    if not record:
        return (
            f"ICP not found: `{icp_id}`\n\n"
            "Run icp(action='preview') with no icp_id to list your saved ICPs."
        )

    raw_json = record.get("icp_json")
    if not raw_json:
        return (
            f"ICP `{record['id'][:8]}` has no generated content yet.\n\n"
            "Its row exists but synthesis never finished. Re-run generate_icp()."
        )
    try:
        icp_json = json.loads(raw_json)
    except (TypeError, ValueError) as e:
        return f"Saved ICP `{record['id'][:8]}` could not be parsed: {e}"

    goal_text = (target_description or "").strip()
    offer = ""
    campaign_note = ""
    if campaign_id:
        from ..db.queries import get_campaign
        from ..services.project_brief import parse_campaign_context

        campaign = await run_db(get_campaign, campaign_id)
        if not campaign:
            return f"Campaign not found: `{campaign_id}`"
        try:
            config = json.loads(campaign.get("config_json") or "{}")
        except (TypeError, ValueError):
            config = {}
        goal_text = goal_text or str(config.get("target_description") or "")
        goal_key = goals.goal_from_config(config)
        ctx = parse_campaign_context(campaign)
        offer_bits: list[str] = []
        for key in ("product", "project_brief"):
            value = ctx.get(key)
            if isinstance(value, str) and value.strip():
                offer_bits.append(value.strip())
        for key in ("offerings", "case_studies", "social_proofs"):
            values = ctx.get(key)
            if isinstance(values, list):
                offer_bits.extend(str(v) for v in values[:4] if str(v).strip())
        offer = "\n".join(offer_bits)
        campaign_note = f"Campaign: {campaign.get('name') or campaign_id[:8]}\n"

    if not goal_text:
        goal_text = str(record.get("target_desc") or "")
    if not goal_text:
        return (
            "No campaign goal to audit against.\n\n"
            "Pass campaign_id=… or target_description='…'."
        )

    verdict = await judge_goal_match(goal_text, offer, icp_json, goal_key=goal_key)
    header = (
        f"ICP `{record['id'][:8]}` — {record.get('name') or 'unnamed'}\n"
        f"{campaign_note}"
        f"Goal: {goal_text[:200]}\n\n"
    )
    tail = ""
    if verdict.blocks_campaign:
        tail = (
            "\n\nThis ICP would be refused by create_campaign. Regenerate it "
            "with a target description that names who decides, or pass "
            "force=True if you know better than the audit."
        )
    elif verdict.verdict == "partial":
        tail = (
            "\n\nThis is a warning, not a block — create_campaign will still "
            "run. Fix the ICP first if the economic buyer is missing."
        )
    return header + format_verdict(verdict) + tail


# ──────────────────────────────────────────────
# preview
# ──────────────────────────────────────────────

async def _preview(icp_id: str, persona: int, limit: int) -> str:
    setup_done = await run_db(get_setting, "setup_complete", False)
    if not setup_done:
        return (
            "Setup required before previewing an ICP.\n\n"
            "Run setup_profile first — the preview searches LinkedIn through your "
            "connected account."
        )

    try:
        limit = max(1, min(int(limit or 10), 50))
    except (TypeError, ValueError):
        # A limit we cannot read is not a reason to refuse the preview.
        limit = 10

    if not icp_id:
        return await _list_icps_for_preview()

    record = await run_db(resolve_icp_record, icp_id)
    if not record:
        return (
            f"ICP not found: `{icp_id}`\n\n"
            "Run icp(action='preview') with no icp_id to list your saved ICPs, "
            "or generate one with generate_icp()."
        )

    try:
        result: IcpResult = icp_result_from_dict(json.loads(record["icp_json"]))
    except Exception as e:
        logger.warning("Saved ICP %s could not be parsed: %s", record["id"][:8], e)
        return (
            f"ICP `{record['id'][:8]}` is saved but its stored JSON could not be "
            f"parsed, so there is nothing to preview.\n\nReason: {e}\n\n"
            "Re-run generate_icp() to produce a readable one."
        )

    if not result.icps:
        return (
            f"ICP `{record['id'][:8]}` ({record.get('name') or 'unnamed'}) contains "
            "no personas, so there is no targeting to search with.\n\n"
            "Re-run generate_icp() with a different target description."
        )

    persona_count = len(result.icps)
    try:
        persona_idx = int(persona)
    except (TypeError, ValueError):
        persona_idx = 1
    if persona_idx < 1 or persona_idx > persona_count:
        listing = "\n".join(
            f"  {i}. {p.name or 'Segment'}" for i, p in enumerate(result.icps, 1)
        )
        return (
            f"persona={persona} is out of range — this ICP has {persona_count} "
            f"persona(s):\n{listing}\n\n"
            f"Try icp(action='preview', icp_id='{record['id'][:8]}', persona=1)."
        )

    single: SingleIcp = result.icps[persona_idx - 1]

    # Same conversion create_campaign feeds to its search loop.
    from .create_campaign import _icp_result_to_legacy

    target_description = record.get("target_desc") or result.summary or ""
    legacy = _icp_result_to_legacy(result, target_description)
    segments = legacy.get("segments", [])
    if persona_idx > len(segments):
        return (
            f"Persona {persona_idx} has no search segment in this ICP, so there is "
            "nothing to send to LinkedIn."
        )
    segment = segments[persona_idx - 1]

    account_id = await run_db(get_account_id)
    if not account_id:
        return (
            "No LinkedIn account connected.\n\n"
            "Run setup_profile first — the preview searches LinkedIn through your "
            "connected account."
        )

    try:
        client = get_linkedin_client()
    except UnipileError as e:
        return f"Preview could not start: {e}"

    # Same resolution create_campaign does. This is the one thing in the preview
    # that can write, and the output says so.
    search_acct_id = account_id
    use_sales_nav = False
    try:
        from ..services.search_account_resolver import resolve_search_account
        search_acct_id, use_sales_nav = await resolve_search_account(
            client=client, sending_account_id=account_id,
        )
    except Exception as e:
        logger.warning("Search account resolution failed, using own account: %s", e)
        search_acct_id = account_id
        use_sales_nav = False
    search_account_override = search_acct_id if search_acct_id != account_id else None

    keywords, filters = build_segment_query(segment, use_sales_nav)
    per_page = page_size(use_sales_nav)

    if not keywords and not filters:
        return _no_query_output(record, persona_idx, persona_count, segment)

    async def _search(kw: str, flt: dict[str, Any]) -> list[dict]:
        results, _cursor = await client.search_people(
            account_id=search_acct_id,
            keywords=kw,
            count=per_page,
            use_sales_navigator=use_sales_nav,
            search_account_id=search_account_override,
            raise_on_error=True,
            **flt,
        )
        return list(results)

    try:
        profiles = await _search(keywords, filters)
    except UnipileResultFormatError as e:
        logger.warning("ICP preview: unreadable search rows: %s", e)
        return (
            f"{_header(record, persona_idx, persona_count, segment)}\n\n"
            "LinkedIn answered this ICP's search with rows this tool could not "
            "read, so this is NOT a 'nothing matches this ICP' result.\n\n"
            f"Reason: {e}\n\n"
            "The request itself succeeded.\n\n"
            + _write_disclosure() + "\n\n"
            + _query_block(segment, single, keywords, filters, use_sales_nav)
        )
    except UnipileAuthError as e:
        logger.warning("ICP preview: search rejected: %s", e)
        return (
            f"{_header(record, persona_idx, persona_count, segment)}\n\n"
            "The search did not run, so this is NOT a 'nothing matches this ICP' "
            "result.\n\n"
            f"Reason: {e}\n\n"
            "The LinkedIn account is not authenticated, so retrying will fail the "
            "same way. Reconnect it with setup_profile(), then preview again.\n\n"
            + _write_disclosure() + "\n\n"
            + _query_block(segment, single, keywords, filters, use_sales_nav)
        )
    except Exception as e:
        logger.warning("ICP preview: search failed: %s", e)
        return (
            f"{_header(record, persona_idx, persona_count, segment)}\n\n"
            "The search did not complete, so this is NOT a 'nothing matches this "
            "ICP' result — the filters below were never answered.\n\n"
            f"Reason: {e}\n\n"
            "Retry in a moment; if it keeps failing, check the LinkedIn connection "
            "with account().\n\n"
            + _write_disclosure() + "\n\n"
            + _query_block(segment, single, keywords, filters, use_sales_nav)
        )

    lines = [_header(record, persona_idx, persona_count, segment), ""]
    lines.append(_write_disclosure())
    lines.append("")
    lines.append(_query_block(segment, single, keywords, filters, use_sales_nav))
    lines.append("")

    returned = len(profiles)
    capped = returned >= per_page
    lines.append("── What came back ──")
    lines.append(
        f"LinkedIn returned {returned} profile(s) for the first page of this "
        f"search (page size {per_page})."
    )
    lines.append(
        f"create_campaign runs this same search with up to "
        f"{max_pages(use_sales_nav)} pages of pagination; the preview reads the "
        "first page only, so it sees at most one page of whatever the campaign "
        "would collect."
    )
    lines.append("")

    # ── Per-filter count contribution ──
    lines.append(await _count_contribution(_search, keywords, filters, returned, capped, per_page))
    lines.append("")

    if not profiles:
        lines.append(
            "With no rows returned there is nothing to score and no profiles to "
            "list. The counts above are the evidence for which filter to change."
        )
        return "\n".join(lines)

    # ── Score against this persona with create_campaign's scorer ──
    from ..services.icp_match_scorer import compute_icp_match

    from ..services.enrolment_why import enrolment_why

    scoring_icp = {"segments": [segment]}
    breakdowns: list[dict[str, float]] = []
    segment_name = str(segment.get("name") or "")
    for p in profiles:
        scored = compute_icp_match(p, scoring_icp)
        p["fit_score"] = scored["icp_match_score"]
        breakdown = scored.get("breakdown") or {}
        breakdowns.append(breakdown)
        p["why"] = enrolment_why(p, breakdown, segment_name)
    profiles.sort(key=lambda p: p.get("fit_score", 0.0), reverse=True)

    lines.append(_fit_block(profiles, breakdowns, persona_idx))
    lines.append("")

    if not use_sales_nav:
        lines.append(_seniority_post_filter_block(profiles, segment))
        lines.append("")
    explain = _seniority_explain(profiles, segment)
    if explain:
        lines.append(explain)
        lines.append("")

    dedup_marks, dedup_block = await _dedup_block(profiles, account_id)
    lines.append(_profile_list(profiles, limit, dedup_marks))
    lines.append("")
    lines.append(dedup_block)
    lines.append("")
    lines.append(_next_steps(record, persona_idx, target_description))

    return "\n".join(lines)


# ──────────────────────────────────────────────
# Output blocks
# ──────────────────────────────────────────────

def _header(record: dict, persona_idx: int, persona_count: int, segment: dict) -> str:
    name = record.get("name") or "unnamed ICP"
    return (
        f"ICP preview — **{name}** (`{record['id'][:8]}`)\n"
        f"Persona {persona_idx} of {persona_count}: {segment.get('name') or 'Segment'}"
    )


def _write_disclosure() -> str:
    return (
        "Nothing was created: this preview wrote no campaign, no outreach, no "
        "contact row and no global_contacts row.\n"
        "It is not a pure read, though — picking the search account can cache two "
        "values in the settings table (premium_search_account_id, "
        "premium_search_detected_at), which is the same cache create_campaign fills."
    )


def _query_block(
    segment: dict,
    single: SingleIcp,
    keywords: str,
    filters: dict[str, Any],
    use_sales_nav: bool,
) -> str:
    mode = "Sales Navigator search" if use_sales_nav else "Classic LinkedIn search"
    lines = [f"── Search built from this ICP ({mode}) ──"]
    lines.append(f"keywords: {json.dumps(keywords)}" if keywords else "keywords: (none sent)")

    code_names = _enriched_names(single)
    for key, value in filters.items():
        lines.append(f"{key}: {_render_filter(key, value, code_names)}")
    enriched = {k: v for k, v in filters.items() if k != "title_keywords"}
    if not enriched:
        lines.append(
            "(no structured filters — this ICP has no enriched LinkedIn codes, so "
            "the search is keyword-only)"
        )
    else:
        lines.append(
            "Read the names above against what you asked for: these are the codes "
            "generate_icp resolved, and they are what LinkedIn filters on. A name "
            "that is not what you meant is a filter pointed at the wrong people."
        )

    if not use_sales_nav:
        dropped = _sales_nav_only_present(segment, code_names)
        if dropped:
            lines.append("")
            lines.append(
                "In the ICP but NOT sent — the Classic search request has no field "
                "for these (the Sales Navigator request does):"
            )
            lines.extend(f"  {item}" for item in dropped)
            if segment.get("seniority"):
                lines.append(
                    "create_campaign applies seniority locally after the search "
                    "instead — see the seniority line further down."
                )
    spec = segment.get("profile_signals") or getattr(single, "profile_signals", None)
    if isinstance(spec, dict) and spec.get("kind") not in (None, "none"):
        lines.append("")
        lines.append("── Profile-signal targeting ──")
        lines.append(f"kind: {spec.get('kind')}  label: {spec.get('label')}")
        for q in spec.get("recall_queries") or []:
            lines.append(
                f"recall: keywords={q.get('keywords')!r} "
                f"title={q.get('title_or') or '(none)'} why={q.get('why')}"
            )
        ev = spec.get("evidence") or {}
        lines.append(
            "evidence keep: school OR language OR worked-in-country"
            if spec.get("kind") == "country_tie"
            else "evidence keep: distinctive about/volunteer/skill term"
        )
        if ev.get("schools"):
            lines.append("schools: " + ", ".join(ev["schools"][:8]))
        if ev.get("languages"):
            lines.append("languages: " + ", ".join(ev["languages"]))
        if ev.get("experience_places"):
            lines.append("experience places: " + ", ".join(ev["experience_places"][:8]))
        if ev.get("about_terms"):
            lines.append("about terms: " + ", ".join(ev["about_terms"][:8]))
        for note in spec.get("explain") or []:
            lines.append(note)
    return "\n".join(lines)


def _render_filter(key: str, value: Any, code_names: dict[str, dict[str, str]]) -> str:
    """Render one filter value, naming every resolved code — no truncation.

    The junk this block exists to reveal is exactly the code that would be hidden
    inside a "+34 more", so the full list is printed however long it is.
    """
    source = _CODE_SOURCE.get(key)
    if source and isinstance(value, list):
        names = code_names.get(source[0], {})
        rendered = [
            f"{names[str(code)]} ({code})" if str(code) in names else f"code {code} — no name stored"
            for code in value
        ]
        label = "code" if len(value) == 1 else "codes"
        return f"{len(value)} {label}: " + ", ".join(rendered)
    if isinstance(value, list):
        return f"{len(value)} value(s) — " + ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items())
    return str(value)


def _enriched_names(single: SingleIcp) -> dict[str, dict[str, str]]:
    """code → name, per enriched field, straight off the stored ICP."""
    out: dict[str, dict[str, str]] = {}
    enriched = single.linkedin_enriched_params
    if not enriched:
        return out
    for field in ("industries", "locations", "job_titles", "departments"):
        holder = getattr(enriched, field, None)
        include = getattr(holder, "include", None) or [] if holder else []
        out[field] = {
            str(p.code): p.name for p in include if getattr(p, "code", "") and getattr(p, "name", "")
        }
    return out


def _sales_nav_only_present(
    segment: dict, code_names: dict[str, dict[str, str]],
) -> list[str]:
    """Which Sales-Nav-only filters this ICP carries but Classic search drops.

    Rendered with the resolved names, not bare codes: a junk title code is
    exactly the kind of thing this line exists to show.
    """
    mapping = {
        "role_codes": "title_codes",
        "seniority": "seniority",
        "company_headcount": "company_headcount",
        "company_types": "company_types",
        "department_codes": "department_codes",
        "tenure": "tenure",
        "spotlight": "spotlight",
        "annual_revenue": "annual_revenue",
        "company_headcount_growth": "company_headcount_growth",
    }
    present = []
    for filter_key in SALES_NAV_ONLY_FILTERS:
        value = segment.get(mapping[filter_key])
        if not value:
            continue
        if filter_key in _CODE_SOURCE:
            present.append(f"{filter_key}={_render_filter(filter_key, value, code_names)}")
        else:
            present.append(f"{filter_key}={_short(value)}")
    return present


def _short(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(str(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {v}" for k, v in value.items()) + "}"
    return str(value)


async def _count_contribution(
    search: Any,
    keywords: str,
    filters: dict[str, Any],
    baseline: int,
    capped: bool,
    per_page: int,
) -> str:
    """Leave-one-out counts: which filter is holding the result set down."""
    lines = ["── Per-filter contribution ──"]

    droppable: list[str] = list(filters.keys())
    if keywords:
        droppable.insert(0, "keywords")

    if not droppable:
        lines.append(
            "Nothing to drop — the search carried neither keywords nor filters."
        )
        return "\n".join(lines)

    if capped:
        lines.append(
            f"Not measured. The first page came back full ({baseline} of a "
            f"{per_page}-row page), so re-running without a filter could not show a "
            "bigger number and would say nothing about which filter binds. "
            "Removing a filter is only measurable once the result set is short."
        )
        return "\n".join(lines)

    lines.append(
        "Each row below re-ran this same search once with that one filter removed "
        "(first page only). A number well above the baseline means that filter is "
        "what is holding the result set down."
    )
    lines.append(f"  {'all filters (baseline)':<34}{baseline}")

    for key in droppable:
        if key == "keywords":
            probe_kw, probe_filters = "", dict(filters)
        else:
            probe_kw = keywords
            probe_filters = {k: v for k, v in filters.items() if k != key}
        try:
            probe = await search(probe_kw, probe_filters)
        except Exception as e:
            # A probe is a diagnostic, not a gate: one failing probe must not
            # cost the user the preview or be printed as a count of zero.
            logger.info("ICP preview probe without %s failed: %s", key, e)
            lines.append(f"  {'without ' + key:<34}not measured — probe failed: {e}")
            continue
        count = len(probe)
        suffix = f"  (page size {per_page} reached — could be more)" if count >= per_page else ""
        lines.append(f"  {'without ' + key:<34}{count}{suffix}")

    return "\n".join(lines)


def _applicable_fit_floor() -> tuple[float, str]:
    """The floor that will actually apply, and who applies it.

    Two definitions of one thing: the client drops below
    MIN_FIT_SCORE_THRESHOLD (0.3) and the cloud scheduler below
    DEFAULT_MIN_FIT_SCORE (0.5). This block printed 0.3 unconditionally, and
    on a hosted account — where the cloud is the sender — that is the wrong
    number for every campaign the user will create from this preview.
    """
    from ..config import is_backend_mode

    if is_backend_mode():
        return HOSTED_MIN_FIT_SCORE, "the hosted scheduler"
    return MIN_FIT_SCORE_THRESHOLD, "create_campaign"


def _seniority_explain(profiles: list[dict], segment: dict) -> str:
    """Per-profile seniority verdict, the way profile_signals explains evidence."""
    from .create_campaign import _segment_seniority

    include, exclude = _segment_seniority(segment)
    if not include and not exclude:
        return ""

    from ..services.icp_match_scorer import seniority_verdict

    verdicts = [(p, seniority_verdict(
        p.get("title") or p.get("headline") or "", segment,
    )) for p in profiles]
    misses = sum(1 for _, v in verdicts if not v["keep"])
    dms = sum(1 for _, v in verdicts if v["decision_maker"])
    lines = [
        "── Seniority per profile ──",
        f"{dms} of {len(verdicts)} are decision makers "
        "(owner / cxo / vp / director); "
        f"{misses} score 0.00 on seniority and are remembered as "
        "seniority_miss.",
    ]
    for p, v in verdicts[:15]:
        name = p.get("name") or "Unknown"
        lines.append(f"  {name[:26]:<28}{(v['level'] or '—'):<10}{v['explain']}")
    return "\n".join(lines)


def _fit_block(
    profiles: list[dict], breakdowns: list[dict[str, float]], persona_idx: int,
) -> str:
    n = len(profiles)
    lines = ["── Fit of what came back ──"]
    lines.append(
        f"Mean per-dimension score from compute_icp_match — the scorer "
        f"create_campaign ranks prospects with — across all {n} returned "
        "profile(s), scored against this persona:"
    )
    for key, label, weight in _DIMENSION_WEIGHTS:
        values = [b.get(key) for b in breakdowns if isinstance(b.get(key), (int, float))]
        if not values:
            lines.append(f"  {label:<14}n/a")
            continue
        mean = sum(values) / len(values)
        lines.append(f"  {label:<14}{mean:.2f}   (weight {weight:.2f})")
    lines.append(
        "  compute_icp_match returns 0.30 for a dimension it has nothing to "
        "compare — either the search row does not carry it (industry and company "
        "size usually don't until a full profile is fetched) or this persona does "
        "not set it — and 0.50 for location in the same situation. Those values "
        "mean \"no signal\", not \"poor match\"."
    )

    floor, floor_note = _applicable_fit_floor()
    below = sum(1 for p in profiles if p.get("fit_score", 0.0) < floor)
    lines.append(
        f"{below} of {n} score below {floor}, the floor {floor_note} drops "
        "prospects at before queueing them."
    )
    if persona_idx > 1:
        lines.append(
            f"Note: create_campaign scores every prospect against persona 1, "
            f"whichever persona found it, so its numbers for persona {persona_idx} "
            "prospects would not be these."
        )
    return "\n".join(lines)


def _seniority_post_filter_block(profiles: list[dict], segment: dict) -> str:
    target = segment.get("seniority") or []
    if not target:
        return (
            "── Seniority ──\n"
            "This persona sets no seniority, so nothing is filtered on it."
        )
    from .create_campaign import _post_filter_classic_prospects

    kept = _post_filter_classic_prospects(profiles, [segment], {})
    dropped = len(profiles) - len(kept)
    return (
        "── Seniority ──\n"
        f"The Classic search request has no seniority field, so create_campaign "
        f"applies {_short(target)} to the titles after the search instead. On "
        f"these rows that drops {dropped} of {len(profiles)}."
    )


def _profile_list(profiles: list[dict], limit: int, marks: dict[str, str]) -> str:
    shown = profiles[:limit]
    lines = [f"── Profiles ({len(shown)} of {len(profiles)}, best fit first) ──"]
    for i, p in enumerate(shown, 1):
        title = p.get("title") or p.get("headline") or "(no title in search row)"
        key = _identity_key(p)
        mark = f"  [{marks[key]}]" if key and marks.get(key) else ""
        lines.append(f"  {i}. " + person_line(
            p.get("name") or "Unknown", p.get("linkedin_url") or "",
            title=title, company=p.get("company") or "", why=p.get("why"),
        ) + mark)
    return "\n".join(lines)


def _identity_key(p: dict) -> str:
    return (p.get("public_id") or p.get("provider_id") or p.get("linkedin_url") or "").lower().strip()


async def _dedup_block(profiles: list[dict], account_id: str) -> tuple[dict[str, str], str]:
    """Mark rows create_campaign would drop, using local reads only.

    dedup_service.fetch_connection_ids is deliberately NOT used here: it
    auto-syncs whenever the connection cache is stale, and that sync inserts one
    global_contacts row per 1st-degree connection. A preview cannot write to the
    contact base, so it reads the cache as it stands and reports it as weaker.
    """
    marks: dict[str, str] = {}
    lines = ["── Overlap with what you already have ──"]

    from ..services.connection_sync import get_local_connection_ids, get_sync_age
    from ..services.dedup_service import get_all_known_linkedin_ids

    try:
        connection_ids = await run_db(get_local_connection_ids, account_id)
        sync_age = await run_db(get_sync_age, account_id)
    except Exception as e:
        logger.warning("ICP preview: local connection cache unreadable: %s", e)
        connection_ids, sync_age = set(), None
        lines.append(
            f"Your local connection cache could not be read ({e}), so no row below "
            "is marked as an existing connection. That is a gap in this preview, "
            "not a statement that none of them are connected."
        )

    try:
        known_ids = await run_db(get_all_known_linkedin_ids)
    except Exception as e:
        logger.warning("ICP preview: contact base unreadable: %s", e)
        known_ids = set()
        lines.append(
            f"Your contact base could not be read ({e}), so no row below is marked "
            "as already contacted."
        )

    connections = 0
    known = 0
    for p in profiles:
        pub = (p.get("public_id") or "").lower().strip()
        ids = {
            pub,
            (p.get("provider_id") or "").lower().strip(),
            (p.get("linkedin_url") or "").lower().strip(),
        } - {""}
        if pub:
            # dedup_prospects compares this constructed form too.
            ids.add(f"https://www.linkedin.com/in/{pub}")
        key = _identity_key(p)
        if ids & connection_ids:
            connections += 1
            if key:
                marks[key] = "already a 1st-degree connection"
        elif ids & known_ids:
            known += 1
            if key:
                marks[key] = "already in your contact base"

    lines.append(
        f"{connections} of {len(profiles)} are 1st-degree connections in your local "
        f"cache and {known} more are already in your contact base. A standard "
        "campaign drops both before queueing. It drops more besides — rows below "
        "the fit floor, and anything its heuristics read as a company page — so "
        "this is a lower bound on what it would discard, not the queue size."
    )
    if sync_age is None:
        lines.append(
            "Caveat: nothing has ever been synced into your local connection cache "
            "on this account, so the connection count above is 0 because there is "
            "nothing to compare against — not because you know none of these "
            "people. create_campaign refreshes that cache; a preview must not, "
            "because the refresh writes a global_contacts row per connection."
        )
    else:
        lines.append(
            f"Caveat: the connection cache was last synced {_age(sync_age)} ago and "
            "this preview does NOT refresh it — the refresh writes a "
            "global_contacts row per connection, which a preview must not do. "
            "Anyone you connected with since then still shows here as new."
        )
    return marks, "\n".join(lines)


def _age(seconds: int) -> str:
    if seconds < 3600:
        return f"{max(seconds // 60, 0)}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _next_steps(record: dict, persona_idx: int, target_description: str) -> str:
    short_id = record["id"][:8]
    target = target_description or record.get("name") or ""
    lines = ["── Next ──"]
    lines.append(
        f"  icp(action='preview', icp_id='{short_id}', persona={persona_idx}, "
        "limit=25) — the same search again, listing up to 25 rows"
    )
    lines.append(
        f"  generate_icp(target_description={json.dumps(target)}) — build fresh "
        "targeting if the filters above are pointed at the wrong people. This "
        "saves a NEW ICP; it does not edit this one."
    )
    lines.append(
        f"  create_campaign(target_description={json.dumps(target)}, "
        f"icp_id='{short_id}') — run this ICP for real. That call does write: a "
        "campaign, a contact row per prospect and one outreach each. The campaign "
        "is created as a draft and sends nothing until campaign(action='launch')."
    )
    return "\n".join(lines)


def _no_query_output(
    record: dict, persona_idx: int, persona_count: int, segment: dict,
) -> str:
    return (
        f"{_header(record, persona_idx, persona_count, segment)}\n\n"
        "No search was sent: this persona produced neither keywords nor a single "
        "structured filter, so the request would have been an unfiltered LinkedIn "
        "search and its results would say nothing about this ICP.\n\n"
        + _write_disclosure() + "\n\n"
        "Re-run generate_icp() for this target — an ICP with no job titles, no "
        "industries, no locations and no keywords cannot be searched with."
    )


async def _list_icps_for_preview() -> str:
    records = await run_db(list_icps)
    if not records:
        return (
            "No saved ICPs to preview.\n\n"
            "Create one with generate_icp(target_description='...'), then run "
            "icp(action='preview', icp_id='<id>')."
        )
    lines = ["Saved ICPs — pass one to preview it:", ""]
    for r in records[:25]:
        lines.append(
            f"  `{r['id'][:8]}`  {r.get('name') or 'unnamed'}"
            + (f"  — {r['target_desc'][:60]}" if r.get("target_desc") else "")
        )
    if len(records) > 25:
        lines.append(f"  ... and {len(records) - 25} more")
    lines.extend([
        "",
        f"  icp(action='preview', icp_id='{records[0]['id'][:8]}')",
    ])
    return "\n".join(lines)
