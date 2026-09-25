"""ICP Generator V2 — rich multi-persona ICP generation.

Produces 2-4 ICPs with pain points, fears, barriers, include/exclude
LinkedIn params, and confidence scores.  Ported from the original
AI in Charge ICP Agent, adapted for HeyLead's lightweight MCP architecture.

Phase 1: Enhanced LLM prompt only (no RAG).
Phase 4 will add RAG pipeline integration when company_context is provided.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from ..db.async_bridge import run_db
from ..services.seniority import apply_seniority_policy
from .icp_schemas import (
    HeadcountParam,
    IcpResult,
    LinkedinSearchParam,
    SingleIcp,
    TenureParam,
)
from .llm import LLMClient

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# System prompt
# ──────────────────────────────────────────────

ICP_V2_SYSTEM = """\
You are the ICP assistant: a research-grade agent with deep reasoning. \
Your mission is to turn context about a client's business into \
2-4 psychologically rich ICP personas with LinkedIn search parameters \
in strict JSON format aligned with LinkedIn Sales Navigator filters.

You output structured JSON only. No markdown, no explanation."""

# ──────────────────────────────────────────────
# User prompt template
# ──────────────────────────────────────────────

ICP_V2_PROMPT_TEMPLATE = """\
{goal_opening}

## TARGET DESCRIPTION
"{target_description}"

## USER CONTEXT (the sender)
Name: {user_name}
Title: {user_title}
Company: {user_company}
Expertise: {user_expertise}
{company_context_section}
{focus_section}
{kb_evidence_section}

## QUALITY BARS
* LinkedIn parameter accuracy >= 85% (top results must clearly fit the persona)
* "That's exactly me" resonance >= 60% (qualitative alignment with LinkedIn behavioral indicators)
* Use evidence from context above — cite or abstain if insufficient evidence

## WORKFLOW
1. Intake & scoping: identify product, audience, regions, success definition.
2. Draft ICPs (2-4): apply DNA-of-Client framework \
(Identity/Values -> Pains/Fears/Triggers -> Barriers/Myths -> Transformation -> Decision Mechanics).

## LINKEDIN PARAMETER FORMAT RULES
Strict formatting for each field:
- **locations**: maximum granularity is country, minimum is city. \
Do NOT use regional terms like "EMEA", "APAC", or "North America".
- **job_titles**: use specific but simple wordings. \
Example: "Director of Sales" not "Director of Sales Operations".
- **departments**: short terms, 1-2 words only: "Sales", "Business Development", "Marketing".
- **industries**: 1-3 words maximum: "Staffing", "Coaching", "Consulting".

## TASK
For each ICP, provide:

### Identity
- **name**: Short segment name (e.g., "Enterprise Fintech CTOs")
- **description**: 2-3 sentence description of this target segment

### {moves_heading}
- **pain_points**: {moves_0}
- **fears**: {moves_1}
- **barriers**: {moves_2}

### LinkedIn Search Parameters
Use include/exclude pattern for precise targeting:
- **industries**: {{"include": ["Financial Services", "Fintech"], "exclude": []}}
- **job_titles**: {{"include": ["CTO", "VP Engineering"], "exclude": ["Intern"]}}
- **locations**: {{"include": ["United States"], "exclude": []}}
- **departments**: {{"include": ["Engineering"], "exclude": []}} or null
- **company_headcount**: {{"min": 51, "max": 500}} \
(valid mins: 1,11,51,201,501,1001,5001,10001; valid maxes: 10,50,200,500,1000,5000,10000)
- **company_types**: list from: self_employed, self_owned, government_agency, \
public_company, privately_held, non_profit, educational_institution, partnership
- **company_locations**: {{"include": [...], "exclude": []}} or null
- **tenure**: {{"min": 1, "max": 5}} (valid mins: 0,1,3,6,10; valid maxes: 1,2,5,10)
- **seniority**: {{"include": ["cxo", "vp", "director"], "exclude": ["manager", "senior", "entry"]}} \
(use these exact keys and no others: owner, cxo, vp, director, manager, senior, entry. \
{seniority_rule})
- **keywords**: list of LinkedIn search keywords
- **spotlight_filters**: {{"changed_jobs": true, "posted_recently": true}} or null
  (Sales Navigator only — target people who recently changed jobs or posted on LinkedIn.
  These are warm lead signals. Use changed_jobs for hiring/transition intent, posted_recently for engaged prospects.)
