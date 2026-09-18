"""Tool: network — Network Intelligence Layer.

Members of the shared pool lend each other their LinkedIn accounts as a
distributed pool for enrichment, search, and network analysis.

The pool is reciprocal. Consuming it — enrich, contact, parallel, search,
reach, intros, insights, trends, patterns — requires that this workspace's own
LinkedIn seat is an active pool member; the backend answers 403 to everyone
else. Joining (status, opt_in, opt_out, sync) is open to all, because a
non-member has to be able to get in.

The refusal is handled where it lands rather than pre-empted: reacting to the
403 costs nothing on the common path, while a membership pre-check would add a
round trip to every one of these calls — including `parallel`, which is one
request covering up to 100 profiles — and could still be raced by an opt-out
between the check and the call. `action='status'` is the one place membership
is read directly, and only because it already fetches the pool status.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..linkedin import NetworkPoolNotMemberError, get_linkedin_client
from ..formatter import table

logger = logging.getLogger(__name__)

# What to print when a consumer route refuses. It leads with the backend's own
# reason where the 403 carried one, so the client is not paraphrasing a verdict
# it did not make, and only then states the rule and the remedy. The remedy is
# phrased as a condition ("if this workspace has not joined") rather than an
# assertion: the 403 proves the request was refused, and nothing here has
# separately established that this workspace is not a member.
_POOL_RECIPROCAL_DEFAULT = (
    "The shared network pool is reciprocal. To use it, join the pool from "
    "Settings, Data and privacy."
)


def _pool_declined(exc: NetworkPoolNotMemberError, action: str) -> str:
    """Guidance for a pool consumer route's 403, in place of a raw HTTP error."""
    reason = exc.detail or _POOL_RECIPROCAL_DEFAULT
    lines = [f"## Network pool declined `{action}`\n", reason, ""]
    # The backend's own detail already states the rule, so restating it would
    # say "reciprocal" twice in five lines. Explain it only when the server did
    # not, and go straight to the remedy either way.
    if "reciprocal" not in reason.lower():
        lines.append(
            "The pool is reciprocal: it lends other members' connections and "
            "seats only to workspaces that lend theirs back."
        )
        lines.append("")
    lines.append(
        "If this workspace has not joined yet, run `network(action='opt_in')` "
        "and then `network(action='sync')` to share your connection graph — "
        "after that this action works. `network(action='status')` shows "
        "whether you are opted in."
    )
    return "\n".join(lines)


async def run_network(
    action: str = "status",
    linkedin_id: str = "",
    linkedin_ids: str = "",
    query: str = "",
    title: str = "",
    max_accounts: int = 5,
    force_refresh: bool = False,
    insight_type: str = "",
    segment: str = "",
    min_confidence: float = 0.0,
) -> str:
    """Dispatch network actions to the backend."""
    client = get_linkedin_client()

    try:
        return await _dispatch(
            client, action, linkedin_id, linkedin_ids, query, title,
            max_accounts, force_refresh, insight_type, segment, min_confidence,
        )
    except NetworkPoolNotMemberError as e:
        # Caught here rather than in server.py so the user gets the remedy
        # instead of "Network intelligence failed: Client error '403
        # Forbidden' for url ...". Only the nine consumer routes raise it.
        logger.info("network(action=%s) refused by the pool: %s", action, e)
        return _pool_declined(e, action)


