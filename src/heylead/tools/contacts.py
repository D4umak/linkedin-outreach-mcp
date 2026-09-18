"""Tool: contacts — Manage the global contact base.

Search, browse, tag, note, and view cross-campaign history for all contacts.
One master record per person across all campaigns.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import time
from datetime import datetime, timezone

from ..db import aio as db
from ..linkedin import (
    UnipileAuthError,
    UnipileRateLimitError,
    UnipileResultFormatError,
    get_account_id,
    get_linkedin_client,
)
from ..formatter import source_badge, stars, table
from ..db.async_bridge import run_db

logger = logging.getLogger(__name__)

# ── linkedin_search bounds ──
# Every result costs one profile fetch plus one posts fetch, so the page the
# tool asks LinkedIn for is capped. Nothing here inspects the query: LinkedIn
# is the only thing that knows which keyword strings it can answer, and
# refusing a search locally would turn a slow empty result into a permanent no.
LINKEDIN_SEARCH_MAX_RESULTS = 25
LINKEDIN_SEARCH_EXAMPLE = "Acme Corp CTO"

# ── linkedin_search pacing ──
# A 25-result search fires 51 upstream calls (1 search + 25 profiles + 25 posts)
# back to back. LinkedIn rate-limited after about 8 of them. The paced backend
# path in this repo sits 75s apart between leads, but that lives in a background
# job; linkedin_search is a synchronous MCP call, so whatever it waits, the
# caller waits at the keyboard.
#
# So the gap is derived from a fixed total, not fixed per call:
#   gap = min(PACE_SECONDS, BUDGET / planned_calls)
# 25 results -> 50 fetches -> 1.2s apart; 5 results -> 10 fetches -> 2.0s apart.
# Either way the tool never sits waiting longer than the budget, and the numbers
# it actually used are printed in the result.
#
# Backoff is keyed on a real HTTP 429 (see UnipileRateLimitError), never on an
# empty profile: get_profile returns {} for private profiles, 404s and timeouts
# too, so backing off on emptiness would spend the whole budget on ten
# contiguous private profiles and still not react to an actual rate limit.
LINKEDIN_SEARCH_PACE_SECONDS = 2.0
LINKEDIN_SEARCH_PACE_BUDGET_SECONDS = 60.0
LINKEDIN_SEARCH_RETRY_AFTER_CAP_SECONDS = 30.0

# Indirection so tests can record the requested delays instead of serving them.
_pace_sleep = asyncio.sleep


class _SearchPacer:
    """Spends a fixed wall-clock budget spreading linkedin_search's fetches out.

    Every wait is charged to the same budget — the planned gaps and the ones a
    429's Retry-After asks for. When the budget is gone the tool stops fetching
    rather than firing the remainder back to back, and says so in the output.
    """

    def __init__(
        self,
        planned_calls: int,
        gap: float | None = None,
        budget: float | None = None,
        retry_after_cap: float | None = None,
    ) -> None:
        # Read at construction time, not as default arguments: a default is
        # evaluated once when this file is imported, which silently freezes the
        # constants and makes anything that reassigns them — a test, a future
        # config load — look like it worked while changing nothing.
        if gap is None:
            gap = LINKEDIN_SEARCH_PACE_SECONDS
        if budget is None:
            budget = LINKEDIN_SEARCH_PACE_BUDGET_SECONDS
        if retry_after_cap is None:
            retry_after_cap = LINKEDIN_SEARCH_RETRY_AFTER_CAP_SECONDS
        self.budget = budget
        self.retry_after_cap = retry_after_cap
        self.initial_gap = min(gap, budget / planned_calls) if planned_calls > 0 else gap
        self.gap = self.initial_gap
        self.spent = 0.0
        self.waits = 0
        self.rate_limits = 0
        self.backoffs_served = 0
        self.exhausted = False

    def _afford(self, seconds: float) -> bool:
        # 1e-9 slack: budget / n * n overshoots the budget in binary floating
        # point, and the last planned gap must not be refused for that.
        return self.spent + seconds <= self.budget + 1e-9

    async def wait(self) -> bool:
        """Pause before the next upstream call. False means: do not make it."""
        if not self._afford(self.gap):
            self.exhausted = True
            return False
        await _pace_sleep(self.gap)
        self.spent += self.gap
        self.waits += 1
        return True

    async def backoff(self, retry_after: float | None) -> bool:
        """Serve a 429. False means the budget is gone — do not retry the call."""
        self.rate_limits += 1
        wait = retry_after if retry_after and retry_after > 0 else self.gap * 2
        wait = min(wait, self.retry_after_cap)
        if not self._afford(wait):
            self.exhausted = True
            return False
        await _pace_sleep(wait)
        self.spent += wait
        self.waits += 1
        self.backoffs_served += 1
        # Give everything after a rate limit more room, still inside the budget.
        self.gap = min(self.gap * 2, self.retry_after_cap)
        return True


async def _fetch_paced(call, pacer: _SearchPacer, default):
    """Run one paced upstream fetch, retrying it once after a real 429.

    ``call`` is a zero-argument factory returning the coroutine, so the retry
    can build a fresh one. Only rate limits are handled here; every other
    exception propagates to the caller's existing handling, which is what keeps
    a private profile or a 404 behaving exactly as it did before.
    """
    try:
        return await call()
    except UnipileRateLimitError as e:
        if not await pacer.backoff(e.retry_after):
            return default
        try:
            return await call()
        except UnipileRateLimitError:
            return default


def _ts_to_date(ts: int | None) -> str:
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _ts_to_relative(ts: int | None) -> str:
    if not ts:
        return "—"
    now = int(time.time())
    diff = now - ts
    days = abs(diff) // 86400
    if diff < 0:
        return f"in {days}d" if days > 0 else "just now"
    if days == 0:
        return "today"
    elif days == 1:
        return "yesterday"
    elif days < 30:
        return f"{days}d ago"
    elif days < 365:
        return f"{days // 30}mo ago"
    else:
        return f"{days // 365}y ago"


def _lifecycle_icon(stage: str) -> str:
    return {
        "prospect": "○",
        "contacted": "◔",
        "connected": "◑",
        "engaged": "◕",
        "customer": "●",
        "lost": "✗",
        "churned": "↻",
        "do_not_contact": "⊘",
    }.get(stage, "?")


async def run_contacts(
    action: str = "list",
    query: str = "",
    contact_id: str = "",
    lifecycle_stage: str = "",
    tag: str = "",
    note: str = "",
    min_fit_score: float = 0.0,
    limit: int = 25,
    format: str = "table",
    campaign_id: str = "",
    match: str = "name",
    dry_run: bool = True,
    connected_since: str = "",
    connected_before: str = "",
) -> str:
    """Route contacts actions."""
    setup_done = await db.get_setting("setup_complete", False)
    if not setup_done:
        return "Setup required. Run setup_profile first."

    action = action.strip().lower()

    if action == "list":
        return await _handle_list(lifecycle_stage, tag, min_fit_score, limit)
    elif action == "search":
        return await _handle_search(query, lifecycle_stage, tag, min_fit_score, limit)
    elif action == "view":
        return await _handle_view(contact_id)
    elif action == "tag":
        return await _handle_tag(contact_id, tag)
    elif action == "note":
        return await _handle_note(contact_id, note)
    elif action == "stage":
        return await _handle_stage(contact_id, lifecycle_stage)
    elif action == "stats":
        return await _handle_stats()
    elif action == "export":
        return await _handle_export(lifecycle_stage, tag, format)
    elif action == "linkedin_search":
        return await _handle_linkedin_search(query, limit)
    elif action == "link":
        return await _handle_link(campaign_id, match, dry_run, limit)
    elif action == "enrich":
        return await _handle_enrich(contact_id, query, limit)
    elif action == "find_duplicates":
        return await _handle_find_duplicates()
    elif action == "merge":
        return await _handle_merge(contact_id, query)
    elif action in ("my_connections", "search_connections"):
        return await _handle_my_connections(
            query, limit, connected_since, connected_before,
        )
    else:
        return (
            f"Unknown action: {action!r}\n\n"
            "Available actions: list, search, view, tag, note, stage, stats, export, "
            "linkedin_search, link, enrich, find_duplicates, merge, my_connections"
        )


# ──────────────────────────────────────────────
# Action handlers
# ──────────────────────────────────────────────

async def _handle_list(
    lifecycle_stage: str, tag: str, min_fit_score: float, limit: int
) -> str:
    contacts = await db.search_global_contacts(
        lifecycle_stage=lifecycle_stage,
        tag=tag,
        min_fit_score=min_fit_score,
        limit=limit,
        order_by="updated_at DESC",
    )
    if not contacts:
        filters = []
        if lifecycle_stage:
            filters.append(f"stage={lifecycle_stage}")
        if tag:
            filters.append(f"tag={tag}")
        if min_fit_score > 0:
            filters.append(f"score>={min_fit_score}")
        filter_str = f" (filters: {', '.join(filters)})" if filters else ""
        return f"No contacts found{filter_str}.\n\nContacts are added automatically when you create campaigns."

    return _format_contact_list(contacts, limit)


async def _handle_search(
    query: str, lifecycle_stage: str, tag: str, min_fit_score: float, limit: int
) -> str:
    if not query:
        return "Search query is required. Usage: contacts(action='search', query='CEO fintech')"

    contacts = await db.search_global_contacts(
        query=query,
        lifecycle_stage=lifecycle_stage,
        tag=tag,
        min_fit_score=min_fit_score,
        limit=limit,
    )
    if not contacts:
        return f"No contacts found matching '{query}'."

    return _format_contact_list(contacts, limit, title=f"Search: \"{query}\"")


async def _handle_view(contact_id: str) -> str:
    if not contact_id:
        return "contact_id is required. Usage: contacts(action='view', contact_id='...')"

    history = await db.get_cross_campaign_history(contact_id)
    if not history:
        return f"Contact {contact_id!r} not found."

    gc = history["global_contact"]
    campaigns = history["campaigns"]
    signals = history["signals"]

    lines = [
        f"Contact: {gc['name']}",
        "=" * 60,
        "",
        f"  Title:     {gc.get('title') or '—'}",
        f"  Company:   {gc.get('company') or '—'}",
        f"  LinkedIn:  {('[Profile](' + gc['linkedin_url'] + ')') if gc.get('linkedin_url') else gc.get('linkedin_id') or '—'}",
        f"  Email:     {gc.get('email') or '—'}",
        f"  Location:  {gc.get('location') or '—'}",
        "",
        f"  Lifecycle: {_lifecycle_icon(gc.get('lifecycle_stage', 'prospect'))} {gc.get('lifecycle_stage', 'prospect')}",
        f"  Fit Score: {stars(gc.get('fit_score') or 0)} ({gc.get('fit_score', 0):.2f})",
        f"  Source:    {source_badge(gc.get('source', 'search'), gc.get('source_detail', ''))}",
        f"  First seen:     {_ts_to_date(gc.get('created_at'))}",
        f"  Last activity:  {_ts_to_relative(gc.get('last_interaction_at'))}",
        f"  Campaigns:      {gc.get('total_campaigns', 0)}",
    ]

    # Tags
    tags = json.loads(gc.get("tags_json") or "[]")
    if tags:
        lines.append(f"  Tags:      {', '.join(tags)}")

    # Notes
    notes = json.loads(gc.get("notes_json") or "[]")
    if notes:
        lines.append("")
        lines.append("  Notes:")
        for n in notes[-5:]:  # Show last 5
            lines.append(f"    [{_ts_to_date(n.get('created_at'))}] {n.get('text', '')}")

    # Enriched profile data
    _append_enriched_profile(lines, gc)

    # Campaign history
    if campaigns:
        lines.append("")
        lines.append(f"Campaign History ({len(campaigns)} campaigns)")
        lines.append("-" * 60)
        for camp in campaigns:
            outreach = camp.get("outreach") or {}
            status = outreach.get("status", "—")
            msgs = camp.get("messages", [])
            engs = camp.get("engagements", [])
            lines.append(
                f"  {camp['campaign_name']} ({camp.get('campaign_status', '?')})"
            )
            lines.append(
                f"    Status: {status}  |  "
                f"Fit: {camp.get('fit_score', 0):.2f}  |  "
                f"Messages: {len(msgs)}  |  Engagements: {len(engs)}"
            )

            # Show messages
            for msg in msgs[-3:]:  # Last 3 messages
                role = "You" if msg.get("role") == "sdr" else "Them"
                text = (msg.get("text") or "")[:80]
                lines.append(f"      [{role}] {text}")

            # Show engagements
            for eng in engs[-2:]:  # Last 2 engagements
                atype = eng.get("action_type", "?")
                text = (eng.get("text") or eng.get("reaction_type") or "")[:60]
                lines.append(f"      [{atype}] {text}")

    # Signals
    if signals:
        lines.append("")
        lines.append(f"Buying Signals ({len(signals)})")
        lines.append("-" * 60)
        for sig in signals[:5]:
            stype = sig.get("signal_type", "?")
            content = (sig.get("content") or "")[:60]
            when = _ts_to_relative(sig.get("detected_at"))
            lines.append(f"  [{stype}] {content} ({when})")

    lines.append("")
    lines.append(f"ID: {contact_id}")
    return "\n".join(lines)


async def _handle_tag(contact_id: str, tag: str) -> str:
    if not contact_id:
        return "contact_id is required. Usage: contacts(action='tag', contact_id='...', tag='enterprise')"
    if not tag:
        return "tag is required. Prefix with '-' to remove. Examples: 'enterprise', '-enterprise'"

    gc = await db.get_global_contact(contact_id)
    if not gc:
        return f"Contact {contact_id!r} not found."

    if tag.startswith("-"):
        remove_tag = tag[1:].strip()
        await db.remove_global_contact_tag(contact_id, remove_tag)
        return f"Removed tag '{remove_tag}' from {gc['name']}."
    else:
        await db.add_global_contact_tag(contact_id, tag)
        return f"Added tag '{tag.strip().lower()}' to {gc['name']}."


async def _handle_note(contact_id: str, note: str) -> str:
    if not contact_id:
        return "contact_id is required. Usage: contacts(action='note', contact_id='...', note='...')"
    if not note:
        return "note text is required."

    gc = await db.get_global_contact(contact_id)
    if not gc:
        return f"Contact {contact_id!r} not found."

    await db.add_global_contact_note(contact_id, note)
    return f"Note added to {gc['name']}."


async def _handle_stage(contact_id: str, lifecycle_stage: str) -> str:
    if not contact_id:
        return "contact_id is required. Usage: contacts(action='stage', contact_id='...', lifecycle_stage='customer')"
    if not lifecycle_stage:
        return (
            "lifecycle_stage is required. Options: "
            "prospect, contacted, connected, engaged, customer, lost, churned, do_not_contact"
        )

    gc = await db.get_global_contact(contact_id)
    if not gc:
        return f"Contact {contact_id!r} not found."

    old_stage = gc.get("lifecycle_stage", "prospect")
    await db.update_global_contact_lifecycle(contact_id, lifecycle_stage)

    # Re-read to check if it actually changed
    gc2 = await db.get_global_contact(contact_id)
    new_stage = gc2.get("lifecycle_stage", old_stage) if gc2 else old_stage

    if new_stage == old_stage and lifecycle_stage != old_stage:
        return (
            f"Cannot change {gc['name']} from '{old_stage}' to '{lifecycle_stage}' "
            f"(lifecycle only promotes forward)."
        )

    return f"Updated {gc['name']}: {old_stage} -> {new_stage}"


async def _handle_stats() -> str:
    stats = await db.get_global_contact_stats()

    if stats["total"] == 0:
        return "No contacts in the global base yet.\n\nContacts are added automatically when you create campaigns."

    lines = [
        "Contact Base",
        "=" * 50,
        "",
        f"  Total contacts: {stats['total']}",
        "",
        "  By Lifecycle:",
    ]

    for stage, count in stats["by_lifecycle"].items():
        icon = _lifecycle_icon(stage)
        lines.append(f"    {icon} {stage}: {count}")

    if stats["by_source"]:
        from ..constants import SOURCE_LABELS
        lines.append("")
        lines.append("  By Source:")
        for src, count in stats["by_source"].items():
            label = SOURCE_LABELS.get(src, src.replace("_", " ").title())
            lines.append(f"    {label}: {count}")

    if stats["top_tags"]:
        lines.append("")
        lines.append("  Top Tags:")
        for tag_name, count in stats["top_tags"]:
            lines.append(f"    #{tag_name}: {count}")

    return "\n".join(lines)


async def _handle_export(lifecycle_stage: str, tag: str, fmt: str) -> str:
    contacts = await db.search_global_contacts(
        lifecycle_stage=lifecycle_stage,
        tag=tag,
        limit=1000,
        order_by="name ASC",
    )

    if not contacts:
        return "No contacts to export."

    if fmt == "json":
        # Strip large blobs for export
        export = []
        for c in contacts:
            export.append({
                "id": c["id"],
                "name": c.get("name") or "",
                "title": c.get("title") or "",
                "company": c.get("company") or "",
                "linkedin_url": c.get("linkedin_url") or "",
                "email": c.get("email") or "",
                "location": c.get("location") or "",
                "lifecycle_stage": c.get("lifecycle_stage") or "prospect",
                "fit_score": c.get("fit_score") or 0.0,
                "tags": json.loads(c.get("tags_json") or "[]"),
                "total_campaigns": c.get("total_campaigns") or 0,
                "source": c.get("source") or "",
            })
        return json.dumps(export, indent=2)

    elif fmt == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Name", "Title", "Company", "LinkedIn URL", "Email",
            "Location", "Lifecycle", "Fit Score", "Tags", "Campaigns", "Source",
        ])
        for c in contacts:
            tags = json.loads(c.get("tags_json") or "[]")
            writer.writerow([
                c.get("name") or "",
                c.get("title") or "",
                c.get("company") or "",
                c.get("linkedin_url") or "",
                c.get("email") or "",
                c.get("location") or "",
                c.get("lifecycle_stage") or "prospect",
                f"{c.get('fit_score', 0):.2f}",
                "; ".join(tags),
                c.get("total_campaigns") or 0,
                c.get("source") or "",
            ])
        return output.getvalue()

    else:
        # Default: markdown table
        headers = ["Name", "Company", "Stage", "Fit", "Campaigns"]
        rows = []
        for c in contacts[:50]:  # Cap table at 50 rows
            rows.append([
                (c.get("name") or "?")[:25],
                (c.get("company") or "—")[:20],
                f"{_lifecycle_icon(c.get('lifecycle_stage', 'prospect'))} {c.get('lifecycle_stage', 'prospect')}",
                stars(c.get("fit_score") or 0),
                str(c.get("total_campaigns") or 0),
            ])

        result = table(headers, rows)
        total = len(contacts)
        if total > 50:
            result += f"\n\n... and {total - 50} more. Use format='csv' or format='json' for full export."
        return result


def _append_enriched_profile(lines: list[str], gc: dict) -> None:
    """Append enriched profile details from profile_json if available."""
    raw = gc.get("profile_json") or ""
    if not raw:
        return
    try:
        profile = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return

    # Only show section if we have rich data (not just basic search fields)
    rich_keys = {"contact_info", "connections_count", "summary", "experience",
                 "education", "skills", "follower_count", "websites"}
    if not rich_keys & set(profile.keys()):
        return

    lines.append("")
    lines.append("Enriched Profile")
    lines.append("-" * 60)

    # Contact info
    ci = profile.get("contact_info") or {}
    emails = ci.get("emails") or []
    phones = ci.get("phones") or []
    addresses = ci.get("adresses") or ci.get("addresses") or []
    if emails:
        lines.append(f"  Email:    {', '.join(emails)}")
    if phones:
        lines.append(f"  Phone:    {', '.join(phones)}")
    if addresses:
        lines.append(f"  Address:  {', '.join(addresses)}")

    # Summary / About
    summary = profile.get("summary") or ""
    if summary:
        lines.append("")
        lines.append("  About:")
        for chunk in [summary[i:i+76] for i in range(0, len(summary), 76)][:5]:
            lines.append(f"    {chunk}")
        if len(summary) > 380:
            lines.append("    ...")

    # Network stats
    stats_parts = []
    conn = profile.get("connections_count")
    if conn:
        stats_parts.append(f"{conn} connections")
    followers = profile.get("follower_count")
    if followers:
        stats_parts.append(f"{followers} followers")
    shared = profile.get("shared_connections_count")
    if shared:
        stats_parts.append(f"{shared} shared")
    distance = profile.get("network_distance") or ""
    if distance:
        stats_parts.append(distance.replace("_", " ").lower())
    if stats_parts:
        lines.append(f"  Network:  {' | '.join(stats_parts)}")

    # Websites
    websites = profile.get("websites") or []
    if websites:
        lines.append(f"  Websites: {', '.join(websites[:3])}")

    # Experience
    experience = profile.get("experience") or []
    if experience:
        lines.append("")
        lines.append("  Experience:")
        for exp in experience[:3]:
            title = exp.get("title") or ""
            company = exp.get("company") or exp.get("company_name") or ""
            start = exp.get("start_date") or exp.get("start") or ""
            end = exp.get("end_date") or exp.get("end") or "Present"
            entry = f"    {title}"
            if company:
                entry += f" @ {company}"
            if start:
                entry += f" ({start} - {end})"
            lines.append(entry)
        if len(experience) > 3:
            lines.append(f"    ... +{len(experience) - 3} more")

    # Education
    education = profile.get("education") or []
    if education:
        lines.append("")
        lines.append("  Education:")
        for edu in education[:3]:
            school = edu.get("school") or edu.get("school_name") or ""
            degree = edu.get("degree") or edu.get("degree_name") or ""
            field = edu.get("field") or edu.get("field_of_study") or ""
            entry = f"    {school}"
            if degree or field:
                entry += f" — {', '.join(filter(None, [degree, field]))}"
            lines.append(entry)

    # Skills
    skills = profile.get("skills") or []
    if skills:
        skill_names = [s if isinstance(s, str) else s.get("name", "") for s in skills[:10]]
        lines.append(f"  Skills:   {', '.join(filter(None, skill_names))}")
        if len(skills) > 10:
            lines.append(f"            ... +{len(skills) - 10} more")

    # Certifications
    certs = profile.get("certifications") or []
    if certs:
        cert_names = [c.get("name", "") for c in certs[:3]]
        lines.append(f"  Certs:    {', '.join(filter(None, cert_names))}")

    # Languages
    languages = profile.get("languages") or []
    if languages:
        lang_strs = []
        for lang in languages[:5]:
            name = lang if isinstance(lang, str) else lang.get("name", "")
            if name:
                lang_strs.append(name)
        if lang_strs:
            lines.append(f"  Languages: {', '.join(lang_strs)}")

    # Flags
    flags = []
    if profile.get("is_premium"):
        flags.append("Premium")
    if profile.get("is_creator"):
        flags.append("Creator")
    if profile.get("is_influencer"):
        flags.append("Influencer")
    if profile.get("is_open_profile"):
        flags.append("Open Profile")
    if flags:
        lines.append(f"  Flags:    {', '.join(flags)}")


async def _handle_linkedin_search(query: str, limit: int) -> str:
    """Search LinkedIn for people by name/title/company."""
    if not query:
        return (
            "Search query is required.\n"
            f"Usage: contacts(action='linkedin_search', query='{LINKEDIN_SEARCH_EXAMPLE}')\n"
            "LinkedIn matches the query as keywords: a company name, a job title, "
            "a person's name, or a combination."
        )

    account_id = await run_db(get_account_id)
    if not account_id:
        return (
            "No LinkedIn account connected.\n\n"
            "Run setup_profile first to connect your LinkedIn account."
        )

    client = get_linkedin_client()
    requested = max(1, limit)
    count = min(requested, LINKEDIN_SEARCH_MAX_RESULTS)

    try:
        try:
            results, _cursor = await client.search_people(
                account_id=account_id,
                keywords=query,
                count=count,
                raise_on_error=True,
            )
        except UnipileResultFormatError as e:
            # LinkedIn did answer, with rows we could not read. Saying "the search
            # did not complete" here would be false, and "retry" would spend rate
            # limit on a response shape that is not going to change by itself.
            logger.warning("LinkedIn search returned unreadable rows for %r: %s", query, e)
            return (
                f"LinkedIn search for \"{query}\" returned profiles this tool could not "
                "read, so this is NOT a 'nobody matches' result.\n\n"
                f"Reason: {e}\n\n"
                "The request itself succeeded; the rows came back in a shape the "
                "parser does not recognise (anonymised out-of-network profiles look "
                "like this). A different query may return readable rows, and "
                "contacts(action='my_connections') searches your synced 1st-degree "
                "connections without this step."
            )
        except UnipileAuthError as e:
            # Retrying a disconnected account never helps, and telling the user
            # to retry buries the one action that does.
            logger.warning("LinkedIn people search rejected for %r: %s", query, e)
            return (
                f"LinkedIn search failed for \"{query}\" — the search did not complete, "
                "so this is NOT a 'nobody matches' result.\n\n"
                f"Reason: {e}\n\n"
                "The LinkedIn account is not authenticated, so retrying will fail the "
                "same way. Reconnect it with setup_profile(), then search again."
            )
        except Exception as e:
            logger.warning("LinkedIn people search failed for %r: %s", query, e)
            return (
                f"LinkedIn search failed for \"{query}\" — the search did not complete, "
                "so this is NOT a 'nobody matches' result.\n\n"
                f"Reason: {e}\n\n"
                "There may well be matching profiles. Retry in a moment; if it keeps "
                "failing, check the LinkedIn connection with account()."
            )

        returned = len(results)
        if not results:
            return (
                f"No LinkedIn profiles matched \"{query}\".\n\n"
                "The search completed and LinkedIn returned 0 profiles — an empty "
                "result, not a failure.\n"
                "LinkedIn matches these keywords literally, so if you expected "
                "someone: drop extra words (company name alone, or 'Company Title'), "
                "check the company's spelling as it appears on LinkedIn, or try "
                "contacts(action='my_connections') to search your synced "
                "1st-degree connections instead."
            )

        # Honour the caller's limit: LinkedIn may return more than asked for, and
        # every extra result costs a profile fetch plus a posts fetch below.
        if returned > count:
            results = results[:count]

        # One profile fetch and one posts fetch per result, spread over the
        # pacing budget. Without this they went out back to back and LinkedIn
        # rate-limited the burst after roughly eight calls.
        fetchable = sum(
            1 for p in results
            if (p.get("provider_id") or p.get("public_id"))
        )
        pacer = _SearchPacer(planned_calls=fetchable * 2)
        skipped_profiles = 0
        skipped_posts = 0

        # Enrich each result with full LinkedIn profile
        enriched_count = 0
        for person in results:
            lid = person.get("provider_id") or person.get("public_id") or ""
            if not lid:
                continue
            if not await pacer.wait():
                skipped_profiles += 1
                continue
            try:
                profile = await _fetch_paced(
                    lambda: client.get_profile(account_id, lid, raise_on_rate_limit=True),
                    pacer,
                    default={},
                )
                if profile and isinstance(profile, dict):
                    person["_profile_json"] = json.dumps(profile)
                    if not person.get("title") and profile.get("headline"):
                        person["title"] = profile["headline"]
                    if not person.get("company"):
                        headline = profile.get("headline", "")
                        if " at " in headline:
                            person["company"] = headline.rsplit(" at ", 1)[1]
                    enriched_count += 1
            except Exception as e:
                logger.debug("Enrich failed for %s: %s", lid, e)

        # Fetch recent posts for enriched profiles (posts-on-enrichment)
        posts_fetched = 0
        for person in results:
            lid = person.get("provider_id") or person.get("public_id") or ""
            if not lid:
                continue
            if not await pacer.wait():
                skipped_posts += 1
                continue
            try:
                posts = await _fetch_paced(
                    lambda: client.get_user_posts(
                        account_id, lid, limit=10, raise_on_rate_limit=True,
                    ),
                    pacer,
                    default=[],
                )
                if posts and not (isinstance(posts[0], dict) and "_status_code" in posts[0]):
                    for post in posts:
                        pid = post.get("id", "")
                        txt = post.get("text", "")
                        if pid and txt:
                            await db.upsert_post(
                                pid,
                                author_linkedin_id=lid,
                                author_name=person.get("name", ""),
                                text=txt[:2000],
                                metrics_json=json.dumps(post.get("metrics", {})),
                                source="enrichment",
                            )
                            posts_fetched += 1
            except Exception:
                pass  # Post fetch failure doesn't block search results
        if posts_fetched:
            logger.info("Fetched %d posts during enrichment for %d profiles", posts_fetched, len(results))
    except Exception as e:
        return f"LinkedIn search failed: {e}"
    finally:
        await client.close()

    # Save results to global_contacts
    saved_ids = []
    idless = 0
    for person in results:
        linkedin_id = person.get("provider_id") or person.get("public_id") or ""
        if not linkedin_id:
            idless += 1
            continue
        try:
            gid = await db.upsert_global_contact(
                linkedin_id=linkedin_id,
                name=person.get("name", ""),
                title=person.get("title", ""),
                company=person.get("company", ""),
                linkedin_url=person.get("linkedin_url", ""),
                location=person.get("location", ""),
                profile_json=person.get("_profile_json", ""),
                source="linkedin_lookup",
                source_detail=f"Search: {query[:80]}",
            )
            saved_ids.append(gid)
        except Exception:
            logger.warning("Failed to save LinkedIn result to global contacts", exc_info=True)

    # Format output
    lines = [
        f"LinkedIn Search: \"{query}\"",
        "=" * 50,
        "",
    ]

    for i, person in enumerate(results, 1):
        name = person.get("name") or "?"
        headline = person.get("headline") or ""
        company = person.get("company") or ""
        title_str = person.get("title") or ""
        location = person.get("location") or ""
        url = person.get("linkedin_url") or ""

        lines.append(f"{i}. {name}")
        if headline:
            lines.append(f"   {headline}")
        elif title_str:
            detail = title_str
            if company:
                detail += f" at {company}"
            lines.append(f"   {detail}")
        if location:
            lines.append(f"   Location: {location}")
        if url:
            lines.append(f"   {url}")
        lines.append("")

    total = len(results)
    saved = len(saved_ids)
    enrich_summary = f"Showing {total} results, enriched {enriched_count} profiles"
    if posts_fetched:
        enrich_summary += f", fetched {posts_fetched} posts"
    enrich_summary += "."
    lines.append(enrich_summary)

    # Make the pacing cost visible: this is a synchronous call, so every second
    # below is a second the caller sat waiting, and a silent 60s tool is worse
    # than a slow one that says why.
    if pacer.waits:
        lines.append(
            f"Paced: {pacer.waits} pauses totalling {pacer.spent:.0f}s were inserted "
            f"between the LinkedIn fetches, starting {pacer.initial_gap:.1f}s apart, "
            f"out of a {LINKEDIN_SEARCH_PACE_BUDGET_SECONDS:.0f}s budget for this call. "
            "The delay is deliberate: these fetches used to go out back to back and "
            "LinkedIn rate-limited them."
        )
    if pacer.rate_limits:
        served = pacer.backoffs_served
        rate_line = (
            f"LinkedIn rate-limited {pacer.rate_limits} of these fetches. "
            f"{served} {'was' if served == 1 else 'were'} retried once after waiting "
            f"out the limit (LinkedIn's Retry-After where it sent one, otherwise a "
            f"doubled gap, either way capped at "
            f"{LINKEDIN_SEARCH_RETRY_AFTER_CAP_SECONDS:.0f}s)"
        )
        unserved = pacer.rate_limits - served
        if unserved:
            rate_line += (
                f"; the other {unserved} {'was' if unserved == 1 else 'were'} not "
                "retried because the pacing budget was already spent"
            )
        lines.append(rate_line + ".")
    if skipped_profiles or skipped_posts:
        skipped_parts = []
        if skipped_profiles:
            skipped_parts.append(f"{skipped_profiles} profile fetch(es)")
        if skipped_posts:
            skipped_parts.append(f"{skipped_posts} posts fetch(es)")
        lines.append(
            f"The {LINKEDIN_SEARCH_PACE_BUDGET_SECONDS:.0f}s pacing budget ran out, so "
            f"{' and '.join(skipped_parts)} were never made. The counts above are what "
            "did complete. Search again with a smaller limit= to spend the same budget "
            "on fewer results."
        )

    # Only state what this code did. Rows we dropped are ours to declare; a
    # short page from LinkedIn is not something we can explain, so we don't try.
    if returned > total:
        # `returned` is a post-parse count, so it cannot be attributed to
        # LinkedIn: rows that failed to parse (anonymised, nameless) are
        # already gone by here. And `count` is the effective cap, which is not
        # necessarily what the caller asked for — say both rather than calling
        # the capped value "your limit".
        limit_note = (
            f"the effective limit of {count} (capped from {requested})"
            if requested > count else f"your limit of {count}"
        )
        lines.append(
            f"{returned} results parsed; {returned - total} beyond {limit_note} "
            "were dropped here."
        )
    if requested > LINKEDIN_SEARCH_MAX_RESULTS:
        lines.append(
            f"limit={requested} was capped at {LINKEDIN_SEARCH_MAX_RESULTS} for this "
            "call: each result costs a profile fetch and a posts fetch against your "
            "LinkedIn rate limit. Narrow the query and search again for more."
        )
    if idless:
        lines.append(
            f"{idless} of {total} results carried no LinkedIn id and could not be "
            "enriched or saved."
        )

    if saved > 0:
        lines.append(
            f"Saved {saved} to your contact base (source: linkedin_lookup). "
            "Use contacts(action='search') to find them, or tag/note them."
        )

    return "\n".join(lines)


# ──────────────────────────────────────────────
# Linking resolved global contacts onto campaign rows
# ──────────────────────────────────────────────

# Only exact-name matching exists. Anything looser (initials, fuzzy distance)
# writes a LinkedIn id onto a campaign row that the next invite or message then
# goes to, so new modes have to earn their way in one at a time.
LINK_MATCH_MODES = ("name",)


def _short_id(value: str) -> str:
    return (value or "")[:8]


def _who(name: str, title: str = "", company: str = "") -> str:
    """'Name — Title @ Company', dropping whichever parts are missing."""
    if title and company:
        detail = f"{title} @ {company}"
    else:
        detail = title or company or ""
    return f"{name or '?'} — {detail}" if detail else (name or "?")


async def _handle_link(campaign_id: str, match: str, dry_run: bool, limit: int) -> str:
    """Resolve campaign contact rows against the global contact base.

    linkedin_search writes only to global_contacts, and enrich only fills in
    rows that already carry a linkedin_id, so a campaign contact imported by
    name stays unresolved for ever no matter how many times the person is found
    on LinkedIn. This closes that gap, in the open, one row at a time.
    """
    campaign_id = (campaign_id or "").strip()
    if not campaign_id:
        return (
            "contacts(action='link') needs campaign_id — the campaign whose contact "
            "rows should be resolved against your global contact base.\n\n"
            "Usage: contacts(action='link', campaign_id='<id>')\n"
            "That call is a dry run: it lists what it would change and writes nothing."
        )

    mode = (match or "name").strip().lower()
    if mode not in LINK_MATCH_MODES:
        return (
            f"Unknown match mode: {mode!r}\n\n"
            f"Supported: {', '.join(LINK_MATCH_MODES)}.\n"
            "Usage: contacts(action='link', campaign_id='<id>', match='name')"
        )

    campaign = await db.get_campaign(campaign_id)
    if not campaign:
        return f"Campaign not found: {campaign_id}"

    plan = await db.plan_contact_links(campaign_id)

    links = plan["links"]
    collisions = list(plan["collisions"])
    ambiguous = plan["ambiguous"]
    unmatched = plan["unmatched"]
    show = max(1, limit)

    # ── Apply ──
    linked: list[dict] = []
    raced = 0
    if not dry_run:
        for row in links:
            outcome = await db.apply_contact_link(
                contact_id=row["contact_id"],
                global_contact_id=row["global_contact_id"],
                linkedin_id=row["linkedin_id"],
                linkedin_url=row["linkedin_url"],
            )
            if outcome == "linked":
                linked.append(row)
            elif outcome == "collision":
                collisions.append({
                    "contact_id": row["contact_id"],
                    "contact_name": row["contact_name"],
                    "contact_company": row["contact_company"],
                    "linkedin_id": row["linkedin_id"],
                    "global_contact_id": row["global_contact_id"],
                    "global_name": row["global_name"],
                    "other_contact_id": "",
                    "other_contact_name": "",
                })
            else:
                raced += 1

    out: list[str] = [
        "Link campaign contacts to your contact base"
        + (" — DRY RUN" if dry_run else ""),
        "=" * 50,
        f"Campaign: {campaign.get('name') or '(unnamed)'} ({campaign_id})",
        f"Match: {mode} (exact, ignoring case and extra spaces)",
        "",
        "Names are matched against your WHOLE contact base, not just this campaign, "
        "so two different people who share a name can be matched to each other. The "
        "LinkedIn id involved is the one the next invite or message goes to"
        + (" — read each pair below before applying." if dry_run
           else ", so check the pairs below."),
        "",
    ]

    # ── Would link / linked ──
    shown_links = linked if not dry_run else links
    heading = "WOULD LINK" if dry_run else "LINKED"
    out.append(f"{heading} — {len(shown_links)}")
    if not shown_links:
        out.append("  (none)")
    for i, row in enumerate(shown_links[:show], 1):
        out.append(
            f"  {i}. {_who(row['contact_name'], row['contact_title'], row['contact_company'])}"
            f"   [contact {_short_id(row['contact_id'])}]"
        )
        out.append(
            f"     -> {_who(row['global_name'], row['global_title'], row['global_company'])}"
        )
        detail = f"        linkedin_id {row['linkedin_id']}"
        if row["linkedin_url"]:
            detail += f"  {row['linkedin_url']}"
        out.append(detail)
        provenance = row["source"] or "unknown source"
        if row["source_detail"]:
            provenance += f" ({row['source_detail']})"
        out.append(
            f"        global contact {_short_id(row['global_contact_id'])} from {provenance}"
        )
    if len(shown_links) > show:
        out.append(
            f"  ... {len(shown_links) - show} more not shown (limit={show}). "
            + (
                f"dry_run=False acts on all {len(shown_links)}, not just the "
                f"{show} above — raise limit= to read them first."
                if dry_run else
                f"All {len(shown_links)} were written."
            )
        )
    out.append("")

    # ── Collisions ──
    if collisions:
        out.append(f"MERGE CANDIDATES — {len(collisions)} (nothing written for these)")
        for i, row in enumerate(collisions[:show], 1):
            out.append(
                f"  {i}. {row['contact_name']} [contact {_short_id(row['contact_id'])}] "
                f"would take linkedin_id {row['linkedin_id']}"
            )
            if row.get("other_contact_id"):
                out.append(
                    f"     but contact {_short_id(row['other_contact_id'])} "
                    f"({row['other_contact_name'] or '?'}) in this campaign already has it."
                )
            else:
                out.append(
                    "     but the campaign's unique index refused it at write time — "
                    "another row already had that id."
                )
            out.append(
                "     One contact per campaign per LinkedIn profile is enforced by the "
                "database, so these two rows are either the same person listed twice or "
                "a wrong name match. Decide which before linking."
            )
        if len(collisions) > show:
            out.append(f"  ... {len(collisions) - show} more not shown (limit={show}).")
        out.append("")

    # ── Ambiguous ──
    if ambiguous:
        out.append(f"AMBIGUOUS — {len(ambiguous)} (nothing written for these)")
        for i, row in enumerate(ambiguous[:show], 1):
            out.append(
                f"  {i}. {row['contact_name']} [contact {_short_id(row['contact_id'])}] "
                f"matches {len(row['candidates'])} different LinkedIn ids:"
            )
            for cand in row["candidates"][:5]:
                line = f"     {cand['linkedin_id']} — {_who(cand['name'], cand['title'], cand['company'])}"
                if cand["linkedin_url"]:
                    line += f"  {cand['linkedin_url']}"
                out.append(line)
            if len(row["candidates"]) > 5:
                out.append(f"     ... and {len(row['candidates']) - 5} more")
            out.append("     The name alone cannot choose between them.")
        if len(ambiguous) > show:
            out.append(f"  ... {len(ambiguous) - show} more not shown (limit={show}).")
        out.append("")

    # ── No match ──
    if unmatched:
        out.append(f"NO MATCH — {len(unmatched)}")
        names = [
            _who(r["contact_name"], "", r["contact_company"]) for r in unmatched[:show]
        ]
        for name in names:
            out.append(f"  {name}")
        if len(unmatched) > show:
            out.append(f"  ... {len(unmatched) - show} more not shown (limit={show}).")
        out.append("")

    # ── Footer ──
    if plan["already_linked"]:
        out.append(
            f"Already resolved: {plan['already_linked']} of {plan['total']} contacts "
            "carry a LinkedIn id already and were not considered."
        )
    if plan["nameless"]:
        out.append(
            f"{plan['nameless']} contact(s) have no name, so name matching has nothing "
            "to work with."
        )

    if dry_run:
        out.append("")
        out.append("Nothing was written: this was a dry run.")
        if links:
            out.append(
                f"Apply with: contacts(action='link', campaign_id='{campaign_id}', "
                "dry_run=False)"
            )
            out.append(
                f"That sets linkedin_id on the {len(links)} row(s) under WOULD LINK, "
                "points their global_contact_id at the matched record, fills "
                "linkedin_url where the row has none, and stamps updated_at. Rows in "
                "the other sections are left exactly as they are."
            )
    else:
        out.append("")
        if linked:
            out.append(
                f"Wrote {len(linked)} link(s): linkedin_id, global_contact_id and "
                "updated_at set on those rows (plus linkedin_url where the row had "
                "none). No other row and no other column was touched."
            )
        else:
            out.append("Wrote 0 links — no row in this campaign changed.")
        if raced:
            out.append(
                f"{raced} planned row(s) had picked up a LinkedIn id between the plan "
                "and the write, so they were left alone."
            )

    return "\n".join(out)


def _parse_date_filter(value: str) -> int | None:
    """YYYY-MM-DD (or an epoch) to epoch seconds. None when unset/unparseable."""
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    from datetime import datetime, timezone

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%Y-%m"):
        try:
            return int(
                datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).timestamp()
            )
        except ValueError:
            continue
    return None


def _format_connected_since(epoch: int | None) -> str:
    if not epoch:
        return ""
    from datetime import datetime, timezone

    try:
        return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""


async def _handle_my_connections(
    query: str,
    limit: int = 25,
    connected_since: str = "",
    connected_before: str = "",
) -> str:
    """Search locally-synced 1st-degree LinkedIn connections.

    ``connected_since`` / ``connected_before`` filter on when the person became
    a connection (9 Sep 2026 — "we want to know since when someone is
    a connection"). Rows synced before that shipped have no date and are left
    out of both windows rather than guessed into one.
    """
    since_ts = _parse_date_filter(connected_since)
    before_ts = _parse_date_filter(connected_before)
    if connected_since and since_ts is None:
        return f"Could not read connected_since={connected_since!r}. Use YYYY-MM-DD."
    if connected_before and before_ts is None:
        return f"Could not read connected_before={connected_before!r}. Use YYYY-MM-DD."
    from ..db.async_bridge import run_db
    from ..db.connection_queries import (
        get_connection_sync_status,
        list_all_connections,
        search_my_connections,
    )
    from ..services.connection_sync import FULL_SYNC_STALE_SECONDS

    # Resolve account_id
    account_id = await db.get_setting("unipile_account_id", "")
    if not account_id:
        return "No LinkedIn account connected. Run setup_profile first."

    # Check sync status
    status = await run_db(get_connection_sync_status, account_id)

    # Auto-sync if never synced or stale
    needs_sync = (
        not status["synced"]
        or (status["age_seconds"] is not None and status["age_seconds"] > FULL_SYNC_STALE_SECONDS)
    )
    if needs_sync:
        try:
            from ..linkedin import get_linkedin_client
            from ..services.connection_sync import sync_connections

            client = get_linkedin_client()
            synced = await sync_connections(
                client, account_id, force=True,
                # Interactive: the user is waiting and no tick can cancel
                # this, so it may take as long as the network needs.
                time_budget=None,
            )
            # Refresh status after sync
            status = await run_db(get_connection_sync_status, account_id)
            if synced == 0 and not status["synced"]:
                return (
                    "Connection sync completed but no connections found. "
                    "Your LinkedIn account may need to be reconnected."
                )
        except Exception as e:
            if not status["synced"]:
                return f"Failed to sync connections: {e}\n\nTry again or run `network(action='sync')` first."
            # If we have stale data, proceed with it
            logger.warning("Connection sync failed, using stale data: %s", e)

    # Search or list
    if query.strip():
        results = await run_db(
            search_my_connections, query.strip(), account_id, limit,
            since_ts, before_ts,
        )
    else:
        results = await run_db(
            list_all_connections, account_id, limit, 0, since_ts, before_ts,
        )

    window = ""
    if since_ts or before_ts:
        window = " connected" + (
            f" since {_format_connected_since(since_ts)}" if since_ts else ""
        ) + (
            f" before {_format_connected_since(before_ts)}" if before_ts else ""
        )

    if not results:
        if query.strip() or window:
            return (
                f"No 1st-degree connections matching '{query}'{window}. "
                f"You have {status['count']:,} connections synced."
                + (
                    f" {status.get('undated', 0):,} of them have no connection "
                    "date yet — they were synced before dates were recorded and "
                    "no date window can match them."
                    if window and status.get("undated") else ""
                )
            )
        return "No connections synced yet."

    # Format results
    lines = [
        "Your 1st-Degree Connections"
        + (f" matching '{query}'" if query.strip() else "")
        + window,
        "=" * 50,
        "",
    ]

    for i, c in enumerate(results, 1):
        name = c.get("name") or "Unknown"
        headline = c.get("headline") or ""
        company = c.get("company") or ""
        location = c.get("location") or ""
        profile_url = c.get("profile_url") or ""
        public_id = c.get("public_id") or ""

        # Build profile URL from public_id if missing
        if not profile_url and public_id:
            profile_url = f"https://www.linkedin.com/in/{public_id}"

        lines.append(f"{i}. **{name}**")
        if headline:
            lines.append(f"   {headline}")
        elif company:
            lines.append(f"   {company}")
        if location:
            lines.append(f"   Location: {location}")
        connected_since_str = _format_connected_since(c.get("connected_at"))
        if connected_since_str:
            lines.append(f"   Connected since: {connected_since_str}")
        if profile_url:
            lines.append(f"   {profile_url}")
        lines.append("")

    # Footer
    age_str = ""
    if status["age_seconds"] is not None:
        hours = status["age_seconds"] // 3600
        if hours < 1:
            age_str = f"{status['age_seconds'] // 60}m ago"
        elif hours < 24:
            age_str = f"{hours}h ago"
        else:
            age_str = f"{hours // 24}d ago"
    lines.append(
        f"Showing {len(results)} of {status['count']:,} connections"
        + (f" (synced {age_str})" if age_str else "")
    )

    return "\n".join(lines)


# ──────────────────────────────────────────────
# Formatting helpers
# ──────────────────────────────────────────────

def _format_contact_list(
    contacts: list[dict], limit: int, title: str = "Contact Base"
) -> str:
    lines = [title, "=" * 50, ""]

    for c in contacts:
        stage = c.get("lifecycle_stage", "prospect")
        icon = _lifecycle_icon(stage)
        name = c.get("name") or "?"
        company = c.get("company") or ""
        title_str = c.get("title") or ""
        score = c.get("fit_score") or 0.0
        tags = json.loads(c.get("tags_json") or "[]")

        line1 = f"{icon} {name}"
        if company:
            line1 += f" @ {company}"
        lines.append(line1)

        details = f"   {stage}"
        if title_str:
            details += f"  |  {title_str[:40]}"
        details += f"  |  {stars(score)}"
        if tags:
            details += f"  |  #{', #'.join(tags[:3])}"
        lines.append(details)
        lines.append(f"   ID: {c['id']}")
        lines.append("")

    total = len(contacts)
    if total >= limit:
        lines.append(f"Showing {limit} contacts. Use limit= to see more, or action='search' to filter.")
    else:
        lines.append(f"Total: {total} contacts")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# Enrichment handler
# ──────────────────────────────────────────────

async def _handle_enrich(contact_id: str, query: str, limit: int) -> str:
    """Enrich contacts with full LinkedIn profiles and recent posts.

    Single contact mode: provide contact_id.
    Batch mode: provide query to search global_contacts missing profile_json.
    """
    import json as _json

    from ..linkedin import get_linkedin_client

    client = get_linkedin_client()
    # accounts[0] is not the selected account on a multi-account install, so
    # this reads the one the user actually chose rather than listing.
    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account configured. Run setup_profile() first."

    targets: list[dict] = []

    if contact_id:
        gc = await db.get_global_contact(contact_id)
        if not gc:
            return f"Contact not found: {contact_id}"
        targets = [gc]
    elif query:
        results = await db.search_global_contacts(query=query, limit=limit or 10)
        # Filter to those missing enrichment
        targets = [r for r in results if not r.get("profile_json")]
        if not targets:
            return f"No un-enriched contacts found matching \"{query}\". They may already be enriched."
    else:
        return "Provide contact_id for single enrichment, or query for batch enrichment."

    enriched_count = 0
    lines = ["Enrichment Results", "=" * 40, ""]

    for gc in targets:
        lid = gc.get("linkedin_id") or ""
        # Try to extract provider_id from existing profile_json
        if gc.get("profile_json") and not contact_id:
            continue  # Skip already enriched (unless explicitly requested by contact_id)
        if not lid:
            try:
                pj = _json.loads(gc.get("profile_json") or "{}")
                lid = pj.get("provider_id", "")
            except (ValueError, TypeError):
                pass
        if not lid:
            lines.append(f"  Skipped {gc.get('name', '?')}: no LinkedIn ID")
            continue

        try:
            profile = await client.get_profile(account_id, lid)
            if not profile or not isinstance(profile, dict):
                lines.append(f"  Skipped {gc.get('name', '?')}: profile not found")
                continue

            from ..services.prospect_email import attach_email_to_profile_json, extract_profile_email
            email = extract_profile_email(profile)
            profile_json = attach_email_to_profile_json(_json.dumps(profile), email) or _json.dumps(profile)
            title = profile.get("title") or profile.get("headline") or gc.get("title", "")
            company = profile.get("company") or gc.get("company", "")
            location = profile.get("location") or gc.get("location", "")

            await db.upsert_global_contact(
                linkedin_id=lid,
                name=gc.get("name") or profile.get("name", ""),
                title=title,
                company=company,
                linkedin_url=gc.get("linkedin_url", ""),
                email=email,
                location=location,
                profile_json=profile_json,
                source="enrichment",
            )

            # Fetch posts
            posts_count = 0
            try:
                from ..db.post_queries import upsert_post
                posts = await client.get_user_posts(account_id, lid, limit=10)
                if posts and isinstance(posts, list):
                    for post in posts:
                        pid = post.get("id", "")
                        txt = post.get("text", "")
                        if pid and txt:
                            await run_db(upsert_post, pid,
                                author_linkedin_id=lid,
                                author_name=gc.get("name", ""),
                                text=txt[:2000],
                                metrics_json=_json.dumps(post.get("metrics", {})),
                                source="enrichment",)
                            posts_count += 1
            except Exception:
                pass

            enriched_count += 1
            name = gc.get("name") or profile.get("name", "?")
            lines.append(f"  {enriched_count}. {name}")
            if title:
                lines.append(f"     Title: {title}")
            if company:
                lines.append(f"     Company: {company}")
            if location:
                lines.append(f"     Location: {location}")
            skills = profile.get("skills", [])
            if skills:
                skill_names = [s.get("name", s) if isinstance(s, dict) else str(s) for s in skills[:5]]
                lines.append(f"     Skills: {', '.join(skill_names)}")
            if posts_count:
                lines.append(f"     Posts fetched: {posts_count}")
            lines.append("")

        except Exception as e:
            lines.append(f"  Failed {gc.get('name', '?')}: {e}")

        # Rate limit
        if len(targets) > 1:
            import asyncio
            await asyncio.sleep(1.0)

    await client.close()

    lines.append(f"Enriched {enriched_count}/{len(targets)} contacts.")
    if enriched_count > 0:
        lines.append("Use contacts(action='view', contact_id='...') to see full enriched profiles.")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# Duplicate detection & merging handlers
# ──────────────────────────────────────────────

async def _handle_find_duplicates() -> str:
    """Find duplicate global contacts that share the same provider_id."""
    from ..db.global_contact_queries import find_duplicate_global_contacts

    dupes = await run_db(find_duplicate_global_contacts)
    if not dupes:
        return "No duplicate contacts found."

    lines = [f"Found **{len(dupes)} duplicate contact pair(s)**:\n"]
    for d in dupes:
        lines.append(f"  - **{d['keep_name']}** (keep: `{d['keep_id'][:8]}…`)")
        lines.append(f"    ↔ **{d['merge_name']}** (merge: `{d['merge_id'][:8]}…`)")
        lines.append(f"    provider_id: `{d['provider_id']}`")
        lines.append("")

    lines.append("To merge: `contacts(action='merge', contact_id='<keep_id>', query='<merge_id>')`")
    return "\n".join(lines)


async def _handle_merge(keep_id: str, merge_id: str) -> str:
    """Merge two global contact records."""
    from ..db.global_contact_queries import merge_global_contacts

    if not keep_id or not merge_id:
        return "Both contact_id (keep) and query (merge) are required.\n\nUsage: `contacts(action='merge', contact_id='<keep_id>', query='<merge_id>')`"

    if keep_id == merge_id:
        return "Cannot merge a contact with itself."

    success = await run_db(merge_global_contacts, keep_id, merge_id)
    if success:
        return f"Merged contact `{merge_id[:8]}…` into `{keep_id[:8]}…` successfully.\n\nAll campaign history has been consolidated."
    else:
        return "Merge failed — one or both contact IDs not found. Use `contacts(action='find_duplicates')` to find valid pairs."
