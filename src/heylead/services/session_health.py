"""Detect a dead LinkedIn session and make sure a human finds out.

28 Aug-5 Sep 2026: the session died (Unipile source CREDENTIALS) and nobody
noticed for a week — LinkedIn silently discarded every send while the
dashboard looked normal. Entitlement reads kept answering, so nothing the
user routinely looked at ever changed.

The daemon calls :func:`check_session_health` from its sync loop. On a
positively-observed death it records ``linkedin_session_dead`` in settings —
``show_status`` banners that — and posts a macOS notification, deduped to one
per window so a dead session is a nudge, not a firehose.

The collapse-pattern rule (21 Aug) applies throughout: a failed probe is NOT
a dead session, and must neither raise the flag nor clear a standing one.
Only a listing that positively shows the state moves it.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any, Awaitable, Callable

from ..db.async_bridge import run_db
from ..db.queries import delete_setting, get_setting, save_setting

logger = logging.getLogger(__name__)

SETTING_KEY = "linkedin_session_dead"           # {"since": ts, "reason": str}
NOTIFIED_KEY = "linkedin_session_dead_notified_at"
RENOTIFY_SECONDS = 6 * 3600
CHECK_INTERVAL_SECONDS = 1800  # daemon probes at most every 30 min


async def _default_notify(title: str, body: str) -> None:
    """Post a macOS user notification. Best-effort, never raises."""
    if sys.platform != "darwin":
        return
    # osascript reads the strings as AppleScript literals — strip the one
    # character that could break out of them.
    title = title.replace('"', "'")
    body = body.replace('"', "'")
    script = f'display notification "{body}" with title "{title}"'
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
    except Exception as e:
        logger.debug("macOS notification failed: %s", e)


async def check_session_health(
    client: Any = None,
    notify: Callable[[str, str], Awaitable[None]] | None = None,
) -> dict[str, Any] | None:
    """Probe the backend's view of the LinkedIn account; record + nudge on death.

    Returns the recorded dead-state dict when the session is dead, else None.
    ``client``/``notify`` are injectable for tests; production uses the
    singleton BackendClient and the macOS notifier.
    """
    from .. import config

    if not config.is_backend_mode():
        return None

    if client is None:
        from ..linkedin import get_linkedin_client
        client = get_linkedin_client()
    if notify is None:
        notify = _default_notify

    try:
        accounts = await client.list_accounts()
    except Exception as e:
        # Unreachable backend != dead session. Change nothing either way.
        logger.debug("Session health probe failed (inconclusive): %s", e)
        return await run_db(get_setting, SETTING_KEY) or None

    from ..linkedin.unipile import interpret_account_status

    reason = ""
    if not accounts:
        # /api/v1/accounts filters to the user's own binding — an empty
        # listing means nothing can send. Distinct from a dead source.
        reason = "No LinkedIn account bound — nothing can send"
    else:
        verdicts = [interpret_account_status(a) for a in accounts if isinstance(a, dict)]
        if any(ok for ok, _ in verdicts):
            # Healthy — clear any standing flag so the banner cannot outlive
            # the reconnect.
            if await run_db(get_setting, SETTING_KEY):
                await run_db(delete_setting, SETTING_KEY)
                logger.info("LinkedIn session healthy again — death flag cleared")
            return None
        reason = verdicts[0][1] if verdicts else "Account listing unreadable"

    now = int(time.time())
    existing = await run_db(get_setting, SETTING_KEY) or {}
    dead = {"since": existing.get("since") or now, "reason": reason}
    await run_db(save_setting, SETTING_KEY, dead)
    logger.warning("LinkedIn session dead: %s (since %s)", reason, dead["since"])

    notified_at = int(await run_db(get_setting, NOTIFIED_KEY, 0) or 0)
    if now - notified_at >= RENOTIFY_SECONDS:
        await run_db(save_setting, NOTIFIED_KEY, now)
        await notify(
            "HeyLead: LinkedIn disconnected",
            f"{reason}. Outreach is NOT being delivered — reconnect via "
            f"account(action='list').",
        )
    return dead


def session_dead_banner_lines(dead: dict[str, Any] | None) -> list[str]:
    """Dashboard banner for a recorded session death. [] when healthy."""
    if not dead:
        return []
    since = dead.get("since") or 0
    hours = max(0, int(time.time()) - int(since)) // 3600
    age = f"{hours // 24}d {hours % 24}h" if hours >= 24 else f"{hours}h"
    return [
        f"🔴 LINKEDIN SESSION DEAD for {age} — {dead.get('reason', 'unknown')}",
        "   Sends are silently discarded until you reconnect: "
        "account(action='list') has the link.",
        "",
    ]