async def _dispatch(
    client: Any,
    action: str,
    linkedin_id: str,
    linkedin_ids: str,
    query: str,
    title: str,
    max_accounts: int,
    force_refresh: bool,
    insight_type: str,
    segment: str,
    min_confidence: float,
) -> str:
    if action == "status":
        return await _handle_status(client)
    elif action == "opt_in":
        return await _handle_opt_in(client)
    elif action == "opt_out":
        return await _handle_opt_out(client)
    elif action == "sync":
        return await _handle_sync(client)
    elif action == "opt_in_all":
        return await _handle_opt_in_all(client)
    elif action == "sync_all":
        return await _handle_sync_all(client)
    elif action == "enrich":
        if not linkedin_id:
            return "linkedin_id is required for enrich action."
        return await _handle_enrich(client, linkedin_id, force_refresh)
    elif action == "contact":
        if not linkedin_id:
            return "linkedin_id is required for contact action."
        return await _handle_contact(client, linkedin_id)
    elif action == "parallel":
        if not linkedin_ids:
            return "linkedin_ids (comma-separated) is required for parallel action."
        ids = [lid.strip() for lid in linkedin_ids.split(",") if lid.strip()]
        return await _handle_parallel(client, ids)
    elif action == "search":
        if not query and not title:
            return "query or title is required for search action."
        return await _handle_search(client, query, title, max_accounts)
    elif action == "reach":
        if not linkedin_id:
            return "linkedin_id is required for reach action."
        return await _handle_reach(client, linkedin_id)
    elif action == "intros":
        if not linkedin_id:
            return "linkedin_id is required for intros action."
        return await _handle_intros(client, linkedin_id)
    elif action == "insights":
        return await _handle_insights(client, insight_type, segment, min_confidence)
    elif action == "trends":
        return await _handle_trends(client, segment)
    elif action == "patterns":
        return await _handle_patterns(client, insight_type)
    else:
        return (
            "Unknown action. Available: status, opt_in, opt_out, sync, "
            "opt_in_all, sync_all, enrich, contact, parallel, search, reach, intros, "
            "insights, trends, patterns"
        )


# ── Action Handlers ──

async def _handle_status(client: Any) -> str:
    data = await client.network_pool_status()
    lines = ["## Network Pool Status\n"]
    lines.append(f"**Active accounts:** {data.get('active_accounts', 0)}/{data.get('total_accounts', 0)}")
    lines.append(f"**Healthy:** {data.get('healthy_accounts', 0)}")
    lines.append(f"**Total 1st-degree connections:** {data.get('total_connections', 0):,}")
    lines.append(f"**API usage today:** {data.get('global_usage_today', 0)}/{data.get('global_daily_cap', 500)}")

    # Three states, not two. `my_participation` is the only membership fact the
    # client is ever handed, and an absent key is not a "no" — reading it as one
    # printed "You are not opted in" for a response that had simply never said.
    # That was tolerable while membership only affected what you contributed;
    # now that it gates nine actions, guessing it would be telling the user the
    # cause of refusals they have not had.
    my = data.get("my_participation")
    my = my if isinstance(my, dict) else {}
    opted_in = my.get("opted_in")
    if opted_in:
        lines.append(f"\nYou are **opted in** (account: {my.get('account_id', '?')})")
        lines.append(
            "The pool is reciprocal, so contributing is what keeps enrich, "
            "contact, parallel, search, reach, intros and insights available "
            "to you."
        )
    elif opted_in is None:
        lines.append(
            "\nThis backend did not report your participation, so whether you "
            "are in the pool is unknown from here. The pool is reciprocal: "
            "enrich, contact, parallel, search, reach, intros and insights "
            "work only for members. `network(action='opt_in')` joins."
        )
    else:
        lines.append(
            "\nYou are **not opted in**. The pool is reciprocal, so enrich, "
            "contact, parallel, search, reach, intros and insights will be "
            "refused until you join. Run `network(action='opt_in')`, then "
            "`network(action='sync')` to share your connection graph."
        )

    accounts = data.get("accounts", [])
    if accounts:
        lines.append("\n### Pool Members\n")
        rows = []
        for a in accounts:
            status = "Active" if a.get("is_active") else "Inactive"
            health = a.get("health_status", "?")
            rows.append([
                a.get("account_name", "")[:20] or a.get("account_id", "")[:12],
                status,
                health,
                str(a.get("connection_count", 0)),
                str(a.get("budget_remaining", 0)),
            ])
        lines.append(table(
            ["Account", "Status", "Health", "Connections", "Budget"],
            rows,
        ))

    return "\n".join(lines)


