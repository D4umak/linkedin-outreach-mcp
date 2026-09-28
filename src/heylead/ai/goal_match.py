"""Campaign goal ↔ ICP match: does this audience contain buyers for this goal?

9 Sep 2026. The "AI projects — UA diaspora" campaign (`be5f78ff…`)
targeted an audience defined by nationality, not by who decides. Nothing in
the product ever asked whether the ICP could buy what the campaign sold: the
only fit rubric — `ai/targeting_recheck.py` / backend `recheck_campaign_fit` —
runs one contact at a time, at reply time, long after the audience is set.

This judge runs at ICP time, on the whole ICP, grounded in the shipped
sales-methodology corpus (`ai/kb_retrieval.py`). Hosted accounts call the
backend route; a self-hosted account with its own LLM key runs the identical
prompt locally.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

VERDICTS = ("match", "partial", "mismatch")
PURCHASE_ROLES = ("economic_buyer", "champion", "influencer", "user", "none")

GOAL_MATCH_SYSTEM_TEMPLATE = """You audit whether an outbound campaign's ICP can \
actually deliver the campaign's goal.

You are given the campaign goal, {offer_noun}, the generated ICP \
personas, and {evidence_noun}.

Output ONLY a JSON object with:
- "verdict": "match" | "partial" | "mismatch"
- "decision_maker_coverage": float 0.0-1.0 — {coverage_definition}
- "persona_alignment": array of {{"persona": str, "role_in_purchase": \
{roles}, "evidence": one short sentence}}
- "warnings": array of short strings — concrete reasons this ICP will \
underdeliver the goal
- "suggestions": array of short strings — concrete title/seniority/segment \
changes that would fix it
- "reason": one short sentence summarising the verdict

Rules:
{rules}
- Nationality, diaspora, language or community membership is NOT a \
{role_noun}. An ICP defined by who people are rather than what they decide is at \
best "partial".
- {kb_rule}
- Never invent company names, customer names or numbers.
No markdown. No code fences."""

_SELL_GOAL_MATCH = dict(
    offer_noun="what the sender offers",
    evidence_noun=(
        "excerpts from a sales-methodology knowledge base distilled from "
        "B2B sales and marketing books"
    ),
    coverage_definition=(
        "the share of the ICP's job titles that hold budget authority for this "
        "purchase (owner / C-level / VP / director of the function that owns the problem)"
    ),
    roles='"economic_buyer" | "champion" | "influencer" | "user" | "none"',
    rules=(
        '- "mismatch" when the ICP\'s buyers cannot authorise or champion what the goal '
        "asks for, or when the ICP's industry/segment is unrelated to the goal.\n"
        '- "partial" when the personas are adjacent but the economic buyer is missing, '
        "or when fewer than half the titles are decision makers.\n"
        '- "match" only when at least one persona is the economic buyer or a champion '
        "with direct access to one."
    ),
    role_noun="buying role",
    kb_rule=(
        'Cite the knowledge base by persona/stage/source in "evidence" where it '
        "supports you. If the knowledge base says nothing relevant, say nothing — do "
        "not invent a citation."
    ),
)


def goal_match_system_for(goal: str) -> str:
    """The judge's system prompt for a goal; twin of the api's (#1153).

    `sell` is byte-identical to the prompt before #1153
    (tests/fixtures/goal_match_system_sell.txt).
    """
    from .. import goals

    g = goals.GOALS[goals.normalize_goal(goal) or goals.DEFAULT_GOAL]
    if g.key == goals.SELL:
        return GOAL_MATCH_SYSTEM_TEMPLATE.format(**_SELL_GOAL_MATCH)
    deciding = g.roles[0].replace("_", " ")
    return GOAL_MATCH_SYSTEM_TEMPLATE.format(
        offer_noun="what the sender brings",
        evidence_noun="nothing else: judge from the titles and the goal",
        coverage_definition=(
            f"the share of the ICP's job titles held by someone who {g.persona}. "
            f"The question is: {g.fit_question}"
        ),
        roles=" | ".join(f'"{r}"' for r in g.roles),
        rules=(
            f'- "mismatch" when no persona {g.persona}, or when the ICP\'s '
            "industry/segment is unrelated to the goal.\n"
            f'- "partial" when the personas are adjacent but the {deciding} is missing, '
            "or when fewer than half the titles qualify.\n"
            f'- "match" only when at least one persona is a {deciding}.'
            + (
                '\n- "peer" for a persona whose titles are the role the sender wants; '
                'an ICP whose personas are all "peer" is "mismatch": they hold the role, '
                "they do not hire for it."
                if g.key == goals.JOB_SEARCH else ""
            )
        ),
        role_noun="qualifying role",
        kb_rule="Do not cite a sales methodology; it does not apply to this goal.",
    )


# The sell prompt under its old name, for callers and tests that read it.
GOAL_MATCH_SYSTEM = goal_match_system_for(goal="sell")


@dataclass
class GoalMatchVerdict:
    """The judge's answer, already validated."""

    verdict: str = "partial"
    decision_maker_coverage: float = 0.0
    persona_alignment: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    reason: str = ""
    kb_cards_used: int = 0
    source: str = "backend"
    goal: str = "sell"
    # Sell only (heylead-api #1636): the titles the share counted.
    decision_maker_titles: list[dict[str, Any]] = field(default_factory=list)
    decision_maker_count: int = 0
    title_count: int = 0

    @property
    def blocks_campaign(self) -> bool:
        return self.verdict == "mismatch"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "decision_maker_coverage": self.decision_maker_coverage,
            "persona_alignment": self.persona_alignment,
            "warnings": self.warnings,
            "suggestions": self.suggestions,
            "reason": self.reason,
            "kb_cards_used": self.kb_cards_used,
            "source": self.source,
            "goal": self.goal,
            "decision_maker_titles": self.decision_maker_titles,
            "decision_maker_count": self.decision_maker_count,
            "title_count": self.title_count,
        }


