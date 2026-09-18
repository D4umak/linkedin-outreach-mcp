"""Tool: manage_watchlist — Add, remove, and list signal keyword watchlists.

Watchlists define the keywords and competitor names that HeyLead monitors
on LinkedIn to detect buying signals from prospect posts.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from ..db.async_bridge import run_db

logger = logging.getLogger(__name__)


async def run_manage_watchlist(
    action: str = "list",
    name: str = "",
    watch_type: str = "keyword",
    keywords: str = "",
    watchlist_id: str = "",
    campaign_id: str = "",
) -> str:
    """Manage signal watchlists for keyword/competitor monitoring.

    Actions:
      list   — Show all active watchlists
      add    — Create a new watchlist (requires name + keywords)
      remove — Delete a watchlist by ID
      pause  — Deactivate a watchlist
      resume — Re-activate a watchlist

    Args:
        action: What to do: 'list', 'add', 'remove', 'pause', 'resume'.
        name: Watchlist name (for 'add').
        watch_type: 'keyword', 'competitor', 'company', 'person', or 'industry' (for 'add').
        keywords: Comma-separated keywords (for 'add'). E.g., "cold outreach, SDR automation".
        watchlist_id: Watchlist ID (for 'remove', 'pause', 'resume').
        campaign_id: Optional campaign to link the watchlist to.

    Returns:
        Formatted result string.
    """
    from ..db.signal_queries import (
        delete_watchlist,
        get_watchlist,
        list_watchlists,
        save_watchlist,
        update_watchlist,
    )

    action = action.lower().strip()

    # ── List ──
    if action == "list":
        watchlists = await run_db(list_watchlists, is_active=None)  # Show all
        if not watchlists:
            return (
                "No watchlists configured yet.\n\n"
                "Create one with: manage_watchlist action='add' name='My Keywords' "
                "keywords='cold outreach, SDR automation'\n\n"
                "Watchlists are also auto-created when you generate an ICP."
            )

        lines = [f"Signal Watchlists ({len(watchlists)}):\n"]
        for wl in watchlists:
            status = "Active" if wl.get("is_active") else "Paused"
            wt = wl.get("watch_type", "keyword")
            kws = wl.get("keywords_list", [])
            kw_str = ", ".join(f'"{k}"' for k in kws[:5])
            if len(kws) > 5:
                kw_str += f" (+{len(kws) - 5} more)"

            lines.append(f"  [{wl['id']}] {wl.get('name', 'Unnamed')} ({wt}, {status})")
            lines.append(f"    Keywords: {kw_str}")
            if wl.get("campaign_id"):
                lines.append(f"    Linked to campaign: {wl['campaign_id']}")
            last_polled = wl.get("last_polled_at")
            if last_polled:
                from ..services.signal_service import _format_time_ago
                lines.append(f"    Last polled: {_format_time_ago(last_polled)}")
            lines.append("")

        return "\n".join(lines)

    # ── Add ──
    if action == "add":
        if not name:
            return "Error: 'name' is required for adding a watchlist."
        if not keywords:
            return "Error: 'keywords' is required. Provide comma-separated keywords."

        # Parse keywords
        kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
        if not kw_list:
            return "Error: No valid keywords found. Provide comma-separated keywords."

        if watch_type not in (
            "keyword", "competitor", "company", "person", "industry",
        ):
            watch_type = "keyword"

        wl_id = await run_db(save_watchlist, name=name,
            watch_type=watch_type,
            keywords=kw_list,
            campaign_id=campaign_id or None,)

        return (
            f"Watchlist created: [{wl_id}] {name}\n"
            f"  Type: {watch_type}\n"
            f"  Keywords: {', '.join(kw_list)}\n"
            f"  Status: Active\n\n"
            f"The scheduler will start monitoring these keywords on LinkedIn posts.\n"
            f"Check signals with: show_signals"
        )

    # ── Remove ──
    if action == "remove":
        if not watchlist_id:
            return "Error: 'watchlist_id' is required. Use action='list' to see IDs."
        wl = await run_db(get_watchlist, watchlist_id)
        if not wl:
            return f"Watchlist '{watchlist_id}' not found."
        # Children first (no ON DELETE CASCADE). Ban keywords only after
        # the delete succeeds so a FK failure does not permanently hide
        # keywords the user still has.
        await run_db(delete_watchlist, watchlist_id)
        import json as _json
        from ..db.signal_queries import save_optimization_history
        try:
            keywords = _json.loads(wl.get("keywords") or "[]")
            for kw in keywords:
                await run_db(
                    save_optimization_history,
                    optimization_type="user_removed_keyword",
                    target=kw.strip().lower(),
                    before_value=f"watchlist {watchlist_id[:8]}",
                    after_value="removed",
                    reason=f"User deleted watchlist '{wl.get('name', '')}'",
                )
        except Exception:
            pass
        return f"Watchlist deleted: [{watchlist_id}] {wl.get('name', 'Unnamed')}"

    # ── Pause ──
    if action == "pause":
        if not watchlist_id:
            return "Error: 'watchlist_id' is required."
        wl = await run_db(get_watchlist, watchlist_id)
        if not wl:
            return f"Watchlist '{watchlist_id}' not found."
        await run_db(update_watchlist, watchlist_id, is_active=0)
        return f"Watchlist paused: [{watchlist_id}] {wl.get('name', 'Unnamed')}"

    # ── Resume ──
    if action == "resume":
        if not watchlist_id:
            return "Error: 'watchlist_id' is required."
        wl = await run_db(get_watchlist, watchlist_id)
        if not wl:
            return f"Watchlist '{watchlist_id}' not found."
        await run_db(update_watchlist, watchlist_id, is_active=1)
        return f"Watchlist resumed: [{watchlist_id}] {wl.get('name', 'Unnamed')}"

    return f"Unknown action: '{action}'. Use 'list', 'add', 'remove', 'pause', or 'resume'."