async def _handle_opt_in(client: Any) -> str:
    data = await client.network_pool_opt_in()
    entry = data.get("pool_entry", {})
    return (
        f"Opted in to the network pool.\n"
        f"Account: {entry.get('account_id', '?')}\n"
        f"Run `network(action='sync')` to sync your connections.\n"
        f"The pool is reciprocal, so sharing yours is what unlocks the rest: "
        f"enrich, contact, parallel, search, reach, intros and insights."
    )


async def _handle_opt_out(client: Any) -> str:
    await client.network_pool_opt_out()
    return (
        "Opted out of the network pool. Your connection data has been removed.\n"
        "Because the pool is reciprocal, leaving it also ends your access to "
        "it: enrich, contact, parallel, search, reach, intros and insights "
        "will be refused until you opt in again. Your own LinkedIn account is "
        "unaffected — searches fall back to your own seat."
    )


async def _handle_sync(client: Any) -> str:
    """Sync the shared backend pool and the local connections cache.

    The local half is the one that matters day to day: it is what
    contacts(action='my_connections') searches and what the 1st-degree send
    guard reads, and it is the manual re-fetch the connection sync names when
    it refuses to prune a response that looks truncated
    (services/connection_sync.py, _MANUAL_RESYNC_COMMAND).

    It had never run. The import was `from ..db.schema import get_setting`, and
    get_setting lives in ..db.queries — so every call raised ImportError inside
    the `except Exception` below, logged one line and returned the pool count
    as if the whole thing had succeeded. Repairing the import alone would not
    have been enough either: the key read was "account_id", which nothing in
    the repo writes. The account id is stored under "unipile_account_id"
    (linkedin/unipile.py, set_account_id).

    Both halves are reported now, failures included. The pool half is skipped
    on self-hosted installs, where network_pool_sync does not exist at all —
    it is defined only on BackendClient — rather than taking the local sync
    down with it.
    """
    from ..db.async_bridge import run_db
    from ..db.queries import get_setting

    lines: list[str] = []

    pool_sync = getattr(client, "network_pool_sync", None)
    if pool_sync is None:
        lines.append(
            "Network pool: not available on this install (talking to Unipile directly)."
        )
    else:
        try:
            data = await pool_sync()
            count = data.get("connections_synced", 0)
            lines.append(
                f"Connection graph synced: **{count:,}** 1st-degree connections indexed."
            )
        except Exception as e:
            logger.warning("Network pool sync failed: %s", e)
            lines.append(f"Network pool sync failed: {e}")

    account_id = await run_db(get_setting, "unipile_account_id", "")
    if not account_id:
        lines.append(
            "Local cache: no LinkedIn account connected — run setup_profile first."
        )
        return "\n".join(lines)

    from ..services.connection_sync import get_connection_count, sync_connections

    try:
        # No time budget: this is user-initiated and may take as long as the
        # network needs. Only the scheduler's in-tick callers are capped.
        fetched = await sync_connections(
                client, account_id, force=True,
                # Interactive: the user is waiting and no tick can cancel
                # this, so it may take as long as the network needs.
                time_budget=None,
            )
    except Exception as e:
        logger.warning("Local connection sync failed during network sync: %s", e)
        lines.append(f"Local cache: sync failed — {e}")
        return "\n".join(lines)

    # Count the table, not the response. sync_connections() returns rows written
    # this run, and the two differ on exactly the account this command exists
    # for: the prune guard stands down on a response that reads as a prefix, so
    # a 500-row answer against 2,000 stored rows keeps all 2,000 — and reporting
    # the response called that "500 connections stored for search" while search
    # was in fact reading 2,000. The inbox fallback splits the same way: it
    # writes only the people the user has chatted with.
    stored = await run_db(get_connection_count, account_id)

    if stored:
        line = f"Local cache: **{stored:,}** connections stored for search."
        if fetched < stored:
            # Not an error to report — the prune guard keeping rows a partial
            # response omitted is the safe outcome — but the user ran this
            # command to refetch, so say how much of the table this run touched.
            line += f" This run refreshed **{fetched:,}** of them; the rest were left in place."
        lines.append(line)
    else:
        # Deliberately no diagnosis here: 0 can mean an empty network, a failed
        # relations walk that the inbox fallback could not cover, or a rolled
        # back write. The reason is in the log; naming one of them would be a
        # guess printed as fact.
        lines.append("Local cache: no connections stored.")
    return "\n".join(lines)


