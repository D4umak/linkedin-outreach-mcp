"""AI: signal_classifier — 3-stage signal intent classification.

Mirrors the inbound_qualifier.py pattern:
1. Fast rules: keyword matching for obvious signals
2. ICP keyword overlap: match signal content against ICP pain points/keywords
3. LLM classification: full semantic analysis with engagement hook extraction

Classifies signals from prospect posts, keyword mentions, and other sources.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from ..constants import (
    LLM_TIER_FAST,
    SIGNAL_CLASSIFY_BATCH,
    SIGNAL_CLASSIFY_BUDGET_SECONDS,
    SIGNAL_CLASSIFY_MAX_PER_RUN,
    SIGNAL_CLASSIFY_ROW_TIMEOUT_SECONDS,
    SIGNAL_CLASSIFY_OLDEST_SHARE,
    SIGNAL_COMPANY_CHANGE,
    SIGNAL_HEADLINE_CHANGE,
    SIGNAL_HEADLINE_INTENT,
    SIGNAL_INTENT_BUYING,
    SIGNAL_INTENT_COMPETITOR_EVAL,
    SIGNAL_INTENT_JOB_SEEKING,
    SIGNAL_INTENT_NOT_RELEVANT,
    SIGNAL_INTENT_PAIN_POINT,
    SIGNAL_INTENT_THOUGHT_LEADERSHIP,
    SIGNAL_INTENT_UNKNOWN,
    SIGNAL_PROMOTION,
    SIGNAL_STATUS_CLASSIFIED,
    SIGNAL_STATUS_NEW,
    SIGNAL_STATUS_SKIPPED,
    COMPANY_LEVEL_SIGNAL_TYPES,
)
from ..db.async_bridge import run_db

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Output schema
# ──────────────────────────────────────────────


@dataclass
class SignalClassification:
    """Result of classifying a signal's intent."""

    intent: str  # buying_signal, pain_point, competitor_eval, thought_leadership, job_seeking, unknown
    confidence: float  # 0.0 - 1.0
    pain_points_detected: list[str]
    keywords_matched: list[str]
    engagement_hook: str  # Suggested angle for outreach referencing this signal
    reasoning: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "pain_points_detected": self.pain_points_detected,
            "keywords_matched": self.keywords_matched,
            "engagement_hook": self.engagement_hook,
            "reasoning": self.reasoning,
        }


# ──────────────────────────────────────────────
# Stage 1: Fast rule-based classification
# ──────────────────────────────────────────────

# Buying signal keywords — someone is actively looking for a solution
_BUYING_KEYWORDS = [
    "looking for a tool",
    "looking for a solution",
    "need a better way",
    "anyone recommend",
    "any recommendations",
    "can anyone suggest",
    "what tool do you use",
    "which platform",
    "evaluating options",
    "in the market for",
    "ready to switch",
    "demo request",
    "free trial",
    "pricing",
    "compared to",
    "switching from",
    "moving away from",
    "replacing our",
    "need help with",
    "struggling with",
    "frustrated with",
    "tired of",
]

# Pain point keywords — expressing a problem that our product solves
_PAIN_POINT_KEYWORDS = [
    "biggest challenge",
    "main struggle",
    "pain point",
    "bottleneck",
    "time-consuming",
    "manual process",
    "not scalable",
    "low response rate",
    "low reply rate",
    "cold outreach",
    "outbound is dead",
    "outreach fatigue",
    "personalization at scale",
    "hard to reach",
    "getting ghosted",
]

# Competitor evaluation — mentioning competitor names or comparing tools
_COMPETITOR_KEYWORDS = [
    "outreach.io",
    "salesloft",
    "apollo",
    "lemlist",
    "instantly",
    "woodpecker",
    "mailshake",
    "reply.io",
    "smartlead",
    "clay.com",
    "trigify",
    "unify",
    "common room",
    "usergems",
    "warmly",
    "linkedin automation",
    "sales engagement",
]

# Competitor names that are also ordinary English words. One of these alone is
# not evidence of a competitor evaluation — "we shipped it instantly" is not a
# product mention — so they only count alongside a second competitor match, and
# they are matched on word boundaries rather than as bare substrings.
_AMBIGUOUS_COMPETITOR_KEYWORDS = frozenset({"apollo", "instantly", "unify"})

_AMBIGUOUS_COMPETITOR_PATTERNS = {
    kw: re.compile(rf"\b{re.escape(kw)}\b")
    for kw in _AMBIGUOUS_COMPETITOR_KEYWORDS
}

# Job seeking signals
_JOB_SEEKING_KEYWORDS = [
    "open to work",
    "looking for opportunities",
    "available for hire",
    "seeking new role",
    "recently laid off",
    "exploring opportunities",
    "#opentowork",
    "just got laid off",
    "let go from",
    "looking for my next",
]


def _detect_mention_context(content: str) -> str:
    """Detect mention context for competitor signals.

    Returns one of: "switching_from", "evaluating", "complaint", "general".
    Used by the activator's hot signal fast-track.
    """
    lower = content.lower()
    for kw in ("switching from", "moving away", "replacing", "looking for alternative",
                "leaving", "ditching", "dropping", "migrating from"):
        if kw in lower:
            return "switching_from"
    for kw in ("evaluating", "comparing", "vs ", " or ", "which is better",
                "considering", "looking at", "exploring options"):
        if kw in lower:
            return "evaluating"
    for kw in ("frustrated", "terrible", "worst", "overpriced", "pricing is crazy",
                "too expensive", "broken", "doesn't work", "hate", "sucks"):
        if kw in lower:
            return "complaint"
    return "general"


