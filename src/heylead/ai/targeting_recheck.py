"""Detect “is this message for me?” replies and recheck campaign fit.

Does not add a sentiment label. Callers branch like decline/vendor-pitch
guards: detect from text, then recheck the contact against the campaign's
original targeting (target_description + ICP).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..config import has_local_llm_key, is_backend_mode
from ..constants import LLM_TIER_FAST
from .llm import LLMClient
from .llm import loads_json_object as parse_json

logger = logging.getLogger(__name__)

TARGETING_MISMATCH_ERROR = "targeting_mismatch"
CLEAR_MISMATCH_CONFIDENCE = 0.7
HEURISTIC_MATCH_FLOOR = 0.45
HEURISTIC_MISMATCH_CEILING = 0.20

_RECHECK_ACTIONS = (
    "targeting_recheck",
    "targeting_mismatch_closed",
    "targeting_recheck_confirmed",
)

# Question-form identity challenges only — not declines like "not for me".
_SKEPTICISM_PHRASES = (
    "is this for me",
    "is this message for me",
    "is this message definitely for me",
    "meant for me",
    "did you mean to message",
    "right person",
    "wrong person",
    "для мене",
    "точно для мене",
    "це для мене",
    "для меня",
    "это мне",
    "это точно для меня",
    "who are you looking for",
    "кого саме ви шукаєте",
    "кого вы ищете",
)

_DECLINE_OVERRIDES = (
    "not for me",
    "не для мене",
    "не для меня",
)


@dataclass(frozen=True)
class FitRecheck:
    """Fresh fit verdict against campaign targeting. Not cached analysis."""

    is_match: bool
    confidence: float
    reason: str
    source: str = "llm"


def is_targeting_skepticism(text: str) -> bool:
    """True when the prospect is asking if we messaged the right person."""
    lowered = (text or "").lower()
    if not lowered.strip():
        return False
    if any(phrase in lowered for phrase in _DECLINE_OVERRIDES):
        return False
    return any(phrase in lowered for phrase in _SKEPTICISM_PHRASES)


def is_clear_mismatch(verdict: FitRecheck) -> bool:
    """Only a high-confidence miss should close or skip. Uncertain = treat as fit."""
    return (not verdict.is_match) and verdict.confidence >= CLEAR_MISMATCH_CONFIDENCE


def targeting_reply_directive(is_match: bool, target_description: str) -> str:
    """Instruction appended to reply_text so hosted generate_reply still follows."""
    target = (target_description or "").strip() or "the buyer we described"
    if is_match:
        return (
            f"[They asked if this message was meant for them. They ARE the right "
            f"person. Confirm briefly and explain who we look for: {target}. "
            f"Do not over-pitch.]"
        )
    return (
        f"[They asked if this message was meant for them. They are NOT a fit "
        f"for {target}. Write a short apology in their language. Do not pitch. "
        f"Do not ask who the right person is unless they offered a name.]"
    )


def already_rechecked(outreach_id: str) -> bool:
    """True if this outreach already ran a targeting recheck."""
    if not outreach_id:
        return False
    from ..db.schema import get_db

    placeholders = ",".join("?" * len(_RECHECK_ACTIONS))
    db = get_db()
    row = db.execute(
        f"""SELECT 1 FROM actions_log
           WHERE outreach_id = ?
             AND action_type IN ({placeholders})
           LIMIT 1""",
        (outreach_id, *_RECHECK_ACTIONS),
    ).fetchone()
    db.close()
    return row is not None


def _heuristic_recheck(
    prospect: dict[str, Any],
    campaign_context: dict[str, Any],
    icp_data: dict[str, Any] | None,
) -> FitRecheck:
    """Conservative ICP-score fallback when the LLM path is unavailable."""
    from ..services.icp_match_scorer import compute_icp_match

    result = compute_icp_match(prospect, icp_data or {})
    # `icp_match_score` is a hard 0.0 when the title states a level the ICP did
    # not ask for. That is the intake answer, and this is not intake: the
    # recheck runs when a prospect asks "was this meant for me?", and its
    # mismatch verdict closes the conversation. A Director against a CXO ICP is
    # not grounds to hang up. Score the six dimensions instead.
    score = float(
        result.get("weighted_score", result.get("icp_match_score")) or 0.0
    )
    target = (campaign_context or {}).get("target_description") or "campaign ICP"
    if score <= HEURISTIC_MISMATCH_CEILING:
        return FitRecheck(
            is_match=False,
            confidence=0.75,
            reason=f"ICP match {score:.2f} is well below {target}",
            source="heuristic",
        )
    if score >= HEURISTIC_MATCH_FLOOR:
        return FitRecheck(
            is_match=True,
            confidence=min(0.85, 0.5 + score / 2),
            reason=f"ICP match {score:.2f} fits {target}",
            source="heuristic",
        )
    return FitRecheck(
        is_match=True,
        confidence=0.4,
        reason=f"ICP match {score:.2f} is uncertain — treat as a fit",
        source="heuristic",
    )


_RECHECK_SYSTEM = """You judge whether a LinkedIn contact matches an outbound campaign's buyer.