async def _handle_opt_in_all(client: Any) -> str:
    data = await client.network_pool_opt_in_all()
    count = data.get("accounts_opted_in", 0)
    return f"Admin: opted in **{count}** accounts to the network pool."


async def _handle_sync_all(client: Any) -> str:
    data = await client.network_pool_sync_all()
    total = data.get("total_synced", 0)
    per = data.get("per_account", {})
    lines = [f"Synced **{total:,}** connections across **{len(per)}** accounts.\n"]
    for acct, count in per.items():
        lines.append(f"- {acct[:12]}...: {count:,} connections")
    return "\n".join(lines)


async def _handle_enrich(client: Any, linkedin_id: str, force_refresh: bool) -> str:
    data = await client.network_enrich(linkedin_id, force_refresh=force_refresh)
    if "error" in data:
        return f"Enrichment failed: {data['error']}"

    profile = data.get("profile", {})
    contact = data.get("contact_info", {})
    degree = data.get("best_degree", 3)
    source = data.get("source", "?")

    lines = [f"## Enriched Profile ({source})\n"]
    lines.append(f"**{profile.get('name', '?')}** — {profile.get('headline', '')}")
    lines.append(f"Location: {profile.get('location', '—')}")
    lines.append(f"Industry: {profile.get('industry', '—')}")
    lines.append(f"Network degree: **{degree}** (1=direct connection)")
    lines.append(f"Connections: {profile.get('connections_count', 0):,}")
    lines.append(f"Shared connections: {profile.get('shared_connections', 0)}")

    if contact:
        emails = contact.get("emails", [])
        phones = contact.get("phones", [])
        if emails:
            lines.append(f"\nEmails: {', '.join(str(e) for e in emails)}")
        if phones:
            lines.append(f"Phones: {', '.join(str(p) for p in phones)}")

    exp = profile.get("experience", [])
    if exp and isinstance(exp, list):
        lines.append(f"\nExperience: {len(exp)} positions")

    skills = profile.get("skills", [])
    if skills and isinstance(skills, list):
        lines.append(f"Skills: {', '.join(str(s) for s in skills[:10])}")

    return "\n".join(lines)


async def _handle_contact(client: Any, linkedin_id: str) -> str:
    data = await client.network_contact_info(linkedin_id)
    contact = data.get("contact_info", {})
    degree = data.get("best_degree", 3)
    msg = data.get("message", "")

    if not contact or contact == {}:
        result = "No contact info available."
        if msg:
            result += f"\n{msg}"
        return result

    lines = [f"## Contact Info (degree: {degree})\n"]
    emails = contact.get("emails", [])
    phones = contact.get("phones", [])
    addresses = contact.get("addresses", [])

    if emails:
        lines.append(f"**Emails:** {', '.join(str(e) for e in emails)}")
    if phones:
        lines.append(f"**Phones:** {', '.join(str(p) for p in phones)}")
    if addresses:
        lines.append(f"**Addresses:** {', '.join(str(a) for a in addresses)}")
    if not emails and not phones:
        lines.append("No email or phone found (may not be shared by prospect).")

    return "\n".join(lines)