def _classify_fast(content: str) -> SignalClassification | None:
    """Stage 1: Fast rule-based classification.

    Returns a classification if strong signal detected, None otherwise.
    """
    content_lower = content.lower()

    # Check buying signals
    matched_buying = [kw for kw in _BUYING_KEYWORDS if kw in content_lower]
    if len(matched_buying) >= 2:
        return SignalClassification(
            intent=SIGNAL_INTENT_BUYING,
            confidence=0.85,
            pain_points_detected=[],
            keywords_matched=matched_buying,
            engagement_hook=f"They're actively searching — mentioned: {', '.join(matched_buying[:3])}",
            reasoning=f"Multiple buying signal keywords detected: {', '.join(matched_buying[:3])}",
        )

    # Check pain points
    matched_pain = [kw for kw in _PAIN_POINT_KEYWORDS if kw in content_lower]
    if len(matched_pain) >= 2:
        return SignalClassification(
            intent=SIGNAL_INTENT_PAIN_POINT,
            confidence=0.75,
            pain_points_detected=matched_pain,
            keywords_matched=matched_pain,
            engagement_hook=f"They're experiencing pain — mentioned: {', '.join(matched_pain[:3])}",
            reasoning=f"Multiple pain point keywords detected: {', '.join(matched_pain[:3])}",
        )

    # Check competitor mentions
    matched_comp = [
        kw for kw in _COMPETITOR_KEYWORDS
        if (
            _AMBIGUOUS_COMPETITOR_PATTERNS[kw].search(content_lower)
            if kw in _AMBIGUOUS_COMPETITOR_KEYWORDS
            else kw in content_lower
        )
    ]
    unambiguous_comp = [
        kw for kw in matched_comp if kw not in _AMBIGUOUS_COMPETITOR_KEYWORDS
    ]
    if unambiguous_comp or len(matched_comp) >= 2:
        return SignalClassification(
            intent=SIGNAL_INTENT_COMPETITOR_EVAL,
            confidence=0.80,
            pain_points_detected=[],
            keywords_matched=matched_comp,
            engagement_hook=f"Mentioned competitor(s): {', '.join(matched_comp[:3])} — opportunity for displacement",
            reasoning=f"Competitor mention detected: {', '.join(matched_comp[:3])}",
        )

    # Check job seeking
    matched_job = [kw for kw in _JOB_SEEKING_KEYWORDS if kw in content_lower]
    if matched_job:
        return SignalClassification(
            intent=SIGNAL_INTENT_JOB_SEEKING,
            confidence=0.80,
            pain_points_detected=[],
            keywords_matched=matched_job,
            engagement_hook="",
            reasoning=f"Job seeking language detected: {', '.join(matched_job[:2])}",
        )

    # Single strong buying keyword
    if matched_buying:
        return SignalClassification(
            intent=SIGNAL_INTENT_BUYING,
            confidence=0.60,
            pain_points_detected=[],
            keywords_matched=matched_buying,
            engagement_hook=f"Possible buying intent — mentioned: {matched_buying[0]}",
            reasoning=f"Single buying signal keyword: {matched_buying[0]}",
        )

    return None


# ──────────────────────────────────────────────
# Stage 2: ICP keyword overlap
# ──────────────────────────────────────────────


def _compute_icp_overlap(
    content: str,
    icp_data: dict[str, Any],
) -> tuple[float, list[str], list[str]]:
    """Compute overlap between signal content and ICP keywords/pain points.

    Returns:
        (overlap_score, matched_keywords, matched_pain_points)
    """
    content_lower = content.lower()
    matched_keywords: list[str] = []
    matched_pain_points: list[str] = []

    # Extract keywords from ICP
    icp_keywords: list[str] = []

    # Direct keywords
    kws = icp_data.get("keywords", [])
    if isinstance(kws, list):
        icp_keywords.extend(str(k).lower() for k in kws if k)

    # Pain points
    pps = icp_data.get("pain_points", [])
    if isinstance(pps, list):
        for pp in pps:
            pp_str = str(pp).lower()
            # Extract key phrases (2+ word chunks)
            words = pp_str.split()
            if len(words) <= 4:
                icp_keywords.append(pp_str)
            else:
                # Use sliding window for long pain points
                for i in range(len(words) - 1):
                    icp_keywords.append(f"{words[i]} {words[i + 1]}")

    # Job titles (include list)
    jt = icp_data.get("job_titles", {})
    if isinstance(jt, dict):
        for title in (jt.get("include") or []):
            icp_keywords.append(str(title).lower())
    elif isinstance(jt, list):
        for title in jt:
            icp_keywords.append(str(title).lower())

    # Industries (include list)
    ind = icp_data.get("industries", {})
    if isinstance(ind, dict):
        for industry in (ind.get("include") or []):
            icp_keywords.append(str(industry).lower())
    elif isinstance(ind, list):
        for industry in ind:
            icp_keywords.append(str(industry).lower())

    if not icp_keywords:
        return 0.0, [], []

    # Deduplicate
    icp_keywords = list(set(icp_keywords))

    # Match
    for kw in icp_keywords:
        if not kw or len(kw) < 3:
            continue
        if kw in content_lower:
            matched_keywords.append(kw)
            # Track if it came from pain points
            for pp in pps:
                if kw in str(pp).lower():
                    matched_pain_points.append(str(pp))
                    break

    if not icp_keywords:
        return 0.0, matched_keywords, matched_pain_points

    overlap = len(matched_keywords) / len(icp_keywords)
    return min(overlap * 3, 1.0), matched_keywords, list(set(matched_pain_points))  # Boost: 3x multiplier


# ──────────────────────────────────────────────
# Stage 3: LLM classification
# ──────────────────────────────────────────────

_CLASSIFY_SYSTEM = """You are an expert B2B sales signal analyst. Given a LinkedIn post by a prospect, \
analyze it for buying signals relevant to the company's ICPs (Ideal Customer Profiles).

You are looking for:
- Buying intent: actively seeking solutions, evaluating tools, requesting recommendations
- Pain points: expressing frustration, challenges, bottlenecks that the company's product could solve
- Competitor evaluation: mentioning or comparing competitor products
- Thought leadership: sharing expertise relevant to the ICP's domain (good for engagement, lower priority)
- Job seeking: career transition signals

Output ONLY a JSON object with these fields:
- intent: "buying_signal" | "pain_point" | "competitor_eval" | "thought_leadership" | "job_seeking" | "not_relevant" | "unknown"
- confidence: 0.0-1.0
- pain_points_detected: [list of pain points expressed in the post]
- keywords_matched: [list of ICP-relevant keywords found]
- engagement_hook: "1-2 sentence suggested angle for outreach referencing this post naturally. Only cite figures that appear verbatim in the post. Never combine or invent multipliers (do not turn 20× and 21% into 21x)."
- reasoning: "1-2 sentence explanation of classification"

Use not_relevant for personal/lifestyle posts (school holidays, iced coffee, art, travel) with no ICP overlap. Leave engagement_hook empty for those.
"""

_CLASSIFY_PROMPT = """Analyze this LinkedIn post for buying signals:

Post author: {name} ({headline}, {company})
Post text:
---
{post_text}
---

Active ICPs:
{icp_section}

Classify the post's intent and extract actionable insights."""


