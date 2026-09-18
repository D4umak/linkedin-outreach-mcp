"""Signal activator — converts high-scoring signals into outreach actions.

The core engine that turns signal intelligence into revenue. Processes
classified signals and triggers contextual outreach based on composite
score thresholds:

Tier 1 (score >= 0.7 + outreach intent): Auto-create contact + outreach
Tier 2 (score >= 0.5 + boost intent): Add to best-matching campaign
Tier 3 (score >= 0.3, existing contact): Boost engagement priority
Below threshold: Mark as actioned, no outreach

All signal-triggered outreaches carry signal context that gets injected
into message generation for natural, contextual personalization.

Runs every 15 minutes via the scheduler.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from ..db.async_bridge import run_db
from .signal_linker import _match_best_campaign

logger = logging.getLogger(__name__)

# ── Runtime threshold override cache (DB-backed, 5-min TTL) ──
_threshold_override_cache: dict[str, float] = {}
_threshold_override_ts: float = 0.0
_THRESHOLD_CACHE_TTL = 300


def _get_effective_threshold(name: str, default: float) -> float:
    """DB override > constant. Cached with 5-min TTL."""
    global _threshold_override_cache, _threshold_override_ts
    now = time.time()
    if now - _threshold_override_ts > _THRESHOLD_CACHE_TTL:
        try:
            from ..db.signal_queries import get_threshold_overrides
            _threshold_override_cache = get_threshold_overrides()
        except Exception:
            pass
        _threshold_override_ts = now
    return _threshold_override_cache.get(name, default)


def invalidate_threshold_cache() -> None:
    """Force cache refresh on next call."""
    global _threshold_override_ts
    _threshold_override_ts = 0.0


STAMP_BACKFILL_FLAG = "signal_stamp_backfill_done_v1"
_PARKED_NEWS_ACTIONS = frozenset({"no_linkedin_id", "no_in_campaign_match"})
# Expected filters — mark the signal, do not write an unexecuted actions_log row.
_QUIET_PARK_REASONS = frozenset({
    "superseded",
    "below_threshold",
    "below_send_threshold",
    "no_linkedin_id",
    "no_in_campaign_match",
    "icp_mismatch",
    "behavioral_icp_mismatch",
})


def backfill_signal_stamps() -> str:
    """One-shot: stamp pending outreaches and re-fan-out parked company news.

    v0.10.270 could not rewrite rows already parked. Sync — call via
    ``await run_db(...)`` from async code, or from daemon startup.
    """
    from ..db.queries import get_setting, save_setting

    if get_setting(STAMP_BACKFILL_FLAG):
        return "Signal stamp backfill already done."

    stamped = _backfill_pending_stamps()
    attached = _backfill_parked_news()
    save_setting(STAMP_BACKFILL_FLAG, int(time.time()))
    summary = (
        f"Signal stamp backfill: {stamped} outreaches stamped, "
        f"{attached} news attached."
    )
    if stamped or attached:
        logger.info(summary)
    return summary


def _backfill_pending_stamps() -> int:
    from ..constants import CLASSIFIED_POST_HOOK_TYPES
    from ..db.queries import get_campaign
    from ..db.signal_queries import list_signals
    from ..db.schema import get_db

    db = get_db()
    rows = db.execute(
        """SELECT c.*, o.id AS outreach_id
           FROM contacts c
           JOIN outreaches o ON o.contact_id = c.id
           WHERE o.status = 'pending'
             AND (o.signal_id IS NULL OR o.signal_id = '')
             AND c.linkedin_id IS NOT NULL AND TRIM(c.linkedin_id) != ''""",
    ).fetchall()
    db.close()

    stamped = 0
    for contact in (dict(r) for r in rows):
        hooks = [
            s for s in list_signals(linkedin_id=contact["linkedin_id"], limit=50)
            if (s.get("signal_type") or "") in CLASSIFIED_POST_HOOK_TYPES
        ]
        if not hooks:
            continue
        winner = _pick_best_signal(hooks)
        campaign = get_campaign(contact.get("campaign_id") or "")
        below = _below_campaign_send_gate(campaign, contact.get("fit_score", 0) or 0)
        _stamp_pending_outreach(contact, winner["id"], fit_override=below)
        _write_signal_context(contact["id"], _build_signal_context(winner))
        stamped += 1
    return stamped


def _backfill_parked_news() -> int:
    from ..constants import (
        NEWS_SIGNAL_TYPES,
        SIGNAL_STATUS_ACTIONED,
        SIGNAL_STATUS_SKIPPED,
    )
    from ..db.queries import list_campaigns
    from ..db.signal_queries import list_signals, update_signal

    now = int(time.time())
    active = list_campaigns(status="active")
    attached = 0
    seen: set[str] = set()
    for status in (SIGNAL_STATUS_SKIPPED, SIGNAL_STATUS_ACTIONED):
        for sig in list_signals(status=status, limit=200):
            if sig["id"] in seen:
                continue
            if (sig.get("signal_type") or "") not in NEWS_SIGNAL_TYPES:
                continue
            if (sig.get("action_taken") or "") not in _PARKED_NEWS_ACTIONS:
                continue
            seen.add(sig["id"])
            result = _attach_news_sync(sig, _build_signal_context(sig), now, active)
            if result == "attached_to_company_contacts":
                attached += 1
            else:
                update_signal(
                    sig["id"],
                    status=SIGNAL_STATUS_SKIPPED,
                    action_taken=result,
                    actioned_at=now,
                )
    return attached


def _attach_news_sync(
    sig: dict[str, Any],
    signal_context: dict[str, Any],
    now: int,
    active_campaigns: list[dict[str, Any]],
) -> str:
    from ..constants import SIGNAL_STATUS_ACTIONED
    from ..db.queries import list_pending_contacts_at_company
    from ..db.signal_queries import update_signal

    metadata = _parse_metadata(sig)
    company = (
        metadata.get("company_name")
        or metadata.get("company")
        or sig.get("prospect_name")
        or ""
    )
    campaign_id = sig.get("campaign_id") or ""
    matches: list[dict[str, Any]] = []
    if campaign_id:
        matches = list_pending_contacts_at_company(company, campaign_id)
    else:
        for camp in active_campaigns:
            matches.extend(list_pending_contacts_at_company(company, camp["id"]))

    if not matches:
        return "no_in_campaign_match"

    for contact in matches:
        _stamp_pending_outreach(contact, sig["id"])
        _write_signal_context(contact["id"], signal_context)

    update_signal(
        sig["id"],
        status=SIGNAL_STATUS_ACTIONED,
        action_taken="attached_to_company_contacts",
        actioned_at=now,
    )
    return "attached_to_company_contacts"


async def _keep_global_only(
    sig: dict, linkedin_id: str, icp_score: float,
) -> None:
    """Below-threshold signals stay in the directory, not the campaign."""
    from ..db.global_contact_queries import upsert_global_contact

    await run_db(
        upsert_global_contact,
        linkedin_id=linkedin_id,
        name=sig.get("prospect_name") or "Unknown",
        title=sig.get("prospect_title") or "",
        company=_extract_company(sig),
        linkedin_url=f"https://www.linkedin.com/in/{linkedin_id}" if linkedin_id else "",
        fit_score=max(icp_score, 0.1),
        source="signal_discovery",
    )


async def _enroll_signal_prospect(
    campaign_id: str,
    sig: dict,
    linkedin_id: str,
    fit_score: float,
    signal_id: str,
    source_detail: str = "",
) -> tuple[str | None, str | None]:
    from ..db.queries import enroll_prospect, get_outreach

    outreach_id = await run_db(
        enroll_prospect,
        campaign_id,
        {
            "name": sig.get("prospect_name") or "Unknown",
            "title": sig.get("prospect_title") or "",
            "company": _extract_company(sig),
            "linkedin_url": (
                f"https://www.linkedin.com/in/{linkedin_id}" if linkedin_id else ""
            ),
            "linkedin_id": linkedin_id,
            "fit_score": fit_score,
        },
        source="signal_discovery",
        source_detail=source_detail,
        signal_id=signal_id,
    )
    if not outreach_id:
        return None, None
    row = await run_db(get_outreach, outreach_id)
    return outreach_id, (row or {}).get("contact_id")


def _below_campaign_send_gate(campaign: dict | None, fit_score: float) -> bool:
    """True when create_outreach would be skipped at send time for fit."""
    from ..constants import MIN_FIT_SCORE_THRESHOLD

    if not campaign:
        threshold = MIN_FIT_SCORE_THRESHOLD
    else:
        try:
            cfg = json.loads(campaign.get("config_json") or "{}")
            threshold = float(cfg.get("min_fit_score", MIN_FIT_SCORE_THRESHOLD))
        except (TypeError, ValueError, json.JSONDecodeError):
            threshold = MIN_FIT_SCORE_THRESHOLD
    return fit_score < threshold


async def _emit_signal_not_activated(
    sig: dict,
    skip_reason: str,
    *,
    score: float | None = None,
    campaign_id: str = "",
) -> None:
    """File log + actions_log when a signal is parked without outreach."""
    from ..db.queries import log_action
    from ..ops_log import log_signal_not_activated

    log_signal_not_activated(
        signal_id=sig.get("id") or "",
        linkedin_id=sig.get("linkedin_id") or "",
        campaign_id=campaign_id,
        skip_reason=skip_reason,
        score=score,
        signal_type=sig.get("signal_type") or "",
    )
    await run_db(
        log_action,
        "signal_not_activated",
        campaign_id=campaign_id or "",
        result="skipped",
        details={
            "signal_id": sig.get("id") or "",
            "linkedin_id": sig.get("linkedin_id") or "",
            "skip_reason": skip_reason,
            "score": score,
        },
    )


_NUMBER_TOKEN_RE = re.compile(
    r"\d+(?:[.,]\d+)?(?:\s*(?:[%x×]|x))?",
    re.IGNORECASE,
)


def _sanitize_engagement_hook(hook: str, content: str) -> str:
    """Drop number tokens in the hook that do not appear in the post."""
    if not hook or not content:
        return hook
    norm_content = content.replace("×", "x").replace("X", "x")

    def _keep(match: re.Match[str]) -> str:
        token = match.group(0)
        norm_token = token.replace("×", "x").replace("X", "x")
        if norm_token.lower() in norm_content.lower():
            return token
        return ""

    cleaned = _NUMBER_TOKEN_RE.sub(_keep, hook)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _is_classified_hook(sig: dict[str, Any], signal_context: dict[str, Any]) -> bool:
    from ..constants import CLASSIFIED_EVENT_HOOK_TYPES, CLASSIFIED_POST_HOOK_TYPES

    st = sig.get("signal_type") or ""
    if st in CLASSIFIED_POST_HOOK_TYPES:
        return True
    hook = str(signal_context.get("engagement_hook") or "").strip()
    return st in CLASSIFIED_EVENT_HOOK_TYPES and bool(hook)


def _is_irrelevant_signal(sig: dict[str, Any], signal_context: dict[str, Any]) -> bool:
    from ..constants import (
        SIGNAL_BEHAVIORAL_TYPES,
        SIGNAL_INTENT_NOT_RELEVANT,
        SIGNAL_PROSPECT_POST,
    )

    st = sig.get("signal_type") or ""
    if st in SIGNAL_BEHAVIORAL_TYPES:
        return False
    if st != SIGNAL_PROSPECT_POST:
        return False
    intent = sig.get("intent") or ""
    if intent == SIGNAL_INTENT_NOT_RELEVANT:
        return True
    if intent not in ("thought_leadership", "unknown", ""):
        return False
    keywords = signal_context.get("keywords_matched") or []
    hook = str(signal_context.get("engagement_hook") or "").strip()
    return not keywords and not hook


def _clears_intent_gate(
    sig: dict[str, Any],
    signal_context: dict[str, Any],
    intents: frozenset[str],
) -> bool:
    """Classified hook types are outreach-intent regardless of the LLM label."""
    if _is_classified_hook(sig, signal_context):
        return True
    return (sig.get("intent") or "") in intents


def _signal_rank_key(sig: dict[str, Any]) -> tuple:
    from ..constants import SIGNAL_TYPE_RANK

    st = sig.get("signal_type") or ""
    return (
        SIGNAL_TYPE_RANK.get(st, 40),
        -(float(sig.get("confidence") or 0)),
        -(float(sig.get("signal_score") or 0)),
    )


def _pick_best_signal(signals: list[dict[str, Any]]) -> dict[str, Any]:
    return min(signals, key=_signal_rank_key)


async def _mark_signal_parked(
    sig: dict,
    action_taken: str,
    now: int,
    *,
    status: str | None = None,
    score: float | None = None,
    campaign_id: str = "",
) -> None:
    from ..constants import SIGNAL_STATUS_DISMISSED, SIGNAL_STATUS_SKIPPED
    from ..db.signal_queries import update_signal

    parked_status = status or SIGNAL_STATUS_SKIPPED
    await run_db(
        update_signal,
        sig["id"],
        status=parked_status,
        action_taken=action_taken,
        actioned_at=now,
    )
    if parked_status != SIGNAL_STATUS_DISMISSED and action_taken not in _QUIET_PARK_REASONS:
        await _emit_signal_not_activated(
            sig, action_taken, score=score, campaign_id=campaign_id,
        )


def _stamp_pending_outreach(
    contact: dict[str, Any],
    signal_id: str,
    *,
    fit_override: bool = False,
    next_action: str | None = None,
) -> str | None:
    from ..constants import SIGNAL_FIT_OVERRIDE
    from ..db.queries import pending_outreach_for_contact, update_outreach

    row = pending_outreach_for_contact(contact.get("id") or "")
    if not row:
        return None
    updates: dict[str, Any] = {"signal_id": signal_id}
    if fit_override:
        updates["next_action"] = SIGNAL_FIT_OVERRIDE
    elif next_action:
        updates["next_action"] = next_action
    update_outreach(row["id"], **updates)
    return row["id"]


def _write_signal_context(contact_id: str, signal_context: dict[str, Any]) -> None:
    from ..db.queries import get_contact_analysis, save_contact_analysis

    existing = get_contact_analysis(contact_id) or {}
    existing["signal_context"] = signal_context
    pains = signal_context.get("pain_points") or []
    if pains:
        merged = list(set((existing.get("pain_points") or []) + list(pains)))
        existing["pain_points"] = merged[:5]
    if signal_context.get("engagement_hook"):
        existing["summary"] = signal_context["engagement_hook"]
    save_contact_analysis(contact_id, existing)


def _weaker_than_stamped(contact: dict[str, Any], sig: dict[str, Any]) -> bool:
    """True when this person already has a better trigger stamped."""
    from ..db.queries import pending_outreach_for_contact
    from ..db.signal_queries import get_signal

    row = pending_outreach_for_contact(contact.get("id") or "")
    prior_id = (row or {}).get("signal_id") or ""
    if not prior_id or prior_id == sig.get("id"):
        return False
    prior = get_signal(prior_id)
    if not prior:
        return False
    return _signal_rank_key(sig) > _signal_rank_key(prior)


def _apply_existing_contact_signal(
    contact: dict[str, Any],
    sig: dict[str, Any],
    signal_context: dict[str, Any],
    composite_score: float,
    now: int,
) -> str:
    """Stamp / write / override for someone already in a campaign.

    Returns the action_taken. Parks return below_send_threshold; the caller
    marks those skipped. Does not inflate fit to cheat the send gate.
    """
    from ..db.queries import get_campaign, update_contact

    del now  # kept so call sites match the old boost signature
    classified = _is_classified_hook(sig, signal_context)
    campaign = get_campaign(contact.get("campaign_id") or "")
    current_fit = contact.get("fit_score", 0) or 0
    below = _below_campaign_send_gate(campaign, current_fit)

    if classified:
        _stamp_pending_outreach(contact, sig["id"], fit_override=below)
        _write_signal_context(contact["id"], signal_context)
        if not below:
            boosted = min(current_fit + composite_score * 0.2, 1.0)
            if boosted > current_fit:
                update_contact(contact["id"], fit_score=boosted)
        return "priority_boosted_existing"

    if below:
        return "below_send_threshold"

    boosted = min(current_fit + composite_score * 0.2, 1.0)
    if boosted > current_fit:
        update_contact(contact["id"], fit_score=boosted)
    return "priority_boosted_existing"


async def _attach_news_to_company_contacts(
    sig: dict[str, Any],
    signal_context: dict[str, Any],
    now: int,
    active_campaigns: list[dict[str, Any]],
) -> str:
    return await run_db(
        _attach_news_sync, sig, signal_context, now, active_campaigns,
    )


async def _handle_existing_contact(
    existing_contact: dict[str, Any],
    sig: dict[str, Any],
    signal_context: dict[str, Any],
    composite_score: float,
    now: int,
) -> str:
    """Apply a signal to someone already in a campaign. Returns action_taken."""
    from ..constants import SIGNAL_STATUS_ACTIONED
    from ..db.signal_queries import update_signal

    if await run_db(_weaker_than_stamped, existing_contact, sig):
        await _mark_signal_parked(sig, "superseded", now)
        return "superseded"

    action = await run_db(
        _apply_existing_contact_signal,
        existing_contact, sig, signal_context, composite_score, now,
    )
    if action == "below_send_threshold":
        await _mark_signal_parked(
            sig, action, now,
            score=composite_score,
            campaign_id=existing_contact.get("campaign_id") or "",
        )
        return action
    await run_db(
        update_signal,
        sig["id"],
        status=SIGNAL_STATUS_ACTIONED,
        action_taken=action,
        actioned_at=now,
    )
    return action


async def activate_pending_signals() -> str:
    """Process classified signals and trigger appropriate outreach actions.

    For each classified signal:
    1. Compute prospect-level composite score
    2. Route by score tier → create outreach / add to campaign / boost priority
    3. Build signal context for message injection
    4. Update signal status to 'actioned' with action_taken

    Returns summary string.
    """
    from ..constants import (
        COMPANY_LEVEL_SIGNAL_TYPES,
        SIGNAL_AUTO_OUTREACH_THRESHOLD,
        SIGNAL_FIT_OVERRIDE,
        SIGNAL_BEHAVIORAL_AUTO_THRESHOLD,
        SIGNAL_BEHAVIORAL_TYPES,
        SIGNAL_BOOST_INTENTS,
        SIGNAL_BOOST_THRESHOLD,
        SIGNAL_HOT_SKIP_WARMUP_THRESHOLD,
        SIGNAL_ICP_MATCH_THRESHOLD,
        SIGNAL_OUTREACH_INTENTS,
        SIGNAL_PROFILE_VIEW,
        SIGNAL_STATUS_ACTIONED,
        SIGNAL_STATUS_CLASSIFIED,
        SIGNAL_STATUS_DISMISSED,
        SIGNAL_WARM_THRESHOLD,
    )
    from ..services.icp_match_scorer import compute_icp_match
    from ..db.queries import (
        list_campaigns,
        update_contact,
    )
    from ..db.signal_queries import (
        resolve_existing_contact,
        list_intent_events,
        list_signals,
        update_signal,
        upsert_signal_account,
    )
    from ..services.signal_scorer import (
        compute_prospect_signal_score,
        detect_all_compound_intents,
    )

    now = int(time.time())

    try:
        await run_db(backfill_signal_stamps)
    except Exception as exc:
        logger.warning("Signal stamp backfill failed: %s", exc)

    # Run compound intent detection before activation so stacked signals
    # produce intent events that inform scoring in the loop below.
    try:
        compound_summary = await run_db(detect_all_compound_intents)
        logger.debug("Compound intent pass: %s", compound_summary)
    except Exception as exc:
        logger.warning("Compound intent detection failed: %s", exc)

    # Fetch classified signals (not yet actioned)
    signals = await run_db(list_signals, status=SIGNAL_STATUS_CLASSIFIED, limit=30)

    # Fallback: pick up stale "new" signals (>30 min old) if classification missed them
    if not signals:
        from ..constants import SIGNAL_STATUS_NEW
        stale_cutoff = now - 1800  # 30 minutes
        new_signals = await run_db(list_signals, status=SIGNAL_STATUS_NEW, limit=15)
        signals = [
            s for s in new_signals
            if (s.get("detected_at", 0) or 0) < stale_cutoff
        ]
        if signals:
            logger.info(
                "Processing %d stale 'new' signals (classification may have failed)",
                len(signals),
            )

    if not signals:
        return "No signals to activate."

    # Get active campaigns for ICP matching. A campaign with discovery switched
    # off has been told not to acquire people automatically; that binds every
    # acquisition path, activation included, or signals quietly repopulate a
    # curated list. Filtering here also covers the matcher's fallbacks, which
    # otherwise reach for any active campaign at all.
    from ..flags import flag_enabled

    all_active = await run_db(list_campaigns, status="active")
    active_campaigns = []
    for camp in all_active:
        try:
            camp_cfg = json.loads(camp.get("config_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            camp_cfg = {}
        if not flag_enabled(camp_cfg, "enable_discovery"):
            continue
        if flag_enabled(camp_cfg, "connections_only", default=False):
            continue
        active_campaigns.append(camp)

    # Tier gates: the optimizer stores tuned thresholds in the DB, and
    # activation has to gate on those rather than on the compiled-in defaults,
    # or the tuning loop compounds off a number nothing reads.
    eff_auto_threshold = await run_db(
        _get_effective_threshold, "auto_outreach", SIGNAL_AUTO_OUTREACH_THRESHOLD
    )
    eff_boost_threshold = await run_db(
        _get_effective_threshold, "boost", SIGNAL_BOOST_THRESHOLD
    )
    eff_warm_threshold = await run_db(
        _get_effective_threshold, "warm", SIGNAL_WARM_THRESHOLD
    )

    # Track results
    outreaches_created = 0
    campaigns_added = 0
    priorities_boosted = 0
    below_threshold = 0
    skipped = 0

    selected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in signals:
        linkedin_id = raw.get("linkedin_id") or ""
        if not linkedin_id:
            selected.append(raw)
            continue
        if linkedin_id in seen_ids:
            continue
        seen_ids.add(linkedin_id)
        siblings = await run_db(
            list_signals,
            status=SIGNAL_STATUS_CLASSIFIED,
            linkedin_id=linkedin_id,
            limit=50,
        )
        by_id = {s["id"]: s for s in siblings}
        by_id[raw["id"]] = raw
        winner = _pick_best_signal(list(by_id.values()))
        for sibling in by_id.values():
            if sibling["id"] == winner["id"]:
                continue
            await _mark_signal_parked(sibling, "superseded", now)
            skipped += 1
        selected.append(winner)

    for sig in selected:
        signal_id = sig["id"]
        linkedin_id = sig.get("linkedin_id", "")
        intent = sig.get("intent", "unknown")
        confidence = sig.get("confidence", 0) or 0

        if not linkedin_id:
            if (sig.get("signal_type") or "") in COMPANY_LEVEL_SIGNAL_TYPES:
                news_ctx = _build_signal_context(sig)
                attached = await _attach_news_to_company_contacts(
                    sig, news_ctx, now, active_campaigns,
                )
                if attached == "attached_to_company_contacts":
                    priorities_boosted += 1
                else:
                    await _mark_signal_parked(sig, attached, now)
                    skipped += 1
                continue
            await _mark_signal_parked(sig, "no_linkedin_id", now)
            skipped += 1
            continue

        # Compute prospect-level composite score
        score_result = await run_db(compute_prospect_signal_score, linkedin_id, now)
        composite_score = score_result.get("composite_score", 0)

        # Apply compound intent boost: if the prospect has active intent events,
        # use the highest compound event score as a floor for the composite score.
        try:
            intent_events = await run_db(list_intent_events, linkedin_id=linkedin_id, active_only=True)
            if intent_events:
                best_intent = max(ie.get("composite_score", 0) for ie in intent_events)
                if best_intent > composite_score:
                    logger.debug(
                        "Compound intent boost for %s: %.2f → %.2f (%s)",
                        linkedin_id, composite_score, best_intent,
                        intent_events[0].get("event_type", ""),
                    )
                    composite_score = best_intent
        except Exception as exc:
            logger.debug("Intent event lookup failed for %s: %s", linkedin_id, exc)

        # Check if this person is already in a campaign
        existing_contact = await run_db(resolve_existing_contact, sig)

        # Build signal context for message injection
        signal_context = _build_signal_context(sig)

        if _is_irrelevant_signal(sig, signal_context):
            await _mark_signal_parked(
                sig, "not_relevant", now, status=SIGNAL_STATUS_DISMISSED,
            )
            skipped += 1
            continue

        # Skip signals where the engagement hook is empty — means we
        # couldn't build a specific enough message (e.g. company_change
        # with no new_company metadata).  Avoids embarrassing generics.
        if not signal_context.get("engagement_hook"):
            angle = signal_context.get("signal_angle", "")
            if angle in (
                "congrats_new_company",
                "congrats_promotion",
                "congrats_reconnect",
            ):
                await _mark_signal_parked(
                    sig, "insufficient_metadata", now,
                    status=SIGNAL_STATUS_DISMISSED,
                )
                skipped += 1
                logger.info(
                    "Skipping %s signal for %s: no engagement hook (angle=%s)",
                    sig.get("signal_type"), sig.get("prospect_name"), angle,
                )
                continue

        # Enrich context with compound intent if available
        try:
            if intent_events:
                top_event = intent_events[0]
                signal_context["compound_intent"] = {
                    "event_type": top_event.get("event_type", ""),
                    "score": top_event.get("composite_score", 0),
                    "signal_types": top_event.get("signal_types_list", []),
                }
                # Provide richer engagement hook based on compound pattern
                compound_hook = _build_compound_hook(top_event)
                if compound_hook:
                    signal_context["engagement_hook"] = compound_hook
        except Exception:
            pass  # intent_events may not be defined if lookup failed

        # ── Hot Signal Fast-Track ──
        # Certain signal+intent combos are so strong they should trigger outreach
        # regardless of composite score. E.g. a CTO posting "leaving Salesloft".
        signal_type = sig.get("signal_type", "")
        metadata = _parse_metadata(sig)

        if signal_type == "competitor_mention":
            mention_ctx = metadata.get("mention_context", "general")
            if mention_ctx in ("switching_from", "evaluating", "complaint"):
                composite_score = max(composite_score, eff_auto_threshold)
                logger.info(
                    "Hot fast-track: competitor %s (%s) for %s",
                    metadata.get("competitor_name", "?"), mention_ctx,
                    sig.get("prospect_name", "Unknown"),
                )
        elif signal_type in ("company_change", "promotion") and intent in SIGNAL_OUTREACH_INTENTS:
            composite_score = max(composite_score, eff_auto_threshold)
        elif signal_type == "headline_intent":
            intent_cats = metadata.get("intent_categories", [])
            if any(cat in ("hiring", "evaluating") for cat in intent_cats):
                composite_score = max(composite_score, eff_auto_threshold)
        # Backward compat for old job_change signals still in DB
        elif signal_type == "job_change" and intent in SIGNAL_OUTREACH_INTENTS:
            composite_score = max(composite_score, eff_auto_threshold)
        elif intent == "buying_signal" and confidence >= 0.75:
            composite_score = max(composite_score, eff_auto_threshold)

        # ── Experiment variant assignment ──
        # If an active signal threshold experiment exists for this campaign,
        # override thresholds based on assigned variant (control/treatment).
        exp_auto_threshold = eff_auto_threshold
        exp_boost_threshold = eff_boost_threshold
        exp_warm_threshold = eff_warm_threshold
        try:
            campaign_id_for_exp = (
                existing_contact.get("campaign_id")
                if existing_contact
                else sig.get("campaign_id")
            )
            if campaign_id_for_exp:
                variant, overrides = await run_db(_get_experiment_thresholds,
                    campaign_id_for_exp, signal_id,
                )
                if overrides:
                    exp_auto_threshold = overrides.get(
                        "auto_outreach", exp_auto_threshold,
                    )
                    exp_boost_threshold = overrides.get(
                        "boost", exp_boost_threshold,
                    )
                    exp_warm_threshold = overrides.get(
                        "warm", exp_warm_threshold,
                    )
                    # Tag signal with experiment variant
                    await run_db(update_signal, signal_id, experiment_variant=variant)
        except Exception:
            pass  # Experiment failure doesn't block activation

        if sig.get("signal_type") == SIGNAL_PROFILE_VIEW and existing_contact:
            meta = _parse_metadata(sig)
            if not meta.get("is_icp_match"):
                await _mark_signal_parked(
                    sig, "behavioral_icp_mismatch", now,
                    score=composite_score,
                    campaign_id=existing_contact.get("campaign_id") or "",
                )
                skipped += 1
                continue
            from ..db.queries import get_campaign, update_contact
            campaign = await run_db(
                get_campaign, existing_contact.get("campaign_id") or "",
            )
            current_fit = existing_contact.get("fit_score", 0) or 0
            below = _below_campaign_send_gate(campaign, current_fit)
            await run_db(
                _stamp_pending_outreach,
                existing_contact, signal_id, fit_override=below,
            )
            await run_db(_write_signal_context, existing_contact["id"], signal_context)
            if not below:
                boosted = min(current_fit + composite_score * 0.2, 1.0)
                if boosted > current_fit:
                    await run_db(update_contact, existing_contact["id"], fit_score=boosted)
            await run_db(
                update_signal,
                signal_id,
                status=SIGNAL_STATUS_ACTIONED,
                action_taken="priority_boosted_existing",
                actioned_at=now,
            )
            priorities_boosted += 1
            continue

        # ── Tier 1: Auto-outreach (score >= threshold + outreach intent) ──
        if (
            composite_score >= exp_auto_threshold
            and _clears_intent_gate(sig, signal_context, SIGNAL_OUTREACH_INTENTS)
        ):
            if existing_contact:
                action = await _handle_existing_contact(
                    existing_contact, sig, signal_context, composite_score, now,
                )
                if action == "priority_boosted_existing":
                    priorities_boosted += 1
                else:
                    skipped += 1
            else:
                # Find best-matching campaign via ICP overlap
                campaign_id = _match_best_campaign(sig, active_campaigns, fallback=True)
                if not campaign_id:
                    await _mark_signal_parked(
                        sig, "no_matching_campaign", now, score=composite_score,
                    )
                    skipped += 1
                    continue

                # ICP validation: score prospect against campaign ICP
                matched_camp = next(
                    (c for c in active_campaigns if c["id"] == campaign_id), None
                )
                icp_score = 0.3  # Neutral default when no campaign ICP
                if matched_camp:
                    icp_result = compute_icp_match(
                        {
                            "title": sig.get("prospect_title") or "",
                            "company": _extract_company(sig),
                            "linkedin_id": linkedin_id,
                        },
                        matched_camp.get("icp_json"),
                    )
                    icp_score = icp_result["icp_match_score"]
                    if icp_score < SIGNAL_ICP_MATCH_THRESHOLD:
                        await _mark_signal_parked(
                            sig, "icp_mismatch", now,
                            score=composite_score, campaign_id=campaign_id,
                        )
                        skipped += 1
                        logger.debug(
                            "Signal %s: ICP mismatch (score=%.2f) for %s",
                            signal_id, icp_score, linkedin_id,
                        )
                        continue

                # Re-read immediately before insert — the scan path can
                # enrol the same person between the earlier lookup and here.
                existing_contact = await run_db(resolve_existing_contact, sig)
                if existing_contact:
                    action = await _handle_existing_contact(
                        existing_contact, sig, signal_context, composite_score, now,
                    )
                    if action == "priority_boosted_existing":
                        priorities_boosted += 1
                    else:
                        skipped += 1
                    continue

                classified_hook = _is_classified_hook(sig, signal_context)
                below_gate = _below_campaign_send_gate(matched_camp, icp_score)
                if below_gate and not classified_hook:
                    await _keep_global_only(sig, linkedin_id, icp_score)
                    await _mark_signal_parked(
                        sig, "below_send_threshold", now,
                        score=icp_score, campaign_id=campaign_id,
                    )
                    skipped += 1
                    continue

                outreach_id, contact_id = await _enroll_signal_prospect(
                    campaign_id, sig, linkedin_id, max(icp_score, 0.1),
                    signal_id if classified_hook else "",
                )
                if not outreach_id or not contact_id:
                    skipped += 1
                    continue

                next_action = None
                if classified_hook:
                    await run_db(_write_signal_context, contact_id, signal_context)
                    if below_gate:
                        next_action = SIGNAL_FIT_OVERRIDE
                    elif composite_score >= SIGNAL_HOT_SKIP_WARMUP_THRESHOLD:
                        next_action = "signal_hot_skip_warmup"

                if next_action:
                    from ..db.queries import update_outreach
                    await run_db(update_outreach, outreach_id, next_action=next_action)

                await run_db(upsert_signal_account,
                    linkedin_id=linkedin_id,
                    prospect_name=sig.get("prospect_name"),
                    company=_extract_company(sig),
                )

                await run_db(update_signal,
                    signal_id,
                    status=SIGNAL_STATUS_ACTIONED,
                    action_taken="outreach_created",
                    actioned_at=now,
                )
                outreaches_created += 1

                logger.info(
                    "Signal activation: created outreach for %s (score=%.2f, signal=%s%s)",
                    sig.get("prospect_name", "Unknown"),
                    composite_score,
                    sig.get("signal_type", ""),
                    " [HOT-SKIP]" if next_action == "signal_hot_skip_warmup" else "",
                )

        # ── Tier 1b: Behavioral signals (profile_view, company_follower, etc.)
        # These express direct interest in YOU — bypass text-intent gate when ICP-matched.
        # Use lower threshold since behavioral signals inherently have weaker scoring
        # (no text content → confidence was historically low).
        elif (
            composite_score >= SIGNAL_BEHAVIORAL_AUTO_THRESHOLD
            and sig.get("signal_type") in SIGNAL_BEHAVIORAL_TYPES
            and not existing_contact
        ):
            campaign_id = _match_best_campaign(sig, active_campaigns, fallback=True)
            if not campaign_id:
                await _mark_signal_parked(
                    sig, "no_matching_campaign", now, score=composite_score,
                )
                skipped += 1
                continue

            # ICP validation — behavioral signals need stronger ICP match to compensate
            # for weaker intent signal.
            from ..constants import SIGNAL_BEHAVIORAL_ICP_THRESHOLD
            matched_camp = next(
                (c for c in active_campaigns if c["id"] == campaign_id), None
            )
            icp_score = 0.0
            if matched_camp:
                icp_result = compute_icp_match(
                    {
                        "title": sig.get("prospect_title") or "",
                        "company": _extract_company(sig),
                        "linkedin_id": linkedin_id,
                    },
                    matched_camp.get("icp_json"),
                )
                icp_score = icp_result["icp_match_score"]

            if icp_score < SIGNAL_BEHAVIORAL_ICP_THRESHOLD:
                await _mark_signal_parked(
                    sig, "behavioral_icp_mismatch", now,
                    score=composite_score, campaign_id=campaign_id,
                )
                skipped += 1
                continue

            # Re-read immediately before insert — scan can enrol first.
            existing_contact = await run_db(resolve_existing_contact, sig)
            if existing_contact:
                action = await _handle_existing_contact(
                    existing_contact, sig, signal_context, composite_score, now,
                )
                if action == "priority_boosted_existing":
                    priorities_boosted += 1
                else:
                    skipped += 1
                continue

            if _below_campaign_send_gate(matched_camp, icp_score):
                await _keep_global_only(sig, linkedin_id, icp_score)
                await _mark_signal_parked(
                    sig, "below_send_threshold", now,
                    score=icp_score, campaign_id=campaign_id,
                )
                skipped += 1
                continue

            outreach_id, contact_id = await _enroll_signal_prospect(
                campaign_id, sig, linkedin_id, max(icp_score, 0.1), signal_id,
            )
            if not outreach_id or not contact_id:
                skipped += 1
                continue

            await run_db(_write_signal_context, contact_id, signal_context)

            await run_db(upsert_signal_account,
                linkedin_id=linkedin_id,
                prospect_name=sig.get("prospect_name"),
                company=_extract_company(sig),
            )

            await run_db(update_signal,
                signal_id,
                status=SIGNAL_STATUS_ACTIONED,
                action_taken="behavioral_outreach_created",
                actioned_at=now,
            )
            outreaches_created += 1

            logger.info(
                "Signal activation: behavioral outreach for %s (score=%.2f, icp=%.2f, signal=%s)",
                sig.get("prospect_name", "Unknown"),
                composite_score,
                icp_score,
                sig.get("signal_type", ""),
            )

        # ── Tier 2: Add to campaign (score >= boost threshold + boost intent) ──
        # Strangers need a classified hook or a true outreach intent —
        # generic thought_leadership prospect_posts must not auto-enroll.
        elif (
            composite_score >= exp_boost_threshold
            and (
                (
                    existing_contact
                    and _clears_intent_gate(
                        sig, signal_context, SIGNAL_BOOST_INTENTS,
                    )
                )
                or (
                    not existing_contact
                    and _clears_intent_gate(
                        sig, signal_context, SIGNAL_OUTREACH_INTENTS,
                    )
                )
            )
        ):
            if existing_contact:
                action = await _handle_existing_contact(
                    existing_contact, sig, signal_context, composite_score, now,
                )
                if action == "priority_boosted_existing":
                    priorities_boosted += 1
                else:
                    skipped += 1
            else:
                campaign_id = _match_best_campaign(sig, active_campaigns, fallback=True)
                if not campaign_id:
                    await _mark_signal_parked(
                        sig, "no_matching_campaign", now, score=composite_score,
                    )
                    skipped += 1
                    continue

                # ICP validation for Tier 2 signals
                matched_camp = next(
                    (c for c in active_campaigns if c["id"] == campaign_id), None
                )
                icp_score = 0.3  # Neutral default when no campaign ICP
                if matched_camp:
                    icp_result = compute_icp_match(
                        {
                            "title": sig.get("prospect_title") or "",
                            "company": _extract_company(sig),
                            "linkedin_id": linkedin_id,
                        },
                        matched_camp.get("icp_json"),
                    )
                    icp_score = icp_result["icp_match_score"]
                    if icp_score < SIGNAL_ICP_MATCH_THRESHOLD:
                        await _mark_signal_parked(
                            sig, "icp_mismatch", now,
                            score=composite_score, campaign_id=campaign_id,
                        )
                        skipped += 1
                        continue

                # Re-read immediately before insert — scan can enrol first.
                existing_contact = await run_db(resolve_existing_contact, sig)
                if existing_contact:
                    action = await _handle_existing_contact(
                        existing_contact, sig, signal_context, composite_score, now,
                    )
                    if action == "priority_boosted_existing":
                        priorities_boosted += 1
                    else:
                        skipped += 1
                    continue

                classified_hook = _is_classified_hook(sig, signal_context)
                below_gate = _below_campaign_send_gate(matched_camp, icp_score)
                if below_gate and not classified_hook:
                    await _keep_global_only(sig, linkedin_id, icp_score)
                    await _mark_signal_parked(
                        sig, "below_send_threshold", now,
                        score=icp_score, campaign_id=campaign_id,
                    )
                    skipped += 1
                    continue

                outreach_id, contact_id = await _enroll_signal_prospect(
                    campaign_id, sig, linkedin_id, max(icp_score, 0.1),
                    signal_id if classified_hook else "",
                )
                if not outreach_id or not contact_id:
                    skipped += 1
                    continue

                if classified_hook:
                    await run_db(_write_signal_context, contact_id, signal_context)
                    if below_gate:
                        from ..db.queries import update_outreach
                        await run_db(
                            update_outreach, outreach_id,
                            next_action=SIGNAL_FIT_OVERRIDE,
                        )

                await run_db(upsert_signal_account,
                    linkedin_id=linkedin_id,
                    prospect_name=sig.get("prospect_name"),
                    company=_extract_company(sig),
                )

                await run_db(update_signal,
                    signal_id,
                    status=SIGNAL_STATUS_ACTIONED,
                    action_taken="campaign_added",
                    actioned_at=now,
                )
                campaigns_added += 1

                logger.info(
                    "Signal activation: added %s to campaign (score=%.2f, signal=%s)",
                    sig.get("prospect_name", "Unknown"),
                    composite_score,
                    sig.get("signal_type", ""),
                )

        # ── Tier 3: Boost existing (score >= warm threshold, already in campaign) ──
        elif composite_score >= exp_warm_threshold and existing_contact:
            action = await _handle_existing_contact(
                existing_contact, sig, signal_context, composite_score, now,
            )
            if action == "priority_boosted_existing":
                priorities_boosted += 1
            else:
                skipped += 1

        # ── Below threshold ──
        else:
            await _mark_signal_parked(
                sig, "below_threshold", now, score=composite_score,
            )
            below_threshold += 1

    # Build summary
    parts = []
    if outreaches_created:
        parts.append(f"{outreaches_created} outreaches created")
    if campaigns_added:
        parts.append(f"{campaigns_added} added to campaigns")
    if priorities_boosted:
        parts.append(f"{priorities_boosted} priorities boosted")
    if below_threshold:
        parts.append(f"{below_threshold} below threshold")
    if skipped:
        parts.append(f"{skipped} skipped")

    if not parts:
        return "No signals processed."

    return f"Signal activation: {', '.join(parts)}"


def _build_signal_context(signal: dict[str, Any]) -> dict[str, Any]:
    """Build signal context dict for injection into prospect_analysis.

    This dict gets stored in contact.analysis_json and is picked up
    by `_build_intelligence_text()` in message_generator.py to create
    natural, contextual outreach messages.
    """
    signal_type = signal.get("signal_type", "unknown")
    content = signal.get("content", "")
    intent = signal.get("intent", "unknown")
    detected_at = signal.get("detected_at", 0)
    metadata = _parse_metadata(signal)

    # Determine signal angle
    angle = _get_signal_angle(signal_type, intent, metadata)

    # The classifier writes a post-specific hook into metadata_json; it is
    # strictly better than any template, so it wins when present.
    hook = str(metadata.get("engagement_hook") or "").strip()
    if not hook:
        hook = _build_engagement_hook(signal_type, content, metadata, angle)
    hook = _sanitize_engagement_hook(hook, content)

    # Extract pain points from classifier output
    pain_points = []
    reasoning = signal.get("reasoning", "")
    if reasoning:
        # Classifier may have embedded pain points in reasoning
        pain_points = metadata.get("pain_points_detected", [])

    # Time ago
    now = int(time.time())
    age_seconds = max(0, now - detected_at) if detected_at else 0
    if age_seconds < 3600:
        detected_ago = f"{age_seconds // 60} minutes ago"
    elif age_seconds < 86400:
        detected_ago = f"{age_seconds // 3600} hours ago"
    else:
        detected_ago = f"{age_seconds // 86400} days ago"

    return {
        "signal_type": signal_type,
        "signal_summary": content[:200] if content else "",
        "engagement_hook": hook,
        "signal_angle": angle,
        "detected_ago": detected_ago,
        "pain_points": pain_points,
        "keywords_matched": metadata.get("keywords_matched", []),
        "DO_NOT": (
            "Don't quote their posts directly. "
            "Don't mention you 'saw' or 'noticed' or 'came across'. "
            "Reference the topic naturally as a shared interest."
        ),
    }


def apply_trigger_signal_context(
    analysis: dict[str, Any] | None,
    signal: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Rebuild analysis['signal_context'] from the outreach's trigger signal.

    The contact-level signal_context is overwritten by whichever of the
    prospect's signals last scored highest, so at send time it can describe a
    different post than the one activation acted on (21 Aug 2026 "Exciting
    News" incident). Callers that know the outreach's signal_id use this to
    make the message reference the true trigger. Returns the analysis
    untouched when there is no signal.
    """
    if not signal:
        return analysis
    merged = dict(analysis) if isinstance(analysis, dict) else {}
    merged["signal_context"] = _build_signal_context(signal)
    return merged