async def _handle_parallel(client: Any, linkedin_ids: list[str]) -> str:
    data = await client.network_parallel_enrich(linkedin_ids)
    stats = data.get("stats", {})
    profiles = data.get("profiles", {})

    lines = [f"## Parallel Enrichment Results\n"]
    lines.append(f"Total requested: {stats.get('total', 0)}")
    lines.append(f"From cache: {stats.get('cached', 0)}")
    lines.append(f"Newly enriched: {stats.get('enriched', 0)}")
    lines.append(f"Accounts used: {stats.get('accounts_used', 0)}")
    if stats.get("errors"):
        lines.append(f"Errors: {stats['errors']}")

    if profiles:
        lines.append(f"\n### Enriched Profiles ({len(profiles)})\n")
        rows = []
        for lid, p in list(profiles.items())[:20]:  # Show top 20
            prof = p.get("profile", {})
            degree = p.get("best_degree", "?")
            has_contact = bool(p.get("contact_info", {}).get("emails"))
            rows.append([
                prof.get("name", lid)[:25],
                prof.get("headline", "")[:35],
                str(degree),
                "Yes" if has_contact else "—",
            ])
        lines.append(table(
            ["Name", "Headline", "Degree", "Contact"],
            rows,
        ))

    return "\n".join(lines)


async def _handle_search(client: Any, query: str, title: str, max_accounts: int) -> str:
    data = await client.network_search(
        keywords=query, title=title, max_accounts=max_accounts,
    )
    stats = data.get("stats", {})
    results = data.get("results", [])

    lines = [f"## Distributed Search Results\n"]
    lines.append(f"Unique results: **{stats.get('unique_results', 0)}**")
    lines.append(f"Total raw (before dedup): {stats.get('total_raw_results', 0)}")
    lines.append(f"Accounts searched: {stats.get('accounts_used', 0)}")

    if results:
        lines.append(f"\n### Top Results\n")
        rows = []
        for r in results[:25]:
            name = r.get("display_name") or r.get("name") or "?"
            headline = r.get("headline") or ""
            lid = r.get("provider_id") or r.get("id") or ""
            rows.append([
                name[:25],
                headline[:40],
                lid[:15],
            ])
        lines.append(table(
            ["Name", "Headline", "LinkedIn ID"],
            rows,
        ))

    return "\n".join(lines)


async def _handle_reach(client: Any, linkedin_id: str) -> str:
    data = await client.network_reach(linkedin_id)
    first = data.get("first_degree_accounts", 0)
    total = data.get("total_pool_accounts", 0)
    accounts = data.get("accounts", [])

    lines = [f"## Network Reach for {linkedin_id}\n"]
    lines.append(f"1st-degree accounts: **{first}** / {total} pool accounts")

    if accounts:
        rows = []
        for a in accounts:
            deg = a.get("degree", 0)
            deg_str = "1st" if deg == 1 else "Unknown"
            rows.append([
                a.get("account_name", "")[:20] or a.get("account_id", "")[:12],
                deg_str,
                str(a.get("connection_count", 0)),
            ])
        lines.append(table(
            ["Account", "Degree", "Connections"],
            rows,
        ))

    return "\n".join(lines)


async def _handle_intros(client: Any, linkedin_id: str) -> str:
    data = await client.network_intros(linkedin_id)
    paths = data.get("warm_paths", 0)
    connections = data.get("connections", [])

    lines = [f"## Warm Introduction Paths to {linkedin_id}\n"]
    lines.append(f"Warm paths found: **{paths}**")

    if connections:
        rows = []
        for c in connections:
            rows.append([
                c.get("account_id", "")[:12],
                c.get("target_name", "")[:25],
                c.get("target_headline", "")[:35],
            ])
        lines.append(table(
            ["Account", "Contact Name", "Headline"],
            rows,
        ))
    else:
        lines.append("\nNo warm introduction paths found in the pool.")

    return "\n".join(lines)