- **boolean_keywords**: Advanced Boolean search string for Sales Navigator
  (e.g., '("VP Sales" OR "Head of Sales") AND fintech NOT "intern"')
  Uses AND, OR, NOT, quotes for exact phrases, parentheses for grouping.
  Leave "" if the regular keywords are sufficient.
- **annual_revenue**: {{"min": 1000000, "max": 50000000}} or null
  (Sales Navigator only — company annual revenue range in USD.
  Use when the target description implies company size by revenue.)
- **company_headcount_growth**: "negative", "neutral", "positive", or "rapid"
  (Sales Navigator only — recent headcount growth trend. Use "rapid" for fast-growing companies,
  "positive" for steadily growing. Leave "" if not relevant.)

### Confidence
Rate your confidence in this ICP (0.0-1.0):
- **overall_confidence**: How reliable is this ICP overall
- **data_completeness**: How many fields are meaningfully populated
- **evidence_strength**: Quality of supporting evidence (lower if no company context provided)

## RESPONSE FORMAT
Return ONLY valid JSON:
{{
    "summary": "One-line description of the ideal {noun}",
    "campaign_name": "Short campaign name (3-5 words)",
    "relevance_hook": "Why the sender is credible reaching out to this audience",
    "icps": [
        {{
            "name": "Segment Name",
            "description": "...",
            "pain_points": ["...", "..."],
            "fears": ["...", "..."],
            "barriers": ["...", "..."],
            "industries": {{"include": ["..."], "exclude": []}},
            "job_titles": {{"include": ["..."], "exclude": []}},
            "locations": {{"include": ["..."], "exclude": []}},
            "departments": {{"include": ["..."], "exclude": []}},
            "company_headcount": {{"min": 51, "max": 500}},
            "company_types": ["privately_held"],
            "company_locations": null,
            "tenure": {{"min": 1, "max": 5}},
            "seniority": {{"include": ["cxo", "vp"], "exclude": []}},
            "keywords": ["fintech", "payments"],
            "spotlight_filters": {{"changed_jobs": true, "posted_recently": false}},
            "boolean_keywords": "",
            "annual_revenue": {{"min": 1000000, "max": 50000000}},
            "company_headcount_growth": "positive",
            "overall_confidence": 0.75,
            "data_completeness": 0.80,
            "evidence_strength": 0.65
        }}
    ]
}}"""

_SELL_OPENING = (
    "Create 2-4 Ideal Customer Profiles (ICPs) targeting different but "
    "FITTING market segments as customers for the sender's business."
)
_SELL_SENIORITY_RULE = (
    "Only owner, cxo, vp and director hold budget authority, so a persona must name one of "
    "those in \"include\" unless the target description explicitly asks for individual "
    "contributors — put the levels you are not targeting in \"exclude\"."
)
_NO_FLOOR_SENIORITY_RULE = {
    "job_search": "Managers and heads of the function hire, so manager is a valid level; "
                  "name the levels that hire for or refer into this role.",
    "hire": "Take the level from the role described; name the levels a candidate for it holds.",
    "research": "Take the level from the experience described; seniority is not a filter on its own.",
}


def icp_prompt_for(goal: str) -> str:
    """The ICP prompt with the goal's frame filled in, ready for the target fields.

    Twin of heylead-api's `llm.icp_prompt_for` (#1153): the ICP step used to
    assume a sale, so a job seeker got customers. `sell` renders
    byte-identical to the prompt before #1153
    (tests/fixtures/icp_v2_prompt_sell.txt).
    """
    from .. import goals

    g = goals.GOALS[goals.normalize_goal(goal) or goals.DEFAULT_GOAL]
    if g.key == goals.SELL:
        opening, heading, rule = _SELL_OPENING, "Buyer Psychology", _SELL_SENIORITY_RULE
    else:
        opening = (
            f"Create 2-4 personas of people who {g.persona}, for a sender whose goal is: "
            f"{g.label}. Not customers: the sender is not selling to them."
        )
        if g.key == goals.JOB_SEARCH:
            opening += " " + goals.JOB_SEARCH_ROLE_RULE
        heading = "What moves them"
        rule = (
            _SELL_SENIORITY_RULE if goals.seniority_floor_applies(g.key)
            else _NO_FLOOR_SENIORITY_RULE[g.key]
        )
    # .replace rather than .format so the JSON braces stay doubled for the
    # later .format() with the target fields.
    return (
        ICP_V2_PROMPT_TEMPLATE
        .replace("{goal_opening}", opening)
        .replace("{moves_heading}", heading)
        .replace("{moves_0}", g.moves[0])
        .replace("{moves_1}", g.moves[1])
        .replace("{moves_2}", g.moves[2])
        .replace("{seniority_rule}", rule)
        .replace("{noun}", g.noun)
    )


# The sell prompt under its old name, for callers and tests that read it.
ICP_V2_PROMPT = icp_prompt_for(goal="sell")


# ──────────────────────────────────────────────
# Sales-methodology evidence (shipped KB)
# ──────────────────────────────────────────────

KB_EVIDENCE_HEADING = (
    "## SALES METHODOLOGY EVIDENCE (cite persona/stage/source; abstain if irrelevant)"
)


def build_kb_evidence_section(
    kb_evidence: list[str] | None, *, target_description: str = "",
) -> str:
    """Render the appended KB block, retrieving it when the caller has none.

    9 Sep 2026: the README's "RAG-powered buyer personas" was true
    only on the crawl path. Retrieving here means the plain
    `generate_icp_v2(target_description=...)` call — the one every campaign
    without a website takes — is grounded in the book corpus too.
    """
    lines = list(kb_evidence or [])
    if not lines and target_description:
        try:
            from .kb_retrieval import format_kb_evidence, retrieve_kb

            rendered = format_kb_evidence(retrieve_kb(target_description, top_k=6))
            if rendered:
                return f"\n{KB_EVIDENCE_HEADING}\n{rendered}\n"
        except Exception:
            logger.warning("KB retrieval failed; continuing without it", exc_info=True)
        return ""
    if not lines:
        return ""
    numbered = "\n".join(f"{i}. {line}" for i, line in enumerate(lines[:8], 1))
    return f"\n{KB_EVIDENCE_HEADING}\n{numbered}\n"


# ──────────────────────────────────────────────
# Generator
# ──────────────────────────────────────────────

async def generate_icp_v2(
    target_description: str,
    company_context: str = "",
    focus_query: str = "",
    user_profile: dict[str, Any] | None = None,
    confidence_threshold: float = 0.4,
    kb_evidence: list[str] | None = None,
    decision_makers_only: bool = True,
    goal: str = "sell",
) -> IcpResult:
    """Generate a rich multi-persona ICP with LinkedIn enrichment.

    Flow:
    1. Generate ICP personas via LLM (backend or local)
    2. Filter by confidence threshold
    3. Enrich with LinkedIn parameter codes (industries, locations, titles → codes)

    Args:
        target_description: Natural language target (e.g., "CTOs at fintech startups")
        company_context: URL or text about the sender's company
        focus_query: Optional focus (e.g., "enterprise segment only")
        user_profile: Sender's profile + expertise data
        confidence_threshold: Min confidence to include an ICP (0.0-1.0)
        kb_evidence: Pre-retrieved sales-methodology lines. When omitted the
            generator retrieves them itself, so the no-context path is
            grounded too (9 Sep 2026).
        decision_makers_only: Constrain every persona's seniority.include to
            owner / cxo / vp / director. Default True — 9 Sep 2026:
            the product targets people who hold budget authority. Applies
            only to the selling-shaped goals (goals.seniority_floor_applies).
        goal: One of heylead.goals.VALID_GOALS; decides whose profile this
            is (customers for sell, hiring managers for job_search, ...) and
            whether the sales KB grounds it (#1153).

    Returns:
        IcpResult with 2-4 SingleIcp personas, enriched with LinkedIn codes.
    """
    from .. import goals

    start_time = time.time()
    goal_key = goals.normalize_goal(goal) or goals.DEFAULT_GOAL
    floor = decision_makers_only and goals.seniority_floor_applies(goal_key)

    # Route through backend if in backend mode and no local LLM key
    from ..config import has_local_llm_key, is_backend_mode
    if is_backend_mode() and not has_local_llm_key():
        result = await _generate_via_backend(
            target_description, company_context, focus_query, user_profile,
            goal=goal_key,
        )
        result.processing_time = time.time() - start_time
        apply_seniority_policy(result, decision_makers_only=floor)
        # Enrich with LinkedIn codes
        await _enrich_result(result)
        return result

    # Build prompt
    user_name = ""
    user_title = ""
    user_company = ""
    user_expertise = ""

    if user_profile:
        user_name = user_profile.get("name", "")
        user_title = user_profile.get("title", "")
        user_company = user_profile.get("company", "")
        user_expertise = user_profile.get(
            "core", user_profile.get("headline", ""),
        )

    company_context_section = ""
    if company_context:
        company_context_section = (
            f"\n## COMPANY CONTEXT\n{company_context[:3000]}\n"
        )

    focus_section = ""
    if focus_query:
        focus_section = f"\n## FOCUS\n{focus_query}\n"

    prompt = icp_prompt_for(goal=goal_key).format(
        target_description=target_description,
        user_name=user_name or "Unknown",
        user_title=user_title or "Founder",
        user_company=user_company or "Unknown",
        user_expertise=user_expertise or "Not specified",
        company_context_section=company_context_section,
        focus_section=focus_section,
        kb_evidence_section=(
            build_kb_evidence_section(kb_evidence, target_description=target_description)
            if goals.uses_sales_kb(goal_key) else ""
        ),
    )

    client = LLMClient()
    raw = await client.generate(
        prompt, system=ICP_V2_SYSTEM, temperature=0.2, max_tokens=8192,
    )

    result = await _parse_icp_response(raw, target_description, goal=goal_key)
    result.processing_time = time.time() - start_time

    # Filter below-threshold ICPs
    if confidence_threshold > 0:
        result.icps = [
            icp for icp in result.icps
            if icp.overall_confidence >= confidence_threshold
        ]

    # Ensure at least 1 ICP survives filtering
    if not result.icps:
        logger.warning("All ICPs below confidence threshold, keeping best one")
        all_icps = (await _parse_icp_response(raw, target_description, goal=goal_key)).icps
        if all_icps:
            best = max(all_icps, key=lambda x: x.overall_confidence)
            result.icps = [best]

    apply_seniority_policy(result, decision_makers_only=floor)

    # Enrich with LinkedIn codes
    await _enrich_result(result)

    return result


async def _generate_via_backend(
    target_description: str,
    company_context: str,
    focus_query: str,
    user_profile: dict[str, Any] | None,
    *,
    goal: str = "sell",
) -> IcpResult:
    """Generate ICP via the backend proxy.

    The backend returns v2 format (icps[] with pain_points, fears, barriers).
    Falls back to legacy conversion if the response has segments[] instead.
    """
    from ..linkedin import get_linkedin_client

    client = get_linkedin_client()
    try:
        icp_data = await client.generate_icp(
            target_description, user_profile or {}, goal=goal,
        )
    finally:
        await client.close()

    # Backend returns v2 format (icps[]) — parse directly. The api cleans a
    # job-search answer itself (#1338), but the client must not depend on
    # the backend's version, so the same pass runs here.
    if "icps" in icp_data:
        return _dict_to_icp_result(icp_data, target_description, goal=goal)

    # Fallback: legacy format (segments[]) — convert
    return _convert_legacy_icp(icp_data, target_description)


# ──────────────────────────────────────────────
# LinkedIn enrichment (resolves names → codes)
# ──────────────────────────────────────────────

async def _enrich_result(result: IcpResult) -> None:
    """Enrich all ICPs in a result with LinkedIn parameter codes.

    Resolves human-readable values (e.g., "Financial Services") to LinkedIn's
    internal codes via the Unipile search/parameters API. This enables structured
    search instead of keyword-only search.

    Operates in-place on the IcpResult.
    """
    if not result.icps:
        return

    from ..db.queries import get_setting
    from ..linkedin import get_linkedin_client
    from .linkedin_enricher import enrich_icp_linkedin_params

    account_id = await run_db(get_setting, "unipile_account_id")
    if not account_id:
        logger.info("No LinkedIn account — skipping enrichment")
        return

    try:
        client = get_linkedin_client()
    except Exception as e:
        logger.warning(f"Cannot create LinkedIn client for enrichment: {e}")
        return

    # Build the get_params_fn closure based on client type
    # Route through premium search account if available
    from ..linkedin.backend_client import BackendClient
    from ..services.search_account_resolver import get_cached_search_account

    premium_acct = await run_db(get_cached_search_account)
    _search_override = premium_acct if premium_acct and premium_acct != account_id else None
    if _search_override:
        logger.info("ICP enrichment routed through premium account %s", _search_override[:8])

    if isinstance(client, BackendClient):
        async def get_params_fn(type: str, keywords: str) -> list[dict[str, str]]:
            return await client.get_search_params(
                type=type, keywords=keywords, search_account_id=_search_override,
            )
    else:
        # Direct Unipile mode: use premium account for enrichment if available
        enrich_acct = _search_override or account_id
        async def get_params_fn(type: str, keywords: str) -> list[dict[str, str]]:
            return await client.get_search_params(
                account_id=enrich_acct, type=type, keywords=keywords,
            )

    enriched_count = 0
    try:
        for icp in result.icps:
            try:
                icp.linkedin_enriched_params = await enrich_icp_linkedin_params(
                    icp, get_params_fn,
                )
                enriched_count += 1
            except Exception as e:
                logger.warning(f"Enrichment failed for ICP '{icp.name}': {e}")

        if enriched_count:
            logger.info(
                f"Enriched LinkedIn params for {enriched_count}/{len(result.icps)} ICPs"
            )
    finally:
        await client.close()


# ──────────────────────────────────────────────
# Response parsing
# ──────────────────────────────────────────────

async def _parse_icp_response(
    raw: str, target_description: str, *, goal: str = "sell",
) -> IcpResult:
    """Parse LLM JSON response into IcpResult.

    Uses a 4-strategy repair pipeline (ported from original AI in Charge):
    1. Raw parse (strip markdown fences)
    2. Regex cleanup (trailing commas, stray chars, extract JSON object)
    3. LLM repair (send broken JSON + schema to LLM)
    4. Fallback skeleton
    """
    # Strategy 1: strip markdown fences and try raw parse
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(
            line for line in lines if not line.strip().startswith("```")
        )

    try:
        data = json.loads(cleaned)
        return _dict_to_icp_result(data, target_description, goal=goal)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Strategy 2: regex cleanup — extract JSON object, fix common issues
    try:
        repaired = _repair_json(cleaned)
        data = json.loads(repaired)
        logger.info("ICP JSON recovered via regex repair")
        return _dict_to_icp_result(data, target_description, goal=goal)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Strategy 3: LLM repair — send broken JSON + schema to LLM for fixing
    try:
        repaired_data = await _llm_repair_json(cleaned, target_description)
        if repaired_data:
            logger.info("ICP JSON recovered via LLM repair")
            return _dict_to_icp_result(repaired_data, target_description, goal=goal)
    except Exception as e:
        logger.warning("LLM JSON repair failed: %s", e)

    # Strategy 4: fallback skeleton
    logger.warning("Failed to parse ICP V2 JSON after all repair strategies, building fallback")
    return _build_fallback(target_description)


def _repair_json(raw: str) -> str:
    """Attempt to repair malformed JSON from LLM output.

    Handles:
    - Stray text before/after the JSON object
    - Trailing commas before } or ]
    - Single quotes instead of double quotes
    - Unquoted keys
    """
    # Extract the outermost JSON object
    match = re.search(r"\{", raw)
    if not match:
        raise ValueError("No JSON object found")

    # Find matching closing brace
    depth = 0
    start = match.start()
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                candidate = raw[start:i + 1]
                break
    else:
        candidate = raw[start:]

    # Remove trailing commas before } or ]
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)

    # Remove JavaScript-style comments
    candidate = re.sub(r"//[^\n]*", "", candidate)

    return candidate


async def _llm_repair_json(broken_json: str, target_description: str) -> dict[str, Any] | None:
    """Strategy 3: Send broken JSON to LLM for repair.

    Ported from original AI in Charge graph.py _llm_repair_json.
    Sends the broken JSON plus the expected schema to the LLM,
    asking it to return only fixed JSON.
    """
    repair_prompt = f"""The following JSON is malformed or does not match the required schema. Fix it and return ONLY the valid JSON, nothing else.