def _get_signal_angle(
    signal_type: str,
    intent: str,
    metadata: dict[str, Any],
) -> str:
    """Determine the outreach angle based on signal characteristics."""
    if signal_type == "company_change":
        return "congrats_new_company"

    if signal_type == "promotion":
        return "congrats_promotion"

    if signal_type == "headline_intent":
        intent_cats = metadata.get("intent_categories", [])
        if "hiring" in intent_cats:
            return "hiring_partnership"
        if "evaluating" in intent_cats:
            return "solution_alignment"
        if "building" in intent_cats:
            return "builder_connection"
        return "contextual_reference"

    if signal_type == "headline_change":
        return "contextual_reference"

    # Backward compat for old job_change signals
    if signal_type == "job_change":
        return "congrats_reconnect"

    if signal_type == "competitor_mention":
        mention_context = metadata.get("mention_context", "general")
        if mention_context in ("switching_from", "evaluating", "complaint"):
            return "competitive_displacement"
        return "contextual_reference"

    if signal_type == "keyword_mention" and intent == "pain_point":
        return "pain_point_alignment"

    if signal_type == "prospect_post" and intent == "buying_signal":
        return "buying_intent_response"

    if signal_type == "profile_view":
        # Phase 4: ICP-matching viewers get a more targeted angle
        if metadata.get("is_icp_match"):
            return "icp_profile_viewer"
        return "reciprocal_interest"

    if signal_type == "commenter_match":
        return "shared_engagement"

    if signal_type == "hiring_surge":
        return "growth_partnership"

    if signal_type in ("funding_event", "news_event"):
        return "milestone_congrats"

    # Granular news signal angles (Phase 1)
    if signal_type == "news_funding":
        return "congrats_funding"

    if signal_type == "news_acquisition":
        return "congrats_acquisition"

    if signal_type == "news_exec_hire":
        return "congrats_exec_hire"

    if signal_type == "news_expansion":
        return "congrats_expansion"

    if signal_type == "news_product_launch":
        return "congrats_product_launch"

    if signal_type == "news_layoffs":
        return "empathy_restructuring"

    # Company page engagement angles (Phase 2)
    if signal_type == "company_post_comment":
        return "company_page_engagement"

    if signal_type == "company_post_reaction":
        return "company_page_engagement"

    if signal_type == "company_follower":
        return "company_follower_outreach"

    # Website visitor tracking angles (Phase 3)
    if signal_type == "website_high_intent":
        return "website_high_intent_outreach"

    if signal_type == "website_visit":
        return "website_visitor_outreach"

    if signal_type == "reddit_mention":
        return "reddit_thread_reference"

    if signal_type == "hn_mention":
        return "hn_thread_reference"

    if signal_type == "g2_review":
        return "g2_review_reference"

    if signal_type == "ats_hiring":
        return "ats_hiring_reference"

    return "contextual_reference"