async def _handle_insights(
    client: Any, insight_type: str, segment: str, min_confidence: float,
) -> str:
    """Query and format aggregated message insights."""
    data = await client.network_insights_query(
        insight_type=insight_type,
        segment=segment,
        min_confidence=min_confidence,
    )
    insights = data.get("insights", [])
    meta = data.get("meta", {})

    lines = ["## Message Insights\n"]
    filters = meta.get("filters_applied", {})
    if filters:
        parts = [f"{k}={v}" for k, v in filters.items()]
        lines.append(f"Filters: {', '.join(parts)}")
    lines.append(f"Results: **{meta.get('total_results', len(insights))}**\n")

    if not insights:
        lines.append("No insights found. Run `network(action='insights')` after message scanning is enabled.")
        return "\n".join(lines)

    rows = []
    for ins in insights:
        value = ins.get("value", ins.get("value_json", {}))
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                value = {}
        desc = value.get("description", "") if isinstance(value, dict) else ""
        rows.append([
            ins.get("insight_type", "")[:15],
            ins.get("insight_key", "")[:25],
            ins.get("segment", "")[:20] or "—",
            desc[:40] or "—",
            f"{ins.get('confidence', 0):.0%}",
            str(ins.get("sample_size", 0)),
        ])

    lines.append(table(
        ["Type", "Key", "Segment", "Description", "Conf.", "N"],
        rows,
    ))

    # Show top insight details
    for ins in insights[:3]:
        value = ins.get("value", ins.get("value_json", {}))
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                value = {}
        if isinstance(value, dict) and value.get("description"):
            lines.append(f"\n**{ins.get('insight_key', '')}** ({ins.get('segment', 'all')})")
            lines.append(f"  {value['description']}")
            if value.get("recommended_action"):
                lines.append(f"  Recommended: {value['recommended_action']}")
            examples = value.get("examples", [])
            if examples:
                lines.append(f"  Examples: {'; '.join(str(e) for e in examples[:2])}")

    return "\n".join(lines)


async def _handle_trends(client: Any, segment: str) -> str:
    """Show industry trend insights."""
    data = await client.network_insights_query(
        insight_type="industry_trend",
        segment=segment,
    )
    insights = data.get("insights", [])

    lines = ["## Industry Trends\n"]
    if segment:
        lines.append(f"Segment filter: {segment}\n")

    if not insights:
        lines.append("No industry trends detected yet. Insights are generated daily from pool messages.")
        return "\n".join(lines)

    for ins in insights:
        value = ins.get("value", ins.get("value_json", {}))
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                value = {}
        seg = ins.get("segment", "all")
        conf = ins.get("confidence", 0)
        desc = value.get("description", "") if isinstance(value, dict) else ""
        pct = value.get("percentage", 0) if isinstance(value, dict) else 0
        action = value.get("recommended_action", "") if isinstance(value, dict) else ""

        lines.append(f"### {ins.get('insight_key', '?')} ({seg})")
        if desc:
            lines.append(f"  {desc}")
        if pct:
            lines.append(f"  Frequency: ~{pct}%")
        lines.append(f"  Confidence: {conf:.0%} | Sample: {ins.get('sample_size', 0)}")
        if action:
            lines.append(f"  Action: {action}")
        lines.append("")

    return "\n".join(lines)


async def _handle_patterns(client: Any, insight_type: str) -> str:
    """Show objection/response/timing patterns."""
    # Default to objection if no type specified
    if not insight_type:
        insight_type = "objection"

    data = await client.network_insights_query(insight_type=insight_type)
    insights = data.get("insights", [])

    type_label = insight_type.replace("_", " ").title()
    lines = [f"## {type_label} Patterns\n"]

    if not insights:
        lines.append(f"No {insight_type} patterns detected yet.")
        return "\n".join(lines)

    rows = []
    for ins in insights:
        value = ins.get("value", ins.get("value_json", {}))
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                value = {}
        desc = value.get("description", "") if isinstance(value, dict) else ""
        pct = value.get("percentage", 0) if isinstance(value, dict) else 0
        action = value.get("recommended_action", "") if isinstance(value, dict) else ""

        rows.append([
            ins.get("insight_key", "")[:25],
            ins.get("segment", "all")[:15],
            desc[:35] or "—",
            f"{pct}%" if pct else "—",
            f"{ins.get('confidence', 0):.0%}",
            action[:30] or "—",
        ])

    lines.append(table(
        ["Pattern", "Segment", "Description", "Freq", "Conf.", "Action"],
        rows,
    ))

    return "\n".join(lines)
