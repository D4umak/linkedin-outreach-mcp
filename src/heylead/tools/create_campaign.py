"""Tool 2: create_campaign — Create a LinkedIn outreach campaign from a natural language description.

Takes a target description like "Find me fintech CTOs" and:
1. Generates an ICP (Ideal Customer Profile) via LLM
2. Searches LinkedIn for matching prospects
3. Scores and ranks prospects by fit
4. Creates a campaign with queued contacts
5. Shows a preview for confirmation
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..ai.icp_schemas import IcpResult, icp_result_from_dict
from ..config import get_tier, is_backend_mode, load_config
from ..constants import (
    CAMPAIGN_TYPE_JOB_SEARCH,
    DEFAULT_CAMPAIGN_TYPE,
    DEFAULT_NOISE_TYPE,
    DEFAULT_VOICE_HUMANIZE,
    FREE_MAX_CAMPAIGNS,
    FREE_MAX_CONTACTS_ANALYZED,
    INMAIL_FALLBACK_AFTER_DAYS,
    STATUS_ACTIVE,
    STATUS_DRAFT,
    TIER_PRO,
    VALID_CAMPAIGN_TYPES,
    VALID_VOICE_MODES,
    VOICE_MODE_TEXT_ONLY,
)
from ..db.queries import (
    assign_variant,
    create_campaign,
    enroll_prospect,
    get_monthly_usage,
    get_setting,
    increment_usage,
    list_ab_tests,
    list_campaigns,
    save_setting,
    update_campaign,
)
from ..formatter import stars, table
from ..linkedin import (
    UnipileAuthError,
    UnipileError,
    get_account_id,
    get_linkedin_client,
)
from ..db.async_bridge import run_db

logger = logging.getLogger(__name__)

# Per-segment cap the LinkedIn pagination loop already used. Directory hits
# merge into it; at least one search_people page still runs so a full
# directory of title-only CEOs cannot cancel a product search.
_SEGMENT_SEAT_TARGET = 200



_OFF_WORDS = frozenset({"off", "false", "0", "no", "none"})


def resolve_exclude_connections(value: str, *, connections_only: str) -> bool:
    """The exclusion a new campaign gets. On unless the caller says off.

    A connections-only campaign is the one deliberate exception: it exists
    to message existing connections, so with nothing said the exclusion is
    off there. Both said on together is refused before this is reached.
    """
    text = str(value or "").strip().lower()
    if text == "on":
        return True
    if text in _OFF_WORDS:
        return False
    return str(connections_only or "").strip().lower() != "on"


def build_campaign_config(
    *,
    target_description: str,
    prospect_count: int,
    voice_mode: str,
    is_connections_only: bool,
    exclude_connections: bool = True,
    campaign_type: str,
    search_account_id: str,
    competitor_companies: str = "",
) -> dict:
    """The config_json a new campaign starts with.

    Extracted from run_create_campaign so the defaults can be tested without a
    LinkedIn search. campaign_type picks the first-touch prompt family;
    message_generator.select_outreach_prompt reads it. The caller validates
    campaign_type; blank means the default.
    """
    return {
        "target_description": target_description,
        "prospect_count": prospect_count,
        "booking_link": "",
        "voice_mode": voice_mode,
        "voice_noise_type": DEFAULT_NOISE_TYPE,
        "voice_humanize": DEFAULT_VOICE_HUMANIZE,
        # Warm-up sequence toggles
        "enable_profile_views": not is_connections_only,
        "enable_follows": not is_connections_only,
        # Off by default, as in heylead-api CAMPAIGN_SETTING_DEFAULTS.
        "enable_endorsements": False,
        "enable_engagements": not is_connections_only,
        "enable_followups": True,
        # Invitation toggle
        "enable_invitations": not is_connections_only,
        # Connections-only flag
        "connections_only": is_connections_only,
        # Never reach someone who was already a 1st-degree connection before
        # this campaign invited them. Default ON for a new campaign (the
        # customer's ask, 9 Sep 2026); the dashboard toggle turns it off. An
        # existing campaign keeps whatever it stored — the hosted guard reads
        # an absent flag as off, so nothing already running changes.
        # Shared verbatim with heylead-api.
        "exclude_connections": exclude_connections,
        # Never first-touch people who work at the client's competitors
        # (a customer, 11 Sep 2026). Default ON. An empty list excludes nobody
        # until research or the operator names the companies.
        "exclude_competitors": True,
        "competitor_companies": competitor_companies,
        # Prompt family: outbound (default) or job_search
        "campaign_type": campaign_type or DEFAULT_CAMPAIGN_TYPE,
        # Engagement settings
        "engagement_mode": "auto",
        # Follow-up settings. New campaigns start from the backend's single
        # source of truth, heylead-api CAMPAIGN_SETTING_DEFAULTS (4 follow-ups
        # on days 1,3,7,14, Mon-Fri). This config_json is pushed to the host, so
        # a different client default silently overrode the backend's and the
        # dashboard showed one schedule while another ran (10 Sep 2026).
        # Existing campaigns keep what they were created with.
        "max_followups": 4,
        "followup_delay_days": [1, 3, 7, 14],
        # InMail first-touch (Open Profile) and 14-day fallback: on unless toggled off.
        "inmail_fallback": True,
        "inmail_fallback_days": INMAIL_FALLBACK_AFTER_DAYS,
        # The hosted sender gates on active_days (scheduler_executor
        # _check_prospect_active_days); the local scheduler does not (e2adb54).
        "active_days": [0, 1, 2, 3, 4],
        # Search account routing
        "search_account_id": search_account_id,
    }


async def run_create_campaign(
    target_description: str,
    campaign_name: str = "",
    icp_id: str = "",
    company_context: str = "",
    mode: str = "autopilot",
    company_url: str = "",
    voice_mode: str = VOICE_MODE_TEXT_ONLY,
    connections_only: str = "",
    exclude_connections: str = "",
    project_brief: str = "",
    campaign_type: str = "",
    force: bool = False,
    _internal_source: str = "",
) -> str:
    """Create a new outreach campaign.

    Args:
        target_description: Who to target (e.g. "CTOs at fintech startups").
        campaign_name: Optional name for the campaign.
        icp_id: Optional saved ICP ID to reuse.
        company_context: Optional website URL or company description.
        project_brief: Optional full project paste (what you are building,
            go-live, volume, what a vendor must confirm). Copied from
            company_context when omitted so one paste still works.
        mode: Always "autopilot". Copilot mode was removed.
        company_url: Optional LinkedIn company URL for account-based targeting.
            When provided, searches for employees at that specific company matching
            the ICP title filters. Example: "https://www.linkedin.com/company/google"
        voice_mode: Voice memo mode for follow-ups/replies. "text_only"
            (default), "mixed" (alternates text and voice), "voice_only", or "ab_test".
        campaign_type: Prompt family. "outbound" (default) or "job_search".
            job_search selects the invitation note and the first DM that may
            name the recipient's company and the role, uses one proof point
            at most, and never lists a CV. InMail is not routed by this
            switch. Stored in config_json and pushed to the cloud alongside
            the campaign's other settings.

    Flow:
    1. Check setup is complete + free tier limits
    2. On first campaign: ask for company context if missing (guide for best results)
    3. Load saved ICP (if icp_id provided) or generate new one (with company_context if given)
    4. Search LinkedIn for matching prospects
    5. Score and rank by fit
    6. Create campaign + queued contacts as a DRAFT — nothing is sent
    7. Return the draft, and how to launch it

    Creating a campaign starts no outreach. `campaign(action='launch')` is the
    step that activates it, switches the scheduler on, and (on a hosted
    account) hands it to the cloud so it keeps running with the laptop closed.
    """

    # Validate voice_mode
    if voice_mode and voice_mode not in VALID_VOICE_MODES:
        return f"❌ Invalid voice_mode '{voice_mode}'. Must be one of: {', '.join(sorted(VALID_VOICE_MODES))}."
    if not voice_mode:
        voice_mode = VOICE_MODE_TEXT_ONLY

    # Validate campaign_type (prompt family). Checked before any I/O so a typo
    # never costs a LinkedIn search.
    wanted_type = (campaign_type or "").strip().lower()
    if wanted_type and wanted_type not in VALID_CAMPAIGN_TYPES:
        return (
            f"❌ Unknown campaign_type '{campaign_type}'. "
            f"Valid values: {', '.join(VALID_CAMPAIGN_TYPES)}."
        )
    campaign_type = wanted_type

    # ── Step 0: Check setup ──
    # Mutually exclusive by construction: connections_only sources the campaign
    # FROM the connections table and exclude_connections removes everybody in
    # it, so together they describe an empty campaign (9 Sep 2026).
    if connections_only == "on" and exclude_connections == "on":
        return (
            "❌ connections_only and exclude_connections cannot both be on.\n\n"
            "connections_only targets your existing 1st-degree connections; "
            "exclude_connections leaves every one of them alone. Together they "
            "select nobody.\n\n"
            "Pick one:\n"
            "├── connections_only='on'    — DM the people you already know\n"
            "└── exclude_connections='on' — cold outreach that never touches them"
        )

    setup_done = await run_db(get_setting, "setup_complete", False)
    if not setup_done:
        return (
            "❌ Setup required before creating campaigns.\n\n"
            "Please run setup_profile first — it connects your LinkedIn account and "
            "analyzes your writing style so messages sound like you.\n\n"
            "If you haven't started setup yet, say 'set up my profile' and I'll walk "
            "you through it step by step (takes about 2 minutes)."
        )

    # ── Step 1: Free tier limits ──
    # Hosted billing lives on the host. A leftover local `tier: free` must
    # not cap a signed-in account. Drafts send nothing, so they do not use
    # the self-hosted free campaign slot either.
    tier = get_tier()
    apply_free_caps = (not is_backend_mode()) and tier != TIER_PRO
    existing = await run_db(list_campaigns)
    if apply_free_caps:
        active_campaigns = [c for c in existing if c["status"] == STATUS_ACTIVE]
        if len(active_campaigns) >= FREE_MAX_CAMPAIGNS:
            return (
                f"⚠️ Free tier limit: {FREE_MAX_CAMPAIGNS} active campaign(s).\n\n"
                "You already have an active campaign. Options:\n"
                "├── Complete or pause your current campaign first\n"
                "└── Upgrade to Pro ($29/mo) for unlimited campaigns\n\n"
                "Tip: Say 'show_status' to see your current campaign."
            )

    # ── Step 1b: First campaign — ask for the project, not only a homepage ──
    is_first_campaign = len(existing) == 0
    from ..services.project_brief import is_real_project_brief
    has_project = is_real_project_brief(project_brief)
    if is_first_campaign and not icp_id and not has_project:
        return (
            "👋 **First campaign — let's set you up for the best results**\n\n"
            "**Share the project** so messages have something buyer-side to say:\n"
            "├── What you are building (product, not just a homepage)\n"
            "├── Go-live date and volume if you know them\n"
            "└── What a vendor must confirm before a call is worth it\n\n"
            "A homepage URL or a one-line company blurb is not enough — "
            "launch and auto-send will refuse until `project_brief` is a real paste.\n\n"
            "**How to provide it:**\n"
            "  `create_campaign(target_description=\"…\", project_brief=\"What you are building, go-live, volume, what a vendor must confirm\")`\n\n"
            "**Example of project_brief:**\n"
            "• \"UK construction compliance product live 1 October. RTW checks at first payment. Must confirm certified IDSP / statutory excuse, IDVT vs NFC, share-code end-to-end.\"\n\n"
            "Once you have the project ready, call create_campaign again with `project_brief`."
        )

    # ── Step 2: Get Unipile account ──
    account_id = await run_db(get_account_id)
    if not account_id:
        return (
            "❌ No LinkedIn account connected.\n\n"
            "Run setup_profile first to connect your LinkedIn account."
        )

    try:
        client = get_linkedin_client()
    except UnipileError as e:
        return f"❌ {e}"

    # ── Step 2b: Resolve search account (separate from sending account) ──
    from ..services.search_account_resolver import resolve_search_account

    try:
        search_acct_id, use_sales_nav = await resolve_search_account(
            client=client,
            sending_account_id=account_id,
        )
    except Exception as e:
        logger.warning("Search account resolution failed, using own account: %s", e)
        search_acct_id = account_id
        use_sales_nav = False

    if search_acct_id != account_id:
        logger.info(
            "Using premium account %s for search (sending via %s)",
            search_acct_id[:8], account_id[:8],
        )

    # Pass search_account_id to backend if using a different account
    _search_account_override = search_acct_id if search_acct_id != account_id else None

    # ── Step 3: Load saved ICP or generate new one ──
    saved_icp_result: IcpResult | None = None

    if icp_id:
        # Full ID first, then prefix match — shared with icp(action='preview')
        # so both tools resolve the same truncated id to the same ICP.
        from ..services.icp_search import resolve_icp_record
        icp_record = await run_db(resolve_icp_record, icp_id)

        if not icp_record:
            return (
                f"ICP not found: `{icp_id}`\n\n"
                "Use show_status() to see saved ICPs, or generate a new one with generate_icp()."
            )

        try:
            icp_data = json.loads(icp_record["icp_json"])
            saved_icp_result = icp_result_from_dict(icp_data)
            logger.info(f"Loaded saved ICP: {icp_record['name']} ({len(saved_icp_result.icps)} personas)")
        except Exception as e:
            logger.warning(f"Failed to parse saved ICP: {e}")
            saved_icp_result = None

    judged_icp: dict = {}
    precomputed_verdict: dict | None = None
    if saved_icp_result and saved_icp_result.icps:
        # Use saved ICP — build segments from enriched data
        icp = _icp_result_to_legacy(saved_icp_result, target_description)
        # A saved ICP may carry an audit, but against the goal it was generated
        # for; this campaign's goal is judged afresh.
        from ..ai.icp_schemas import icp_result_to_dict
        judged_icp = icp_result_to_dict(saved_icp_result)
    else:
        # Generate new ICP inline (with company_context when provided)
        # The workspace's seat, not the laptop's owner (services/sender_context.py).
        from ..services.sender_context import sender_context
        user_context = await sender_context()

        try:
            from ..tools.generate_icp import generate_icp_result_for_campaign
        except ImportError as e:
            logger.error(f"ICP module import failed: {e}", exc_info=True)
            return (
                f"❌ Could not load ICP generator: {e}\n\n"
                "This may be a missing dependency. Try: pip install heylead[icp]"
            )

        try:
            result = await generate_icp_result_for_campaign(
                target_description=target_description,
                company_context=company_context or "",
                focus_query="",
                user_context=user_context,
            )
            if not result.icps:
                return (
                    "❌ Could not generate an ICP for this description.\n\n"
                    "Try a different or broader target description."
                )
            icp = _icp_result_to_legacy(result, target_description)
            from ..ai.icp_schemas import icp_result_to_dict
            judged_icp = icp_result_to_dict(result)
            precomputed_verdict = (result.source_info or {}).get("goal_match")
        except Exception as e:
            logger.error(f"ICP generation failed: {e}", exc_info=True)
            return (
                f"❌ Failed to generate ICP: {e}\n\n"
                "Check your LLM API key in ~/.heylead/config.json"
            )

    # ── Step 3b: Goal <-> ICP audit — before any search spends LinkedIn quota ──
    refusal, goal_match_notes = await _goal_match_gate(
        target_description=target_description,
        offer=project_brief or company_context or "",
        icp_json=judged_icp,
        precomputed=precomputed_verdict,
        force=force,
    )
    if refusal:
        return refusal

    # ── Step 3c: Research competitor employers to keep out of the list ──
    from ..services.competitors import (
        format_competitor_companies,
        parse_competitor_names,
    )
    competitor_names = parse_competitor_names(
        icp.get("competitors"),
        judged_icp.get("competitors"),
    )
    if not competitor_names:
        try:
            from ..services.competitor_research import research_competitors
            from ..services.sender_context import sender_context
            sender = await sender_context()
            competitor_names = await research_competitors(
                str((sender or {}).get("company") or ""),
                company_context=company_context,
                target_description=target_description,
            )
        except Exception:
            logger.info("Competitor research failed; campaign will start with an empty list")
            competitor_names = []
    if competitor_names:
        icp["competitors"] = competitor_names
        judged_icp["competitors"] = competitor_names

    # ── Step 4: Sales Navigator status (resolved in Step 2b) ──
    if use_sales_nav:
        logger.info("Sales Navigator available — using enhanced search")

    # ── Step 4b: ABM — enrich with company info if company_url provided ──
    abm_company_name = ""
    company_data: dict = {}
    if company_url:
        try:
            # Extract company identifier from URL (e.g., "google" from linkedin.com/company/google)
            import re
            match = re.search(r"linkedin\.com/company/([^/?#]+)", company_url)
            identifier = match.group(1) if match else company_url.strip()
            fetched = await client.get_company_profile(account_id, identifier)
            if isinstance(fetched, dict):
                company_data = fetched
                abm_company_name = company_data.get("name") or company_data.get("company_name") or ""
                logger.info("ABM mode: targeting employees at '%s'", abm_company_name)
        except Exception as e:
            logger.warning("Company profile fetch failed: %s", e)
            # Fall back to using the URL/identifier as company name
            if not abm_company_name:
                match = re.search(r"linkedin\.com/company/([^/?#]+)", company_url)
                abm_company_name = match.group(1).replace("-", " ").title() if match else ""

    # ── Step 5: Find prospects ──
    all_prospects: list[dict] = []
    segments = icp.get("segments", [])

    # Track which segment each prospect came from (for per-segment scoring)
    segment_index_map: dict[str, int] = {}  # prospect key → segment index

    if connections_only == "on":
        # ── CONNECTIONS-ONLY: source from local connections table ──
        # No LinkedIn search needed — we already have all 1st-degree connections.
        from ..services.connection_sync import ensure_synced, get_all_connections
        from ..db.global_contact_queries import get_global_contacts_by_identifiers

        await ensure_synced(client, account_id)
        raw_connections = await run_db(get_all_connections, account_id)
        logger.info("Connections-only: %d 1st-degree connections found", len(raw_connections))

        # Batch lookup existing global_contacts for enrichment data
        all_identifiers = []
        for c in raw_connections:
            if c.get("provider_id"):
                all_identifiers.append(c["provider_id"])
            if c.get("public_id"):
                all_identifiers.append(c["public_id"])
        gc_map = await run_db(get_global_contacts_by_identifiers, all_identifiers)

        # Convert connections to prospect dicts, merging any existing enrichment
        for conn in raw_connections:
            pid = conn.get("provider_id", "")
            pub = conn.get("public_id", "")
            # Find matching global_contact (try provider_id first, then public_id)
            gc = gc_map.get(pid.lower().strip()) or gc_map.get(pub.lower().strip()) or {}
            # Merge enriched data if available
            profile_json = gc.get("profile_json", "")
            title = gc.get("title") or conn.get("headline", "")
            company = gc.get("company", "")
            location = gc.get("location", "")
            if profile_json and not company:
                try:
                    pj = json.loads(profile_json) if isinstance(profile_json, str) else profile_json
                    company = pj.get("company", "") or company
                    location = pj.get("location", "") or location
                    if not title:
                        title = pj.get("headline", "") or pj.get("title", "")
                except (json.JSONDecodeError, TypeError):
                    pass

            prospect = {
                "name": conn.get("name", ""),
                "title": title,
                "headline": conn.get("headline", ""),
                "company": company,
                "location": location,
                "linkedin_id": pid or pub,
                "provider_id": pid,
                "public_id": pub,
                "linkedin_url": f"https://www.linkedin.com/in/{pub}" if pub else "",
                "profile_json": profile_json,
                "_source_tag": "connection",
            }
            if pid or pub:
                all_prospects.append(prospect)
    else:
        # ── STANDARD: Search LinkedIn via Unipile with structured filters ──

        # Pagination config — shared with icp(action='preview') so a preview's
        # single page is the page this loop fetches first.
        from ..services.icp_search import build_segment_query, max_pages, page_size
        MAX_PAGES = max_pages(use_sales_nav)
        RESULTS_PER_PAGE = page_size(use_sales_nav)

        for seg_idx, segment in enumerate(segments):
            titles = segment.get("titles", [])
            has_structured = segment.get("has_structured", False)

            # Keywords + structured filters, built by the shared builder that
            # icp(action='preview') calls, so the preview shows this request.
            search_keywords, search_filters = build_segment_query(
                segment, use_sales_nav, abm_company_name,
            )

            # Directory first: reuse people the shared pool already has.
            # Merge only — a full directory must not cancel LinkedIn search.
            segment_prospects = await run_db(
                _directory_prospects_for_segment,
                segment,
                _SEGMENT_SEAT_TARGET,
                abm_company_name,
            )
            if segment_prospects:
                logger.info(
                    "Segment '%s': %d prospects from local directory",
                    segment.get("name"), len(segment_prospects),
                )

            # Always fetch at least one LinkedIn page so product keywords
            # (IDSP, open banking, Right to Work) still reach search.
            cursor = None
            page = -1
            searched_linkedin = False

            for page in range(MAX_PAGES):
                remaining = _SEGMENT_SEAT_TARGET - len(segment_prospects)
                if remaining <= 0 and searched_linkedin:
                    break
                try:
                    fetch_count = min(
                        RESULTS_PER_PAGE,
                        remaining if remaining > 0 else RESULTS_PER_PAGE,
                    )
                    prospects, next_cursor = await client.search_people(
                        account_id=search_acct_id,
                        keywords=search_keywords,
                        count=fetch_count,
                        use_sales_navigator=use_sales_nav,
                        cursor=cursor,
                        search_account_id=_search_account_override,
                        **search_filters,
                    )
                    searched_linkedin = True
                    prospects = [
                        p for p in prospects
                        if p.get("public_id") or p.get("linkedin_url")
                    ]
                    known = _directory_identity_keys(segment_prospects)
                    prospects = [
                        p for p in prospects
                        if not (_prospect_identity_keys(p) & known)
                    ]
                    segment_prospects.extend(prospects)

                    if not next_cursor or len(segment_prospects) >= _SEGMENT_SEAT_TARGET:
                        break
                    cursor = next_cursor
                    logger.info(
                        "Page %d: got %d prospects, paginating...",
                        page + 1, len(prospects),
                    )
                except UnipileAuthError:
                    if _search_account_override:
                        from ..services.search_account_resolver import invalidate_search_account_cache
                        # Via run_db: this is the event loop thread, and the
                        # helper writes settings synchronously. Called directly
                        # it raised RuntimeError out of the recovery path, so a
                        # stale premium account turned a recoverable auth
                        # failure into a crash.
                        await run_db(invalidate_search_account_cache)
                        logger.warning(
                            "Premium search account auth failed, falling back to sending account"
                        )
                        search_acct_id = account_id
                        _search_account_override = None
                        use_sales_nav = False
                        break
                    await client.close()
                    return (
                        "🔑 LinkedIn account disconnected.\n\n"
                        "Run setup_profile() again to reconnect."
                    )
                except UnipileError:
                    await client.close()
                    raise
                except Exception as e:
                    logger.warning(f"Search failed for segment '{segment.get('name')}' page {page}: {e}")
                    break

            # Fallback: if structured search yielded too few results, retry with
            # relaxed filters (drop company_headcount, add title keywords)
            if (
                len(segment_prospects) < _SEGMENT_SEAT_TARGET
                and len(segment_prospects) < 5
                and has_structured
                and not use_sales_nav
            ):
                relaxed_filters = {k: v for k, v in search_filters.items()}
                relaxed_filters.pop("company_headcount", None)
                relaxed_filters.pop("tenure", None)
                title_kw = " OR ".join(titles[:3]) if titles else ""
                logger.info(
                    "Segment '%s': only %d prospects with full filters, "
                    "retrying with relaxed filters + title keywords '%s'",
                    segment.get("name"), len(segment_prospects), title_kw[:60],
                )
                try:
                    retry_prospects, _ = await client.search_people(
                        account_id=search_acct_id,
                        keywords=title_kw,
                        count=RESULTS_PER_PAGE,
                        use_sales_navigator=False,
                        search_account_id=_search_account_override,
                        **relaxed_filters,
                    )
                    retry_prospects = [
                        p for p in retry_prospects
                        if p.get("public_id") or p.get("linkedin_url")
                    ]
                    existing_ids = {
                        p.get("public_id") or p.get("linkedin_url")
                        for p in segment_prospects
                    }
                    known = _directory_identity_keys(segment_prospects)
                    for p in retry_prospects:
                        pid = p.get("public_id") or p.get("linkedin_url")
                        if (
                            pid
                            and pid not in existing_ids
                            and not (_prospect_identity_keys(p) & known)
                        ):
                            segment_prospects.append(p)
                            existing_ids.add(pid)
                            known |= _prospect_identity_keys(p)
                    logger.info(
                        "Relaxed retry added %d new prospects (total: %d)",
                        len(retry_prospects), len(segment_prospects),
                    )
                except Exception as e:
                    logger.warning(f"Relaxed search fallback failed: {e}")

            if segment_prospects:
                logger.info(
                    "Segment '%s': %d prospects across %d pages%s",
                    segment.get("name"), len(segment_prospects),
                    min(page + 1, MAX_PAGES),
                    " (Sales Nav)" if use_sales_nav else "",
                )

            # Tag each prospect with its source segment index (first segment wins)
            for p in segment_prospects:
                key = p.get("public_id") or p.get("linkedin_url") or p.get("name")
                if key and key not in segment_index_map:
                    segment_index_map[key] = seg_idx

            all_prospects.extend(segment_prospects)

    if not all_prospects:
        if connections_only == "on":
            return (
                f"❌ No 1st-degree connections found in your network.\n\n"
                "Your connections may not be synced yet. Try:\n"
                "1. Run setup_profile() to re-sync your LinkedIn account\n"
                "2. Drop connections_only to run standard cold outreach with invitations"
            )
        return (
            "😕 No prospects found on LinkedIn for this description.\n\n"
            f"Searched for: \"{target_description}\"\n\n"
            "Try:\n"
            "├── Use broader keywords (e.g., 'startup founders' instead of 'fintech CTO Series A')\n"
            "├── Check your LinkedIn connection (run setup_profile)\n"
            "├── In backend mode: check heylead-api logs and Unipile account/API limits\n"
            "└── Try a different target description"
        )

    # Deduplicate by public_id or linkedin_url
    seen = set()
    unique_prospects = []
    for p in all_prospects:
        key = p.get("public_id") or p.get("linkedin_url") or p.get("name")
        if key and key not in seen:
            seen.add(key)
            unique_prospects.append(p)

    # ── Step 5a: Dedup — filter existing connections + cross-campaign contacts ──
    dedup_summary = ""
    try:
        from ..services.dedup_service import (
            dedup_prospects,
            fetch_connection_ids,
            filter_to_connections_only,
            format_dedup_summary,
            get_all_known_linkedin_ids,
            get_enrolled_people,
            get_excluded_linkedin_ids,
        )
        known_ids = await run_db(get_all_known_linkedin_ids)
        enrolled = await run_db(get_enrolled_people)
        connection_ids = await fetch_connection_ids(client, account_id)
        excluded_ids = await run_db(get_excluded_linkedin_ids)

        # Filter out contacts excluded from automation (do-not-automate tag / do_not_contact)
        if excluded_ids:
            pre_excluded = len(unique_prospects)
            unique_prospects = [
                p for p in unique_prospects
                if not ({
                    (p.get("public_id") or "").lower().strip(),
                    (p.get("provider_id") or "").lower().strip(),
                    (p.get("linkedin_url") or "").lower().strip(),
                } - {""}) & excluded_ids
            ]
            excluded_count = pre_excluded - len(unique_prospects)
            if excluded_count > 0:
                logger.info("Excluded %d contacts from automation (do-not-automate)", excluded_count)
                dedup_summary += f"\n{excluded_count} excluded from automation (do-not-automate)"

        if connections_only == "on":
            # Connections-only: prospects come from local connections table
            # (already verified 1st-degree). Only remove cross-campaign dupes.
            pre_count = len(unique_prospects)
            unique_prospects = [
                p for p in unique_prospects
                if not ({
                    (p.get("public_id") or "").lower().strip(),
                    (p.get("provider_id") or "").lower().strip(),
                    (p.get("linkedin_url") or "").lower().strip(),
                } - {""}) & known_ids
            ]
            dupes = pre_count - len(unique_prospects)
            if dupes > 0:
                dedup_summary += f"\nConnections filter: {pre_count} connections, {dupes} cross-campaign duplicates removed"
                logger.info("Connections-only: %s", dedup_summary)
        else:
            # Standard: filter OUT existing connections + duplicates
            unique_prospects, dedup_stats = dedup_prospects(
                unique_prospects, known_ids, connection_ids,
                enrolled_people=enrolled,
            )
            dedup_summary = format_dedup_summary(dedup_stats)
            if dedup_summary:
                logger.info("Dedup: %s", dedup_summary)
    except Exception as e:
        logger.warning("Dedup check failed (non-critical): %s", e)

    # ── Known-contacts detection (connections-only) ──
    known_contacts_warning = ""
    if connections_only == "on" and unique_prospects:
        try:
            known_contacts = await run_db(
                _detect_known_contacts, unique_prospects,
            )
            if known_contacts:
                known_contacts_warning = (
                    f"\n⚠️ {len(known_contacts)} contacts with existing conversations detected:\n"
                    + "\n".join(
                        f"  - {kc['name']} ({kc['message_count']} messages exchanged)"
                        for kc in known_contacts[:5]
                    )
                )
                if len(known_contacts) > 5:
                    known_contacts_warning += f"\n  ... and {len(known_contacts) - 5} more"
                known_contacts_warning += (
                    "\n\nTag contacts with 'do-not-automate' to exclude them: "
                    "contacts(action='tag', contact_id='...', tag='do-not-automate')"
                )
                logger.info("Known-contacts warning: %d contacts with prior conversations",
                            len(known_contacts))
        except Exception as e:
            logger.warning("Known-contacts detection failed (non-critical): %s", e)

    if not unique_prospects:
        if connections_only == "on":
            return (
                f"❌ No existing connections found matching \"{target_description}\".\n\n"
                f"All {len(all_prospects)} connections are already in other campaigns.\n\n"
                "Options:\n"
                "1. Drop connections_only to run standard cold outreach with invitations\n"
                "2. Use a broader target description to match more of your network\n"
                "3. Connect with target people first, then create a connections-only campaign"
            )
        return (
            "All found prospects are already in your campaigns or connections.\n\n"
            f"Searched for: \"{target_description}\"\n\n"
            "Try a different or broader target description to find new prospects."
        )

    # ── Step 5b (pre): Post-search filtering for Classic LinkedIn ──
    # Classic search drops seniority, company_headcount, etc.
    # Apply soft filtering here to remove obvious mismatches.
    if not use_sales_nav and segments:
        pre_count = len(unique_prospects)
        unique_prospects = _post_filter_classic_prospects(
            unique_prospects, segments, segment_index_map,
        )
        filtered_out = pre_count - len(unique_prospects)
        if filtered_out > 0:
            logger.info(
                "Post-search filter: removed %d/%d prospects (Classic LinkedIn)",
                filtered_out, pre_count,
            )

    if not unique_prospects:
        return (
            "All prospects were filtered out by ICP seniority/title matching.\n\n"
            f"Searched for: \"{target_description}\"\n\n"
            "Try broader targeting or adjust your ICP criteria."
        )

    # ── Step 5b: Score each prospect against campaign ICP ──
    from ..services.icp_match_scorer import compute_icp_match
    icp_json_for_scoring = icp  # Parsed dict with "segments" key

    # Connections-only: preliminary score → enrich top 50 → full score
    if connections_only == "on":
        # Preliminary score using headline-only data
        for prospect in unique_prospects:
            result = compute_icp_match(prospect, icp_json_for_scoring)
            prospect["_preliminary_score"] = result["icp_match_score"]
        unique_prospects.sort(key=lambda p: p.get("_preliminary_score", 0), reverse=True)

        # Enrich top 50 with full LinkedIn profiles for better scoring
        try:
            from ..services.connection_sync import enrich_prospects
            enriched_count = await enrich_prospects(
                client, account_id, unique_prospects[:50], max_enrich=50,
            )
            if enriched_count > 0:
                logger.info("Enriched %d connections with full profiles before scoring", enriched_count)
        except Exception as e:
            logger.warning("Connection enrichment failed (non-critical): %s", e)

    # Full ICP score (with enriched data for connections-only top candidates)
    for prospect in unique_prospects:
        result = compute_icp_match(prospect, icp_json_for_scoring)
        prospect["fit_score"] = result["icp_match_score"]

    # Sort by score, highest first
    unique_prospects.sort(key=lambda p: p.get("fit_score", 0), reverse=True)

    # Filter out prospects the send gate would immediately skip
    from ..services.campaign_refill_service import _drop_below_send_threshold
    pre_filter_count = len(unique_prospects)
    from ..constants import MIN_FIT_SCORE_THRESHOLD
    from ..ops_log import log_prospects_below_threshold, log_prospects_sampled

    log_prospects_below_threshold(unique_prospects, threshold=MIN_FIT_SCORE_THRESHOLD)
    log_prospects_sampled(unique_prospects, threshold=MIN_FIT_SCORE_THRESHOLD)
    unique_prospects = _drop_below_send_threshold(unique_prospects, {})
    low_score_filtered = pre_filter_count - len(unique_prospects)
    if competitor_names:
        from ..services.competitors import drop_competitor_people
        before_comp = len(unique_prospects)
        unique_prospects, _dropped_comp = drop_competitor_people(
            unique_prospects, competitor_names,
        )
        if before_comp - len(unique_prospects):
            logger.info(
                "Dropped %d prospects at competitor companies",
                before_comp - len(unique_prospects),
            )
    if low_score_filtered > 0:
        logger.info("Filtered %d prospects below the campaign send threshold", low_score_filtered)
    if not unique_prospects:
        return (
            "LinkedIn returned people, but none cleared the product fit line "
            f"({MIN_FIT_SCORE_THRESHOLD:.1f}).\n\n"
            f"Dropped {low_score_filtered} for missing product evidence "
            "(e.g. IDSP / Right to Work / open banking on the profile).\n"
            f"{dedup_summary}\n\n"
            "A campaign was not created. Broaden the ICP or import named vendors."
        )

    # Free tier: cap contacts (self-hosted free only — the host owns billing)
    max_contacts = FREE_MAX_CONTACTS_ANALYZED if apply_free_caps else 1000
    prospects_to_save = unique_prospects[:max_contacts]

    # SN-mode searches return SN-space ids (ACw…) that classic invitations
    # 400 on — repair or reject before the rows become outreaches. Uses the
    # sending account: that is the account the ids must be valid for.
    from ..services.provider_id_resolver import ensure_classic_provider_ids
    prospects_to_save = await ensure_classic_provider_ids(
        client, account_id, prospects_to_save,
    )

    # ── Step 6: Create campaign in DB ──
    final_name = campaign_name or icp.get("campaign_name", target_description[:40])

    # Build config — connections-only disables invitations + all warm-up
    is_connections_only = connections_only == "on"
    config = build_campaign_config(
        target_description=target_description,
        prospect_count=len(prospects_to_save),
        voice_mode=voice_mode,
        is_connections_only=is_connections_only,
        exclude_connections=resolve_exclude_connections(
            exclude_connections, connections_only=connections_only,
        ),
        campaign_type=campaign_type,
        search_account_id=search_acct_id if search_acct_id != account_id else "",
        competitor_companies=format_competitor_companies(competitor_names),
    )

    # Build context_json — persist brief + short offerings so prompts have both
    from ..services.project_brief import build_context_payload
    context = build_context_payload(
        company_context=company_context, project_brief=project_brief,
    )
    context_json = json.dumps(context) if context else ""

    campaign_id = await run_db(create_campaign, name=final_name,
        icp_json=json.dumps(icp),
        status=STATUS_DRAFT,
        mode=mode,
        config_json=json.dumps(config),
        context_json=context_json,)

    # ── ABM company watchlist — after campaign_id exists ──
    # Step 4c used to run here-before-here and LOAD_FAST campaign_id
    # while it was still unbound. The exception was debug-logged and
    # the watchlist the company_url feature exists for was never created.
    if abm_company_name and company_url:
        try:
            from ..db.signal_queries import list_watchlists, save_watchlist
            import re as _re
            _match = _re.search(r"linkedin\.com/company/([^/?#]+)", company_url)
            company_identifier = _match.group(1) if _match else ""
            if company_data:
                company_identifier = company_data.get("provider_id") or company_identifier
            if company_identifier:
                existing_company_wl = [
                    w for w in await run_db(list_watchlists, is_active=True)
                    if w.get("watch_type") == "company"
                    and company_identifier in (w.get("keywords_list") or [])
                ]
                if not existing_company_wl:
                    await run_db(save_watchlist, name=abm_company_name or company_identifier,
                        watch_type="company",
                        keywords=[company_identifier],
                        campaign_id=campaign_id,)
                    logger.info("Auto-created company watchlist for ABM target: %s", abm_company_name)
        except Exception as e:
            logger.debug("ABM company watchlist creation failed (non-fatal): %s", e)

    # Save contacts + create outreach records
    _source = _internal_source or "linkedin_search"
    _source_detail = f"ICP: {target_description[:100]}"
    if _internal_source == "strategy_spawn":
        _source_detail = f"Auto-spawned: {target_description[:100]}"
    from ..db.queries import has_running_message_ab_test
    has_ab_test = await run_db(has_running_message_ab_test, campaign_id)
    for prospect in prospects_to_save:
        variant = await run_db(assign_variant, campaign_id) if has_ab_test else None
        await run_db(
            enroll_prospect,
            campaign_id,
            {
                **prospect,
                "linkedin_id": prospect.get("public_id") or prospect.get("linkedin_id") or "",
                "profile_json": json.dumps(prospect),
            },
            source=_source,
            source_detail=_source_detail,
            variant=variant,
        )

    # ── Step 6a-extra: Auto-create company watchlists from top prospect companies ──
    try:
        from collections import Counter as _Counter
        from ..db.signal_queries import list_watchlists as _list_wl, save_watchlist as _save_wl

        company_counts = _Counter(
            p.get("company", "").strip()
            for p in prospects_to_save
            if p.get("company", "").strip()
        )
        # Take top 5 companies (any count ≥1) — diverse campaigns often have 1 per company
        existing_wl = await run_db(_list_wl, is_active=True)
        existing_company_wls = [w for w in existing_wl if w.get("watch_type") == "company"]
        existing_company_kw = {
            kw.lower()
            for w in existing_company_wls
            for kw in (w.get("keywords_list") or [])
        }
        existing_company_names = {
            w.get("name", "").lower()
            for w in existing_company_wls
        }
        company_wl_created = 0
        for comp_name, count in company_counts.most_common(10):
            if company_wl_created >= 5:
                break
            comp_lower = comp_name.lower()
            # Dedup against both keywords and watchlist names
            if comp_lower in existing_company_kw or comp_lower in existing_company_names:
                continue
            await run_db(
                _save_wl,
                name=comp_name,
                watch_type="company",
                keywords=[comp_name],
                campaign_id=campaign_id,
            )
            company_wl_created += 1
        if company_wl_created:
            logger.info("Auto-created %d company watchlists from prospect companies", company_wl_created)
    except Exception as e:
        logger.debug("Company watchlist auto-creation from contacts failed (non-fatal): %s", e)

    # ── Step 6b: Signal intelligence — watchlists + retroactive matching ──
    signal_summary_lines: list[str] = []
    try:
        from ..services.signal_linker import analyze_signal_coverage, scan_signal_pool_for_campaign
        from ..services.signal_service import create_watchlists_from_icp

        # Analyze what's already being monitored
        coverage = await run_db(analyze_signal_coverage, icp)

        # Auto-create campaign-specific watchlists for missing topics.
        # This one opens the DB on its first statement (the list_watchlists
        # dedup read), so it runs on the DB worker thread — get_db() raises
        # when it is called from the event loop thread.
        wl_ids = await run_db(
            create_watchlists_from_icp,
            icp_json=icp,
            campaign_id=campaign_id,
            icp_name=final_name,
        )

        if wl_ids or coverage["already_covered"] > 0:
            parts = []
            if coverage["already_covered"] > 0:
                parts.append(f"{coverage['already_covered']} topics already monitored")
            if wl_ids:
                parts.append(f"{len(wl_ids)} new watchlists created")
            signal_summary_lines.append(f"📡 Signal monitoring: {', '.join(parts)}")

        # Scan existing signal pool for hot leads matching this ICP
        match_result = await run_db(scan_signal_pool_for_campaign, campaign_id, icp)
        if match_result["hot_leads_found"] > 0:
            signal_summary_lines.append(
                f"🔥 {match_result['hot_leads_found']} hot leads found from existing signals!"
            )
    except Exception as e:
        logger.debug("Signal intelligence setup failed (non-fatal): %s", e)

    # Track usage
    await run_db(increment_usage, "campaigns_created")

    # Hosted: push this draft so settings/resume have a row before launch.
    # One campaign, not a full-account sync — launch must not wait on that.
    if is_backend_mode():
        try:
            from ..services.cloud_sync import sync_to_cloud
            await sync_to_cloud(campaign_id=campaign_id)
        except Exception as e:
            logger.warning("Draft campaign push failed (non-fatal): %s", e)

    # ── Step 7: Format launch confirmation ──
    header = "✅ Campaign Created (not started)"
    output_lines = [
        f"{header}: **{final_name}**",
        f"📋 Campaign ID: `{campaign_id[:8]}...`",
        "",
    ]

    # ICP summary
    summary = icp.get("summary", target_description)
    if summary:
        output_lines.append(summary)
    if icp.get("relevance_hook"):
        output_lines.append(f"Hook: {icp['relevance_hook']}")
    if search_acct_id != account_id:
        output_lines.append("Searched via premium account (Sales Navigator)")
    elif use_sales_nav:
        output_lines.append("Searched with Sales Navigator")
    output_lines.append(f"{len(prospects_to_save)} prospects queued")
    if dedup_summary:
        output_lines.append(f"🔍 {dedup_summary}")
    if known_contacts_warning:
        output_lines.append(known_contacts_warning)
    if signal_summary_lines:
        for line in signal_summary_lines:
            output_lines.append(line)
    output_lines.append("")

    # Show top 5 prospects
    output_lines.append(f"Top prospects (of {len(prospects_to_save)}):")
    for i, p in enumerate(prospects_to_save[:5]):
        is_last = i == min(4, len(prospects_to_save) - 1)
        prefix = "└──" if is_last else "├──"
        name = p.get("name", "Unknown")
        title = p.get("title", "")
        company = p.get("company", "")
        score = p.get("fit_score", 0)
        star_rating = stars(score)

        role = f"{title}" if title else ""
        if company:
            role += f" at {company}" if role else company

        output_lines.append(f"{prefix} {i+1}. {name} — {role} (fit: {star_rating})")

    if len(prospects_to_save) > 5:
        output_lines.append(f"    ... and {len(prospects_to_save) - 5} more")

    # Nothing has been sent yet — the campaign is a draft until it is launched.
    output_lines.extend([
        "",
        "📝 **Nothing has been sent yet.** The campaign is saved as a draft.",
        "",
        "Review the prospects above, then start outreach with:",
        f"   campaign(action='launch', campaign_id='{campaign_id}')",
        "",
        "Once launched:",
        "├── 💬 Warm-up — engaging with prospect posts (25-40 min intervals)",
        "├── 🤝 Invitations — personalized connection requests after warm-up (20-40 min)",
        "├── 📩 Follow-ups — automatic DMs on days 1, 3, 7, 14",
        "└── 📬 Reply detection — checked every 5 min, hot leads surfaced",
        "",
        "All messages pass a 5-stage validation pipeline and respect LinkedIn rate limits.",
        "",
        "Before launching you can still:",
        "├── edit_campaign() — adjust targeting, messaging, or follow-up settings",
        "└── show_status() — review the queued prospects",
    ])

    # Voice mode info
    if voice_mode != "text_only":
        voice_label = {"mixed": "Mixed (alternating text & voice)", "voice_only": "Voice only", "ab_test": "A/B testing text vs voice"}.get(voice_mode, voice_mode)
        output_lines.extend(["", f"🎤 **Voice memos: {voice_label}** — edit_campaign(voice_mode='text_only') to disable"])

    # Campaign type info
    if config["campaign_type"] == CAMPAIGN_TYPE_JOB_SEARCH:
        output_lines.extend(["", "🎯 Campaign type: job_search (first touch may name the recipient's company and the role; one proof point, no CV listing)"])

    # Nudge to add campaign context if missing (critical for message quality)
    if not context.get("project_brief"):
        output_lines.extend([
            "",
            "⚠️ **No project_brief set** — launch and auto-send will refuse until it is.",
            "   Add the project now (what you are building, go-live, volume, must-confirm):",
            "   → `edit_campaign(project_brief='What you are building and what a vendor must confirm')`",
            "   → `edit_campaign(offerings='Short product line')`",
            "   → `edit_campaign(booking_link='https://cal.com/you/15min')`",
        ])

    if is_first_campaign:
        output_lines.extend([
            "",
            "🔍 **Prospects will check your profile before accepting** — run a quick brand audit",
            "   to make sure your LinkedIn profile converts visitors into connections:",
            "   → `brand_strategy(action='analyze')` — scores your headline, summary, and content",
            "",
            "💡 For your next campaign, keep using company_context for sharper ICPs and more relevant outreach.",
        ])

    if apply_free_caps and len(unique_prospects) > max_contacts:
        output_lines.extend([
            "",
            f"💡 Found {len(unique_prospects)} total matches but free tier caps at {max_contacts}.",
            "   Upgrade to Pro ($29/mo) for unlimited contacts.",
        ])

    if goal_match_notes:
        output_lines.extend(goal_match_notes)

    # ── Flag brand re-analysis with new ICP context ──
    try:
        from ..services.brand_service import load_brand_analysis
        if await run_db(load_brand_analysis):
            await run_db(save_setting, "brand_reanalyze_needed", True)
            logger.info("Flagged brand for ICP-driven re-analysis after campaign creation")
    except Exception as e:
        logger.debug("Brand re-analysis flag failed (non-fatal): %s", e)

    from ..services.dashboard_snapshot import status_footer

    output_lines.extend(status_footer(
        "campaign", campaign_id, snapshot=False,
        label="Review prospects in the dashboard",
    ))
    return "\n".join(output_lines)


async def _goal_match_gate(
    *,
    target_description: str,
    offer: str,
    icp_json: dict,
    precomputed: dict | None,
    force: bool,
) -> tuple[str | None, list[str]]:
    """Audit the campaign goal against its ICP. Returns (refusal, notes).

    `mismatch` refuses unless force; `partial` warns and continues; `match`
    adds nothing. The judge never raises, and an audit that could not run
    comes back `partial` — a broken judge warns, it never blocks a campaign.
    Runs before prospect search, so a refused campaign costs no LinkedIn
    quota and saves nothing.
    """
    from ..ai.goal_match import coerce_verdict, format_verdict, judge_goal_match

    if precomputed:
        verdict = coerce_verdict(
            precomputed, source=str(precomputed.get("source") or "backend"),
        )
    else:
        verdict = await judge_goal_match(target_description, offer, icp_json)

    if verdict.verdict == "match":
        return None, []
    block = format_verdict(verdict)
    if verdict.blocks_campaign and not force:
        return (
            "❌ **Campaign not created — the ICP does not match the goal.**\n\n"
            f"{block}\n\n"
            "Nothing was searched and nothing was saved. Revise the target "
            "description or the ICP, or pass force=True to create it anyway.",
            [],
        )
    heading = (
        "⚠️ **Created despite a goal ↔ ICP mismatch (force=True).**"
        if verdict.blocks_campaign
        else "⚠️ **Goal ↔ ICP: partial match — review before launching.**"
    )
    return None, ["", heading, block]


def _prospect_identity_keys(prospect: dict[str, Any]) -> set[str]:
    """Identifiers that mean "this LinkedIn person" — slug and member id."""
    keys: set[str] = set()
    for field in ("public_id", "provider_id"):
        val = (prospect.get(field) or "").strip().lower()
        if val:
            keys.add(val)
    return keys


def _directory_identity_keys(prospects: list[dict[str, Any]]) -> set[str]:
    known: set[str] = set()
    for prospect in prospects:
        known |= _prospect_identity_keys(prospect)
    return known


def _directory_row_to_prospect(row: dict[str, Any]) -> dict[str, Any] | None:
    """Map a global_contacts row to the dict shape search_people returns."""
    from ..author_identity import normalize_public_slug, slug_from_profile_url

    lid = (row.get("linkedin_id") or "").strip()
    if not lid or lid.isdigit():
        return None
    provider_id = ""
    public_id = ""
    raw = row.get("profile_json") or ""
    blob: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, dict):
                blob = parsed
        except (json.JSONDecodeError, TypeError, ValueError):
            blob = {}
    if lid.startswith("ACoAA"):
        provider_id = lid
    else:
        public_id = lid
    provider_id = (blob.get("provider_id") or provider_id or "").strip()
    public_id = (
        normalize_public_slug(blob.get("public_id"))
        or slug_from_profile_url(row.get("linkedin_url") or "")
        or ("" if public_id.startswith("ACoAA") else public_id)
    )
    url = (row.get("linkedin_url") or "").strip()
    if not url and public_id:
        url = f"https://www.linkedin.com/in/{public_id}"
    if not public_id and not provider_id:
        return None
    return {
        "public_id": public_id,
        "name": row.get("name") or "",
        "title": row.get("title") or "",
        "company": row.get("company") or "",
        "linkedin_url": url,
        "provider_id": provider_id,
    }


def _directory_prospects_for_segment(
    segment: dict[str, Any],
    limit: int,
    extra_company: str = "",
) -> list[dict[str, Any]]:
    """Crude title/company LIKE match against the local shared directory."""
    titles = [str(t).strip() for t in (segment.get("titles") or []) if str(t).strip()]
    companies = [str(c).strip() for c in (segment.get("companies") or []) if str(c).strip()]
    if extra_company and extra_company.strip():
        companies.append(extra_company.strip())
    needles = titles + companies
    if not needles or limit <= 0:
        return []

    clauses: list[str] = []
    params: list[Any] = []
    for needle in needles:
        clauses.append("(title LIKE ? OR company LIKE ?)")
        like = f"%{needle}%"
        params.extend([like, like])

    from ..db.schema import get_db

    db = get_db()
    try:
        rows = db.execute(
            f"""SELECT * FROM global_contacts
                WHERE ({' OR '.join(clauses)})
                  AND linkedin_id IS NOT NULL AND linkedin_id != ''
                  AND linkedin_id GLOB '*[^0-9]*'
                LIMIT ?""",
            [*params, limit],
        ).fetchall()
    finally:
        db.close()

    prospects: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        mapped = _directory_row_to_prospect(dict(raw))
        if not mapped:
            continue
        key = (
            mapped.get("public_id")
            or mapped.get("linkedin_url")
            or mapped.get("provider_id")
            or ""
        ).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        prospects.append(mapped)

    stems = _precision_stems_for_segment(segment)
    if stems:
        from ..services.icp_match_scorer import prospect_mentions_precision_stems
        prospects = [
            p for p in prospects if prospect_mentions_precision_stems(p, stems)
        ]
    return prospects


def _precision_stems_for_segment(segment: dict[str, Any]) -> list[str]:
    from ..services.icp_match_scorer import precision_stems_from_text

    blob = " ".join(
        str(segment.get(key) or "")
        for key in ("keywords", "boolean_keywords", "description")
    )
    return precision_stems_from_text(blob)


def _icp_result_to_legacy(result: IcpResult, target_description: str) -> dict:
    """Convert an IcpResult to structured search segments preserving enriched codes.

    Each segment carries both human-readable fields AND enriched LinkedIn codes
    so the search can use structured Unipile filters instead of keyword-only.
    """
    segments = []
    for icp in result.icps:
        titles = icp.job_titles.include if icp.job_titles else []
        industries = icp.industries.include if icp.industries else []
        locations = icp.locations.include if icp.locations else []
        keywords_list = icp.keywords or []

        # Fallback keyword string (used when no enriched codes)
        keyword_parts = []
        if titles:
            keyword_parts.extend(titles[:2])
        if industries:
            keyword_parts.extend(industries[:2])
        if keywords_list:
            keyword_parts.extend(keywords_list[:3])

        # ── Extract enriched LinkedIn codes (from Phase 6 enrichment) ──
        enriched = icp.linkedin_enriched_params
        industry_codes: list[str] = []
        location_codes: list[str] = []
        title_codes: list[str] = []
        department_codes: list[str] = []

        if enriched:
            if enriched.industries and enriched.industries.include:
                industry_codes = [p.code for p in enriched.industries.include if p.code]
            if enriched.locations and enriched.locations.include:
                location_codes = [p.code for p in enriched.locations.include if p.code]
            if enriched.job_titles and enriched.job_titles.include:
                title_codes = [p.code for p in enriched.job_titles.include if p.code]
            if enriched.departments and enriched.departments.include:
                department_codes = [p.code for p in enriched.departments.include if p.code]

        # ── Structured parameters ──
        seniority_list = icp.seniority.include if icp.seniority else []

        headcount: dict[str, int] | None = None
        if icp.company_headcount and (icp.company_headcount.min or icp.company_headcount.max):
            headcount = {}
            if icp.company_headcount.min:
                headcount["min"] = icp.company_headcount.min
            if icp.company_headcount.max:
                headcount["max"] = icp.company_headcount.max

        tenure_param: dict[str, int] | None = None
        if icp.tenure and (icp.tenure.min or icp.tenure.max):
            tenure_param = {}
            if icp.tenure.min:
                tenure_param["min"] = icp.tenure.min
            if icp.tenure.max:
                tenure_param["max"] = icp.tenure.max

        company_types_list = icp.company_types or []
        has_structured = bool(industry_codes or location_codes or title_codes)

        if has_structured:
            logger.info(
                "Segment '%s': %d industry, %d location, %d title codes; seniority=%s",
                icp.name, len(industry_codes), len(location_codes),
                len(title_codes), seniority_list[:2] or "any",
            )

        segments.append({
            "name": icp.name or "Segment",
            "description": icp.description or "",
            # Persona psychology — dropped here since the feature shipped,
            # which starved prospect_analyzer and the message brief of the
            # pain points the ICP generator had already written.
            "pain_points": list(icp.pain_points or []),
            "fears": list(icp.fears or []),
            "barriers": list(icp.barriers or []),
            "titles": titles,
            "keywords": ", ".join(keyword_parts) if keyword_parts else target_description,
            "industries": industries,
            "locations": locations,
            # Enriched LinkedIn codes for structured search
            "industry_codes": industry_codes,
            "location_codes": location_codes,
            "title_codes": title_codes,
            "department_codes": department_codes,
            # Structured parameters
            "seniority": seniority_list,
            "company_headcount": headcount,
            "company_types": company_types_list,
            "tenure": tenure_param,
            # Sales Navigator advanced filters
            "spotlight": icp.spotlight_filters,
            "boolean_keywords": icp.boolean_keywords or "",
            "annual_revenue": icp.annual_revenue,
            "company_headcount_growth": icp.company_headcount_growth or "",
            "has_structured": has_structured,
            "profile_signals": icp.profile_signals,
        })

    first = result.icps[0] if result.icps else None
    return {
        "segments": segments,
        # Top-level aggregates: prospect_analyzer reads icp.get("pain_points")
        # etc. at the root — point them at the primary persona.
        "pain_points": list(first.pain_points) if first else [],
        "fears": list(first.fears) if first else [],
        "barriers": list(first.barriers) if first else [],
        "summary": result.summary or target_description,
        "campaign_name": result.campaign_name or target_description[:40],
        "relevance_hook": result.relevance_hook or "",
    }


def _detect_known_contacts(prospects: list[dict]) -> list[dict]:
    """Detect prospects with existing conversation history (>5 messages).

    Returns a list of dicts with name and message_count for prospects
    that have significant prior conversations.
    """
    from ..db.schema import get_db
    db = get_db()
    known = []
    for p in prospects:
        linkedin_id = (p.get("provider_id") or p.get("public_id") or "").strip()
        if not linkedin_id:
            continue
        row = db.execute(
            """SELECT gc.name, COUNT(m.id) as msg_count
               FROM global_contacts gc
               JOIN contacts c ON c.global_contact_id = gc.id
               JOIN outreaches o ON o.contact_id = c.id
               JOIN messages m ON m.outreach_id = o.id
               WHERE gc.linkedin_id = ?
               GROUP BY gc.id
               HAVING msg_count > 5""",
            (linkedin_id,),
        ).fetchone()
        if row:
            known.append({"name": row["name"], "message_count": row["msg_count"]})
    db.close()
    return known


def _post_filter_classic_prospects(
    prospects: list[dict],
    segments: list[dict],
    segment_map: dict[str, int],
) -> list[dict]:
    """Apply ICP criteria that Classic LinkedIn doesn't support as search filters.

    Classic search only uses industry + location as structured filters, dropping
    seniority, company_headcount, etc. This function applies soft title-based
    filtering to remove obvious mismatches (e.g., PhD students, interns when
    targeting VP-level sales leaders).
    """
    filtered = []
    for p in prospects:
        key = p.get("public_id") or p.get("linkedin_url") or p.get("name")
        seg_idx = segment_map.get(key, 0) if key else 0
        segment = segments[seg_idx] if seg_idx < len(segments) else {}

        include, exclude = _segment_seniority(segment)
        if include or exclude:
            title = p.get("title") or p.get("headline") or ""
            if title and not _title_matches_seniority(title, include, exclude):
                continue

        filtered.append(p)
    return filtered


def _segment_seniority(segment: dict) -> tuple[list[str], list[str]]:
    """Canonical (include, exclude) levels for a segment.

    A segment's `seniority` is a bare list on search segments and an
    include/exclude dict on ICP personas, and either can hold the ICP prompt's
    LinkedIn vocabulary ("vice_president"). Both shapes, one answer.
    """
    from ..services.seniority import normalize_seniority_list

    value = segment.get("seniority")
    if isinstance(value, dict):
        return (
            normalize_seniority_list(value.get("include")),
            normalize_seniority_list(value.get("exclude")),
        )
    return normalize_seniority_list(value), []


def _title_matches_seniority(
    title: str,
    include: list[str],
    exclude: list[str] | None = None,
) -> bool:
    """Does this title clear the segment's seniority filter?

    Two defects lived here. It tested `kw in title`, so "cto" matched
    "dire(cto)r" and every Director passed a CXO-only filter — the substring
    family `textutil.contains_term` was written for. And it looked levels up in
    `SENIORITY_KEYWORDS` by their raw ICP spelling, so an ICP written in the
    prompts' own vocabulary ("vice_president") found an empty keyword list,
    returned False for everyone, and the Classic post-filter dropped 100% of
    the prospects the search had just found.

    A title that states no level at all passes: Classic search rows often carry
    a company tagline rather than a role, and dropping them is a judgement
    about the row, not the person.
    """
    from ..services.seniority import (
        infer_seniority_level,
        normalize_seniority_list,
    )

    level = infer_seniority_level(title)
    if level is None:
        return True
    # Normalised here as well as in `_segment_seniority`: this is called
    # directly from `icp preview` and from tests with raw ICP spellings, and
    # an include list that normalises to nothing must read as "no filter",
    # never as "exclude everyone".
    if normalize_seniority_list(exclude) and level in normalize_seniority_list(exclude):
        return False
    include = normalize_seniority_list(include)
    if not include:
        return True
    return level in include