Output ONLY a JSON object with:
- "is_match": boolean — true if this person is a buyer the campaign is looking for
- "confidence": float 0.0-1.0
- "reason": one short sentence

Rules:
- Match the person's title, headline and seniority against the campaign target and ICP titles.
- A CEO/founder who merely mentions a domain word (e.g. "people-oriented") is NOT a Head of People / VP HR / CHRO.
- Adjacent seniority without a title match is not enough.
- VP of Product, CPO, and AI Product Leader match Head of Product / product-leadership ICP.
- Product Marketing and Business Development do not.
- When unsure, set is_match true and confidence below 0.6.
No markdown. No code fences."""


async def _llm_recheck_fit(
    prospect: dict[str, Any],
    campaign_context: dict[str, Any],
    icp_data: dict[str, Any] | None,
    our_last_message: str = "",
) -> FitRecheck:
    title = prospect.get("title") or ""
    headline = prospect.get("headline") or title
    target = (campaign_context or {}).get("target_description") or ""
    icp = icp_data or {}
    titles: list[str] = []
    if isinstance(icp, dict):
        icps = icp.get("icps") or icp.get("segments") or [icp]
        if isinstance(icps, list):
            for block in icps[:2]:
                if not isinstance(block, dict):
                    continue
                job = block.get("job_titles") or {}
                if isinstance(job, dict):
                    titles.extend(str(t) for t in (job.get("include") or [])[:6])
                titles.extend(str(t) for t in (block.get("titles") or [])[:6])

    last = (our_last_message or "").strip()
    last_section = f'\nOur last message to them:\n"{last[:400]}"\n' if last else ""

    prompt = (
        f"Campaign target: {target or 'not specified'}\n"
        f"ICP titles: {', '.join(titles) or 'not specified'}\n\n"
        f"Contact: {prospect.get('name') or 'Unknown'}\n"
        f"Title: {title or 'unknown'}\n"
        f"Headline: {headline or 'unknown'}\n"
        f"Company: {prospect.get('company') or 'unknown'}\n"
        f"{last_section}\n"
        "Is this the buyer the campaign is looking for?"
    )

    client = LLMClient()
    raw = await client.generate(
        prompt, system=_RECHECK_SYSTEM, temperature=0.1, max_tokens=200,
        tier=LLM_TIER_FAST,
    )
    data = parse_json(raw, fallback={"is_match": True, "confidence": 0.3, "reason": "parse failed"})
    confidence = data.get("confidence", 0.3)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.3
    confidence = max(0.0, min(1.0, confidence))
    is_match = bool(data.get("is_match", True))
    return FitRecheck(
        is_match=is_match,
        confidence=confidence,
        reason=str(data.get("reason") or "")[:240],
        source="llm",
    )


async def recheck_campaign_fit(
    prospect: dict[str, Any],
    campaign_context: dict[str, Any],
    icp_data: dict[str, Any] | None = None,
    our_last_message: str = "",
) -> FitRecheck:
    """Re-score this person against the campaign's original targeting.

    Bypasses contacts.analysis_json. Uncertain results are a fit (do not close).
    """
    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client

        backend_client = get_linkedin_client()
        try:
            data = await backend_client.recheck_campaign_fit(
                prospect=prospect,
                campaign_context=campaign_context,
                icp_data=icp_data or {},
                our_last_message=our_last_message,
            )
            if isinstance(data, FitRecheck):
                return data
            if isinstance(data, dict):
                confidence = float(data.get("confidence") or 0.3)
                return FitRecheck(
                    is_match=bool(data.get("is_match", True)),
                    confidence=max(0.0, min(1.0, confidence)),
                    reason=str(data.get("reason") or "")[:240],
                    source="backend",
                )
        except Exception as e:
            logger.warning("Backend fit recheck failed: %s, using heuristic", e)
            return _heuristic_recheck(prospect, campaign_context, icp_data)
        finally:
            await backend_client.close()

    try:
        return await _llm_recheck_fit(
            prospect, campaign_context, icp_data, our_last_message,
        )
    except Exception as e:
        logger.warning("LLM fit recheck failed: %s, using heuristic", e)
        return _heuristic_recheck(prospect, campaign_context, icp_data)
