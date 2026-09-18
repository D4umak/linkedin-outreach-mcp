"""Signal service — orchestrates signal collection, classification, and lifecycle.

Central coordination layer for the proactive signal-based selling pipeline.
Delegates to collectors for detection and to signal_queries for persistence.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Watchlist management (auto-create from ICP)
# ──────────────────────────────────────────────

def create_watchlists_from_icp(
    icp_json: str | dict,
    campaign_id: str | None = None,
    icp_name: str = "",
) -> list[str]:
    """Auto-create signal watchlists from ICP keywords, pain points, and competitors.

    Extracts relevant terms from the ICP and creates watchlists:
    1. Pain point keywords → 'keyword' watchlist
    2. Competitor names → 'competitor' watchlist
    3. Industry keywords → 'keyword' watchlist

    Args:
        icp_json: ICP data (JSON string or dict).
        campaign_id: Optional campaign to link watchlists to.
        icp_name: ICP name for watchlist naming.

    Returns:
        List of created watchlist IDs.
    """
    from ..db.signal_queries import list_watchlists, save_watchlist

    # Parse ICP data
    if isinstance(icp_json, str):
        try:
            parsed = json.loads(icp_json)
        except (json.JSONDecodeError, TypeError):
            return []
    else:
        parsed = icp_json

    # Handle nested ICP structure (icps list or single ICP)
    icps_list = parsed.get("icps", [parsed]) if isinstance(parsed, dict) else [parsed]

    created_ids: list[str] = []
    existing_keywords: set[str] = set()

    # Gather existing keywords to avoid duplicates
    for wl in list_watchlists(is_active=True, campaign_id=campaign_id):
        for kw in wl.get("keywords_list", []):
            existing_keywords.add(kw.lower().strip())

    for icp in icps_list:
        if not isinstance(icp, dict):
            continue

        persona_name = icp.get("name", icp_name or "ICP")

        # 1. Pain point keywords
        pain_points = icp.get("pain_points", [])
        if pain_points:
            # Extract short keyword phrases from pain points
            pain_keywords = _extract_keywords_from_phrases(pain_points)
            new_pain = [k for k in pain_keywords if k.lower().strip() not in existing_keywords]
            if new_pain:
                wl_id = save_watchlist(
                    name=f"{persona_name} — Pain Points",
                    watch_type="keyword",
                    keywords=new_pain[:10],  # Cap at 10 keywords
                    campaign_id=campaign_id,
                )
                created_ids.append(wl_id)
                existing_keywords.update(k.lower() for k in new_pain)

        # 2. Competitor names
        competitors = icp.get("competitors", [])
        if not competitors:
            # Try alternative fields
            competitors = icp.get("competitor_names", [])
        if competitors:
            comp_names = []
            for c in competitors:
                if isinstance(c, dict):
                    comp_names.append(c.get("name", ""))
                elif isinstance(c, str):
                    comp_names.append(c)
            # Disambiguate known ambiguous competitor names
            _AMBIGUOUS_COMPETITORS = {
                "apollo": "Apollo.io", "outreach": "Outreach.io",
                "salesloft": "SalesLoft", "drift": "Drift.com",
                "chorus": "Chorus.ai", "gong": "Gong.io",
            }
            comp_names = [
                _AMBIGUOUS_COMPETITORS.get(n.lower().strip(), n)
                for n in comp_names
            ]
            comp_names = [n for n in comp_names if n and n.lower().strip() not in existing_keywords]
            if comp_names:
                wl_id = save_watchlist(
                    name=f"{persona_name} — Competitors",
                    watch_type="competitor",
                    keywords=comp_names[:10],
                    campaign_id=campaign_id,
                )
                created_ids.append(wl_id)
                existing_keywords.update(n.lower() for n in comp_names)

        # 3. Industry/topic keywords
        keywords = icp.get("keywords", [])
        if keywords:
            new_kw = [k for k in keywords if isinstance(k, str) and k.lower().strip() not in existing_keywords]
            if new_kw:
                wl_id = save_watchlist(
                    name=f"{persona_name} — Industry Keywords",
                    watch_type="industry",
                    keywords=new_kw[:10],
                    campaign_id=campaign_id,
                )
                created_ids.append(wl_id)
                existing_keywords.update(k.lower() for k in new_kw)

        # 4. Target company watchlists (for company page monitoring)
        # NOTE: The ICP generator does NOT populate target_companies.
        # Primary company watchlist creation now happens in create_campaign.py
        # Step 6a-extra (auto-creates from top prospect companies).
        # This section is kept as a secondary source for manual ICP input
        # or future ABM ICP generators that provide explicit company targets.
        target_companies = icp.get("target_companies", []) or icp.get("companies", [])
        if not target_companies:
            search_params = icp.get("linkedin_search_params", {})
            if isinstance(search_params, dict):
                target_companies = search_params.get("companies", [])

        if target_companies:
            for company in target_companies[:3]:
                name = company if isinstance(company, str) else company.get("name", "")
                identifier = name
                if isinstance(company, dict):
                    identifier = company.get("linkedin_id", "") or company.get("url", "") or name
                if name and name.lower().strip() not in existing_keywords:
                    wl_id = save_watchlist(
                        name=f"{name} — Company Page",
                        watch_type="company",
                        keywords=[identifier],
                        campaign_id=campaign_id,
                    )
                    created_ids.append(wl_id)
                    existing_keywords.add(name.lower())

    return created_ids


def _extract_keywords_from_phrases(phrases: list[str], max_words: int = 4) -> list[str]:
    """Extract concise keyword phrases from longer pain point descriptions.

    Keeps phrases short enough for effective LinkedIn post search.
    """
    keywords: list[str] = []
    for phrase in phrases:
        if not isinstance(phrase, str):
            continue
        phrase = phrase.strip()
        words = phrase.split()
        if len(words) <= max_words:
            keywords.append(phrase)
        else:
            # Take the first max_words as a keyword phrase
            keywords.append(" ".join(words[:max_words]))
    return keywords


# ──────────────────────────────────────────────
# Signal lifecycle
# ──────────────────────────────────────────────

def expire_stale_signals() -> str:
    """Expire signals past their TTL. Returns summary."""
    from ..db.signal_queries import expire_old_signals
    count = expire_old_signals()
    if count:
        logger.info("Expired %d stale signals", count)
    return f"Expired {count} stale signals." if count else "No signals expired."


def run_decay_cycle() -> str:
    """Run full signal decay cycle: expire → recompute → expire intent events.

    This is the scheduled decay engine that runs every 6 hours.
    1. Expire signals past their TTL
    2. Expire old intent events past their expiry
    3. Recompute all signal account composite scores (reflect decayed weights)

    Returns summary of actions taken.
    """
    import time as _time
    from ..db.signal_queries import expire_old_signals
    from ..db.schema import get_db
    from ..services.signal_scorer import recompute_all_signal_accounts

    parts: list[str] = []

    # Step 1: Expire old signals
    expired_signals = expire_old_signals()
    if expired_signals:
        parts.append(f"expired {expired_signals} signals")

    # Step 2: Expire old intent events
    now = int(_time.time())
    try:
        db = get_db()
        cursor = db.execute(
            "UPDATE intent_events SET action_taken = COALESCE(action_taken, 'expired') "
            "WHERE expires_at <= ? AND action_taken IS NULL",
            (now,),
        )
        expired_intents = cursor.rowcount
        db.commit()
        db.close()
        if expired_intents:
            parts.append(f"expired {expired_intents} intent events")
    except Exception as e:
        logger.debug("Intent event expiry failed: %s", e)

    # Step 3: Recompute signal account scores
    try:
        recompute_result = recompute_all_signal_accounts()
        parts.append(recompute_result.lower())
    except Exception as e:
        logger.debug("Score recompute failed: %s", e)
        parts.append("score recompute failed")

    summary = "Decay cycle: " + (", ".join(parts) if parts else "no changes")
    logger.info(summary)
    return summary


# ──────────────────────────────────────────────
# Signal dashboard helpers
# ──────────────────────────────────────────────

def format_signal_dashboard(days: int = 7) -> str:
    """Format the Signal Intelligence dashboard section for show_status.

    Returns a formatted string with signal stats, top prospects, and recent signals.
    """
    from ..db.signal_queries import (
        get_signal_funnel_stats,
        list_signal_accounts,
        list_signals,
        list_watchlists,
    )

    stats = get_signal_funnel_stats(days=days)
    total = stats.get("total", 0)

    if total == 0:
        watchlists = list_watchlists(is_active=True)
        if not watchlists:
            return ""  # No signal system active yet
        return (
            f"\n\u2500\u2500 Signal Intelligence \u2500\u2500\n"
            f"Active watchlists: {len(watchlists)}\n"
            f"No signals detected in the last {days} days.\n"
        )

    lines: list[str] = []
    lines.append(f"\n\u2500\u2500 Signal Intelligence \u2500\u2500")

    # Watchlist health
    watchlists = list_watchlists(is_active=True)
    total_keywords = sum(len(wl.get("keywords_list", [])) for wl in watchlists)
    lines.append(f"Active watchlists: {len(watchlists)} ({total_keywords} keywords tracked)")

    # Signal type breakdown
    by_type = stats.get("by_type", {})
    lines.append(f"Signals (last {days} days): {total} total")
    type_parts = []
    for sig_type, count in sorted(by_type.items(), key=lambda x: -x[1]):
        label = sig_type.replace("_", " ").title()
        type_parts.append(f"  {label}: {count}")
    if type_parts:
        lines.extend(type_parts)

    # Top signal-heavy prospects (with composite scores)
    top_accounts = list_signal_accounts(limit=5)
    if top_accounts:
        lines.append("")
        lines.append("\U0001f525 Top Signal-Heavy Accounts:")
        for i, acct in enumerate(top_accounts, 1):
            name = acct.get("prospect_name", "Unknown")
            company = acct.get("company") or ""
            count = acct.get("total_signals", 0)
            score = acct.get("composite_score", 0)
            top_type = (acct.get("top_signal_type") or "").replace("_", " ")
            line = f"  {i}. {name}"
            if company:
                line += f" ({company})"
            line += f" \u2014 Score: {score:.2f}"
            line += f" ({count} signals"
            if top_type:
                line += f", top: {top_type}"
            line += ")"
            lines.append(line)
    elif stats.get("top_prospects"):
        # Fallback to basic top prospects from stats
        top_prospects = stats["top_prospects"]
        lines.append("")
        lines.append("\U0001f525 Top Signal-Heavy Prospects:")
        for i, p in enumerate(top_prospects[:5], 1):
            name = p.get("prospect_name", "Unknown")
            title = p.get("prospect_title") or ""
            count = p.get("cnt", 0)
            score = p.get("max_score", 0)
            line = f"  {i}. {name}"
            if title:
                line += f" ({title})"
            line += f" \u2014 {count} signals, score: {score:.2f}"
            lines.append(line)

    # Signal activation stats
    by_status = stats.get("by_status", {})
    actioned = by_status.get("actioned", 0)
    classified = by_status.get("classified", 0)
    new_count = by_status.get("new", 0)
    if actioned > 0 or classified > 0:
        lines.append("")
        lines.append("Pipeline:")
        pipeline_parts = []
        if new_count:
            pipeline_parts.append(f"{new_count} new")
        if classified:
            pipeline_parts.append(f"{classified} classified")
        if actioned:
            pipeline_parts.append(f"{actioned} actioned")
        lines.append(f"  {' → '.join(pipeline_parts)}")

    # Signal-triggered outreach performance (if any exist)
    try:
        from ..db.signal_queries import get_signal_performance
        perf = get_signal_performance()
        sig_total = perf["signal"]["total"]
        if sig_total > 0:
            lines.append("")
            lines.append("Signal vs Cold Outreach:")
            sig_acc = perf["signal"]["acceptance_rate"]
            cold_acc = perf["cold"]["acceptance_rate"]
            lines.append(
                f"  Signal-triggered: {sig_total} outreaches "
                f"({sig_acc:.0%} accept)"
            )
            cold_total = perf["cold"]["total"]
            if cold_total > 0:
                lines.append(
                    f"  Cold outreach: {cold_total} outreaches "
                    f"({cold_acc:.0%} accept)"
                )
                lift = perf.get("signal_lift", {})
                acc_lift = lift.get("acceptance")
                if acc_lift is not None and acc_lift != 0:
                    direction = "↑" if acc_lift > 0 else "↓"
                    lines.append(
                        f"  Signal lift: {direction}{abs(acc_lift):.0%} acceptance"
                    )
    except Exception:
        pass

    # Compound intent events (stacked signals → high-value composite events)
    try:
        from ..db.signal_queries import get_intent_event_stats, list_intent_events

        intent_stats = get_intent_event_stats(days=days)
        intent_total = intent_stats.get("total", 0)
        if intent_total > 0:
            lines.append("")
            lines.append("\u26a1 Compound Intent Events:")
            intent_by_type = intent_stats.get("by_type", {})
            for etype, edata in sorted(
                intent_by_type.items(), key=lambda x: -x[1].get("count", 0)
            ):
                label = etype.replace("_", " ").title()
                cnt = edata.get("count", 0)
                avg = edata.get("avg_score", 0)
                lines.append(f"  {label}: {cnt} (avg score: {avg:.2f})")

            # Show top active intent events
            active_events = list_intent_events(active_only=True, limit=3)
            if active_events:
                for ie in active_events:
                    etype = ie.get("event_type", "").replace("_", " ").title()
                    lid = ie.get("linkedin_id", "")
                    score = ie.get("composite_score", 0)
                    types = ie.get("signal_types_list", [])
                    types_str = " + ".join(t.replace("_", " ") for t in types)
                    company = ie.get("company") or ""
                    detected = ie.get("detected_at", 0)
                    age = _format_time_ago(detected)
                    line = f"  → [{age}] {etype}"
                    if company:
                        line += f" ({company})"
                    line += f" — {types_str} (score: {score:.2f})"
                    lines.append(line)
    except Exception:
        pass

    # Recent signals (last 5)
    recent = list_signals(limit=5)
    if recent:
        lines.append("")
        lines.append("Recent Signals:")
        for sig in recent:
            sig_type = sig.get("signal_type", "unknown").upper()
            name = sig.get("prospect_name") or "Unknown"
            content_preview = (sig.get("content") or "")[:60]
            detected = sig.get("detected_at", 0)
            age = _format_time_ago(detected)
            lines.append(f"  \u2022 [{age}] {sig_type}: {content_preview}... by {name}")

    return "\n".join(lines)


def format_signal_feed(
    *,
    signal_type: str | None = None,
    campaign_id: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> str:
    """Format a detailed signal feed for the show_signals tool."""
    from ..db.signal_queries import list_signals

    # Ranked by stored signal_score (issue #65): scores are persisted at save
    # and classification time, so the feed can finally order by value instead
    # of pure recency. detected_at breaks ties so equal scores stay stable.
    signals = list_signals(
        signal_type=signal_type,
        campaign_id=campaign_id,
        status=status,
        limit=limit,
        order_by="signal_score DESC, detected_at DESC",
    )

    if not signals:
        filters = []
        if signal_type:
            filters.append(f"type={signal_type}")
        if campaign_id:
            filters.append(f"campaign={campaign_id}")
        filter_str = f" (filters: {', '.join(filters)})" if filters else ""
        return f"No signals found{filter_str}."

    lines: list[str] = []
    lines.append(f"Signal Feed ({len(signals)} signals, ranked by score):\n")

    # Lazy-import scorer for computed scores
    try:
        from .signal_scorer import compute_signal_score as _compute_score
    except ImportError:
        _compute_score = None

    for sig in signals:
        sig_type = sig.get("signal_type", "unknown")
        name = sig.get("prospect_name") or "Unknown"
        title = sig.get("prospect_title") or ""
        content = (sig.get("content") or "")[:120]
        intent = sig.get("intent") or "unclassified"
        confidence = sig.get("confidence", 0)
        stored_score = sig.get("signal_score", 0)
        sig_status = sig.get("status", "new")
        detected = sig.get("detected_at", 0)
        age = _format_time_ago(detected)

        # Display the stored score — it is the sort key, so the numbers shown
        # match the order claimed in the header. Live recompute only covers
        # rows the backfill has not reached yet (stored 0).
        display_score = stored_score or (_compute_score(sig) if _compute_score else 0)

        # Parse metadata for extra context
        metadata = {}
        try:
            metadata = json.loads(sig.get("metadata_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            pass

        # Score tier indicator
        if display_score >= 0.3:
            tier_icon = "\U0001f525"  # fire
        elif display_score >= 0.15:
            tier_icon = "\U0001f7e1"  # yellow
        else:
            tier_icon = "\u26aa"  # white

        header = f"\u250c {tier_icon} {sig_type.upper()} [{sig_status}] ({age})"
        lines.append(header)
        lines.append(f"\u2502 Prospect: {name}")
        if title:
            lines.append(f"\u2502 Title: {title}")
        if content:
            lines.append(f"\u2502 Content: {content}...")
        if intent != "unclassified":
            lines.append(f"\u2502 Intent: {intent} ({confidence:.0%} confidence)")
        if display_score > 0:
            lines.append(f"\u2502 Score: {display_score:.2f}")
        # Type-specific metadata context
        extra = _format_signal_metadata(sig_type, metadata)
        if extra:
            lines.append(f"\u2502 {extra}")
        # Action taken (if actioned)
        action = sig.get("action_taken")
        if action:
            lines.append(f"\u2502 Action: {action.replace('_', ' ')}")
        lines.append(f"\u2514")
        lines.append("")

    return "\n".join(lines)


def _format_signal_metadata(sig_type: str, metadata: dict) -> str:
    """Format type-specific metadata context for a signal entry."""
    if not metadata:
        return ""

    if sig_type == "keyword_mention":
        keyword = metadata.get("keyword", "")
        wl_name = metadata.get("watchlist_name", "")
        if keyword:
            return f'Keyword: "{keyword}"' + (f" (watchlist: {wl_name})" if wl_name else "")

    elif sig_type == "competitor_mention":
        comp_name = metadata.get("competitor_name", "")
        context = metadata.get("mention_context", "")
        parts = []
        if comp_name:
            parts.append(f"Competitor: {comp_name}")
        if context and context != "general":
            parts.append(f"Context: {context.replace('_', ' ')}")
        return " | ".join(parts)

    elif sig_type == "company_change":
        old_co = metadata.get("old_company", "")
        new_co = metadata.get("new_company", "")
        old_t = metadata.get("old_title", "")
        new_t = metadata.get("new_title", "")
        old_sen = metadata.get("old_seniority", "")
        new_sen = metadata.get("new_seniority", "")
        parts = []
        if old_co and new_co:
            parts.append(f"Company: {old_co} → {new_co}")
        if old_t and new_t and old_t != new_t:
            parts.append(f"Title: {old_t} → {new_t}")
        if old_sen and new_sen and old_sen != new_sen:
            parts.append(f"Seniority: {old_sen} → {new_sen}")
        return " | ".join(parts)

    elif sig_type == "promotion":
        old_t = metadata.get("old_title", "")
        new_t = metadata.get("new_title", "")
        company = metadata.get("company", "")
        old_sen = metadata.get("old_seniority", "")
        new_sen = metadata.get("new_seniority", "")
        parts = []
        if old_t and new_t:
            parts.append(f"Promoted: {old_t} → {new_t}")
        if company:
            parts.append(f"at {company}")
        if old_sen and new_sen:
            parts.append(f"({old_sen} → {new_sen})")
        return " ".join(parts)

    elif sig_type == "headline_intent":
        cats = metadata.get("intent_categories", [])
        headline = metadata.get("new_headline", "")
        parts = []
        if cats:
            parts.append(f"Intent: {', '.join(cats)}")
        if headline:
            parts.append(f'"{headline[:80]}"')
        return " | ".join(parts)

    elif sig_type == "headline_change":
        if metadata.get("is_additional_role"):
            pos_t = metadata.get("new_position_title", "")
            pos_co = metadata.get("new_position_company", "")
            return f"New role: {pos_t} at {pos_co}"
        old_h = metadata.get("old_headline", "")
        new_h = metadata.get("new_headline", "")
        if old_h and new_h:
            return f'"{old_h[:50]}" → "{new_h[:50]}"'

    # Backward compat for old job_change signals
    elif sig_type == "job_change":
        old_title = metadata.get("old_title", "")
        new_title = metadata.get("new_title", "")
        old_company = metadata.get("old_company", "")
        new_company = metadata.get("new_company", "")
        if old_title and new_title:
            return f"Changed: {old_title} → {new_title}"
        if old_company and new_company:
            return f"Moved: {old_company} → {new_company}"

    elif sig_type == "hiring_surge":
        job_count = metadata.get("job_count", 0)
        titles = metadata.get("job_titles", [])
        company = metadata.get("company_name", "")
        parts = []
        if company and job_count:
            parts.append(f"{company}: {job_count} open roles")
        if titles:
            parts.append(f"Roles: {', '.join(titles[:3])}")
        return " | ".join(parts)

    elif sig_type in ("funding_event", "news_event"):
        news_type = metadata.get("news_type", "")
        source = metadata.get("source", "")
        date = metadata.get("date", "")
        parts = []
        if news_type and news_type != "general":
            parts.append(f"Type: {news_type}")
        if source:
            parts.append(f"Source: {source}")
        if date:
            parts.append(f"Date: {date}")
        return " | ".join(parts)

    elif sig_type in (
        "news_funding", "news_acquisition", "news_exec_hire",
        "news_expansion", "news_product_launch", "news_layoffs",
    ):
        company = metadata.get("company_name", "")
        source = metadata.get("source", "")
        date = metadata.get("date", "")
        link = metadata.get("link", "")
        # Friendly labels for signal sub-types
        label_map = {
            "news_funding": "Funding",
            "news_acquisition": "Acquisition/M&A",
            "news_exec_hire": "Executive Hire",
            "news_expansion": "Expansion",
            "news_product_launch": "Product Launch",
            "news_layoffs": "Layoffs/Restructuring",
        }
        label = label_map.get(sig_type, "News")
        parts = [label]
        if company:
            parts.append(f"Company: {company}")
        if source:
            parts.append(f"Source: {source}")
        if date:
            parts.append(f"Date: {date}")
        return " | ".join(parts)

    elif sig_type == "company_post_comment":
        company = metadata.get("company_name", "")
        commenter = metadata.get("commenter_name", "")
        parts = []
        if commenter:
            parts.append(f"Commenter: {commenter}")
        if company:
            parts.append(f"On: {company} post")
        return " | ".join(parts) if parts else "Comment on company post"

    elif sig_type == "company_post_reaction":
        reactor = metadata.get("reactor_name", "")
        company = metadata.get("company_name", "")
        parts = []
        if reactor:
            parts.append(f"Reactor: {reactor}")
        if company:
            parts.append(f"On: {company} post")
        return " | ".join(parts) if parts else "Reaction on company post"

    elif sig_type == "company_follower":
        follower = metadata.get("follower_name", "")
        company = metadata.get("company_name", "")
        if follower and company:
            return f"New follower: {follower} → {company}"
        if follower:
            return f"New follower: {follower}"
        return "New company page follower"

    # Website visitor tracking (Phase 3)
    elif sig_type == "website_high_intent":
        company = metadata.get("company_name", "")
        page = metadata.get("page_url", "")
        parts = []
        if company:
            parts.append(f"Company: {company}")
        if page:
            parts.append(f"Page: {page}")
        return " | ".join(parts) if parts else "High-intent website visit"

    elif sig_type == "website_visit":
        company = metadata.get("company_name", "")
        page = metadata.get("page_url", "")
        if company:
            return f"Website visit from {company}"
        if page:
            return f"Website visit: {page}"
        return "Website visit detected"

    elif sig_type in ("reddit_mention", "hn_mention", "g2_review", "ats_hiring"):
        label_map = {
            "reddit_mention": "Reddit",
            "hn_mention": "HN",
            "g2_review": "G2",
            "ats_hiring": "ATS hiring",
        }
        label = label_map.get(sig_type, "Web")
        term = metadata.get("term", "") or metadata.get("company_name", "")
        link = metadata.get("link", "")
        parts = [label]
        if term:
            parts.append(f'Term: "{term}"')
        if link:
            parts.append(f"Link: {link}")
        return " | ".join(parts)

    elif sig_type == "profile_view":
        # Phase 4: Enhanced profile view with ICP match info
        is_icp = metadata.get("is_icp_match", False)
        fit = metadata.get("fit_score", 0)
        seniority = metadata.get("viewer_seniority", "")
        company = metadata.get("viewer_company", "")
        parts = ["Profile view"]
        if is_icp:
            parts[0] = "ICP MATCH — Profile view"
            if fit:
                parts.append(f"Fit: {fit:.0%}")
        if seniority and seniority != "unknown":
            parts.append(f"Seniority: {seniority.replace('_', '-')}")
        if company:
            parts.append(f"Company: {company}")
        return " | ".join(parts)

    elif sig_type == "commenter_match":
        match_type = metadata.get("match_type", "")
        if match_type:
            return f"Match: {match_type.replace('_', ' ')}"

    elif sig_type == "prospect_post":
        keywords_matched = metadata.get("keywords_matched", [])
        if keywords_matched:
            return f"Keywords: {', '.join(keywords_matched[:5])}"

    # Fallback: keyword field
    keyword = metadata.get("keyword", "")
    if keyword:
        return f'Keyword: "{keyword}"'

    return ""


def _format_time_ago(unix_ts: int) -> str:
    """Format a unix timestamp as a human-readable time ago string."""
    if not unix_ts:
        return "unknown"
    diff = int(time.time()) - unix_ts
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    days = diff // 86400
    return f"{days}d ago"
