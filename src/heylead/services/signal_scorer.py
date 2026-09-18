"""Service: signal_scorer — Weighted composite scoring per prospect with freshness decay.

Computes a 0.0-1.0 composite signal score for each prospect based on:
- Signal type weights (job_change=0.5, competitor=0.4, etc.)
- Signal confidence from classification
- Freshness decay (signals lose value over time)
- Best-signal-as-baseline with diminishing stacking from additional types

A single strong signal (e.g. competitor mention with high confidence) can
score high enough to trigger outreach on its own. Additional signal types
add supplementary value.

Scores are stored in the signal_accounts table for fast dashboard lookups.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..constants import (
    COMPOUND_INTENT_LOOKBACK_DAYS,
    COMPOUND_INTENT_MIN_TYPES,
    COMPOUND_INTENT_PATTERNS,
    SIGNAL_BAD_FIT,
    SIGNAL_COMMENTER_MATCH,
    SIGNAL_COMPANY_CHANGE,
    SIGNAL_COMPETITOR_MENTION,
    SIGNAL_COMPETITOR_USER,
    SIGNAL_FUNDING_EVENT,
    SIGNAL_HEADLINE_CHANGE,
    SIGNAL_HEADLINE_INTENT,
    SIGNAL_HIRING_SURGE,
    SIGNAL_JOB_CHANGE,
    SIGNAL_KEYWORD_MENTION,
    SIGNAL_NEWS_EVENT,
    SIGNAL_POST_ENGAGEMENT,
    SIGNAL_PROFILE_VIEW,
    SIGNAL_PROMOTION,
    SIGNAL_PROSPECT_POST,
    SIGNAL_RECENT_CHURN,
    SIGNAL_STATUS_CLASSIFIED,
    SIGNAL_WEIGHT_BAD_FIT,
    SIGNAL_WEIGHT_COMMENTER_MATCH,
    SIGNAL_WEIGHT_COMPANY_CHANGE,
    SIGNAL_WEIGHT_COMPETITOR_MENTION,
    SIGNAL_WEIGHT_COMPETITOR_USER,
    SIGNAL_WEIGHT_FUNDING_EVENT,
    SIGNAL_WEIGHT_HEADLINE_CHANGE,
    SIGNAL_WEIGHT_HEADLINE_INTENT,
    SIGNAL_WEIGHT_HIRING_SURGE,
    SIGNAL_WEIGHT_JOB_CHANGE,
    SIGNAL_WEIGHT_KEYWORD_MENTION,
    SIGNAL_WEIGHT_NEWS_EVENT,
    SIGNAL_WEIGHT_NEWS_ACQUISITION,
    SIGNAL_WEIGHT_NEWS_EXEC_HIRE,
    SIGNAL_WEIGHT_NEWS_EXPANSION,
    SIGNAL_WEIGHT_NEWS_FUNDING,
    SIGNAL_WEIGHT_NEWS_LAYOFFS,
    SIGNAL_WEIGHT_NEWS_PRODUCT_LAUNCH,
    SIGNAL_WEIGHT_POST_ENGAGEMENT,
    SIGNAL_WEIGHT_PROFILE_VIEW,
    SIGNAL_WEIGHT_PROMOTION,
    SIGNAL_WEIGHT_PROSPECT_POST,
    SIGNAL_WEIGHT_RECENT_CHURN,
)
from ..constants import (
    SIGNAL_COMPANY_FOLLOWER,
    SIGNAL_COMPANY_POST_COMMENT,
    SIGNAL_COMPANY_POST_REACTION,
    SIGNAL_NEWS_ACQUISITION,
    SIGNAL_NEWS_EXEC_HIRE,
    SIGNAL_NEWS_EXPANSION,
    SIGNAL_NEWS_FUNDING,
    SIGNAL_NEWS_LAYOFFS,
    SIGNAL_NEWS_PRODUCT_LAUNCH,
    SIGNAL_WEIGHT_COMPANY_FOLLOWER,
    SIGNAL_WEIGHT_COMPANY_POST_COMMENT,
    SIGNAL_WEIGHT_COMPANY_POST_REACTION,
)
from ..constants import (
    SIGNAL_WEBSITE_HIGH_INTENT,
    SIGNAL_WEBSITE_VISIT,
    SIGNAL_WEIGHT_WEBSITE_HIGH_INTENT,
    SIGNAL_WEIGHT_WEBSITE_VISIT,
)
from ..constants import (
    SIGNAL_COMPANY_HIRING_POST,
    SIGNAL_COMPANY_MILESTONE_POST,
    SIGNAL_COMPANY_TECH_STACK_POST,
    SIGNAL_COMPETITOR_POST_COMMENTER,
    SIGNAL_INDUSTRY_THREAD_PARTICIPANT,
    SIGNAL_POST_BUDGET_SIGNAL,
    SIGNAL_POST_NEGATIVE_EXPERIENCE,
    SIGNAL_POST_PAIN_POINT,
    SIGNAL_POST_SEEKING_RECS,
    SIGNAL_POST_TECH_EVALUATION,
    SIGNAL_PROSPECT_COMMENTS_ON_COMPETITOR,
    SIGNAL_REACTION_INSIGHTFUL_COMPETITOR,
    SIGNAL_REACTION_PATTERN,
    SIGNAL_RESHARES_COMPETITOR,
    SIGNAL_RESHARES_OUR_CONTENT,
    SIGNAL_WEIGHT_COMPANY_HIRING_POST,
    SIGNAL_WEIGHT_COMPANY_MILESTONE_POST,
    SIGNAL_WEIGHT_COMPANY_TECH_STACK_POST,
    SIGNAL_WEIGHT_COMPETITOR_POST_COMMENTER,
    SIGNAL_WEIGHT_INDUSTRY_THREAD_PARTICIPANT,
    SIGNAL_WEIGHT_POST_BUDGET_SIGNAL,
    SIGNAL_WEIGHT_POST_NEGATIVE_EXPERIENCE,
    SIGNAL_WEIGHT_POST_PAIN_POINT,
    SIGNAL_WEIGHT_POST_SEEKING_RECS,
    SIGNAL_WEIGHT_POST_TECH_EVALUATION,
    SIGNAL_WEIGHT_PROSPECT_COMMENTS_ON_COMPETITOR,
    SIGNAL_WEIGHT_REACTION_INSIGHTFUL_COMPETITOR,
    SIGNAL_WEIGHT_REACTION_PATTERN,
    SIGNAL_WEIGHT_RESHARES_COMPETITOR,
    SIGNAL_WEIGHT_RESHARES_OUR_CONTENT,
)
from ..constants import (
    SIGNAL_ATS_HIRING,
    SIGNAL_G2_REVIEW,
    SIGNAL_HN_MENTION,
    SIGNAL_LINKEDIN_SELLER,
    SIGNAL_REDDIT_MENTION,
    SIGNAL_VIRAL_POST,
    SIGNAL_COMMENT_MINE_MARKER,
    SIGNAL_WEIGHT_ATS_HIRING,
    SIGNAL_WEIGHT_G2_REVIEW,
    SIGNAL_WEIGHT_HN_MENTION,
    SIGNAL_WEIGHT_LINKEDIN_SELLER,
    SIGNAL_WEIGHT_NETWORK_HUB_CONTACT,
    SIGNAL_WEIGHT_REDDIT_MENTION,
    SIGNAL_WEIGHT_TARGET_COMPANY_CONTACT,
    SIGNAL_WEIGHT_TARGET_COMPANY_RECRUITER,
    SIGNAL_WEIGHT_VIRAL_POST,
    SIGNAL_WEIGHT_COMMENT_MINE_MARKER,
)

logger = logging.getLogger(__name__)

# Signal type → base weight mapping
SIGNAL_WEIGHTS: dict[str, float] = {
    SIGNAL_KEYWORD_MENTION: SIGNAL_WEIGHT_KEYWORD_MENTION,
    SIGNAL_PROSPECT_POST: SIGNAL_WEIGHT_PROSPECT_POST,
    SIGNAL_VIRAL_POST: SIGNAL_WEIGHT_VIRAL_POST,
    SIGNAL_COMMENT_MINE_MARKER: SIGNAL_WEIGHT_COMMENT_MINE_MARKER,
    # Curated job-search signals — hand-verified, so they outrank inferred ones
    "target_company_recruiter": SIGNAL_WEIGHT_TARGET_COMPANY_RECRUITER,
    "target_company_contact": SIGNAL_WEIGHT_TARGET_COMPANY_CONTACT,
    "network_hub_contact": SIGNAL_WEIGHT_NETWORK_HUB_CONTACT,
    SIGNAL_JOB_CHANGE: SIGNAL_WEIGHT_JOB_CHANGE,
    SIGNAL_COMPETITOR_MENTION: SIGNAL_WEIGHT_COMPETITOR_MENTION,
    SIGNAL_HIRING_SURGE: SIGNAL_WEIGHT_HIRING_SURGE,
    SIGNAL_FUNDING_EVENT: SIGNAL_WEIGHT_FUNDING_EVENT,
    SIGNAL_NEWS_EVENT: SIGNAL_WEIGHT_NEWS_EVENT,
    SIGNAL_PROFILE_VIEW: SIGNAL_WEIGHT_PROFILE_VIEW,
    SIGNAL_POST_ENGAGEMENT: SIGNAL_WEIGHT_POST_ENGAGEMENT,
    SIGNAL_COMMENTER_MATCH: SIGNAL_WEIGHT_COMMENTER_MATCH,
    SIGNAL_COMPANY_CHANGE: SIGNAL_WEIGHT_COMPANY_CHANGE,
    SIGNAL_PROMOTION: SIGNAL_WEIGHT_PROMOTION,
    SIGNAL_HEADLINE_CHANGE: SIGNAL_WEIGHT_HEADLINE_CHANGE,
    SIGNAL_HEADLINE_INTENT: SIGNAL_WEIGHT_HEADLINE_INTENT,
    # Granular news signals (Phase 1 intent expansion)
    SIGNAL_NEWS_FUNDING: SIGNAL_WEIGHT_NEWS_FUNDING,
    SIGNAL_NEWS_ACQUISITION: SIGNAL_WEIGHT_NEWS_ACQUISITION,
    SIGNAL_NEWS_EXEC_HIRE: SIGNAL_WEIGHT_NEWS_EXEC_HIRE,
    SIGNAL_NEWS_EXPANSION: SIGNAL_WEIGHT_NEWS_EXPANSION,
    SIGNAL_NEWS_PRODUCT_LAUNCH: SIGNAL_WEIGHT_NEWS_PRODUCT_LAUNCH,
    SIGNAL_NEWS_LAYOFFS: SIGNAL_WEIGHT_NEWS_LAYOFFS,
    # Company page engagement signals (Phase 2 intent expansion)
    SIGNAL_COMPANY_POST_COMMENT: SIGNAL_WEIGHT_COMPANY_POST_COMMENT,
    SIGNAL_COMPANY_POST_REACTION: SIGNAL_WEIGHT_COMPANY_POST_REACTION,
    SIGNAL_COMPANY_FOLLOWER: SIGNAL_WEIGHT_COMPANY_FOLLOWER,
    # Website visitor tracking signals (Phase 3 intent expansion)
    SIGNAL_WEBSITE_VISIT: SIGNAL_WEIGHT_WEBSITE_VISIT,
    SIGNAL_WEBSITE_HIGH_INTENT: SIGNAL_WEIGHT_WEBSITE_HIGH_INTENT,
    # Post content intent signals (Phase 5)
    SIGNAL_POST_PAIN_POINT: SIGNAL_WEIGHT_POST_PAIN_POINT,
    SIGNAL_POST_TECH_EVALUATION: SIGNAL_WEIGHT_POST_TECH_EVALUATION,
    SIGNAL_POST_BUDGET_SIGNAL: SIGNAL_WEIGHT_POST_BUDGET_SIGNAL,
    SIGNAL_POST_SEEKING_RECS: SIGNAL_WEIGHT_POST_SEEKING_RECS,
    SIGNAL_POST_NEGATIVE_EXPERIENCE: SIGNAL_WEIGHT_POST_NEGATIVE_EXPERIENCE,
    # Reaction type intelligence signals (Phase 5)
    SIGNAL_REACTION_INSIGHTFUL_COMPETITOR: SIGNAL_WEIGHT_REACTION_INSIGHTFUL_COMPETITOR,
    SIGNAL_REACTION_PATTERN: SIGNAL_WEIGHT_REACTION_PATTERN,
    # Repost/share intelligence signals (Phase 5)
    SIGNAL_RESHARES_COMPETITOR: SIGNAL_WEIGHT_RESHARES_COMPETITOR,
    SIGNAL_RESHARES_OUR_CONTENT: SIGNAL_WEIGHT_RESHARES_OUR_CONTENT,
    # Company post topic signals (Phase 5)
    SIGNAL_COMPANY_HIRING_POST: SIGNAL_WEIGHT_COMPANY_HIRING_POST,
    SIGNAL_COMPANY_MILESTONE_POST: SIGNAL_WEIGHT_COMPANY_MILESTONE_POST,
    SIGNAL_COMPANY_TECH_STACK_POST: SIGNAL_WEIGHT_COMPANY_TECH_STACK_POST,
    # Comment mining signals (Phase 5)
    SIGNAL_COMPETITOR_POST_COMMENTER: SIGNAL_WEIGHT_COMPETITOR_POST_COMMENTER,
    SIGNAL_INDUSTRY_THREAD_PARTICIPANT: SIGNAL_WEIGHT_INDUSTRY_THREAD_PARTICIPANT,
    SIGNAL_PROSPECT_COMMENTS_ON_COMPETITOR: SIGNAL_WEIGHT_PROSPECT_COMMENTS_ON_COMPETITOR,
    # Seller detection signal
    SIGNAL_LINKEDIN_SELLER: SIGNAL_WEIGHT_LINKEDIN_SELLER,
    # Off-LinkedIn watchlist web signals
    SIGNAL_REDDIT_MENTION: SIGNAL_WEIGHT_REDDIT_MENTION,
    SIGNAL_HN_MENTION: SIGNAL_WEIGHT_HN_MENTION,
    SIGNAL_G2_REVIEW: SIGNAL_WEIGHT_G2_REVIEW,
    SIGNAL_ATS_HIRING: SIGNAL_WEIGHT_ATS_HIRING,
    # Negative signals (penalty weights)
    SIGNAL_COMPETITOR_USER: SIGNAL_WEIGHT_COMPETITOR_USER,
    SIGNAL_RECENT_CHURN: SIGNAL_WEIGHT_RECENT_CHURN,
    SIGNAL_BAD_FIT: SIGNAL_WEIGHT_BAD_FIT,
}

# Divisor that maps the best single signal onto the 0.0-1.0 composite scale.
# It must stay a constant: the tier thresholds (0.65 auto-outreach, 0.40
# behavioural) are calibrated against it, so deriving it from the current
# maximum of SIGNAL_WEIGHTS would silently rescale every prospect's score the
# next time a heavier signal type is registered.
NORMALIZATION_CEILING = 0.60

# ── Runtime weight override cache (DB-backed, 5-min TTL) ──
_weight_override_cache: dict[str, float] = {}
_weight_override_ts: float = 0.0
_WEIGHT_CACHE_TTL = 300  # seconds


def _get_effective_weight(signal_type: str) -> float:
    """DB override > hardcoded constant. Cached with 5-min TTL."""
    global _weight_override_cache, _weight_override_ts
    now = time.time()
    if now - _weight_override_ts > _WEIGHT_CACHE_TTL:
        try:
            from ..db.signal_queries import get_weight_overrides
            _weight_override_cache = get_weight_overrides()
        except Exception:
            pass  # Use stale cache on DB error
        _weight_override_ts = now
    if signal_type in _weight_override_cache:
        return _weight_override_cache[signal_type]
    if signal_type not in SIGNAL_WEIGHTS:
        # Silence here is what let three high-value types sit at the floor
        # weight unnoticed while strangers outranked them.
        logger.warning(
            "Unregistered signal type %r scored at the default weight %.2f — "
            "add it to SIGNAL_WEIGHTS so it ranks deliberately",
            signal_type, 0.2,
        )
    return SIGNAL_WEIGHTS.get(signal_type, 0.2)


def invalidate_weight_cache() -> None:
    """Force cache refresh on next call (after optimization applies changes)."""
    global _weight_override_ts
    _weight_override_ts = 0.0


# Freshness decay thresholds (age_in_days → multiplier)
FRESHNESS_DECAY: list[tuple[int, float]] = [
    (1, 1.0),     # < 1 day: full value
    (3, 0.85),    # 1-3 days
    (7, 0.65),    # 3-7 days
    (14, 0.40),   # 7-14 days
    (30, 0.20),   # 14-30 days
]
# Beyond 30 days: 0.1 (nearly expired)
DECAY_FLOOR = 0.1



def compute_freshness_decay(detected_at: int, now: int | None = None) -> float:
    """Compute freshness decay multiplier for a signal.

    Args:
        detected_at: Unix timestamp when signal was detected.
        now: Current Unix timestamp (defaults to time.time()).

    Returns:
        Decay multiplier between DECAY_FLOOR and 1.0.
    """
    if now is None:
        now = int(time.time())

    age_days = max(0, (now - detected_at) / 86400)

    for threshold_days, multiplier in FRESHNESS_DECAY:
        if age_days <= threshold_days:
            return multiplier

    return DECAY_FLOOR


def compute_signal_score(signal: dict[str, Any], now: int | None = None) -> float:
    """Compute the weighted score for a single signal.

    Formula: weight * confidence * freshness_decay

    Args:
        signal: Signal dict from the DB.
        now: Current Unix timestamp.

    Returns:
        Score between 0.0 and 1.0.
    """
    signal_type = signal.get("signal_type", "")
    confidence = signal.get("confidence", 0.0) or 0.0
    detected_at = signal.get("detected_at", 0) or 0

    # Get base weight for signal type (DB override > hardcoded constant)
    weight = _get_effective_weight(signal_type)

    # Freshness decay
    decay = compute_freshness_decay(detected_at, now)

    # For signals with no confidence set, use a moderate default
    if not confidence:
        confidence = 0.5

    # Negative weights produce negative scores (penalties)
    return weight * confidence * decay


def update_signal_with_score(
    signal_id: str,
    *,
    signal_type: str = "",
    detected_at: int = 0,
    now: int | None = None,
    **fields: Any,
) -> None:
    """Persist signal fields together with a freshly computed signal_score.

    One UPDATE, so the stored score can never drift from the confidence it was
    computed from. Sync — call via ``await run_db(...)`` from async code:
    compute_signal_score reaches the DB through _get_effective_weight.
    """
    from ..db.signal_queries import update_signal

    try:
        fields["signal_score"] = compute_signal_score(
            {
                "signal_type": signal_type,
                "confidence": fields.get("confidence", 0.0),
                "detected_at": detected_at,
            },
            now,
        )
    except Exception as e:  # scoring must never block the classification write
        logger.warning("signal_score compute failed for %s: %s", signal_id[:8], e)
    update_signal(signal_id=signal_id, **fields)


# One-shot backfill for rows written before scores were persisted (issue #65).
# Keyed in settings so completed installs pay one settings read per startup.
SCORE_BACKFILL_FLAG = "signal_score_backfill_done_v1"


def backfill_signal_scores(max_rows: int = 50000) -> str:
    """Score existing rows whose signal_score was never written.

    Runs in sync context at startup (daemon.prepare_process and server.main),
    never on the event loop. Bounded: one SELECT of at most *max_rows* per
    call — no unbounded scan. The default covers the live backlog (31,953
    rows measured 2026-08-19) in a single startup pass. Idempotent: the computation is deterministic,
    rewriting a row yields the same value, and once a call drains the backlog
    a settings flag ends the scan for good.

    Returns a summary string.
    """
    from ..db.queries import get_setting, save_setting
    from ..db.signal_queries import bulk_set_signal_scores, list_unscored_signals

    if get_setting(SCORE_BACKFILL_FLAG):
        return "Signal score backfill already done."

    rows = list_unscored_signals(limit=max_rows)
    scored = 0
    if rows:
        now_ts = int(time.time())
        updates: list[tuple[float, str]] = []
        for row in rows:
            try:
                updates.append((compute_signal_score(row, now_ts), row["id"]))
            except Exception as e:
                logger.debug("Backfill score failed for %s: %s", row.get("id", "?")[:8], e)
        scored = bulk_set_signal_scores(updates)

    if len(rows) < max_rows:
        save_setting(SCORE_BACKFILL_FLAG, int(time.time()))
        summary = f"Signal score backfill complete: scored {scored} signals."
    else:
        summary = f"Signal score backfill: scored {scored} signals, more remain."
    if scored:
        logger.info(summary)
    return summary


def compute_prospect_signal_score(linkedin_id: str, now: int | None = None) -> dict[str, Any]:
    """Compute composite signal score for a prospect across all signal types.

    Formula:
    1. For each signal: score = weight * confidence * freshness_decay
    2. Best signal defines baseline, normalized so max single = ~0.80
    3. Additional signal types add diminishing value (50% of their score)

    Args:
        linkedin_id: The prospect's LinkedIn provider ID.
        now: Current Unix timestamp.

    Returns:
        Dict with composite_score, total_signals, signal_types, top_signal_type, details.
    """
    from ..db.signal_queries import list_signals

    if now is None:
        now = int(time.time())

    # Get all non-expired signals for this prospect
    signals = list_signals(linkedin_id=linkedin_id, limit=100)

    if not signals:
        return {
            "composite_score": 0.0,
            "total_signals": 0,
            "signal_types": [],
            "top_signal_type": "",
            "details": [],
        }

    # Compute individual scores
    signal_scores: list[dict[str, Any]] = []
    type_set: set[str] = set()
    type_max: dict[str, float] = {}

    for sig in signals:
        # Skip expired signals
        expires_at = sig.get("expires_at", 0) or 0
        if expires_at and expires_at < now:
            continue

        score = compute_signal_score(sig, now)
        sig_type = sig.get("signal_type", "")
        type_set.add(sig_type)

        # Track highest score per type
        if sig_type not in type_max or score > type_max[sig_type]:
            type_max[sig_type] = score

        signal_scores.append({
            "signal_id": sig.get("id", ""),
            "signal_type": sig_type,
            "score": score,
            "confidence": sig.get("confidence", 0),
            "detected_at": sig.get("detected_at", 0),
            "intent": sig.get("intent", ""),
        })

    if not signal_scores:
        return {
            "composite_score": 0.0,
            "total_signals": 0,
            "signal_types": [],
            "top_signal_type": "",
            "details": [],
        }

    # Separate positive and negative signals
    positive_types = {t: s for t, s in type_max.items() if s >= 0}
    negative_types = {t: s for t, s in type_max.items() if s < 0}

    # Composite score: best signal as baseline, additional types stack on top.
    # A single strong signal (e.g. competitor mention) should score high on its own.
    sorted_scores = sorted(positive_types.values(), reverse=True)
    best_score = sorted_scores[0] if sorted_scores else 0.0

    # Additional signal types add diminishing value (supplementary, not duplicative)
    additional = sum(sorted_scores[1:]) * 0.5

    # Normalize so best possible single signal (job_change=0.50, conf=1.0, fresh)
    # maps to ~0.80, leaving room for stacking to push toward 1.0
    normalized = min((best_score / NORMALIZATION_CEILING) * 0.80 + additional, 1.0)

    # Subtract negative signal penalties (floor at 0.0)
    penalty = sum(abs(s) for s in negative_types.values())
    normalized = max(0.0, normalized - penalty)

    # Find top signal type
    top_type = max(type_max, key=type_max.get) if type_max else ""

    return {
        "composite_score": round(normalized, 3),
        "total_signals": len(signal_scores),
        "signal_types": sorted(type_set),
        "top_signal_type": top_type,
        "details": sorted(signal_scores, key=lambda x: x["score"], reverse=True)[:10],
    }


def recompute_all_signal_accounts() -> str:
    """Recompute composite scores for all prospects in signal_accounts.

    Called periodically by the scheduler (e.g., every hour) or manually.

    Returns:
        Summary string of results.
    """
    from ..db.signal_queries import list_signal_accounts, upsert_signal_account

    accounts = list_signal_accounts(limit=500)
    if not accounts:
        return "No signal accounts to recompute."

    now = int(time.time())
    updated = 0

    for acct in accounts:
        linkedin_id = acct.get("linkedin_id", "")
        if not linkedin_id:
            continue

        result = compute_prospect_signal_score(linkedin_id, now)

        # Update signal_accounts table
        upsert_signal_account(
            linkedin_id=linkedin_id,
            prospect_name=acct.get("prospect_name", ""),
            company=acct.get("company", ""),
        )

        # Now update the composite score directly
        from ..db.signal_queries import _update_signal_account_score

        _update_signal_account_score(
            linkedin_id=linkedin_id,
            composite_score=result["composite_score"],
            total_signals=result["total_signals"],
            top_signal_type=result["top_signal_type"],
        )
        updated += 1

    summary = f"Recomputed scores for {updated} signal accounts"
    logger.info(summary)
    return summary


# ──────────────────────────────────────────────
# Compound Intent Detection
# ──────────────────────────────────────────────


def detect_compound_intent(linkedin_id: str, now: int | None = None) -> list[dict[str, Any]]:
    """Detect compound intent events from stacked signals for a single prospect.

    Scans all non-expired signals within the lookback window and matches
    against known compound patterns (e.g., job_change + hiring_surge →
    "new_leader_building"). Creates intent_events in the DB for new matches.

    Compound patterns (defined in constants.COMPOUND_INTENT_PATTERNS):
      - job_change + hiring_surge → "new_leader_building" (0.90)
      - competitor_mention + keyword_mention → "active_evaluation" (0.85)
      - prospect_post + commenter_match → "engaged_thought_leader" (0.70)
      - funding_event + hiring_surge → "growth_mode" (0.80)
      - job_change + keyword_mention → "new_role_exploring" (0.80)
      - job_change + competitor_mention → "new_role_evaluating" (0.85)
      - funding_event + keyword_mention → "funded_and_searching" (0.75)
      - competitor_mention + prospect_post → "vocal_evaluator" (0.80)

    Args:
        linkedin_id: The prospect's LinkedIn provider ID.
        now: Current Unix timestamp (defaults to time.time()).

    Returns:
        List of intent event dicts that were created (empty if no new events).
    """
    from ..db.signal_queries import (
        intent_event_exists,
        list_signals,
        save_intent_event,
    )

    if now is None:
        now = int(time.time())

    # Fetch signals within the lookback window
    cutoff = now - (COMPOUND_INTENT_LOOKBACK_DAYS * 86400)
    signals = list_signals(linkedin_id=linkedin_id, limit=200)

    # Filter to recent, non-expired signals
    recent_signals: list[dict[str, Any]] = []
    for sig in signals:
        detected_at = sig.get("detected_at", 0) or 0
        expires_at = sig.get("expires_at", 0) or 0
        if detected_at >= cutoff and (not expires_at or expires_at > now):
            recent_signals.append(sig)

    if not recent_signals:
        return []

    # Group signals by type
    by_type: dict[str, list[dict[str, Any]]] = {}
    for sig in recent_signals:
        sig_type = sig.get("signal_type", "")
        if sig_type:
            by_type.setdefault(sig_type, []).append(sig)

    # Need at least 2 distinct signal types for any compound event
    if len(by_type) < COMPOUND_INTENT_MIN_TYPES:
        return []

    # Check against known compound patterns
    prospect_types = set(by_type.keys())
    created_events: list[dict[str, Any]] = []

    for pattern_types, (event_type, score_boost) in COMPOUND_INTENT_PATTERNS.items():
        # Check if the prospect has all signal types in this pattern
        if not pattern_types.issubset(prospect_types):
            continue

        # Dedup: skip if this compound event was already created recently
        if intent_event_exists(linkedin_id, event_type):
            continue

        # Gather contributing signal IDs and types
        contributing_ids: list[str] = []
        contributing_types: list[str] = []
        company: str | None = None

        for sig_type in pattern_types:
            # Pick the highest-confidence signal of this type
            type_signals = by_type[sig_type]
            best_sig = max(
                type_signals,
                key=lambda s: (s.get("confidence", 0) or 0, s.get("detected_at", 0) or 0),
            )
            sig_id = best_sig.get("id", "")
            if sig_id:
                contributing_ids.append(sig_id)
            contributing_types.append(sig_type)

            # Extract company from any signal that has it
            if not company:
                meta = best_sig.get("metadata_json")
                if meta:
                    try:
                        import json
                        meta_dict = json.loads(meta)
                        company = meta_dict.get("company") or meta_dict.get("new_company")
                    except (ValueError, TypeError):
                        pass

        # Create the intent event
        event_id = save_intent_event(
            linkedin_id=linkedin_id,
            event_type=event_type,
            signal_ids=contributing_ids,
            signal_types=contributing_types,
            composite_score=score_boost,
            company=company,
        )

        event = {
            "id": event_id,
            "linkedin_id": linkedin_id,
            "event_type": event_type,
            "signal_ids": contributing_ids,
            "signal_types": contributing_types,
            "composite_score": score_boost,
            "company": company,
        }
        created_events.append(event)
        logger.info(
            "Compound intent detected: %s for %s (score=%.2f, signals=%s)",
            event_type, linkedin_id, score_boost, contributing_types,
        )

    return created_events


def detect_all_compound_intents() -> str:
    """Scan all signal accounts for compound intent events.

    Called periodically by the scheduler. Iterates through accounts
    with 2+ signal types and runs detect_compound_intent() on each.

    Returns:
        Summary string of results.
    """
    from ..db.signal_queries import list_signal_accounts

    accounts = list_signal_accounts(limit=500)
    if not accounts:
        return "No signal accounts to scan for compound intents."

    now = int(time.time())
    total_events = 0
    scanned = 0

    for acct in accounts:
        linkedin_id = acct.get("linkedin_id", "")
        if not linkedin_id:
            continue

        # Quick check: need at least 2 signals to form a compound
        total_signals = acct.get("total_signals", 0)
        if total_signals < COMPOUND_INTENT_MIN_TYPES:
            continue

        scanned += 1
        try:
            events = detect_compound_intent(linkedin_id, now)
            total_events += len(events)
        except Exception as e:
            logger.warning("Compound intent detection failed for %s: %s", linkedin_id, e)

    summary = (
        f"Scanned {scanned} accounts for compound intents, "
        f"created {total_events} new intent events"
    )
    logger.info(summary)
    return summary