Common issues to fix:
- Stray characters before property names
- Missing commas
- Trailing commas before closing brackets
- Truncated content
- Invalid escape sequences
- Fields that violate schema types or required properties

EXPECTED SCHEMA:
{{
    "summary": "string",
    "campaign_name": "string",
    "relevance_hook": "string",
    "icps": [
        {{
            "name": "string",
            "description": "string",
            "pain_points": ["string"],
            "fears": ["string"],
            "barriers": ["string"],
            "industries": {{"include": ["string"], "exclude": ["string"]}},
            "job_titles": {{"include": ["string"], "exclude": ["string"]}},
            "locations": {{"include": ["string"], "exclude": ["string"]}},
            "departments": {{"include": ["string"], "exclude": ["string"]}} or null,
            "company_headcount": {{"min": number, "max": number}} or null,
            "company_types": ["string"] or null,
            "company_locations": {{"include": ["string"], "exclude": ["string"]}} or null,
            "tenure": {{"min": number, "max": number}} or null,
            "seniority": {{"include": ["string"], "exclude": ["string"]}} or null,
            "keywords": ["string"] or null,
            "spotlight_filters": {{"changed_jobs": bool, "posted_recently": bool}} or null,
            "boolean_keywords": "string" or "",
            "annual_revenue": {{"min": number, "max": number}} or null,
            "company_headcount_growth": "string" or "",
            "overall_confidence": number (0-1),
            "data_completeness": number (0-1),
            "evidence_strength": number (0-1)
        }}
    ]
}}