def _build_engagement_hook(
    signal_type: str,
    content: str,
    metadata: dict[str, Any],
    angle: str,
) -> str:
    """Build a suggested engagement hook for the message generator.

    Returns a natural conversation starter that references the signal
    without being creepy.
    """
    if angle == "congrats_new_company":
        new_title = metadata.get("new_title", "")
        new_company = metadata.get("new_company", "")
        if new_title and new_company:
            return f"Congrats on the {new_title} role at {new_company}!"
        elif new_company:
            return f"Congrats on joining {new_company}!"
        # No specific company/title → suppress (avoid embarrassing generic)
        return ""

    if angle == "congrats_promotion":
        new_title = metadata.get("new_title", "")
        company = metadata.get("company", "")
        if new_title and company:
            return f"Congrats on the {new_title} promotion at {company}!"
        elif new_title:
            return f"Congrats on the promotion to {new_title}!"
        # No specific title → suppress
        return ""

    if angle == "hiring_partnership":
        return "Sounds like you're growing the team — exciting times!"

    if angle == "solution_alignment":
        return "It sounds like you're rethinking your approach — always a great time to explore options"

    if angle == "builder_connection":
        return "Love the builder energy — would be great to connect"

    # Backward compat for old job_change signals
    if angle == "congrats_reconnect":
        new_title = metadata.get("new_title", "")
        new_company = metadata.get("new_company", "")
        if new_title and new_company:
            return f"Congrats on the {new_title} role at {new_company}!"
        elif new_company:
            return f"Congrats on joining {new_company}!"
        # No specific metadata → suppress
        return ""

    if angle == "competitive_displacement":
        competitor = metadata.get("competitor_name", "")
        if competitor:
            return f"Sounds like you're exploring alternatives to {competitor}"
        return "Sounds like you're evaluating new solutions"

    if angle == "pain_point_alignment":
        keyword = metadata.get("keyword", "")
        if keyword:
            return f"Noticed the conversation around {keyword} — it's a challenge many teams face"
        return "Your perspective on this challenge resonates"

    if angle == "buying_intent_response":
        # Extract a brief topic from content
        topic = _extract_topic(content)
        if topic:
            return f"Your take on {topic} aligns with what we're seeing in the market"
        return "Your insights on this topic really resonate"

    if angle == "icp_profile_viewer":
        company = metadata.get("viewer_company", "")
        if company:
            return f"Noticed you checked us out — {company} seems like a great fit"
        return "Noticed you've been checking us out — would love to connect"

    if angle == "reciprocal_interest":
        return "We seem to be in similar circles"

    if angle == "shared_engagement":
        return "Great discussion — your perspective stood out"

    if angle == "growth_partnership":
        company = metadata.get("company", "")
        if company:
            return f"Exciting growth at {company}!"
        return "Impressive team growth!"

    if angle == "milestone_congrats":
        return "Congrats on the milestone!"

    # Granular news engagement hooks (Phase 1)
    if angle == "congrats_funding":
        company = metadata.get("company_name", "")
        if company:
            return f"Congrats to {company} on the funding round — exciting times ahead!"
        return "Congrats on the funding round!"

    if angle == "congrats_acquisition":
        company = metadata.get("company_name", "")
        if company:
            return f"Big moves at {company} — transitions like this open up interesting opportunities"
        return "Big moves — transitions like this open up interesting opportunities"

    if angle == "congrats_exec_hire":
        company = metadata.get("company_name", "")
        if company:
            return f"Exciting leadership changes at {company} — new energy always drives fresh thinking"
        return "New leadership always brings fresh perspective"

    if angle == "congrats_expansion":
        company = metadata.get("company_name", "")
        if company:
            return f"Great to see {company} expanding — growth mode is always energizing"
        return "Growth mode is always energizing!"

    if angle == "congrats_product_launch":
        company = metadata.get("company_name", "")
        if company:
            return f"Saw {company}'s latest launch — interesting direction"
        return "Interesting product direction!"

    if angle == "empathy_restructuring":
        company = metadata.get("company_name", "")
        if company:
            return f"Transitions at {company} can be challenging — hope things stabilize soon"
        return "Transitions can be tough — hope things settle well"

    # Company page engagement hooks (Phase 2)
    if angle == "company_page_engagement":
        post_text = metadata.get("post_text", "")
        topic = _extract_topic(post_text) if post_text else ""
        if topic:
            return f"Your thoughts on {topic} resonated — great perspective"
        return "Great engagement on our recent post — would love to connect"

    if angle == "company_follower_outreach":
        return "Thanks for following us — always great to connect with people in the space"

    # Website visitor tracking hooks (Phase 3)
    if angle == "website_high_intent_outreach":
        page = metadata.get("page_url", "")
        company = metadata.get("company_name", "")
        if company:
            return f"Noticed {company} has been exploring our solutions — would love to chat"
        return "Looks like you've been checking us out — happy to answer any questions"

    if angle == "website_visitor_outreach":
        company = metadata.get("company_name", "")
        if company:
            return f"I see {company} in our space — thought it'd be great to connect"
        return "Came across your profile and thought we might have some synergies"

    if angle == "reddit_thread_reference":
        term = metadata.get("term", "")
        if term:
            return f"Saw the Reddit thread on {term}"
        return "Saw a relevant Reddit thread on this"

    if angle == "hn_thread_reference":
        term = metadata.get("term", "")
        if term:
            return f"Saw the HN thread on {term}"
        return "Saw a relevant HN thread on this"

    if angle == "g2_review_reference":
        term = metadata.get("term", "") or metadata.get("company_name", "")
        if term:
            return f"G2 thread on {term}"
        return "Saw a G2 thread in this category"

    if angle == "ats_hiring_reference":
        company = metadata.get("company_name", "") or metadata.get("term", "")
        title = metadata.get("title", "")
        if company and title:
            return f"They're hiring {title} on Greenhouse"
        if company:
            return f"They're hiring on a public job board at {company}"
        return "Saw an open role on a public job board"

    # Default contextual reference
    topic = _extract_topic(content)
    if topic:
        return f"Your work around {topic} caught my attention"
    return "Your background is impressive"


