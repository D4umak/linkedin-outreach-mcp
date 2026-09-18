"""Campaign prospect enrichment — continuously discover new prospects from all sources.

Proactively feeds active campaigns from 13 sources:

LinkedIn search (API), global contacts, below-threshold signals, job changers,
signal accounts, profile viewers, post authors, competitor commenters,
company page engagers, 1st-degree connections, post commenters on your content,
low-confidence inbound leads, and news event company resolution.

All sources produce a uniform prospect dict that flows through a single pipeline:
collect → dedup → score via compute_icp_match() → sort by fit_score → save.

The communication strategist then picks highest-scored prospects first for
engagement (ORDER BY fit_score DESC).

Runs every hour via the scheduler as JOB_CAMPAIGN_REFILL.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from typing import Any

from typing import NamedTuple

from ..constants import CAMPAIGN_TARGET_QUEUE_SIZE
from ..flags import flag_enabled
from ..db import aio as db
from ..db import queries
from ..db.async_bridge import run_db
from .cloud_sync import cloud_owns_outbound

logger = logging.getLogger(__name__)


class RefillOutcome(NamedTuple):
    """What one campaign's refill attempt did, and whether it looked at all.

    ``ran`` is the load-bearing field. The 24h cooldown used to be stamped after
    any zero return, including the paths that never went looking — discovery
    switched off, no LinkedIn account connected. A session that happened to be
    disconnected on the one tick a campaign came off cooldown therefore cost it
    a full further day, and switching discovery back on took up to a day to have
    any effect. The cooldown exists to stop us hammering LinkedIn, so only a
    search that actually happened should arm it.
    """

    added: int
    ran: bool
    reason: str = ""


# Why a campaign was passed over. Reported one by one rather than merged: the
# summary used to read "N skipped (cooldown/no ICP)" for five different
# situations, so an operator could not tell a campaign waiting twenty minutes
# from one whose ICP means it will never refill at all.
REASON_DISCOVERY_OFF = "discovery switched off"
REASON_NO_ACCOUNT = "no LinkedIn account connected"
REASON_COOLDOWN = "waiting on cooldown"
REASON_NO_ICP = "no usable ICP"
REASON_CLOUD = "left to the cloud scheduler"

# The job is granted 44s at best (_TICK_JOB_DEADLINE_SECONDS). A run that is
# still inside LinkedIn search when asyncio.wait_for fires has no return
# value: collected prospects are dropped, cursors are not written, and the
# next hour repeats page one. Stop ourselves with enough slack for one
# in-flight HTTP call, same sizing as SIGNAL_CLASSIFY_BUDGET_SECONDS.
REFILL_BUDGET_SECONDS = 30.0
_NEWS_MIN_REMAINING_SECONDS = 8.0
_SEARCH_MIN_REMAINING_SECONDS = 1.0
_REPAIR_MIN_REMAINING_SECONDS = 8.0
# One LinkedIn search page per campaign per job. Hourly cadence already
# exists; one page that commits beats five that never do.
_SEARCH_PAGES_PER_JOB = 1

# How long a segment stays "exhausted" before the search is allowed to walk it
# again. `refill_exhausted` used to be written once and never cleared, so the
# LinkedIn branch went dark for the life of the campaign — nobody who joined,
# changed job or updated a headline afterwards could ever be found, however
# empty the queue got. Weekly: long enough not to re-derive the same page on
# the hourly tick, short enough that a campaign recovers on its own.
REFILL_EXHAUSTED_RETRY_HOURS = 168


def _seconds_left(deadline: float) -> float:
    return deadline - time.monotonic()


# ──────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────


async def refill_campaigns(budget_seconds: float | None = None) -> str:
    """Proactively enrich all active campaigns with new prospects from all sources.

    Returns summary string.
    """
    from ..constants import (
        CAMPAIGN_REFILL_BATCH_SIZE,
        CAMPAIGN_REFILL_COOLDOWN_HOURS,
        CAMPAIGN_REFILL_MAX_PAGES,
        STATUS_ACTIVE,
    )

    if budget_seconds is None:
        budget_seconds = REFILL_BUDGET_SECONDS

    try:
        from .own_identity import park_own_account_outreaches
        parked = await run_db(park_own_account_outreaches)
        if parked:
            logger.info("Parked %d own-account outreach(es)", parked)
    except Exception as e:
        logger.warning("own-account park failed: %s", e)

    campaigns = await db.list_campaigns(status=STATUS_ACTIVE)
    if not campaigns:
        return "No active campaigns"

    enriched = 0
    found_nobody = 0
    total_added = 0
    passed_over: dict[str, int] = {}
    now = int(time.time())

    def _passed_over(reason: str) -> None:
        passed_over[reason] = passed_over.get(reason, 0) + 1

    for campaign in campaigns:
        campaign_id = campaign["id"]
        campaign_name = campaign.get("name", campaign_id[:8])

        try:
            config = json.loads(campaign.get("config_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            config = {}

        _clear_stale_exhaustion(config, now)

        from ..constants import MIN_FIT_SCORE_THRESHOLD
        from ..db.queries import delete_never_contacted_below_threshold
        try:
            min_fit = float(config.get("min_fit_score", MIN_FIT_SCORE_THRESHOLD))
        except (TypeError, ValueError):
            min_fit = MIN_FIT_SCORE_THRESHOLD
        try:
            purged = await run_db(
                delete_never_contacted_below_threshold, campaign_id, min_fit,
            )
            if purged:
                logger.info(
                    "Campaign %s: removed %d never-contacted rows below fit %.2f",
                    campaign_name, purged, min_fit,
                )
        except Exception as e:
            logger.warning(
                "Campaign %s: sendable-queue repair failed: %s", campaign_name, e,
            )

        # The cloud refills per campaign; this job is account-wide, so the
        # job-level ownership gate never sees it (every campaign_refill row is
        # queued with campaign_id=None). This loop is the only place that knows
        # which campaign it is about, so the stand-down belongs here.
        #
        # Empty queue is the cloud's too. Local refill used to fail open here
        # and that is the implicit takeover sending_host=cloud forbids.
        if await run_db(cloud_owns_outbound, campaign_id):
            _passed_over(REASON_CLOUD)
            continue

        # Cooldown — avoid hammering sources. A queue below target is
        # send-path work: ignore a stuck/future last_refill_at so we can enroll.
        last_refill = int(config.get("last_refill_at") or 0)
        if last_refill > now:
            last_refill = 0
        understocked = await run_db(
            _sendable_queue_below_target, campaign_id, min_fit, _target_depth(config),
        )
        if (
            not understocked
            and last_refill
            and now - last_refill < CAMPAIGN_REFILL_COOLDOWN_HOURS * 3600
        ):
            _passed_over(REASON_COOLDOWN)
            continue

        # Parse ICP
        try:
            icp = json.loads(campaign.get("icp_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            _passed_over(REASON_NO_ICP)
            continue

        if not icp.get("segments"):
            _passed_over(REASON_NO_ICP)
            continue

        try:
            outcome = await _enrich_campaign(
                campaign_id=campaign_id,
                campaign_name=campaign_name,
                icp=icp,
                config=config,
                max_pages=CAMPAIGN_REFILL_MAX_PAGES,
                batch_size=CAMPAIGN_REFILL_BATCH_SIZE,
                budget_seconds=budget_seconds,
            )
        except Exception as e:
            logger.warning("Enrichment failed for campaign %s: %s", campaign_name, e)
            continue

        if outcome.added > 0:
            enriched += 1
            total_added += outcome.added
            logger.info(
                "Campaign %s: added %d prospects via enrichment",
                campaign_name, outcome.added,
            )
        elif outcome.ran:
            found_nobody += 1
        elif outcome.reason:
            _passed_over(outcome.reason)
            continue

        if not outcome.ran:
            # Local collectors only. Do not stamp last_refill_at — the
            # cooldown exists to stop hammering LinkedIn, and the next
            # campaign in this job can still search.
            continue

        config["last_refill_at"] = now
        # Only the key this step owns: the config above was read before the
        # awaited enrichment, and writing all of it back reverted whatever
        # else had been saved meanwhile (heylead-api#482, same shape here).
        await run_db(queries.merge_campaign_config, campaign_id,
                     {"last_refill_at": now})
        # One searching campaign per job. Otherwise campaign 1 eats the
        # tick and the others never refill.
        break

    parts = []
    if enriched:
        parts.append(f"{enriched} campaigns enriched ({total_added} new prospects)")
    if found_nobody:
        parts.append(f"{found_nobody} searched, found nobody new")
    # Sorted so the same situation always reads the same way run to run.
    for reason, count in sorted(passed_over.items()):
        parts.append(f"{count} {reason}")
    return "; ".join(parts) or "No campaigns to enrich"


# ──────────────────────────────────────────────
# Core enrichment pipeline
# ──────────────────────────────────────────────


async def _enrich_campaign(
    campaign_id: str,
    campaign_name: str,
    icp: dict[str, Any],
    config: dict[str, Any],
    max_pages: int,
    batch_size: int,
    budget_seconds: float | None = None,
) -> RefillOutcome:
    """Collect prospects from all sources, dedup, score, and save the best ones.

    Local collectors enroll first so a LinkedIn search that overruns the tick
    cannot throw away people already in memory. ``ran`` is True only when a
    LinkedIn or news search page actually completed.
    """
    from ..linkedin import get_account_id, get_linkedin_client

    # People already in the campaign but missing an outreach row are enroll
    # work, not discovery. Do this before the LinkedIn client opens.
    enrolled = await _enroll_unenrolled_campaign_contacts(
        campaign_id, batch_size, config,
    )

    if not flag_enabled(config, "enable_discovery"):
        logger.info(
            "Campaign %s: discovery disabled — no prospects will be collected",
            campaign_name,
        )
        if enrolled:
            return RefillOutcome(enrolled, ran=False)
        return RefillOutcome(0, ran=False, reason=REASON_DISCOVERY_OFF)

    client = get_linkedin_client()
    account_id = await run_db(get_account_id)
    if not account_id:
        if enrolled:
            return RefillOutcome(enrolled, ran=False)
        return RefillOutcome(0, ran=False, reason=REASON_NO_ACCOUNT)

    deadline = time.monotonic() + (
        REFILL_BUDGET_SECONDS if budget_seconds is None else float(budget_seconds)
    )
    is_connections_only = flag_enabled(config, "connections_only", default=False)

    local_prospects: list[dict] = []

    # Tier A: Person-level data, ready to score
    tier_a = [lambda: _collect_from_global_contacts(campaign_id)]
    if not is_connections_only:
        tier_a += [
            lambda: _collect_from_below_threshold_signals(),
            lambda: _collect_from_job_changers(),
            lambda: _collect_from_signal_accounts(),
        ]
    for collector in tier_a:
        try:
            local_prospects += await collector()
        except Exception as e:
            logger.debug("Collector failed (non-critical): %s", e)

    # Tier B: Partial data, headline as title proxy.
    # Connections-only campaigns source from the connections table alone —
    # every other collector here surfaces people the user has never met.
    from .outreach_channel import exclude_connections_enabled
    excludes_connections = exclude_connections_enabled(config)
    # A campaign with exclude_connections on must not source candidates from
    # the connections table: the enrol gate refuses every one of them, and the
    # DB refill has no rejection memory, so it re-fetches and re-rejects the
    # same people on every run (9 Sep 2026).
    tier_b = [] if excludes_connections else [
        lambda: _collect_from_connections(account_id)
    ]
    if not is_connections_only:
        tier_b += [
            lambda: _collect_from_profile_viewers(),
            lambda: _collect_from_post_authors(),
            lambda: _collect_from_competitor_commenters(),
            lambda: _collect_from_company_engagers(),
            lambda: _collect_from_post_commenters(),
            lambda: _collect_from_low_confidence_inbound(),
        ]
    for collector in tier_b:
        try:
            local_prospects += await collector()
        except Exception as e:
            logger.debug("Collector failed (non-critical): %s", e)

    added = enrolled + await _score_dedup_save(
        campaign_id, local_prospects, icp, config, batch_size,
        client, account_id, is_connections_only, deadline,
    )

    searched = False
    if not is_connections_only:
        if _seconds_left(deadline) >= _NEWS_MIN_REMAINING_SECONDS:
            try:
                news_prospects = await _collect_from_news_events(
                    client, account_id, icp, deadline=deadline,
                )
                if news_prospects:
                    searched = True
                    added += await _score_dedup_save(
                        campaign_id, news_prospects, icp, config, batch_size,
                        client, account_id, is_connections_only, deadline,
                    )
            except Exception as e:
                logger.debug("News event collector failed (non-critical): %s", e)

        if (
            not flag_enabled(config, "refill_exhausted", default=False)
            and _seconds_left(deadline) >= _SEARCH_MIN_REMAINING_SECONDS
        ):
            try:
                search_result = await _search_linkedin(
                    client, account_id, icp, config, max_pages, is_connections_only,
                    deadline=deadline, campaign_id=campaign_id,
                )
                if isinstance(search_result, tuple):
                    search_prospects, page_ran = search_result
                else:
                    search_prospects, page_ran = search_result, bool(search_result)
                searched = searched or page_ran
                if search_prospects:
                    added += await _score_dedup_save(
                        campaign_id, search_prospects, icp, config, batch_size,
                        client, account_id, is_connections_only, deadline,
                    )
            except Exception as e:
                logger.debug("LinkedIn search failed (non-critical): %s", e)

    return RefillOutcome(added, ran=searched)


async def _score_dedup_save(
    campaign_id: str,
    prospects: list[dict],
    icp: dict[str, Any],
    config: dict[str, Any],
    batch_size: int,
    client: Any,
    account_id: str,
    is_connections_only: bool,
    deadline: float,
) -> int:
    """Dedup, score, repair ids if time remains, and enroll a batch."""
    from ..services.connection_sync import get_local_connection_ids
    from ..services.dedup_service import (
        dedup_prospects,
        get_all_known_linkedin_ids,
        get_enrolled_people,
        get_excluded_linkedin_ids,
    )
    from ..services.icp_match_scorer import compute_icp_match
    from ..services.provider_id_resolver import ensure_classic_provider_ids

    if not prospects:
        return 0

    seen: set[str] = set()
    unique: list[dict] = []
    for p in prospects:
        key = (
            p.get("linkedin_id") or p.get("public_id")
            or p.get("linkedin_url") or ""
        ).lower().strip()
        if key and key not in seen:
            raw_lid = (p.get("linkedin_id") or p.get("public_id") or "").strip()
            if raw_lid and raw_lid.isdigit():
                continue
            if raw_lid and not p.get("public_id"):
                p["public_id"] = raw_lid
            seen.add(key)
            unique.append(p)

    try:
        known_ids = await run_db(get_all_known_linkedin_ids)
        connection_ids = await run_db(get_local_connection_ids, account_id)
        excluded = await run_db(get_excluded_linkedin_ids)
        enrolled = await run_db(get_enrolled_people)
        from .own_identity import own_identity_tokens
        excluded = set(excluded) | await run_db(own_identity_tokens)
        if is_connections_only:
            unique = [
                p for p in unique
                if not ({
                    (p.get("public_id") or "").lower().strip(),
                    (p.get("linkedin_id") or "").lower().strip(),
                    (p.get("provider_id") or "").lower().strip(),
                    (p.get("linkedin_url") or "").lower().strip(),
                } - {""}) & (known_ids | excluded)
            ]
        else:
            unique, _ = dedup_prospects(
                unique, known_ids, connection_ids, exclusion_ids=excluded,
                enrolled_people=enrolled,
            )
    except Exception as e:
        logger.warning("Dedup failed (non-critical): %s", e)

    if not unique:
        return 0

    for prospect in unique:
        result = compute_icp_match(prospect, icp)
        prospect["fit_score"] = result["icp_match_score"]

    unique.sort(key=lambda p: p.get("fit_score", 0), reverse=True)

    existing = await db.get_contacts_for_campaign(campaign_id)
    existing_ids: set[str] = set()
    for c in existing:
        lid = (c.get("linkedin_id") or "").lower().strip()
        if lid:
            existing_ids.add(lid)
        try:
            blob = json.loads(c.get("profile_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            blob = {}
        if isinstance(blob, dict):
            for key in ("provider_id", "public_id", "public_identifier"):
                val = str(blob.get(key) or "").lower().strip()
                if val:
                    existing_ids.add(val)
    if existing_ids:
        unique = [
            p for p in unique
            if not {
                (p.get("linkedin_id") or "").lower().strip(),
                (p.get("public_id") or "").lower().strip(),
                (p.get("provider_id") or "").lower().strip(),
            } - {""} & existing_ids
        ]

    unique = _drop_below_send_threshold(unique, config)
    if not unique:
        return 0
    from .competitors import (
        competitor_names_from,
        drop_competitor_people,
        exclude_competitors_enabled,
    )
    if exclude_competitors_enabled(config):
        names = competitor_names_from(config, icp)
        if names:
            unique, dropped_comp = drop_competitor_people(unique, names)
            if dropped_comp:
                logger.info(
                    "Refill dropped %d competitor-company prospects",
                    len(dropped_comp),
                )
            if not unique:
                return 0

    to_save = unique[:batch_size]
    remaining = _seconds_left(deadline)
    max_lookups = 0
    if remaining >= _REPAIR_MIN_REMAINING_SECONDS:
        max_lookups = max(0, min(3, int(remaining / 3)))
    to_save = await ensure_classic_provider_ids(
        client, account_id, to_save, max_lookups=max_lookups,
    )
    return await _save_prospects(campaign_id, to_save)


async def _save_prospects(campaign_id: str, to_save: list[dict]) -> int:
    """Save prospects as contacts and enroll each as a pending outreach.

    Every enrollment writes an actions_log record: refill runs unattended, so
    silently-created recipients are otherwise invisible until row counts stop
    reconciling (21 untraced enrollments, 6 Aug 2026).
    """
    from ..db.queries import log_action
    from ..db.schema import get_db

    def _existing_oids() -> set[str]:
        db = get_db()
        rows = db.execute(
            "SELECT id FROM outreaches WHERE campaign_id = ?", (campaign_id,),
        ).fetchall()
        db.close()
        return {r["id"] for r in rows}

    already = await run_db(_existing_oids)
    added = 0
    for prospect in to_save:
        source_tag = prospect.pop("_source_tag", "enrichment")
        # Ensure provider_id from search results is persisted in profile_json
        # so DM send can use the correct ACoAAA-format ID.
        profile_json_str = prospect.get("profile_json", "")
        prov_id = prospect.get("provider_id", "")
        if prov_id and not profile_json_str:
            profile_json_str = json.dumps({"provider_id": prov_id})
        elif prov_id and profile_json_str:
            try:
                pj = json.loads(profile_json_str)
                if not pj.get("provider_id"):
                    pj["provider_id"] = prov_id
                    profile_json_str = json.dumps(pj)
            except (json.JSONDecodeError, TypeError):
                pass
        from ..db.queries import enroll_prospect

        prospect = {
            **prospect,
            "profile_json": profile_json_str,
            "linkedin_id": prospect.get("linkedin_id") or prospect.get("public_id", ""),
        }
        outreach_id = await run_db(
            enroll_prospect,
            campaign_id,
            prospect,
            source="auto_enrichment",
            source_detail=source_tag,
        )
        if not outreach_id or outreach_id in already:
            continue
        already.add(outreach_id)
        await run_db(
            log_action, "outreach_created",
            outreach_id=outreach_id,
            result="refill",
            campaign_id=campaign_id,
            details={
                "prospect": prospect.get("name", ""),
                "source": source_tag,
                "fit_score": prospect.get("fit_score", 0.0),
            },
        )
        added += 1

    return added


# ──────────────────────────────────────────────
# Tier A: Person-level, full data
# ──────────────────────────────────────────────


async def _collect_from_global_contacts(campaign_id: str) -> list[dict]:
    """Prospects already in global DB but not in this campaign."""
    # Get existing linkedin_ids in this campaign
    existing = await db.get_contacts_for_campaign(campaign_id)
    existing_ids = {
        (c.get("linkedin_id") or "").lower().strip()
        for c in existing
    } - {""}

    contacts = await db.search_global_contacts(
        lifecycle_stage="prospect", min_fit_score=0.1, limit=200,
    )
    results = []
    for c in contacts:
        lid = (c.get("linkedin_id") or "").lower().strip()
        if lid and lid not in existing_ids and not lid.isdigit():
            results.append({
                "name": c.get("name", ""),
                "title": c.get("title", ""),
                "company": c.get("company", ""),
                "linkedin_id": c.get("linkedin_id", ""),
                "linkedin_url": c.get("linkedin_url", ""),
                "location": c.get("location", ""),
                "profile_json": c.get("profile_json", ""),
                "_source_tag": "global_contacts",
            })
    return results


async def _collect_from_below_threshold_signals() -> list[dict]:
    """Signals that scored too low for one campaign may match another."""
    signals = []
    for status in ("skipped", "actioned"):
        signals.extend(await db.list_signals(status=status, limit=200))
    results = []
    for sig in signals:
        if sig.get("action_taken") != "below_threshold":
            continue
        lid = sig.get("linkedin_id", "")
        if not lid:
            continue
        meta = _parse_meta(sig)
        results.append({
            "name": sig.get("prospect_name", ""),
            "title": sig.get("prospect_title", ""),
            "company": meta.get("company", ""),
            "linkedin_id": lid,
            "linkedin_url": "",
            "_source_tag": "signal_reeval",
        })
    return results


async def _collect_from_job_changers() -> list[dict]:
    """People who recently changed jobs — new role, new budget."""
    from ..constants import SIGNAL_JOB_CHANGE

    signals = await db.list_signals(signal_type=SIGNAL_JOB_CHANGE, limit=100)
    results = []
    for sig in signals:
        lid = sig.get("linkedin_id", "")
        if not lid:
            continue
        meta = _parse_meta(sig)
        # Use new role data for scoring
        results.append({
            "name": sig.get("prospect_name", ""),
            "title": meta.get("new_title") or sig.get("prospect_title", ""),
            "company": meta.get("new_company") or meta.get("company", ""),
            "linkedin_id": lid,
            "linkedin_url": "",
            "_source_tag": "job_change",
        })
    return results


async def _collect_from_signal_accounts() -> list[dict]:
    """Aggregated multi-signal prospects with composite scores."""
    accounts = await db.list_signal_accounts(min_score=0.3, limit=200)
    results = []
    for acct in accounts:
        lid = acct.get("linkedin_id", "")
        if not lid:
            continue
        results.append({
            "name": acct.get("prospect_name", ""),
            "title": "",
            "company": acct.get("company", ""),
            "linkedin_id": lid,
            "linkedin_url": "",
            "_source_tag": "signal_account",
        })
    return results


# ──────────────────────────────────────────────
# Tier B: Partial data, headline as title proxy
# ──────────────────────────────────────────────


async def _collect_from_profile_viewers() -> list[dict]:
    """People who viewed your profile and match ICP."""
    from ..constants import SIGNAL_PROFILE_VIEW

    signals = await db.list_signals(signal_type=SIGNAL_PROFILE_VIEW, limit=100)
    results = []
    for sig in signals:
        lid = sig.get("linkedin_id", "")
        if not lid:
            continue
        meta = _parse_meta(sig)
        # Only include ICP-matched viewers
        if not meta.get("is_icp_match"):
            continue
        results.append({
            "name": sig.get("prospect_name", ""),
            "title": sig.get("prospect_title") or meta.get("viewer_title", ""),
            "company": meta.get("viewer_company", ""),
            "linkedin_id": lid,
            "linkedin_url": "",
            "_source_tag": "profile_viewer",
        })
    return results


async def _collect_from_post_authors() -> list[dict]:
    """Active LinkedIn authors posting about relevant topics."""
    authors = await db.list_post_authors(limit=200)
    results = []
    for author in authors:
        lid = author.get("linkedin_id", "")
        if not lid:
            continue
        # Only active authors (2+ posts seen)
        if (author.get("posts_seen") or 0) < 2:
            continue
        results.append({
            "name": author.get("name", ""),
            "title": author.get("headline", ""),  # headline as title proxy
            "company": author.get("company", ""),
            "linkedin_id": lid,
            "linkedin_url": "",
            "_source_tag": "post_author",
        })
    return results


async def _collect_from_competitor_commenters() -> list[dict]:
    """People commenting on competitor posts — potential buyers."""
    from ..constants import SIGNAL_COMPETITOR_POST_COMMENTER

    signals = await db.list_signals(signal_type=SIGNAL_COMPETITOR_POST_COMMENTER, limit=100)
    results = []
    for sig in signals:
        lid = sig.get("linkedin_id", "")
        if not lid:
            continue
        meta = _parse_meta(sig)
        results.append({
            "name": sig.get("prospect_name", ""),
            "title": sig.get("prospect_title") or meta.get("author_headline", ""),
            "company": meta.get("author_company", ""),
            "linkedin_id": lid,
            "linkedin_url": "",
            "_source_tag": "competitor_commenter",
        })
    return results


async def _collect_from_company_engagers() -> list[dict]:
    """People who engaged with your company's LinkedIn page."""
    from ..constants import (
        SIGNAL_COMPANY_FOLLOWER,
        SIGNAL_COMPANY_POST_COMMENT,
        SIGNAL_COMPANY_POST_REACTION,
    )

    results = []
    for signal_type in (
        SIGNAL_COMPANY_POST_COMMENT,
        SIGNAL_COMPANY_POST_REACTION,
        SIGNAL_COMPANY_FOLLOWER,
    ):
        signals = await db.list_signals(signal_type=signal_type, limit=50)
        for sig in signals:
            lid = sig.get("linkedin_id", "")
            if not lid:
                continue
            meta = _parse_meta(sig)
            results.append({
                "name": sig.get("prospect_name", ""),
                "title": sig.get("prospect_title") or meta.get("author_headline", ""),
                "company": meta.get("author_company", ""),
                "linkedin_id": lid,
                "linkedin_url": "",
                "_source_tag": "company_engager",
            })
    return results