BROKEN JSON:
{broken_json[:6000]}

Return ONLY the fixed JSON. Do not include any explanations, just the JSON itself."""

    try:
        client = LLMClient()
        raw = await client.generate(repair_prompt, temperature=0.0, max_tokens=8192)

        # Clean and parse the repaired JSON
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            cleaned = "\n".join(
                line for line in lines if not line.strip().startswith("```")
            )

        data = json.loads(cleaned)
        if "icps" in data and isinstance(data["icps"], list):
            return data
        return None

    except Exception as e:
        logger.debug("LLM JSON repair inner error: %s", e)
        return None


def _dict_to_icp_result(
    data: dict[str, Any], target_description: str, *, goal: str = "sell",
) -> IcpResult:
    """Convert a parsed JSON dict into an IcpResult.

    For a job search the sender's own role (`target_role`, asked for by
    goals.JOB_SEARCH_ROLE_RULE) is moved to every persona's exclude list first,
    whatever the model or the backend put in include (#1338). The dataclass
    reads only the keys it knows, so target_role itself is dropped here.
    """
    from .. import goals

    target_role = data.get("target_role")
    if goal == goals.JOB_SEARCH and isinstance(target_role, str) and target_role.strip():
        goals.exclude_target_role(data, target_role)
    icps = []
    for icp_data in data.get("icps", []):
        icp = SingleIcp(
            name=icp_data.get("name", ""),
            description=icp_data.get("description", ""),
            pain_points=icp_data.get("pain_points", []),
            fears=icp_data.get("fears", []),
            barriers=icp_data.get("barriers", []),
            industries=_to_search_param(icp_data.get("industries")),
            job_titles=_to_search_param(icp_data.get("job_titles")),
            locations=_to_search_param(icp_data.get("locations")),
            departments=_to_search_param(icp_data.get("departments")),
            company_headcount=HeadcountParam(
                min=_safe_int(icp_data.get("company_headcount", {}).get("min")),
                max=_safe_int(icp_data.get("company_headcount", {}).get("max")),
            ) if isinstance(icp_data.get("company_headcount"), dict) else HeadcountParam(),
            company_types=icp_data.get("company_types", []),
            company_locations=_to_search_param(icp_data.get("company_locations")),
            tenure=TenureParam(
                min=_safe_int(icp_data.get("tenure", {}).get("min")),
                max=_safe_int(icp_data.get("tenure", {}).get("max")),
            ) if isinstance(icp_data.get("tenure"), dict) else TenureParam(),
            seniority=_to_search_param(icp_data.get("seniority")),
            keywords=icp_data.get("keywords", []),
            spotlight_filters=icp_data.get("spotlight_filters"),
            boolean_keywords=icp_data.get("boolean_keywords", "") or "",
            annual_revenue=icp_data.get("annual_revenue"),
            company_headcount_growth=icp_data.get("company_headcount_growth", "") or "",
            overall_confidence=_safe_float(icp_data.get("overall_confidence"), 0.5),
            data_completeness=_safe_float(icp_data.get("data_completeness"), 0.5),
            evidence_strength=_safe_float(icp_data.get("evidence_strength"), 0.5),
        )
        icps.append(icp)

    return IcpResult(
        icps=icps,
        summary=data.get("summary", f"Target: {target_description}"),
        campaign_name=data.get("campaign_name", target_description[:30]),
        relevance_hook=data.get("relevance_hook", ""),
    )


def _convert_legacy_icp(
    old_icp: dict[str, Any], target_description: str,
) -> IcpResult:
    """Convert the old-format ICP (from generate_icp v1) to IcpResult.

    Old format has: summary, campaign_name, relevance_hook, segments[]
    where each segment has: name, keywords, titles[], seniority[],
    industries[], company_size[], geography[], estimated_results
    """
    icps = []
    for seg in old_icp.get("segments", []):
        icp = SingleIcp(
            name=seg.get("name", "Primary"),
            description=f"Target: {seg.get('keywords', '')}",
            pain_points=[],
            fears=[],
            barriers=[],
            industries=LinkedinSearchParam(
                include=seg.get("industries", []),
            ),
            job_titles=LinkedinSearchParam(
                include=seg.get("titles", []),
            ),
            locations=LinkedinSearchParam(
                include=seg.get("geography", []),
            ),
            seniority=LinkedinSearchParam(
                include=seg.get("seniority", []),
            ),
            keywords=seg.get("keywords", "").split(",") if isinstance(seg.get("keywords"), str) else [],
            overall_confidence=0.4,
            data_completeness=0.4,
            evidence_strength=0.2,
        )
        icps.append(icp)

    return IcpResult(
        icps=icps,
        summary=old_icp.get("summary", f"Target: {target_description}"),
        campaign_name=old_icp.get("campaign_name", target_description[:30]),
        relevance_hook=old_icp.get("relevance_hook", ""),
    )


def _build_fallback(target_description: str) -> IcpResult:
    """Build a minimal fallback IcpResult when parsing fails."""
    return IcpResult(
        icps=[
            SingleIcp(
                name="Primary",
                description=f"Target: {target_description}",
                keywords=target_description.split()[:5],
                overall_confidence=0.2,
                data_completeness=0.2,
                evidence_strength=0.1,
            ),
        ],
        summary=f"Target: {target_description}",
        campaign_name=target_description[:30],
    )


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _to_search_param(data: Any) -> LinkedinSearchParam:
    """Convert a dict or None to LinkedinSearchParam."""
    if data is None:
        return LinkedinSearchParam()
    if isinstance(data, dict):
        return LinkedinSearchParam(
            include=data.get("include", []) or [],
            exclude=data.get("exclude", []) or [],
        )
    if isinstance(data, list):
        # Legacy format: bare list → include only
        return LinkedinSearchParam(include=data)
    return LinkedinSearchParam()


def _safe_int(value: Any) -> int | None:
    """Safely convert to int or None."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _safe_float(value: Any, default: float = 0.5) -> float:
    """Coerce null / non-numeric confidence fields to a default."""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default
