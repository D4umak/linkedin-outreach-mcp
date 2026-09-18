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

GOAL_MATCH_SYSTEM = """You audit whether an outbound campaign's ICP can \
actually deliver the campaign's goal.

You are given the campaign goal, what the sender offers, the generated ICP \
personas, and excerpts from a sales-methodology knowledge base distilled from \
B2B sales and marketing books.

Output ONLY a JSON object with:
- "verdict": "match" | "partial" | "mismatch"
- "decision_maker_coverage": float 0.0-1.0 — the share of the ICP's job \
titles that hold budget authority for this purchase (owner / C-level / VP / \
director of the function that owns the problem)
- "persona_alignment": array of {"persona": str, "role_in_purchase": \
"economic_buyer" | "champion" | "influencer" | "user" | "none", "evidence": \
one short sentence}
- "warnings": array of short strings — concrete reasons this ICP will \
underdeliver the goal
- "suggestions": array of short strings — concrete title/seniority/segment \
changes that would fix it
- "reason": one short sentence summarising the verdict

Rules:
- "mismatch" when the ICP's buyers cannot authorise or champion what the goal \
asks for, or when the ICP's industry/segment is unrelated to the goal.
- "partial" when the personas are adjacent but the economic buyer is missing, \
or when fewer than half the titles are decision makers.
- "match" only when at least one persona is the economic buyer or a champion \
with direct access to one.
- Nationality, diaspora, language or community membership is NOT a buying \
role. An ICP defined by who people are rather than what they decide is at \
best "partial".
- Cite the knowledge base by persona/stage/source in "evidence" where it \
supports you. If the knowledge base says nothing relevant, say nothing — do \
not invent a citation.
- Never invent company names, customer names or numbers.
No markdown. No code fences."""


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
        }


def coerce_verdict(data: dict[str, Any], source: str = "backend") -> GoalMatchVerdict:
    """Validate a judge payload. An unreadable verdict is never a `match`."""
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        verdict = "partial"
    try:
        coverage = float(data.get("decision_maker_coverage", 0.0))
    except (TypeError, ValueError):
        coverage = 0.0
    coverage = max(0.0, min(1.0, coverage))

    alignment: list[dict[str, Any]] = []
    for entry in (data.get("persona_alignment") or [])[:6]:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role_in_purchase") or "none").strip().lower()
        if role not in PURCHASE_ROLES:
            role = "none"
        alignment.append({
            "persona": str(entry.get("persona") or "")[:120],
            "role_in_purchase": role,
            "evidence": str(entry.get("evidence") or "")[:300],
        })

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
) -> tuple[str, int]:
    """The judge prompt and how many KB cards it cites."""
    from .kb_retrieval import personas_for_titles, retrieve_kb

    icp_block, titles = summarize_icp(icp_json)
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
    prompt = (
        f"## CAMPAIGN GOAL\n{(goal or 'not specified')[:1200]}\n\n"
        f"## WHAT THE SENDER OFFERS\n{(offer or 'not specified')[:1500]}\n\n"
        f"## GENERATED ICP\n{icp_block[:4000]}\n\n"
        "## SALES METHODOLOGY EVIDENCE (cite persona/stage/source; abstain if irrelevant)\n"
        f"{kb_block or 'none retrieved'}\n\n"
        "Can this ICP deliver this goal?"
    )
    return prompt, len(cards)


async def judge_goal_match(
    goal: str,
    offer: str = "",
    icp_json: dict[str, Any] | None = None,
) -> GoalMatchVerdict:
    """Run the judge — backend route when hosted, same prompt locally otherwise.

    Never raises: an audit that cannot run must not stop ICP generation. A
    failure returns `partial` with the reason, which warns but does not block.
    """
    from ..config import has_local_llm_key, is_backend_mode

    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client

        client = get_linkedin_client()
        try:
            data = await client.icp_goal_match(goal, offer, icp_json or {})
            return coerce_verdict(data, source="backend")
        except Exception as e:
            logger.warning("Backend goal match failed: %s", e)
            return GoalMatchVerdict(
                verdict="partial",
                reason=f"goal/ICP audit unavailable: {e}",
                warnings=["The goal/ICP audit did not run."],
                source="unavailable",
            )
        finally:
            await client.close()

    prompt, used = build_goal_match_prompt(goal, offer, icp_json)
    try:
        from .llm import LLMClient

        raw = await LLMClient().generate(
            prompt, system=GOAL_MATCH_SYSTEM, temperature=0.1, max_tokens=1200,
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
        )
    verdict = coerce_verdict(data, source="local")
    verdict.kb_cards_used = used
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
    icon = {"match": "✓", "partial": "!", "mismatch": "✗"}.get(v.verdict, "?")
    pct = round(v.decision_maker_coverage * 100)
    lines = [
        f"{icon} Goal ↔ ICP: **{v.verdict}** "
        f"({pct}% decision-maker coverage, {v.kb_cards_used} KB cards cited)",
    ]
    if v.reason:
        lines.append(f"  {v.reason}")
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