async def _collect_from_connections(account_id: str) -> list[dict]:
    """1st-degree connections — warm leads, skip invitation step."""
    def _query_connections(acct_id: str) -> list[dict]:
        from ..db.schema import get_db
        conn = get_db()
        rows = conn.execute(
            "SELECT name, headline, provider_id, public_id FROM connections WHERE account_id = ?",
            (acct_id,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    rows = await run_db(_query_connections, account_id)

    results = []
    for r in rows:
        lid = r.get("provider_id") or r.get("public_id") or ""
        if not lid:
            continue
        results.append({
            "name": r.get("name", ""),
            "title": r.get("headline", ""),  # headline as title proxy
            "company": "",
            "linkedin_id": lid,
            "public_id": r.get("public_id", ""),
            "linkedin_url": "",
            "_source_tag": "connection",
        })
    return results


async def _collect_from_post_commenters() -> list[dict]:
    """People who commented on your published posts."""
    signals = await db.list_inbound_signals(signal_type="comment", limit=100)
    results = []
    for sig in signals:
        sid = sig.get("sender_id", "")
        if not sid:
            continue
        results.append({
            "name": sig.get("sender_name", ""),
            "title": sig.get("sender_headline", ""),
            "company": sig.get("sender_company", ""),
            "linkedin_id": sid,
            "linkedin_url": sig.get("sender_url", ""),
            "profile_json": sig.get("profile_json", ""),
            "_source_tag": "post_commenter",
        })
    return results


async def _collect_from_low_confidence_inbound() -> list[dict]:
    """Inbound leads that scored low but aren't spam — worth re-evaluating."""
    signals = await db.list_inbound_signals(status="qualified", limit=100)
    results = []
    for sig in signals:
        # Skip spam and high-confidence (already processed)
        if sig.get("intent") == "spam":
            continue
        conf = sig.get("confidence") or 0
        if conf >= 0.5:
            continue
        sid = sig.get("sender_id", "")
        if not sid:
            continue
        results.append({
            "name": sig.get("sender_name", ""),
            "title": sig.get("sender_headline", ""),
            "company": sig.get("sender_company", ""),
            "linkedin_id": sid,
            "linkedin_url": sig.get("sender_url", ""),
            "profile_json": sig.get("profile_json", ""),
            "_source_tag": "inbound_low_conf",
        })
    return results


# ──────────────────────────────────────────────
# Tier C: Company-level, needs LinkedIn search
# ──────────────────────────────────────────────


async def _collect_from_news_events(
    client: Any, account_id: str, icp: dict[str, Any],
    deadline: float | None = None,
) -> list[dict]:
    """Companies from news events (funding, acquisition, etc.) — resolve to people."""
    from ..constants import (
        SIGNAL_ATS_HIRING,
        SIGNAL_NEWS_ACQUISITION,
        SIGNAL_NEWS_EXEC_HIRE,
        SIGNAL_NEWS_EXPANSION,
        SIGNAL_NEWS_FUNDING,
        SIGNAL_NEWS_PRODUCT_LAUNCH,
    )

    # Collect unique company names from recent news and ATS-hiring signals
    companies: dict[str, str] = {}  # company_name → signal_type
    for sig_type in (
        SIGNAL_NEWS_FUNDING, SIGNAL_NEWS_ACQUISITION, SIGNAL_NEWS_EXEC_HIRE,
        SIGNAL_NEWS_EXPANSION, SIGNAL_NEWS_PRODUCT_LAUNCH,
        SIGNAL_ATS_HIRING,
    ):
        signals = await db.list_signals(signal_type=sig_type, limit=20)
        for sig in signals:
            meta = _parse_meta(sig)
            company = meta.get("company_name") or meta.get("company", "")
            if company and company not in companies:
                companies[company] = sig_type

    if not companies:
        return []

    # Extract target title from ICP for search
    segments = icp.get("segments", [])
    target_title = ""
    if segments:
        titles = segments[0].get("titles", [])
        target_title = titles[0] if titles else segments[0].get("keywords", "")

    # Search for people at each company (cap: 5 companies × 10 people)
    results = []
    for company_name in list(companies.keys())[:5]:
        if deadline is not None and _seconds_left(deadline) < _NEWS_MIN_REMAINING_SECONDS:
            break
        try:
            search = client.search_people(
                account_id=account_id,
                keywords=f"{target_title} {company_name}".strip(),
                count=10,
            )
            if deadline is not None:
                prospects, _ = await asyncio.wait_for(
                    search, timeout=max(0.1, _seconds_left(deadline)),
                )
            else:
                prospects, _ = await search
            for p in prospects:
                if not (p.get("public_id") or p.get("linkedin_url")):
                    continue
                p["_source_tag"] = f"news_{companies[company_name]}"
                results.append(p)
        except asyncio.TimeoutError:
            break
        except Exception as e:
            logger.debug("News company search failed for %s: %s", company_name, e)
            continue

    return results


# ──────────────────────────────────────────────
# LinkedIn search (existing source)
# ──────────────────────────────────────────────


async def _persist_search_progress(
    campaign_id: str, config: dict[str, Any], before: dict[str, Any] | None = None,
) -> None:
    """Write what this run changed, and nothing else.

    ``config`` was read before an awaited LinkedIn search, so writing the
    whole document back reverted any setting saved during it
    (heylead-api#482). ``before`` is the snapshot taken at that read; the
    difference against it is what goes to the database.

    A snapshot rather than a list of key names on purpose: the first version
    of this listed the keys it thought the search owned, guessed
    "refill_search_cursors" where the code writes "search_cursors", and
    silently stopped persisting the cursor at all.
    """
    if not campaign_id:
        return
    await run_db(
        queries.merge_campaign_config_delta, campaign_id,
        before if before is not None else {}, config,
    )


async def _search_linkedin(
    client: Any,
    account_id: str,
    icp: dict[str, Any],
    config: dict[str, Any],
    max_pages: int,
    is_connections_only: bool,
    deadline: float | None = None,
    campaign_id: str = "",
) -> tuple[list[dict], bool]:
    """Search LinkedIn for new prospects using campaign ICP with cursor resumption.

    At most one page per job. The cursor is written immediately so a later
    cancel cannot send the next hour back to page one. ``ran`` is True only
    when a search_people call completed.

    The snapshot below is what this function writes its difference against:
    the searches are awaited, and putting the whole config back afterwards
    reverted anything saved during them (heylead-api#482).
    """
    from ..linkedin.unipile import UnipileAuthError
    from ..services.search_account_resolver import resolve_search_account

    config_before = copy.deepcopy(config)

    del max_pages  # capped by _SEARCH_PAGES_PER_JOB below

    if deadline is not None and _seconds_left(deadline) < _SEARCH_MIN_REMAINING_SECONDS:
        return [], False

    search_acct_id = config.get("search_account_id") or account_id
    try:
        resolved_id, use_sales_nav = await resolve_search_account(client, account_id)
        if not config.get("search_account_id"):
            search_acct_id = resolved_id
    except Exception:
        use_sales_nav = False

    if deadline is not None and _seconds_left(deadline) < _SEARCH_MIN_REMAINING_SECONDS:
        return [], False

    segments = icp.get("segments", [])
    cursors = config.get("search_cursors", {})
    results_per_page = 100 if use_sales_nav else 50
    all_prospects: list[dict] = []
    pages_done = 0

    for seg_idx, segment in enumerate(segments):
        if pages_done >= _SEARCH_PAGES_PER_JOB:
            break
        seg_key = str(seg_idx)
        if cursors.get(seg_key) == "__exhausted__":
            continue

        cursor = cursors.get(seg_key)
        keywords, search_filters = _build_search_filters(
            segment, use_sales_nav, is_connections_only,
        )

        if deadline is not None and _seconds_left(deadline) < _SEARCH_MIN_REMAINING_SECONDS:
            break

        try:
            search = client.search_people(
                account_id=search_acct_id,
                keywords=keywords,
                count=results_per_page,
                use_sales_navigator=use_sales_nav,
                cursor=cursor,
                # Without this a transport error comes back as ([], None),
                # which reads as "no more results" and marks the segment
                # permanently exhausted. The except below breaks out
                # without touching the cursor, which is what we want.
                raise_on_error=True,
                **search_filters,
            )
            if deadline is not None:
                prospects, next_cursor = await asyncio.wait_for(
                    search, timeout=max(0.1, _seconds_left(deadline)),
                )
            else:
                prospects, next_cursor = await search
        except asyncio.TimeoutError:
            break
        except UnipileAuthError:
            logger.warning("Auth error during LinkedIn search")
            break
        except Exception as e:
            logger.warning("Search failed segment %d: %s", seg_idx, e)
            break

        pages_done += 1
        prospects = [
            p for p in prospects
            if p.get("public_id") or p.get("linkedin_url")
        ]
        for p in prospects:
            p["_source_tag"] = "linkedin_search"
        all_prospects.extend(prospects)

        if not next_cursor:
            cursors[seg_key] = "__exhausted__"
        else:
            cursors[seg_key] = next_cursor

        config["search_cursors"] = cursors
        if all(
            cursors.get(str(i)) == "__exhausted__" for i in range(len(segments))
        ):
            config["refill_exhausted"] = True
            # Dated, so _clear_stale_exhaustion can let it go again.
            config["refill_exhausted_at"] = int(time.time())
        await _persist_search_progress(campaign_id, config, config_before)
        break

    config["search_cursors"] = cursors
    return all_prospects, pages_done > 0


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


def _drop_below_send_threshold(
    prospects: list[dict], config: dict[str, Any],
) -> list[dict]:
    """Drop people generate_and_send would skip for fit.

    Refill used to save the top N scorers regardless of the campaign's
    min_fit_score. An empty-queue recovery then enrolled a batch the send
    gate immediately refused, so the campaign looked restocked and still
    sent nobody.
    """
    from ..constants import MIN_FIT_SCORE_THRESHOLD

    raw = config.get("min_fit_score", MIN_FIT_SCORE_THRESHOLD)
    try:
        threshold = float(raw)
    except (TypeError, ValueError):
        threshold = MIN_FIT_SCORE_THRESHOLD
    return [p for p in prospects if (p.get("fit_score") or 0) >= threshold]


def _clear_stale_exhaustion(config: dict[str, Any], now: int) -> bool:
    """Let a long-exhausted search start over. Mutates *config*.

    A missing ``refill_exhausted_at`` means the flag predates the stamp, so it
    is treated as stale rather than as "exhausted just now" — otherwise the
    campaigns the retry exists for are the ones it never reaches.
    """
    if not flag_enabled(config, "refill_exhausted", default=False):
        return False
    try:
        stamped = int(config.get("refill_exhausted_at") or 0)
    except (TypeError, ValueError):
        stamped = 0
    if stamped and now - stamped < REFILL_EXHAUSTED_RETRY_HOURS * 3600:
        return False

    config.pop("refill_exhausted", None)
    config.pop("refill_exhausted_at", None)
    config["search_cursors"] = {}
    return True


def _target_depth(config: dict[str, Any]) -> int:
    """How deep this campaign's sendable queue should be kept.

    A campaign may ask for a deeper bench than the default; anything
    unparseable or non-positive falls back rather than disabling top-ups.
    """
    try:
        target = int(config.get("target_queue_size") or CAMPAIGN_TARGET_QUEUE_SIZE)
    except (TypeError, ValueError):
        return CAMPAIGN_TARGET_QUEUE_SIZE
    return target if target > 0 else CAMPAIGN_TARGET_QUEUE_SIZE


def _sendable_queue_below_target(
    campaign_id: str, min_fit: float, target: int,
) -> bool:
    """True when the campaign holds less sendable stock than it should."""
    from ..db.queries import count_sendable_queue

    return count_sendable_queue(campaign_id, min_fit_score=min_fit) < target


def _list_contacts_without_outreach(campaign_id: str) -> list[dict]:
    """Campaign contacts that never got an outreach row."""
    from ..db.schema import get_db

    conn = get_db()
    rows = conn.execute(
        """SELECT c.name, c.title, c.company, c.linkedin_id, c.linkedin_url,
                  c.profile_json, c.fit_score
           FROM contacts c
           WHERE c.campaign_id = ?
             AND NOT EXISTS (
                 SELECT 1 FROM outreaches o
                 WHERE o.contact_id = c.id AND o.campaign_id = ?
             )
           ORDER BY c.fit_score DESC""",
        (campaign_id, campaign_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def _enroll_unenrolled_campaign_contacts(
    campaign_id: str,
    batch_size: int,
    config: dict[str, Any],
) -> int:
    """Create outreach rows for people already in the campaign."""
    from ..constants import MIN_FIT_SCORE_THRESHOLD

    contacts = await run_db(_list_contacts_without_outreach, campaign_id)
    if not contacts:
        return 0
    try:
        min_fit = float((config or {}).get("min_fit_score", MIN_FIT_SCORE_THRESHOLD))
    except (TypeError, ValueError):
        min_fit = MIN_FIT_SCORE_THRESHOLD
    prospects: list[dict] = []
    for c in contacts:
        try:
            fit = float(c.get("fit_score") or 0)
        except (TypeError, ValueError):
            fit = 0.0
        if fit < min_fit:
            continue
        prospects.append({
            "name": c.get("name", ""),
            "title": c.get("title", ""),
            "company": c.get("company", ""),
            "linkedin_id": c.get("linkedin_id", ""),
            "linkedin_url": c.get("linkedin_url", ""),
            "profile_json": c.get("profile_json", ""),
            "fit_score": fit,
            "_source_tag": "campaign_contacts",
        })
        if len(prospects) >= batch_size:
            break
    if not prospects:
        return 0
    return await _save_prospects(campaign_id, prospects)


def _count_pending_outreaches(campaign_id: str) -> int:
    """How many outreaches this campaign can still send. Sync: via run_db."""
    from ..db.schema import get_db

    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM outreaches "
        "WHERE campaign_id = ? AND status = 'pending'",
        (campaign_id,),
    ).fetchone()
    conn.close()
    return int(row["n"] if row else 0)


def _parse_meta(sig: dict[str, Any]) -> dict[str, Any]:
    """Parse metadata_json from a signal row."""
    raw = sig.get("metadata_json", "")
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return {}


def _build_search_filters(
    segment: dict[str, Any],
    use_sales_nav: bool,
    connections_only: bool,
) -> tuple[str, dict[str, Any]]:
    """Build search keywords and filters from an ICP segment."""
    keywords = segment.get("keywords", "")
    titles = segment.get("titles", [])
    has_structured = segment.get("has_structured", False)

    search_keywords = keywords
    if not has_structured and titles and titles[0].lower() not in keywords.lower():
        search_keywords = f"{titles[0]} {keywords}"

    search_filters: dict[str, Any] = {}
    if has_structured or segment.get("industry_codes"):
        if segment.get("industry_codes"):
            search_filters["industry_codes"] = segment["industry_codes"]
        if segment.get("location_codes"):
            search_filters["location_codes"] = segment["location_codes"]

        if use_sales_nav:
            for key, param in [
                ("title_codes", "role_codes"), ("seniority", "seniority"),
                ("company_headcount", "company_headcount"), ("company_types", "company_types"),
                ("department_codes", "department_codes"), ("tenure", "tenure"),
                ("spotlight", "spotlight"), ("annual_revenue", "annual_revenue"),
                ("company_headcount_growth", "company_headcount_growth"),
            ]:
                if segment.get(key):
                    search_filters[param] = segment[key]
            if segment.get("boolean_keywords"):
                search_keywords = segment["boolean_keywords"]
            elif search_filters.get("role_codes"):
                search_keywords = ""
        else:
            title_kw = " OR ".join(titles[:3]) if titles else ""
            if title_kw:
                search_keywords = title_kw
            elif search_filters.get("industry_codes"):
                search_keywords = ""

    if connections_only:
        search_filters["network_distance"] = [1]

    return search_keywords, search_filters


# ──────────────────────────────────────────────
# Profile enrichment for contacts with empty profile_json
# ──────────────────────────────────────────────

ENRICH_BATCH_SIZE = 50

# Per-run LOOKUP budgets. Two corrections over the sizing these replace, both
# measured on the live install rather than derived (see
# tests/test_backfill_profiles_fits_its_job_timeout.py for the numbers):
#
#   1. The job is granted 44s, not the "~45s" the old comment assumed.
#      _process_ready_jobs clamps to int(deadline - time.monotonic()), and the
#      failures in the log read "failed in 44003ms". 44 is also the BEST case —
#      a job that waited on that function's semaphore is clamped against a
#      deadline which has already moved, so it gets less. Hence the margin.
#   2. A lookup costs the pause PLUS the call. The old sum counted 16 lookups
#      at ENRICH_DELAY_SECONDS and gave get_profile itself a cost of zero:
#      "12 + 4 lookups = 40s worst case". Over 959 calls the call measures
#      median 1.49s / p90 1.77s / p99 2.19s, so a paced lookup is ~4.0s and
#      those same 16 come to ~75s — against 44.
#
# 6 lookups at p99 (2.2s) with 5 pauses of 2.5s is 25.7s, inside the 30s
# self-stop below. Raise the drain rate by running the job MORE OFTEN rather
# than by looking up more per run: per-run is capped by the job timeout,
# frequency is not.
#
# These bound LOOKUPS, not selected rows. Rows that cost no call — company
# pages — must keep flowing once the bound is spent, or a head of the queue
# full of them (38 of the 446 unenriched rows today, and they are never
# cleared) drains nothing at all. Independent budgets so the flag pass can
# never be starved by the empty-profile backlog.
_EMPTY_PASS_BUDGET = 4
_FLAG_PASS_BUDGET = 2

# How many rows each pass SELECTS to find those lookups in. Larger than the
# lookup budget precisely because skipped rows do not spend it.
_EMPTY_PASS_SELECT = 50
_FLAG_PASS_SELECT = 25

# Backstop for the counts above, for when latency is worse than they are sized
# against. It is not the normal exit — the count is — and it must stay far
# enough under the 44s a job is given to cover the last lookup the loop is
# allowed to start, which can begin just under this and then run to
# _BACKFILL_LOOKUP_TIMEOUT_SECONDS. 30 + 8 = 38 against 44.
#
# A run that reaches this ends by RETURNING its summary. That is what makes
# success-with-remaining-work reportable at all: a job killed by the engine's
# asyncio.wait_for has no return value, so the rows it already committed are
# filed as "[network_error]: TimeoutError" and the queue retries work that was
# never lost. See SIGNAL_CLASSIFY_BUDGET_SECONDS, fixed for this same reason.
_BACKFILL_BUDGET_SECONDS = 30.0

# Cap on a single lookup. Without one a stalled call blows the job's budget on
# its own however the run is bounded: backend_client allows a 30s read and
# _retry_request repeats it 3 times, and a 13.3s profile fetch is already in
# the measured record. A lookup that hits this cap counts as a failed row and
# the run carries on with the next one.
_BACKFILL_LOOKUP_TIMEOUT_SECONDS = 8.0

# The empty-profile pass's selection, shared with _count_unenriched so the
# "rows left for the next run" number cannot drift from the rows a run would
# actually take. They did drift, the first time this arm was added to one and
# not the other, and a count that disagrees with its own selection is worse
# than no count. Same idiom as _FLAGLESS_WHERE below.
#
# Concatenated into its callers rather than f-stringed: the literal '{}' is an
# empty replacement field to an f-string, so this constant cannot appear in one.
# A failed or empty lookup is parked for a week so the same four slugs
# stop being selected every tick. After the cooldown they re-enter.
_LOOKUP_FAILED_COOLDOWN_SECONDS = 7 * 86400

_UNENRICHED_WHERE = """
    linkedin_id IS NOT NULL AND linkedin_id != ''
    AND (
      (profile_json IS NULL OR profile_json = '' OR profile_json = '{}'
       OR NOT json_valid(profile_json))
      OR (
        json_valid(profile_json)
        AND json_extract(profile_json, '$.lookup_failed') IS NOT NULL
        AND CAST(json_extract(profile_json, '$.lookup_failed_at') AS INTEGER)
            < (CAST(strftime('%s','now') AS INTEGER) - 604800)
      )
    )
    AND NOT (
      json_valid(profile_json)
      AND json_extract(profile_json, '$.lookup_failed') IS NOT NULL
      AND CAST(COALESCE(json_extract(profile_json, '$.lookup_failed_at'), 0) AS INTEGER)
          >= (CAST(strftime('%s','now') AS INTEGER) - 604800)
    )
"""

# Selection must match get_inmail_fallback_candidates' json_extract exactly:
# a substring test disagrees with it on nested payloads, leaving rows that
# neither heal nor qualify. json_valid guards malformed blobs from aborting
# the query — that query now reads the column through _SAFE_CONTACT_PROFILE,
# which substitutes '{}' for anything json_valid rejects; on the rows this
# WHERE admits (valid, non-empty JSON) the two extractions are identical.
_FLAGLESS_WHERE = """
    c.profile_json IS NOT NULL
    AND c.profile_json != '' AND c.profile_json != '{}'
    AND json_valid(c.profile_json)
    AND json_extract(c.profile_json, '$.is_open_profile') IS NULL
    AND json_extract(c.profile_json, '$.lookup_failed') IS NULL
    AND c.linkedin_id IS NOT NULL AND c.linkedin_id != ''
"""


def _count_flagless() -> int:
    """How many contacts still lack the Open Profile flag. Sync: via run_db."""
    from ..db.schema import get_db

    conn = get_db()
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM contacts c WHERE {_FLAGLESS_WHERE}"
    ).fetchone()
    conn.close()
    return int(row["n"] if row else 0)


ENRICH_DELAY_SECONDS = 2.5


def _count_unenriched() -> int:
    """How many global contacts a future run could still enrich. Via run_db.

    _UNENRICHED_WHERE is the pass's own selection, so the two cannot disagree.
    The one clause added on top excludes company pages, matching the loop:
    it skips any all-digit linkedin_id as "not a person" and writes nothing
    for it, so nothing ever clears those rows. Counting them would give the
    backlog a permanent floor — 38 of them today — which is exactly the number
    that cannot show whether the job is draining.

    GLOB '*[^0-9]*' is "contains a non-digit", i.e. the negation of the
    isdigit() test the loop applies.
    """
    from ..db.schema import get_db

    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM global_contacts WHERE " + _UNENRICHED_WHERE +
        " AND linkedin_id GLOB '*[^0-9]*'"
    ).fetchone()
    conn.close()
    return int(row["n"] if row else 0)


class _RunBudget:
    """The clock, the pacing and the per-lookup cap that a whole run shares.

    One object across both passes, not one each. LinkedIn sees a single
    account, so the 2.5s spacing is a property of the RUN — pacing per pass
    lets the flag pass open with a call the instant the empty pass closed with
    one. And the clock has to be the run's too, or the second pass starts a
    fresh budget on a job that has no time left.

    Pacing goes BEFORE the clock check, so the worst case between passing that
    check and returning is one lookup at the cap and nothing else. It is also
    skipped ahead of the run's first lookup: a pause before there is anything
    to space from buys no spacing, and the two trailing ones this replaces cost
    5s of a 44s job for the same nothing.
    """

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.lookups = 0
        self.ran_out_of_time = False

    def exhausted(self) -> bool:
        if time.monotonic() - self.started >= _BACKFILL_BUDGET_SECONDS:
            self.ran_out_of_time = True
            return True
        return False

    async def pace(self) -> None:
        import asyncio

        if self.lookups:
            await asyncio.sleep(ENRICH_DELAY_SECONDS)

    async def fetch(self, client, account_id: str, identifier: str):
        """get_profile under the per-lookup cap.

        A cap hit raises asyncio.TimeoutError, which is an Exception and so
        lands in each caller's existing failure branch as an ordinary
        transient failure — it is not a verdict on the identifier, and
        _is_permanent_lookup_failure must not treat it as one.
        """
        import asyncio

        self.lookups += 1
        return await asyncio.wait_for(
            client.get_profile(account_id, identifier, raise_on_invalid=True),
            timeout=_BACKFILL_LOOKUP_TIMEOUT_SECONDS,
        )


def _is_permanent_lookup_failure(exc: Exception) -> bool:  # noqa: D401
    """Has LinkedIn told us this identifier is simply not valid?

    A 422 on a profile lookup is a verdict on the identifier, not a hiccup:
    asking again tomorrow gets the same answer. Timeouts, 429s and 5xx are
    worth retrying and must not match here.

    Read the status code, not the message. get_profile ends in
    raise_for_status(), and httpx renders that as "Client error '422
    Unprocessable Entity' for url ..." — the response body never appears, so
    the API's own 'invalid_recipient' string is not in the exception at all.
    A first attempt at this matched on that string and therefore never fired
    once in production, while the retry loop it was meant to stop carried on
    spending 2,800 calls a day.
    """
    from ..linkedin.unipile import UnipileInvalidRecipientError

    if isinstance(exc, UnipileInvalidRecipientError):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 422:
        return True
    return "invalid_recipient" in str(exc).lower()


async def backfill_empty_profiles() -> str:
    """Periodic backfill: enrich global contacts with empty profile_json.

    Scheduled every 15 min via JOB_BACKFILL_PROFILES. Skips company page IDs.
    Rate-limited to ENRICH_BATCH_SIZE per run with delays between calls.
    Prioritizes 1st-degree connections (network_degree=1) so synced
    connections get enriched first.
    """
    from ..db.async_bridge import run_db
    from ..db.global_contact_queries import upsert_global_contact
    from ..linkedin import get_account_id, get_linkedin_client

    client = get_linkedin_client()
    account_id = await run_db(get_account_id)
    if not account_id:
        return "No account connected"

    def _find_unenriched() -> list[dict]:
        from ..db.schema import get_db

        conn = get_db()
        rows = conn.execute(
            "SELECT id, linkedin_id, name, title, company, linkedin_url, fit_score"
            " FROM global_contacts WHERE " + _UNENRICHED_WHERE +
            " ORDER BY network_degree DESC NULLS LAST, created_at DESC LIMIT ?",
            (_EMPTY_PASS_SELECT,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    budget = _RunBudget()
    rows = await run_db(_find_unenriched)
    flagless = await run_db(_count_flagless)

    if not rows and not flagless:
        return "No contacts need profile backfill"

    enriched = 0
    skipped_company = 0
    failed = 0
    permanently_invalid = 0
    empty_lookups = 0
    for row in rows:
        lid = row["linkedin_id"]
        if lid.strip().isdigit():
            skipped_company += 1
            continue
        # Counted here rather than at the top of the loop so rows that cost no
        # lookup keep flowing once the bound is spent. The head of this queue
        # is 38 company pages that nothing ever clears, and breaking on them
        # would drain zero real rows per run.
        #
        # Against a pass-local count, not budget.lookups: that one is shared
        # with the flag pass so the two space themselves apart, and spending it
        # here is exactly the starvation the independent budgets exist to stop.
        if empty_lookups >= _EMPTY_PASS_BUDGET:
            break
        empty_lookups += 1
        await budget.pace()
        if budget.exhausted():
            break
        try:
            profile = await budget.fetch(client, account_id, lid)
            if not isinstance(profile, dict) or not profile:
                # `str(profile)` used to stand in here for anything that was
                # not a dict, which on a str is the identity function — that is
                # how 1046 rows came to hold a Unipile redirect page as their
                # profile, and a row holding one reads as enriched and is never
                # asked about again. Stamp the same poison pill as a 422 so
                # `_UNENRICHED_WHERE` stops reselecting the slug for a week.
                failed += 1
                await run_db(
                    upsert_global_contact,
                    linkedin_id=lid,
                    name=row.get("name", ""),
                    title=row.get("title") or "",
                    company=row.get("company") or "",
                    linkedin_url=row.get("linkedin_url", ""),
                    profile_json=json.dumps({
                        "lookup_failed": "empty_profile",
                        "lookup_failed_at": int(time.time()),
                    }),
                    fit_score=row.get("fit_score", 0.0),
                    source="profile_backfill",
                )
                continue
            profile_str = json.dumps(profile)

            title = profile.get("title") or row.get("title") or ""
            company = profile.get("company") or row.get("company") or ""

            await run_db(
                upsert_global_contact,
                linkedin_id=lid,
                name=row.get("name", ""),
                title=title,
                company=company,
                linkedin_url=row.get("linkedin_url", ""),
                profile_json=profile_str,
                fit_score=row.get("fit_score", 0.0),
                source="profile_backfill",
            )
            enriched += 1
        except Exception as e:
            logger.debug("Backfill failed for %s: %s", lid[:20], e)
            failed += 1
            if _is_permanent_lookup_failure(e):
                # Record the refusal, or _find_unenriched hands this row back
                # every 15 minutes for ever: the query selects on profile_json
                # being empty, and a failure used to write nothing at all.
                await run_db(
                    upsert_global_contact,
                    linkedin_id=lid,
                    name=row.get("name", ""),
                    title=row.get("title") or "",
                    company=row.get("company") or "",
                    linkedin_url=row.get("linkedin_url", ""),
                    profile_json=json.dumps({
                        "lookup_failed": "invalid_recipient",
                        "lookup_failed_at": int(time.time()),
                    }),
                    fit_score=row.get("fit_score", 0.0),
                    source="profile_backfill",
                )
                permanently_invalid += 1
                # A rejection used to skip the pause, because pacing all 50 of
                # them cost 125s against the tick. _EMPTY_PASS_BUDGET is what
                # stops that now — 4 lookups can pace for at most 7.5s — and
                # the pause is better spent here than saved: the delay spaces
                # LinkedIn between CALLS, and a 422 is a call it served. Free
                # 422s in a tight loop are how this account's API health was
                # dragged to "Down" in the first place.
                continue

    # Lower-priority pass: contacts stored before the normalizers carried the
    # Open Profile flag. Its budget is independent — tying it to the
    # empty-profile backlog (a DIFFERENT table) starved it to zero on any
    # install where refill keeps that backlog full, which is the normal state.
    flagged = 0
    flag_failed = 0
    if flagless and not budget.exhausted():
        flagged, flag_failed = await _backfill_open_profile_flags(
            client, account_id, _FLAG_PASS_BUDGET, budget,
        )

    parts = []
    if enriched:
        parts.append(f"{enriched} profiles enriched")
    if skipped_company:
        parts.append(f"{skipped_company} company pages skipped")
    if failed:
        parts.append(f"{failed} failed")
    if permanently_invalid:
        parts.append(f"{permanently_invalid} rejected by LinkedIn, not retried")
    if flagged:
        parts.append(f"{flagged} open-profile flags backfilled")
    if flag_failed:
        parts.append(f"{flag_failed} open-profile lookups failed")

    # Success with work remaining, which is this job's normal shape. It is
    # reported by RETURNING, and returning is only possible because the run
    # ends itself: the same run cancelled by the engine's asyncio.wait_for
    # produced no value at all, so every row it had already committed was
    # filed as "[network_error]: TimeoutError" and retried for nothing.
    # Two passes, two tables: adding them produced one unlabeled number that
    # disagreed with `_UNENRICHED_WHERE` by exactly the flagless contacts
    # backlog (~293 on 22 Aug). Name each remainder as itself.
    remaining = await run_db(_count_unenriched)
    flagless_left = await run_db(_count_flagless)
    if remaining or flagless_left:
        # Only the clock gets to say "stopped": the lookup bounds are per-pass,
        # so a run that merely ran out of rows in one of them has hit nothing.
        stopped = (
            f"stopped after {_BACKFILL_BUDGET_SECONDS:.0f}s with "
            if budget.ran_out_of_time
            else ""
        )
        if remaining:
            parts.append(f"{stopped}{remaining} rows left for the next run")
            stopped = ""
        if flagless_left:
            parts.append(f"{stopped}{flagless_left} open-profile flags left")
    return "; ".join(parts) or "No contacts need profile backfill"


async def _backfill_open_profile_flags(
    client, account_id: str, lookup_budget: int, budget: "_RunBudget",
) -> tuple[int, int]:
    """Re-fetch contacts whose stored profile predates the is_open_profile key.

    Free-tier InMail routing reads the flag out of contacts.profile_json via
    json_extract, and save_contact's duplicate short-circuit returns the
    existing row without touching profile_json — so contacts saved before the
    normalizers carried the flag can only be healed here. The fetch MERGES
    into the stored JSON (contacts and global_contacts both) rather than
    replacing it, or every key the fetch does not carry would be lost.

    Contacts sitting on an invited outreach come first: they are the ones the
    InMail fallback planner needs an answer for next.
    """
    from ..db.async_bridge import run_db
    from ..db.global_contact_queries import upsert_global_contact
    from ..db.queries import update_contact

    def _find_flagless() -> list[dict]:
        from ..db.schema import get_db

        conn = get_db()
        rows = conn.execute(
            f"""SELECT c.id, c.linkedin_id, c.name, c.title, c.company,
                      c.linkedin_url, c.fit_score, c.profile_json
               FROM contacts c
               WHERE {_FLAGLESS_WHERE}
               ORDER BY (c.id IN (
                   SELECT contact_id FROM outreaches WHERE status = 'invited'
               )) DESC, c.created_at DESC
               LIMIT ?""",
            (_FLAG_PASS_SELECT,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    async def _write_merged(row: dict, merged: dict) -> None:
        merged_str = json.dumps(merged)
        await run_db(update_contact, row["id"], profile_json=merged_str)
        # upsert_global_contact keeps whichever profile_json is longer; the
        # merge adds keys, so the healed version wins there too.
        await run_db(
            upsert_global_contact,
            linkedin_id=row["linkedin_id"],
            name=row.get("name") or "",
            title=row.get("title") or "",
            company=row.get("company") or "",
            linkedin_url=row.get("linkedin_url") or "",
            profile_json=merged_str,
            fit_score=row.get("fit_score") or 0.0,
            source="profile_backfill",
        )

    updated = 0
    failed = 0
    lookups = 0
    for row in await run_db(_find_flagless):
        try:
            existing = json.loads(row.get("profile_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}

        identifier = (existing.get("provider_id") or "").strip() or row["linkedin_id"]
        if identifier.strip().isdigit():
            # A company page is never an Open Profile person. Mark it, or
            # this row is re-selected every run forever.
            existing["is_open_profile"] = False
            await _write_merged(row, existing)
            continue

        # Bound the LOOKUPS, and count them only once a row is known to need
        # one — a window full of company pages costs a DB write each and must
        # not spend a budget that exists to bound calls to LinkedIn.
        if lookups >= lookup_budget:
            break
        lookups += 1
        await budget.pace()
        if budget.exhausted():
            break

        try:
            profile = await budget.fetch(client, account_id, identifier)
        except Exception as e:
            logger.debug("Open-profile backfill failed for %s: %s", identifier[:20], e)
            failed += 1
            if _is_permanent_lookup_failure(e):
                # An identifier LinkedIn refuses can never receive an InMail;
                # False both records that and clears the selection criterion,
                # or this row comes back every 15 minutes forever.
                existing["is_open_profile"] = False
                await _write_merged(row, existing)
            continue

        if not isinstance(profile, dict) or not profile:
            failed += 1
            continue

        merged = dict(existing)
        for key, value in profile.items():
            # Truthy fetched values win; the flags overlay even when False —
            # they are the verdict this pass exists to record.
            if value or key in ("is_open_profile", "is_premium"):
                merged[key] = value
        # A transport that does not carry the flag (the hosted backend passes
        # provider JSON verbatim today) must still terminate this row.
        merged.setdefault("is_open_profile", False)
        await _write_merged(row, merged)
        updated += 1
    return updated, failed