_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\U0001F1E6-\U0001F1FF"
    "\uFE0F\u200D"
    "]+"
)

# Announcement filler that opens posts but names no topic — templating it
# produced "Your work around Exciting News caught my attention" (21 Aug 2026).
_JUNK_TOPIC_RE = re.compile(
    r"^(?:(?:some|big|great|exciting|breaking|huge|amazing|personal)\s+)*"
    r"(?:news|announcement|update)$"
    r"|^(?:we(?:'|’)?re hiring|now hiring|thrilled to announce|"
    r"excited to announce|excited to share|proud to announce|drum ?roll)\b.*$",
    re.IGNORECASE,
)


def _clean_topic(candidate: str) -> str:
    """Strip emoji and dangling punctuation; '' if nothing usable remains."""
    cleaned = _EMOJI_RE.sub(" ", candidate)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t-–—:;,.!?…")
    if len(cleaned) < 4 or _JUNK_TOPIC_RE.match(cleaned):
        return ""
    return cleaned


def _extract_topic(content: str, _retry: bool = False) -> str:
    """Extract a brief topic phrase from signal content.

    Returns the first meaningful phrase (up to ~50 chars) or empty string.
    A sentence boundary must be followed by whitespace, so "$1.5B" is never
    cut to "$1"; a junk opener gives the text after it one chance before
    giving up entirely — a generic hook beats a nonsense one.
    """
    if not content:
        return ""
    content = content.strip()
    # First sentence boundary in position order
    for m in re.finditer(r"[.!?\n]", content[:80]):
        idx = m.start()
        if idx <= 10:
            continue
        if content[idx] != "\n":
            nxt = content[idx + 1 : idx + 2]
            if nxt and not nxt.isspace():
                continue  # mid-token punctuation, e.g. the "." in "$1.5B"
        topic = _clean_topic(content[:idx])
        if topic:
            return topic
        if not _retry:
            return _extract_topic(content[idx + 1 :], _retry=True)
        return ""
    if _retry:
        return ""
    # Fallback: first 50 chars at word boundary
    if len(content) > 50:
        truncated = content[:50]
        last_space = truncated.rfind(" ")
        if last_space > 20:
            return _clean_topic(truncated[:last_space])
    return _clean_topic(content[:50])