def _build_icp_section(active_icps: list[dict[str, Any]]) -> str:
    """Build a compact ICP summary for the LLM prompt."""
    if not active_icps:
        return "No ICPs defined."

    parts: list[str] = []
    for icp_row in active_icps[:3]:  # Max 3 ICPs to keep prompt short
        icp_json_str = icp_row.get("icp_json", "{}")
        try:
            parsed = json.loads(icp_json_str) if isinstance(icp_json_str, str) else icp_json_str
        except (json.JSONDecodeError, TypeError):
            continue

        icp_name = icp_row.get("name", "Unknown")
        # Get sub-ICPs
        sub_icps = parsed.get("icps", [parsed]) if parsed else []
        for sub in sub_icps[:2]:  # Max 2 sub-ICPs per ICP
            desc = str(sub.get("description", ""))[:150]
            pps = sub.get("pain_points", [])[:3]
            kws = sub.get("keywords", [])[:5]
            pain_str = ", ".join(str(p) for p in pps) if pps else "none"
            kw_str = ", ".join(str(k) for k in kws) if kws else "none"
            parts.append(
                f"ICP '{icp_name}': {desc}\n"
                f"  Pain points: {pain_str}\n"
                f"  Keywords: {kw_str}"
            )

    return "\n\n".join(parts) if parts else "No ICP details available."


def _parse_classification_response(
    raw: str,
) -> SignalClassification:
    """Parse LLM JSON response into SignalClassification."""
    # Strip markdown fences
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to extract JSON from surrounding text
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                return _fallback_classification("Could not parse LLM response as JSON")
        else:
            return _fallback_classification("No JSON found in LLM response")

    # Validate intent
    valid_intents = {
        SIGNAL_INTENT_BUYING, SIGNAL_INTENT_PAIN_POINT,
        SIGNAL_INTENT_COMPETITOR_EVAL, SIGNAL_INTENT_THOUGHT_LEADERSHIP,
        SIGNAL_INTENT_JOB_SEEKING, SIGNAL_INTENT_UNKNOWN,
        SIGNAL_INTENT_NOT_RELEVANT,
    }
    intent = data.get("intent", SIGNAL_INTENT_UNKNOWN)
    if intent not in valid_intents:
        intent = SIGNAL_INTENT_UNKNOWN

    # Validate confidence
    confidence = data.get("confidence", 0.3)
    try:
        confidence = float(confidence)
    except (ValueError, TypeError):
        confidence = 0.3
    confidence = max(0.0, min(1.0, confidence))

    return SignalClassification(
        intent=intent,
        confidence=confidence,
        pain_points_detected=data.get("pain_points_detected", []) or [],
        keywords_matched=data.get("keywords_matched", []) or [],
        engagement_hook=str(data.get("engagement_hook", "")),
        reasoning=str(data.get("reasoning", "")),
    )


def _fallback_classification(reason: str) -> SignalClassification:
    """Return a low-confidence unknown classification."""
    return SignalClassification(
        intent=SIGNAL_INTENT_UNKNOWN,
        confidence=0.2,
        pain_points_detected=[],
        keywords_matched=[],
        engagement_hook="",
        reasoning=reason,
    )


# ──────────────────────────────────────────────
# Main classification function
# ──────────────────────────────────────────────


async def classify_signal(
    content: str,
    author_name: str = "",
    author_headline: str = "",
    author_company: str = "",
    active_icps: list[dict[str, Any]] | None = None,
) -> SignalClassification:
    """Classify a signal's intent using the 3-stage pipeline.

    Args:
        content: The signal text (post text, keyword mention, etc.)
        author_name: Name of the person who created the content.
        author_headline: Their LinkedIn headline.
        author_company: Their company name.
        active_icps: ICP rows (id / name / icp_json) to judge the signal
            against — the ICP of the campaign that produced it. Empty means
            no campaign could be resolved: the fast rules and the model still
            run, but there is no ICP relevance to measure, and the caller
            records that in the signal's reasoning.

    Returns:
        SignalClassification with intent, confidence, and engagement hook.
    """
    if not content or not content.strip():
        return _fallback_classification("Empty signal content")

    icps = active_icps or []

    # ── Stage 1: Fast rules ──
    fast_result = _classify_fast(content)
    if fast_result and fast_result.confidence >= 0.75:
        logger.info(
            "Signal classified (fast): %s → %s (%.0f%%)",
            author_name or "unknown", fast_result.intent, fast_result.confidence * 100,
        )
        return fast_result

    # ── Stage 2: ICP keyword overlap ──
    best_overlap = 0.0
    best_keywords: list[str] = []
    best_pain_points: list[str] = []

    for icp_row in icps:
        icp_json_str = icp_row.get("icp_json", "{}")
        try:
            parsed = json.loads(icp_json_str) if isinstance(icp_json_str, str) else icp_json_str
        except (json.JSONDecodeError, TypeError):
            continue

        for single_icp in (parsed.get("icps", [parsed]) if parsed else []):
            overlap, matched_kws, matched_pps = _compute_icp_overlap(content, single_icp)
            if overlap > best_overlap:
                best_overlap = overlap
                best_keywords = matched_kws
                best_pain_points = matched_pps

    # Merge fast result with ICP overlap for boosted confidence
    if fast_result and best_overlap > 0:
        # Boost fast result confidence with ICP overlap
        boosted_confidence = min(fast_result.confidence + (best_overlap * 0.2), 1.0)
        fast_result.confidence = boosted_confidence
        fast_result.keywords_matched = list(set(fast_result.keywords_matched + best_keywords))
        fast_result.pain_points_detected = list(set(fast_result.pain_points_detected + best_pain_points))
        logger.info(
            "Signal classified (fast+ICP boost): %s → %s (%.0f%%)",
            author_name or "unknown", fast_result.intent, fast_result.confidence * 100,
        )
        return fast_result

    # ── Stage 3: LLM classification ──
    from ..config import has_local_llm_key, is_backend_mode

    # Try backend route first
    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client

        backend_client = get_linkedin_client()
        try:
            result = await backend_client.classify_signal(
                content=content,
                author_name=author_name,
                author_headline=author_headline,
                author_company=author_company,
                icp_summaries=[
                    {
                        "id": icp.get("id"),
                        "name": icp.get("name"),
                        "icp_json": icp.get("icp_json", "{}"),
                    }
                    for icp in icps
                ],
            )
            logger.info(
                "Signal classified (backend): %s → %s (%.0f%%)",
                author_name or "unknown", result.intent, result.confidence * 100,
            )
            return result
        except Exception as e:
            logger.warning("Backend classify_signal failed: %s, trying local LLM", e)
        finally:
            await backend_client.close()

    # Try local LLM
    try:
        from .llm import LLMClient

        client = LLMClient()
        icp_section = _build_icp_section(icps)

        prompt = _CLASSIFY_PROMPT.format(
            name=author_name or "Unknown",
            headline=author_headline or "Unknown",
            company=author_company or "Unknown",
            post_text=content[:1500],  # Limit content length
            icp_section=icp_section,
        )

        raw = await client.generate(prompt, system=_CLASSIFY_SYSTEM, temperature=0.2, max_tokens=500,
                              tier=LLM_TIER_FAST)
        result = _parse_classification_response(raw)
        logger.info(
            "Signal classified (LLM): %s → %s (%.0f%%)",
            author_name or "unknown", result.intent, result.confidence * 100,
        )
        return result

    except Exception as e:
        logger.warning("LLM classify_signal failed: %s, using keyword overlap", e)

    # ── Fallback: keyword overlap only ──
    if best_overlap >= 0.2:
        intent = SIGNAL_INTENT_PAIN_POINT if best_pain_points else SIGNAL_INTENT_UNKNOWN
        return SignalClassification(
            intent=intent,
            confidence=best_overlap,
            pain_points_detected=best_pain_points,
            keywords_matched=best_keywords,
            engagement_hook=f"ICP keyword overlap ({best_overlap:.0%}) — matches: {', '.join(best_keywords[:3])}" if best_keywords else "",
            reasoning=f"ICP keyword overlap: {best_overlap:.0%}",
        )

    # If fast result exists with lower confidence, return it
    if fast_result:
        return fast_result

    return _fallback_classification("No strong signals detected in content.")