def _judged_coverage(data: dict[str, Any]) -> float:
    """The share the payload reports: the backend's, which counts it for sell,
    or the local model's, which is overridden for sell (heylead-api #1605)."""
    try:
        coverage = float(data.get("decision_maker_coverage", 0.0))
    except (TypeError, ValueError):
        coverage = 0.0
    return max(0.0, min(1.0, coverage))


def counted_coverage(goal_key: str, titles: list[str]) -> tuple[int, int] | None:
    """For sell, the titles that decide, counted: (deciders, titles).

    The judge's own estimate of this share moved between 33% and 27% for the
    same 15 titles (heylead-api #1605). The other goals ask who hires, who
    would take the role, who sells it: not a seniority question, so None.
    """
    from .. import goals
    from ..services import seniority

    if (goals.normalize_goal(goal_key) or goals.DEFAULT_GOAL) != goals.SELL:
        return None
    counted = seniority.decision_maker_share(titles)
    return counted if counted[1] else None


def goal_match_personas(icp_json: dict[str, Any] | None) -> list[tuple[str, list[str]]]:
    """Each persona the judge sees, as (name, titles), read the way
    summarize_icp reads them (twin of heylead-api llm._goal_match_personas)."""
    icp = icp_json or {}
    blocks = icp.get("icps") or icp.get("segments") or ([icp] if icp else [])
    if not isinstance(blocks, list):
        return []
    personas: list[tuple[str, list[str]]] = []
    for block in blocks[:4]:
        if not isinstance(block, dict):
            continue
        job = block.get("job_titles") or {}
        titles = list(job.get("include") or []) if isinstance(job, dict) else []
        titles += list(block.get("titles") or [])
        titles = [t for t in (str(x).strip() for x in titles if x is not None) if t][:10]
        personas.append((str(block.get("name") or "unnamed")[:120], titles))
    return personas


_ROLE_REASONS = {
    "economic_buyer": "{titles} {verb} director level or above: budget authority for this purchase.",
    "champion": "{titles} {verb} manager level: {pronoun} can champion it to the budget holder, not sign for it.",
    "influencer": "{titles} {verb} senior level: {pronoun} shape the choice, not sign for it.",
    "user": "{titles} {verb} entry level: {pronoun} would use it, not buy it.",
}


def _counted_reason(role: str, titles: list[str]) -> str:
    """Why a persona has its role, naming only titles it searches
    (heylead-api #1636)."""
    named = titles[:4]
    joined = named[0] if len(named) == 1 else ", ".join(named[:-1]) + " and " + named[-1]
    one = len(named) == 1
    return _ROLE_REASONS[role].format(titles=joined, verb="is" if one else "are", pronoun="they")