def _boost_existing_contact(
    contact: dict[str, Any],
    signal_context: dict[str, Any],
    composite_score: float,
    now: int,
) -> None:
    """Legacy wrapper — prefer _apply_existing_contact_signal."""
    _apply_existing_contact_signal(
        contact, {"id": "", "signal_type": ""}, signal_context, composite_score, now,
    )




def _extract_company(signal: dict[str, Any]) -> str:
    """Extract company name from signal metadata or content."""
    metadata = _parse_metadata(signal)
    company = metadata.get("new_company") or metadata.get("company") or ""
    if not company:
        # Try to extract from prospect title
        title = signal.get("prospect_title") or ""
        if " at " in title:
            company = title.split(" at ")[-1].strip()
    return company


def _parse_metadata(signal: dict[str, Any]) -> dict[str, Any]:
    """Safely parse signal metadata_json."""
    raw = signal.get("metadata_json", "")
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return {}


def _build_compound_hook(intent_event: dict[str, Any]) -> str:
    """Build an engagement hook tailored to a compound intent event.

    Compound events represent multiple layered buying signals, so the
    hook should reflect the richer context naturally.
    """
    event_type = intent_event.get("event_type", "")
    company = intent_event.get("company", "")

    hooks: dict[str, str] = {
        "new_leader_building": (
            f"Exciting times at {company} — building out the team!"
            if company
            else "Exciting to see the team growth!"
        ),
        "active_evaluation": (
            "Sounds like you're evaluating solutions in this space"
        ),
        "engaged_thought_leader": (
            "Your perspective in the conversation really stood out"
        ),
        "growth_mode": (
            f"Great momentum at {company}!"
            if company
            else "Impressive growth trajectory!"
        ),
        "new_role_exploring": (
            "Congrats on the new role — always interesting to hear "
            "how new leaders approach the first 90 days"
        ),
        "new_role_evaluating": (
            "Congrats on the new role — a fresh look at your tech stack "
            "can be a great early win"
        ),
        "funded_and_searching": (
            f"Congrats on the fundraise at {company}!"
            if company
            else "Congrats on the recent fundraise!"
        ),
        "vocal_evaluator": (
            "Your analysis of the market options is really insightful"
        ),
        "promoted_and_building": (
            f"Congrats on the promotion — building out the team at {company}!"
            if company
            else "Congrats on the promotion — exciting growth ahead!"
        ),
        "promoted_and_exploring": (
            "Congrats on the promotion — always a good time to reassess the toolstack"
        ),
        "active_market_participant": (
            "Sounds like you're actively exploring the space"
        ),
        "hiring_and_signaling": (
            f"Impressive growth at {company} — building out the team!"
            if company
            else "Impressive growth trajectory!"
        ),
    }

    return hooks.get(event_type, "")


def _get_experiment_thresholds(
    campaign_id: str,
    signal_id: str,
) -> tuple[str, dict[str, float] | None]:
    """Check for active signal threshold experiments and return variant + overrides.

    Uses a deterministic hash of signal_id to assign control/treatment variant.
    Returns ("control", None) if no experiment is active.
    """
    import hashlib
    import json as _json

    from ..db.schema import get_db

    db = get_db()
    row = db.execute(
        """SELECT id, control_config, treatment_config
           FROM signal_experiments
           WHERE campaign_id = ? AND status = 'running'
             AND experiment_type = 'threshold'
           LIMIT 1""",
        (campaign_id,),
    ).fetchone()
    db.close()

    if not row:
        return ("control", None)

    # Deterministic variant assignment: hash signal_id → even=control, odd=treatment
    h = int(hashlib.md5(signal_id.encode()).hexdigest(), 16)
    variant = "treatment" if h % 2 else "control"

    config_str = row["treatment_config"] if variant == "treatment" else row["control_config"]
    try:
        config = _json.loads(config_str)
    except (_json.JSONDecodeError, TypeError):
        return ("control", None)

    return (variant, config)