# ──────────────────────────────────────────────
# Which ICP is a signal judged against?
# ──────────────────────────────────────────────

# Appended to the reasoning of a signal that could not be tied to a campaign.
# Such a signal is classified with NO ICP rather than skipped: skipping would
# leave it 'new' for ever — the same starvation this module is being fixed for
# — and on the live DB roughly half the unclassified rows carry neither a
# campaign_id nor a watchlist_id, so they would pin the backlog until their
# TTL quietly threw them away. The fast keyword rules and the model still run;
# only the ICP-relevance part of the verdict is missing, and the note says so.
_SCOPE_NO_CAMPAIGN = (
    "No campaign or watchlist links this signal to an ICP, so it was "
    "classified without one"
)
_SCOPE_CAMPAIGN_HAS_NO_ICP = (
    "Campaign {name!r} has no usable ICP, so this signal was classified "
    "without one"
)
# Signals.campaign_id is a foreign key and PRAGMA foreign_keys is ON, so this
# fires only if the campaign went away underneath the batch. Worded for what
# is actually known — the row was not returned — rather than guessing why.
_SCOPE_CAMPAIGN_UNAVAILABLE = (
    "Campaign {cid} could not be loaded, so this signal was classified "
    "without an ICP"
)

# metadata_json["icp_scope"] records WHICH ICP produced a verdict: the campaign
# id, or this sentinel when the verdict was reached without one. Its presence
# is also what marks a row as having been classified under #67's regime at all
# — every row written before it has no such key.
_SCOPE_ID_NONE = "none"


def _segment_to_icp(segment: dict[str, Any]) -> dict[str, Any]:
    """Convert one campaign 'segments' entry to the sub-ICP shape read here.

    A campaign stores its ICP as ``{"segments": [...]}`` (create_campaign.py
    builds it that way) while the icps table stores ``{"icps": [...]}``. The
    two use different key names for the same fields — ``titles`` vs
    ``job_titles``, a comma-joined ``keywords`` string vs a list — so handing a
    campaign's raw icp_json to _compute_icp_overlap() would match nothing at
    all and read as "this signal is irrelevant". Mirrors _convert_legacy_icp()
    in ai/icp_generator_v2.py, without the pydantic models.
    """
    raw_kws = segment.get("keywords", "")
    if isinstance(raw_kws, str):
        keywords = [k.strip() for k in raw_kws.split(",") if k.strip()]
    elif isinstance(raw_kws, list):
        keywords = [str(k) for k in raw_kws if k]
    else:
        keywords = []

    name = str(segment.get("name") or "")
    return {
        "name": name or "Primary",
        "description": name or ", ".join(keywords[:6]),
        "pain_points": [],
        "keywords": keywords,
        "job_titles": {"include": list(segment.get("titles") or [])},
        "industries": {"include": list(segment.get("industries") or [])},
        "locations": {"include": list(segment.get("locations") or [])},
    }


def _campaign_icp_rows(campaign: dict[str, Any]) -> list[dict[str, Any]]:
    """The ICP rows a campaign's signals are judged against, [] if it has none.

    Shaped like the rows list_icps() returns (id / name / icp_json) because
    that is what classify_signal() and the backend route already consume.
    """
    raw = campaign.get("icp_json") or ""
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if not isinstance(parsed, dict):
        return []

    subs = parsed.get("icps")
    if isinstance(subs, list) and subs:
        icps = [s for s in subs if isinstance(s, dict)]
    else:
        segments = parsed.get("segments")
        icps = (
            [_segment_to_icp(s) for s in segments if isinstance(s, dict)]
            if isinstance(segments, list)
            else []
        )
    if not icps:
        return []

    return [{
        "id": campaign.get("id", ""),
        "name": campaign.get("name") or "campaign",
        "icp_json": json.dumps({"icps": icps}),
    }]