def _judged_alignment(data: dict[str, Any], roles: tuple[str, ...]) -> list[dict[str, Any]]:
    """The payload's own Who buys: the backend's (which follows the titles
    for sell), or the local judge's, which counted_alignment overrides for
    sell (heylead-api #1636)."""
    alignment: list[dict[str, Any]] = []
    for entry in (data.get("persona_alignment") or [])[:6]:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role_in_purchase") or "none").strip().lower()
        if role not in roles:
            role = "none"
        alignment.append({
            "persona": str(entry.get("persona") or "")[:120],
            "role_in_purchase": role,
            "evidence": str(entry.get("evidence") or "")[:300],
        })
    return alignment


def counted_alignment(
    personas: list[tuple[str, list[str]]], judged: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Sell's Who buys: one entry per persona, its role and reason from its
    titles; a persona whose titles state no level keeps the judge's entry,
    matched by name (twin of heylead-api llm._counted_alignment)."""
    from ..services import seniority

    by_name = {entry["persona"].strip().lower(): entry for entry in judged}
    alignment: list[dict[str, Any]] = []
    for i, (name, titles) in enumerate(personas):
        derived = seniority.buying_role(titles)
        if derived:
            role, giving = derived
            alignment.append({"persona": name, "role_in_purchase": role, "evidence": _counted_reason(role, giving)})
            continue
        fallback = by_name.get(name.strip().lower()) or (judged[i] if i < len(judged) else None)
        alignment.append(dict(fallback, persona=name) if fallback else
                         {"persona": name, "role_in_purchase": "none", "evidence": ""})
    return alignment


def _counted_titles(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The backend's list of the titles its share counted, validated."""
    counted = []
    for entry in (data.get("decision_maker_titles") or [])[:20]:
        if isinstance(entry, dict) and str(entry.get("title") or "").strip():
            try:
                counted.append({"title": str(entry["title"])[:120], "count": max(1, int(entry.get("count") or 1))})
            except (TypeError, ValueError):
                continue
    return counted


def coerce_verdict(
    data: dict[str, Any], source: str = "backend", goal: str = "sell",
) -> GoalMatchVerdict:
    """Validate a judge payload. An unreadable verdict is never a `match`.

    Roles are checked against the goal's vocabulary (#1153). A payload that
    names its own `goal_key` (the backend knows what it judged) or `goal`
    (a round-tripped `to_dict()`) wins over the argument.
    """
    from .. import goals

    goal = (
        goals.normalize_goal(data.get("goal_key") or data.get("goal") or goal)
        or goals.DEFAULT_GOAL
    )
    roles = goals.GOALS[goal].roles
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        verdict = "partial"
    coverage = _judged_coverage(data)
    alignment = _judged_alignment(data, roles)

    def _count(key: str) -> int:
        try:
            return max(0, int(data.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    def _strings(key: str) -> list[str]:
        return [str(x)[:240] for x in (data.get(key) or [])[:6] if str(x).strip()]

    try:
        used = int(data.get("kb_cards_used") or 0)
    except (TypeError, ValueError):
        used = 0

    return GoalMatchVerdict(
        verdict=verdict,
        decision_maker_coverage=coverage,
        persona_alignment=alignment,
        warnings=_strings("warnings"),
        suggestions=_strings("suggestions"),
        reason=str(data.get("reason") or "")[:300],
        kb_cards_used=used,
        source=source,
        goal=goal,
        decision_maker_titles=_counted_titles(data),
        decision_maker_count=_count("decision_maker_count"),
        title_count=_count("title_count"),
    )


def summarize_icp(icp_json: dict[str, Any] | None) -> tuple[str, list[str]]:
    """Flatten an ICP payload to a prompt block plus the titles it names."""
    icp = icp_json or {}
    blocks = icp.get("icps") or icp.get("segments") or ([icp] if icp else [])
    if not isinstance(blocks, list):
        blocks = []
    lines: list[str] = []
    titles_all: list[str] = []
    for i, block in enumerate(blocks[:4], 1):
        if not isinstance(block, dict):
            continue
        job = block.get("job_titles") or {}
        titles = list(job.get("include") or []) if isinstance(job, dict) else []
        titles += list(block.get("titles") or [])
        titles = [str(t) for t in titles][:10]
        titles_all.extend(titles)
        sen = block.get("seniority") or {}
        seniority = list(sen.get("include") or []) if isinstance(sen, dict) else []
        inds = block.get("industries") or {}
        industries = list(inds.get("include") or []) if isinstance(inds, dict) else []
        locs = block.get("locations") or {}
        locations = list(locs.get("include") or []) if isinstance(locs, dict) else []
        pains = [str(x) for x in (block.get("pain_points") or [])[:4]]
        lines.append(
            f"Persona {i}: {block.get('name') or 'unnamed'}\n"
            f"  description: {str(block.get('description') or '')[:300]}\n"
            f"  titles: {', '.join(titles) or 'not specified'}\n"
            f"  seniority: {', '.join(str(x) for x in seniority) or 'not specified'}\n"
            f"  industries: {', '.join(str(x) for x in industries) or 'not specified'}\n"
            f"  locations: {', '.join(str(x) for x in locations) or 'not specified'}\n"
            f"  pain points: {', '.join(pains) or 'not specified'}"
        )
    return ("\n".join(lines) or "no personas provided"), titles_all


def build_goal_match_prompt(
    goal: str, offer: str, icp_json: dict[str, Any] | None,
    *, goal_key: str = "sell",
) -> tuple[str, int]:
    """The judge prompt and how many KB cards it cites.

    `goal` is the free-text goal; `goal_key` is one of goals.VALID_GOALS and
    picks the question. The sales KB grounds only the selling-shaped goals
    (#1153); for sell every heading stays exactly as before.
    """
    from .. import goals

    key = goals.normalize_goal(goal_key) or goals.DEFAULT_GOAL
    icp_block, titles = summarize_icp(icp_json)
    cards: list[Any] = []
    if goals.uses_sales_kb(key):
        from .kb_retrieval import personas_for_titles, retrieve_kb

        try:
            personas = personas_for_titles(titles)
            cards = retrieve_kb(
                f"{goal} {offer}".strip() or "outbound campaign targeting",
                personas=personas or None,
                top_k=6,
            )
        except Exception:
            logger.warning("KB retrieval failed for goal match", exc_info=True)
            cards = []
    kb_block = "\n".join(
        f"{i}. {c.as_evidence_line()}" for i, c in enumerate(cards, 1)
    )
    label_suffix = "" if key == goals.SELL else f" ({goals.GOALS[key].label})"
    closing = "Can this ICP deliver this goal?" if key == goals.SELL else goals.GOALS[key].fit_question
    evidence_heading = (
        "## SALES METHODOLOGY EVIDENCE (cite persona/stage/source; abstain if irrelevant)"
        if goals.uses_sales_kb(key) else "## EVIDENCE"
    )
    counted = counted_coverage(key, titles)
    counted_block = ""
    if counted:
        from ..services import seniority

        counted_block = (
            "## DECISION-MAKER COVERAGE (counted from the titles; use it, do not estimate it)\n"
            f"{counted[0]} of {counted[1]} titles state owner, C-level, VP or director: "
            f"{round(100 * counted[0] / counted[1])}%\n\n"
        )
        # Who buys comes from the titles too (heylead-api #1636).
        role_lines = [
            f"Persona {i}: {name}: {derived[0]} ({', '.join(derived[1][:4])})"
            for i, (name, person_titles) in enumerate(goal_match_personas(icp_json), 1)
            if (derived := seniority.buying_role(person_titles))
        ]
        if role_lines:
            counted_block += (
                "## WHO BUYS (from the titles; use these roles)\n" + "\n".join(role_lines) + "\n\n"
            )
    prompt = (
        f"## CAMPAIGN GOAL{label_suffix}\n{(goal or 'not specified')[:1200]}\n\n"
        f"## WHAT THE SENDER {'OFFERS' if key == goals.SELL else 'BRINGS'}\n"
        f"{(offer or 'not specified')[:1500]}\n\n"
        f"## GENERATED ICP\n{icp_block[:4000]}\n\n"
        f"{counted_block}"
        f"{evidence_heading}\n"
        f"{kb_block or 'none retrieved'}\n\n"
        f"{closing}"
    )
    return prompt, len(cards)


async def judge_goal_match(
    goal: str,
    offer: str = "",
    icp_json: dict[str, Any] | None = None,
    *,
    goal_key: str = "sell",
) -> GoalMatchVerdict:
    """Run the judge — backend route when hosted, same prompt locally otherwise.

    Never raises: an audit that cannot run must not stop ICP generation. A
    failure returns `partial` with the reason, which warns but does not block.
    """
    from .. import goals
    from ..config import has_local_llm_key, is_backend_mode

    goal_key = goals.normalize_goal(goal_key) or goals.DEFAULT_GOAL
    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client

        client = get_linkedin_client()
        try:
            data = await client.icp_goal_match(goal, offer, icp_json or {}, goal_key=goal_key)
            return coerce_verdict(data, source="backend", goal=goal_key)
        except Exception as e:
            logger.warning("Backend goal match failed: %s", e)
            return GoalMatchVerdict(
                verdict="partial",
                reason=f"goal/ICP audit unavailable: {e}",
                warnings=["The goal/ICP audit did not run."],
                source="unavailable",
                goal=goal_key,
            )
        finally:
            await client.close()

    prompt, used = build_goal_match_prompt(goal, offer, icp_json, goal_key=goal_key)
    try:
        from .llm import LLMClient

        raw = await LLMClient().generate(
            prompt, system=goal_match_system_for(goal=goal_key), temperature=0.1, max_tokens=1200,
        )
        data = _parse_json(raw)
    except Exception as e:
        logger.warning("Local goal match failed: %s", e)
        return GoalMatchVerdict(
            verdict="partial",
            reason=f"goal/ICP audit unavailable: {e}",
            warnings=["The goal/ICP audit did not run."],
            kb_cards_used=used,
            source="unavailable",
            goal=goal_key,
        )
    verdict = coerce_verdict(data, source="local", goal=goal_key)
    verdict.kb_cards_used = used
    titles = summarize_icp(icp_json)[1]
    counted = counted_coverage(goal_key, titles)
    if counted:
        from ..services import seniority

        verdict.decision_maker_coverage = counted[0] / counted[1]
        verdict.decision_maker_count, verdict.title_count = counted
        verdict.decision_maker_titles = [
            {"title": t, "count": n} for t, n in seniority.decision_maker_titles(titles)
        ]
        verdict.persona_alignment = counted_alignment(goal_match_personas(icp_json), verdict.persona_alignment)
        # "match" over titles none of which decide contradicts the count.
        if verdict.verdict == "match" and counted[0] == 0:
            verdict.verdict = "partial"
    return verdict


def _parse_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in response")
    return json.loads(text[start : end + 1])


def format_verdict(v: GoalMatchVerdict) -> str:
    """Human-readable block for the icp() tool and create_campaign warnings."""
    from .. import goals

    icon = {"match": "✓", "partial": "!", "mismatch": "✗"}.get(v.verdict, "?")
    pct = round(v.decision_maker_coverage * 100)
    goal = goals.normalize_goal(v.goal) or goals.DEFAULT_GOAL
    if goal == goals.SELL:
        # Sell keeps the words it had before #1153.
        lines = [
            f"{icon} Goal ↔ ICP: **{v.verdict}** "
            f"({pct}% decision-maker coverage, {v.kb_cards_used} KB cards cited)",
        ]
    else:
        copy = goals.fit_copy(goal)
        headline = copy.get(v.verdict, copy["unknown"])
        lines = [
            f"{icon} Goal ↔ ICP ({goals.GOALS[goal].label}): **{v.verdict}**: {headline} "
            f"({copy['coverage'].format(pct=pct)})",
        ]
    if goal == goals.SELL and v.title_count:
        listed = ", ".join(
            f"{e['title']} ×{e['count']}" if e["count"] > 1 else e["title"] for e in v.decision_maker_titles
        )
        lines.append(
            f"  {v.decision_maker_count} of {v.title_count} titles decide" + (f": {listed}" if listed else "")
        )
    if v.reason:
        lines.append(f"  {v.reason}")
    if v.persona_alignment and goal != goals.SELL:
        lines.append(f"  {goals.fit_copy(goal)['who']}:")
    for entry in v.persona_alignment:
        lines.append(
            f"  • {entry['persona'] or 'persona'} — "
            f"{entry['role_in_purchase'].replace('_', ' ')}"
            + (f": {entry['evidence']}" if entry["evidence"] else "")
        )
    if v.warnings:
        lines.append("  Warnings:")
        lines.extend(f"    - {w}" for w in v.warnings)
    if v.suggestions:
        lines.append("  Suggestions:")
        lines.extend(f"    - {s}" for s in v.suggestions)
    if v.source == "unavailable":
        lines.append("  (the audit did not run — treat this as unknown, not as a pass)")
    return "\n".join(lines)