async def _resolve_icp_scopes(
    signals: list[dict[str, Any]],
) -> dict[str, tuple[list[dict[str, Any]], str]]:
    """Map signal id → (ICP rows to judge it against, scope note).

    #67: every signal used to be judged against ``list_icps(status='active')``
    — every ICP on the whole account. Signals from a client's watchlists came
    back rejected for "not aligning with the provided ICP of UK fintech
    leaders", which is HeyLead's own profile, not the client's. A signal
    belongs to the campaign that produced it, and that campaign's ICP is the
    only one it should be measured against.

    Resolution is campaign_id first, then the campaign of the signal's
    watchlist. Every lookup is batched: at most two queries for the whole
    batch, regardless of how many signals it holds.

    The note is "" when an ICP was resolved, and otherwise says why there is
    none — it is appended to the stored reasoning so nobody reads an
    unscoped verdict as an ICP judgement.
    """
    from ..db.queries import batch_get_campaigns
    from ..db.signal_queries import batch_get_watchlist_campaigns

    watchlist_ids = [
        s.get("watchlist_id") or ""
        for s in signals
        if not s.get("campaign_id") and s.get("watchlist_id")
    ]
    watchlist_campaigns: dict[str, str] = {}
    if watchlist_ids:
        watchlist_campaigns = await run_db(
            batch_get_watchlist_campaigns, watchlist_ids
        )

    campaign_of_signal: dict[str, str] = {}
    for signal in signals:
        campaign_id = signal.get("campaign_id") or watchlist_campaigns.get(
            signal.get("watchlist_id") or "", ""
        )
        if campaign_id:
            campaign_of_signal[signal.get("id") or ""] = campaign_id

    campaigns: dict[str, dict[str, Any]] = {}
    if campaign_of_signal:
        campaigns = await run_db(
            batch_get_campaigns, list(campaign_of_signal.values())
        )

    icps_by_campaign = {
        cid: _campaign_icp_rows(row) for cid, row in campaigns.items()
    }

    scopes: dict[str, tuple[list[dict[str, Any]], str]] = {}
    for signal in signals:
        signal_id = signal.get("id") or ""
        campaign_id = campaign_of_signal.get(signal_id, "")
        if not campaign_id:
            scopes[signal_id] = ([], _SCOPE_NO_CAMPAIGN)
            continue
        campaign = campaigns.get(campaign_id)
        if campaign is None:
            scopes[signal_id] = (
                [], _SCOPE_CAMPAIGN_UNAVAILABLE.format(cid=campaign_id)
            )
            continue
        icp_rows = icps_by_campaign.get(campaign_id) or []
        if not icp_rows:
            scopes[signal_id] = (
                [],
                _SCOPE_CAMPAIGN_HAS_NO_ICP.format(
                    name=campaign.get("name") or campaign_id
                ),
            )
            continue
        scopes[signal_id] = (icp_rows, "")

    return scopes


def _same_icp_scope(signal: dict[str, Any], cached: dict[str, Any]) -> bool:
    """May *signal* inherit *cached*'s verdict without changing its meaning?

    Verdict reuse is keyed on (signal_type, post_id, linkedin_id), which says
    the two rows hold the same text by the same author — not that they were
    judged against the same customer profile. Rows for one post really do span
    campaigns in the live DB, and copying a verdict across them would put back
    exactly the bug #67 is about, through the reuse door: the row would carry
    a verdict, a reasoning string and an engagement_hook written against
    somebody else's ICP.

    Equal campaign_id and equal watchlist_id is the conservative test — it
    cannot resolve to a different ICP. A mismatch costs one model call, never
    a wrong verdict.
    """
    return (
        (signal.get("campaign_id") or "") == (cached.get("campaign_id") or "")
        and (signal.get("watchlist_id") or "") == (cached.get("watchlist_id") or "")
    )


def _verdict_was_scoped(cached_meta: dict[str, Any], own_scope: str) -> bool:
    """Was *cached*'s verdict reached under the same ICP this signal resolves to?

    _same_icp_scope() compares the two rows' campaign_id and watchlist_id,
    which is not enough on its own: a row classified BEFORE #67 landed has the
    same scope keys and a reasoning computed against every ICP on the account.
    Equal keys, wrong verdict — and it would be copied verbatim onto a new
    signal, which is #67 arriving through the back door after being closed at
    the front. On the live DB this is not hypothetical: all 2,900 classified
    rows predate the fix, 262 pending rows have a same-scope classified
    sibling, and every one of those cached reasonings judges the signal against
    an ICP that was never the campaign's.

    So reuse is allowed only from a row that RECORDED its scope and recorded
    the same one. metadata_json["icp_scope"] is written on every verdict this
    module produces and on no verdict written before it, which makes its
    absence an exact marker for "pre-fix" — verified read-only on the live DB:
    0 of 2,900 classified rows carry the key.
    """
    return cached_meta.get("icp_scope") == own_scope


# ──────────────────────────────────────────────
# Batch classification for scheduler
# ──────────────────────────────────────────────


def _select_classification_batch(
    oldest: list[dict[str, Any]], newest: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """INTERLEAVE the two halves of a run's batch, without duplicates.

    Interleaved rather than concatenated, because a run does not finish its
    batch: it stops at SIGNAL_CLASSIFY_MAX_PER_RUN model calls, well short of
    the SIGNAL_CLASSIFY_BATCH rows it read. Concatenating as (*oldest, *newest)
    put every fresh row at positions 21-30, which no run ever reached: the
    split that justifies taking from both ends existed only in the fixture,
    where a fake classifier returns instantly and all 30 rows run. In
    production the code was pure oldest-first.

    So the mix has to hold in every PREFIX of the result, not just in the whole
    of it. Each step hands the next slot to whichever half has so far consumed
    the smaller fraction of itself, which reproduces the 2:1 limit split all
    the way down: the first twelve positions are 8 old and 4 fresh.

    Once the backlog is smaller than the batch the two halves overlap; the
    union is then the whole backlog, so nothing is lost and nothing is done
    twice.
    """
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    n_old, n_new = len(oldest), len(newest)
    o_i = n_i = 0

    while o_i < n_old or n_i < n_new:
        if n_i >= n_new:
            row, o_i = oldest[o_i], o_i + 1
        elif o_i >= n_old:
            row, n_i = newest[n_i], n_i + 1
        elif o_i * n_new <= n_i * n_old:
            # o_i/n_old <= n_i/n_new — the old end is the one running behind.
            row, o_i = oldest[o_i], o_i + 1
        else:
            row, n_i = newest[n_i], n_i + 1

        row_id = row.get("id") or ""
        if row_id in seen:
            continue
        seen.add(row_id)
        merged.append(row)

    return merged


def _should_skip_network_lifestyle(signal: dict[str, Any]) -> bool:
    """Leave network_scan prospect_posts new unless a hook or ICP title says otherwise."""
    from ..constants import SIGNAL_PROSPECT_POST
    from ..collectors.profile_view_collector import _check_icp_match
    from ..db.signal_queries import has_classified_hook_sibling

    if signal.get("source") != "network_scan":
        return False
    if (signal.get("signal_type") or "") != SIGNAL_PROSPECT_POST:
        return False
    if has_classified_hook_sibling(
        linkedin_id=signal.get("linkedin_id") or "",
        post_id=signal.get("post_id") or "",
    ):
        return False
    title = signal.get("prospect_title") or ""
    if title and _check_icp_match(title, "").get("is_match"):
        return False
    return True


async def classify_pending_signals() -> str:
    """Classify signals with status='new', from both ends of the backlog.

    Called by the scheduler every SIGNAL_CLASSIFY_SECONDS.

    Flow:
    1. Fetch signals with status='new' — part of the batch from the oldest end
       so the backlog drains, the rest from the newest so fresh signals stay
       fast (see SIGNAL_CLASSIFY_OLDEST_SHARE), INTERLEAVED so a run that stops
       early has still done some of each
    2. Resolve each signal's own campaign ICP
    3. Classify each signal against that ICP
    4. Update signal with intent, confidence, reasoning, engagement_hook
    5. Set status to 'classified'

    Not every selected row is reached: the loop stops at
    SIGNAL_CLASSIFY_MAX_PER_RUN model calls and leaves the rest 'new' for the
    next run. Ending itself is what lets it RETURN this summary — a run that
    instead waits to be cancelled by the scheduler has no return value, and its
    finished rows are recorded as a job failure rather than as the work they
    were.

    Returns:
        Summary string of results, including what it did not reach and the
        backlog left behind. A run with work remaining is a success.
    """
    from ..db.signal_queries import (
        count_unclassified_signals,
        get_classified_signal_by_post_id,
        list_signals,
    )
    from ..services.signal_scorer import update_signal_with_score

    # ── Pick the batch from BOTH ends of the backlog ──
    # This used to be a single newest-first LIMIT, so every run classified the
    # 30 newest rows and anything older than the arrival rate was buried for
    # good: on the live DB the newest 30 unclassified signals all landed
    # within three minutes of each other while 2,433 older ones — the oldest
    # five days old, 412 of them due to hit their TTL within three days — had
    # never been looked at. Both halves are LIMITed and both are served by
    # idx_signals_status_detected, so a tick READS at most SIGNAL_CLASSIFY_BATCH
    # rows however big the backlog gets. Before that index existed the LIMIT
    # bounded only the rows RETURNED: the planner took idx_signals_status and
    # sorted every 'new' row — SELECT *, so content and metadata_json too — in
    # a temp B-tree, twice per tick. Measured on a copy of the live 32,052-row
    # signals table: 62.01ms ASC + 4.32ms DESC before, 0.14ms + 0.08ms after.
    drain_limit = max(1, int(SIGNAL_CLASSIFY_BATCH * SIGNAL_CLASSIFY_OLDEST_SHARE))
    fresh_limit = max(1, SIGNAL_CLASSIFY_BATCH - drain_limit)

    oldest = await run_db(
        list_signals, status=SIGNAL_STATUS_NEW, limit=drain_limit,
        order_by="detected_at ASC",
    )
    newest = await run_db(
        list_signals, status=SIGNAL_STATUS_NEW, limit=fresh_limit,
        order_by="detected_at DESC",
    )
    signals = _select_classification_batch(oldest, newest)
    if not signals:
        return "No signals pending classification."

    # Which end each row came from, so the summary can report what a truncated
    # run actually touched rather than which window it was drawn from. A row in
    # both windows counts as backlog: the two windows have met.
    oldest_ids = {r.get("id") or "" for r in oldest}
    fresh_ids = {r.get("id") or "" for r in newest} - oldest_ids
    # Disjoint windows mean the backlog is bigger than a batch; overlapping
    # ones mean this batch IS the whole backlog.
    windows_meet = len(signals) < len(oldest) + len(newest)

    # Each signal is judged against the ICP of the campaign that produced it,
    # not against every ICP on the account (#67).
    icp_scopes = await _resolve_icp_scopes(signals)

    classified = 0
    errors = 0
    unscoped = 0
    from_oldest = 0
    from_newest = 0
    started = time.monotonic()
    ran_out_of_time = False
    # What the run spends: rows decided from metadata or from a stored verdict
    # cost a DB write, a model call costs ~3.3s. The bound counts the latter.
    model_calls = 0
    hit_call_bound = False

    for signal in signals:
        # Backstop, not the exit: SIGNAL_CLASSIFY_MAX_PER_RUN below ends a
        # healthy run. This catches the run whose calls are slower than the
        # count was sized against, before the job's own 44s timeout does — a
        # job killed there has no return value, so its finished rows are filed
        # as [network_error] TimeoutError instead of as the work they were.
        if time.monotonic() - started >= SIGNAL_CLASSIFY_BUDGET_SECONDS:
            ran_out_of_time = True
            break

        signal_id = signal.get("id", "")
        if signal_id in fresh_ids:
            from_newest += 1
        else:
            from_oldest += 1

        if (
            (signal.get("signal_type") or "") not in COMPANY_LEVEL_SIGNAL_TYPES
            and not (signal.get("linkedin_id") or "").strip()
        ):
            from ..db.signal_queries import update_signal
            await run_db(
                update_signal,
                signal_id,
                status=SIGNAL_STATUS_SKIPPED,
                action_taken="no_linkedin_id",
            )
            if signal_id in fresh_ids:
                from_newest -= 1
            else:
                from_oldest -= 1
            continue

        skip_lifestyle = await run_db(_should_skip_network_lifestyle, signal)
        if skip_lifestyle:
            if signal_id in fresh_ids:
                from_newest -= 1
            else:
                from_oldest -= 1
            continue

        content = signal.get("content", "")
        prospect_name = signal.get("prospect_name", "")
        prospect_title = signal.get("prospect_title", "")

        # Extract company from metadata
        metadata = {}
        meta_str = signal.get("metadata_json", "")
        if meta_str:
            try:
                metadata = json.loads(meta_str)
            except (json.JSONDecodeError, TypeError):
                pass
        company = metadata.get("contact_company", "") or metadata.get("company", "")

        # ── Fast-path: profile change signals (already validated by collector) ──
        signal_type = signal.get("signal_type", "")
        if signal_type in (SIGNAL_COMPANY_CHANGE, SIGNAL_PROMOTION):
            try:
                await run_db(
                    update_signal_with_score,
                    signal_id=signal_id,
                    signal_type=signal_type,
                    detected_at=signal.get("detected_at") or 0,
                    intent=SIGNAL_INTENT_BUYING,
                    confidence=0.70,
                    reasoning=f"Profile change ({signal_type}) indicates potential buying opportunity",
                    status=SIGNAL_STATUS_CLASSIFIED,
                    classified_at=int(time.time()),
                )
            except Exception as e:
                logger.warning("Fast-path write failed for %s: %s", signal_id[:8], e)
                errors += 1
                continue
            classified += 1
            continue

        if signal_type == SIGNAL_HEADLINE_INTENT:
            intent_cats = metadata.get("intent_categories", [])
            if any(c in ("hiring", "evaluating") for c in intent_cats):
                hi_intent = SIGNAL_INTENT_BUYING
                hi_conf = 0.75
            elif "building" in intent_cats:
                hi_intent = SIGNAL_INTENT_BUYING
                hi_conf = 0.60
            elif "open_to_opportunities" in intent_cats:
                hi_intent = SIGNAL_INTENT_JOB_SEEKING
                hi_conf = 0.80
            else:
                hi_intent = SIGNAL_INTENT_UNKNOWN
                hi_conf = 0.40
            try:
                await run_db(
                    update_signal_with_score,
                    signal_id=signal_id,
                    signal_type=signal_type,
                    detected_at=signal.get("detected_at") or 0,
                    intent=hi_intent,
                    confidence=hi_conf,
                    reasoning=f"Headline intent categories: {', '.join(intent_cats)}",
                    status=SIGNAL_STATUS_CLASSIFIED,
                    classified_at=int(time.time()),
                )
            except Exception as e:
                logger.warning("Fast-path write failed for %s: %s", signal_id[:8], e)
                errors += 1
                continue
            classified += 1
            continue

        if signal_type == SIGNAL_HEADLINE_CHANGE:
            try:
                await run_db(
                    update_signal_with_score,
                    signal_id=signal_id,
                    signal_type=signal_type,
                    detected_at=signal.get("detected_at") or 0,
                    intent=SIGNAL_INTENT_UNKNOWN,
                    confidence=0.30,
                    reasoning="Generic headline change, no strong intent signal",
                    status=SIGNAL_STATUS_CLASSIFIED,
                    classified_at=int(time.time()),
                )
            except Exception as e:
                logger.warning("Fast-path write failed for %s: %s", signal_id[:8], e)
                errors += 1
                continue
            classified += 1
            continue

        # Resolved here rather than just before the classifier call, because
        # the reuse branch below has to compare this signal's scope against the
        # scope the cached verdict was actually reached under.
        signal_icps, scope_note = icp_scopes.get(signal_id, ([], _SCOPE_NO_CAMPAIGN))
        own_scope = signal_icps[0].get("id", "") if signal_icps else _SCOPE_ID_NONE

        # ── Reuse: this post, by this author, was already classified ──
        # Duplicate rows for one post_id still exist from before the collector
        # dedup landed, and two collectors can legitimately see the same post.
        # Re-sending identical text to the model is pure quota burn.
        # The lookup is keyed on author as well as type: rows sharing a post_id
        # are not necessarily the same text (commenter_match stores one row per
        # commenter under the raw shared post_id), and a verdict borrowed from a
        # different author would put their engagement_hook into outreach.
        post_id = signal.get("post_id") or ""
        cached = None
        if post_id:
            try:
                cached = await run_db(
                    get_classified_signal_by_post_id,
                    signal_type, post_id, signal.get("linkedin_id") or "",
                )
            except Exception as e:
                # Fail open: reuse is an optimisation, so a failed lookup must
                # cost one extra model call, never a skipped classification.
                logger.debug("Verdict reuse lookup failed for %s: %s", signal_id[:8], e)
        try:
            cached_meta = json.loads(cached.get("metadata_json") or "{}") if cached else {}
        except (json.JSONDecodeError, TypeError):
            cached_meta = {}

        if (
            cached
            and cached.get("id") != signal_id
            and _same_icp_scope(signal, cached)
            and _verdict_was_scoped(cached_meta, own_scope)
        ):
            # Keep this signal's own metadata (post_date, metrics, company) and
            # copy over only what classification produced.
            reused_meta = {**metadata}
            for key in (
                "pain_points_detected", "keywords_matched",
                "engagement_hook", "mention_context",
            ):
                if key in cached_meta:
                    reused_meta[key] = cached_meta[key]
            # Written, not copied: _verdict_was_scoped() has established the two
            # are equal, and writing it means every row this run touches carries
            # the key, so the "which ICP judged this?" query has no hole.
            reused_meta["icp_scope"] = own_scope

            try:
                await run_db(
                    update_signal_with_score,
                    signal_id=signal_id,
                    signal_type=signal_type,
                    detected_at=signal.get("detected_at") or 0,
                    intent=cached.get("intent"),
                    confidence=cached.get("confidence") or 0.0,
                    reasoning=cached.get("reasoning") or "",
                    status=SIGNAL_STATUS_CLASSIFIED,
                    classified_at=int(time.time()),
                    metadata_json=json.dumps(reused_meta),
                )
            except Exception as e:
                # The classify path below wraps its own update_signal, so a
                # transient "database is locked" there costs one signal and
                # increments errors. Unwrapped, the same lock here escaped
                # classify_pending_signals and abandoned the whole remaining
                # batch — a containment regression introduced by the reuse
                # path, in exactly the area it was meant to improve.
                logger.warning(
                    "Verdict reuse write failed for %s: %s", signal_id[:8], e
                )
                errors += 1
                continue
            classified += 1
            continue

        # Checked HERE rather than at the top of the loop, so that rows which
        # cost no model call keep flowing once the bound is spent: a batch that
        # happened to be mostly profile changes and duplicates would otherwise
        # drain fewer signals than one that was not, for no saving at all.
        # `continue`, not `break` — the row stays 'new' and the next run takes
        # it, which is the ordinary path rather than the sweeper's.
        if model_calls >= SIGNAL_CLASSIFY_MAX_PER_RUN:
            hit_call_bound = True
            # Give back the end this row was attributed to on the way in. The
            # counters say what the run REACHED, and a row left 'new' was not
            # reached — carrying it would print "20 from the oldest end, 10
            # from the newest" on a run that classified eight, which is the
            # drawn-from-window number this summary exists to avoid.
            if signal_id in fresh_ids:
                from_newest -= 1
            else:
                from_oldest -= 1
            continue

        try:
            model_calls += 1
            # One stalled call must not be able to spend the job's whole
            # budget: ai/llm.py allows a 120s read, and the job has 44s.
            result = await asyncio.wait_for(
                classify_signal(
                    content=content,
                    author_name=prospect_name,
                    author_headline=prospect_title,
                    author_company=company,
                    active_icps=signal_icps,
                ),
                timeout=SIGNAL_CLASSIFY_ROW_TIMEOUT_SECONDS,
            )

            # Author pattern boost: repeated topic → higher confidence
            linkedin_id = signal.get("linkedin_id", "")
            confidence = result.confidence
            pattern_note = ""
            if linkedin_id and result.intent != SIGNAL_INTENT_UNKNOWN:
                try:
                    from ..constants import (
                        AUTHOR_PATTERN_CONFIDENCE_BOOST,
                        AUTHOR_PATTERN_MIN_POSTS,
                        AUTHOR_PATTERN_WINDOW_DAYS,
                    )
                    from ..db.post_queries import get_recent_topics_by_author

                    topics = await run_db(
                        get_recent_topics_by_author,
                        linkedin_id, days=AUTHOR_PATTERN_WINDOW_DAYS,
                    )
                    # Check if any topic appears enough times for a pattern
                    repeated = {t: c for t, c in topics.items() if c >= AUTHOR_PATTERN_MIN_POSTS}
                    if repeated:
                        confidence = min(1.0, confidence + AUTHOR_PATTERN_CONFIDENCE_BOOST)
                        top_topic = max(repeated, key=repeated.get)
                        pattern_note = (
                            f" [Author pattern: posted about '{top_topic}' "
                            f"{repeated[top_topic]}x in {AUTHOR_PATTERN_WINDOW_DAYS}d]"
                        )
                except Exception:
                    pass  # Pattern detection failure doesn't block classification

            # Update signal with classification results

            # Merge classification metadata into existing metadata
            classification_meta = {
                **metadata,
                "pain_points_detected": result.pain_points_detected,
                "keywords_matched": result.keywords_matched,
                "engagement_hook": result.engagement_hook,
            }
            if pattern_note:
                classification_meta["author_pattern"] = pattern_note.strip(" []")
            classification_meta["icp_scope"] = own_scope

            # Detect mention_context for competitor signals (used by fast-track)
            if signal.get("signal_type") == "competitor_mention" and content:
                mention_context = _detect_mention_context(content)
                classification_meta["mention_context"] = mention_context

            # A verdict reached without an ICP must say so where the verdict is
            # read. The bug this replaces wrote "does not align with the
            # provided ICP of <somebody else's ICP>" into exactly this column.
            scope_suffix = f" [{scope_note}]" if scope_note else ""

            await run_db(
                update_signal_with_score,
                signal_id=signal_id,
                signal_type=signal_type,
                detected_at=signal.get("detected_at") or 0,
                intent=result.intent,
                confidence=confidence,
                reasoning=result.reasoning + pattern_note + scope_suffix,
                status=SIGNAL_STATUS_CLASSIFIED,
                classified_at=int(time.time()),
                metadata_json=json.dumps(classification_meta),
            )
            classified += 1
            # Counted HERE, not at the scope lookup above. Incremented before
            # the try, it counted rows that then raised, and the summary said
            # "0/1 classified, 1 errors, 1 classified without a campaign ICP"
            # — 0 classified and 1 classified in one sentence. The number now
            # means what it says: rows that reached the classifier with an
            # empty ICP set AND came back with a verdict that was stored.
            if scope_note:
                unscoped += 1

        except asyncio.TimeoutError:
            # Stringifies to "", which is how this reached the scheduler as a
            # bare "TimeoutError" and was categorised as a network fault.
            logger.warning(
                "Classification of signal %s exceeded %.0fs and was dropped",
                signal_id[:8], SIGNAL_CLASSIFY_ROW_TIMEOUT_SECONDS,
            )
            errors += 1
        except Exception as e:
            logger.warning("Failed to classify signal %s: %s", signal_id[:8], e)
            errors += 1

    # Denominator is what the run ATTEMPTED, not what it selected: with a time
    # budget those differ, and "13/30 classified" with no errors reads as 17
    # silent failures. The rows it did not reach are named separately below.
    attempted = from_oldest + from_newest
    summary = f"Signal classification complete: {classified}/{attempted} classified"
    if windows_meet:
        # The two windows returned overlapping rows, so every row here is
        # simultaneously among the oldest and among the newest and attributing
        # it to an end is meaningless: a 15-row backlog was reported as "15
        # oldest-first, 0 newest-first", which an operator reads as "no fresh
        # signals were processed" — the opposite of what happened. Says only
        # what is known at selection time; whether the backlog is now empty is
        # the separate, freshly-counted clause at the end of the line.
        summary += " (the two ends met — one batch covered the whole backlog)"
    else:
        # Counts of what was REACHED from each end, which is the number that
        # says whether the interleave is working. Drawn-from-window counts
        # would print "20 oldest, 10 newest" on a run that only ever got to 13.
        summary += f" ({from_oldest} from the oldest end, {from_newest} from the newest)"
    if errors:
        summary += f", {errors} errors"
    if unscoped:
        # Precisely what was counted: rows that reached the classifier with an
        # empty ICP set and were then stored. Signal types that never consult
        # an ICP (profile changes), rows that reused a verdict, and rows that
        # raised are not in this number.
        summary += f", {unscoped} classified without a campaign ICP"
    if hit_call_bound or ran_out_of_time:
        # Success with work remaining, which is the run's normal shape: it is
        # reported by RETURNING, and returning is only possible because the run
        # ends itself. See SIGNAL_CLASSIFY_MAX_PER_RUN.
        reason = (
            f"at the {SIGNAL_CLASSIFY_MAX_PER_RUN}-classification bound"
            if hit_call_bound
            else f"after {SIGNAL_CLASSIFY_BUDGET_SECONDS:.0f}s"
        )
        summary += (
            f", stopped {reason} with "
            f"{len(signals) - attempted} of {len(signals)} selected rows "
            "left for the next run"
        )

    # Report the backlog every run. Without this the only way to see that the
    # old end was starving was to go and query the DB, which is why a
    # 2,433-signal backlog sat there being described as "27 unclassified".
    # Reachable only because of the time budget above: while the loop ran until
    # the scheduler cancelled the tick, this line was never built — 0 of the 60
    # classify runs in the live logs ever printed it.
    try:
        backlog, oldest_detected_at = await run_db(count_unclassified_signals)
    except Exception as e:
        logger.debug("Backlog count failed: %s", e)
    else:
        if backlog:
            age_days = (
                max(0.0, (time.time() - oldest_detected_at) / 86400.0)
                if oldest_detected_at else 0.0
            )
            summary += (
                f"; backlog {backlog} unclassified, oldest {age_days:.1f}d old"
            )
        else:
            summary += "; backlog clear"

    logger.info(summary)
    return summary
