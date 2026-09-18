"""Tool: unlink_account — Disconnect LinkedIn from HeyLead.

In backend mode: calls backend DELETE /accounts/unlink and clears local account_id.
In direct mode: clears local account_id only (no Unipile API to unlink).
User can reconnect later via setup_profile.
"""

from __future__ import annotations

import logging

from ..config import is_backend_mode
from ..linkedin import get_account_id, get_linkedin_client, set_account_id
from ..linkedin.backend_client import BackendClient
from ..db.async_bridge import run_db

logger = logging.getLogger(__name__)


async def run_unlink_account() -> str:
    """Disconnect the current LinkedIn account from HeyLead.

    Backend mode: unlinks on the server and clears local account_id.
    Direct mode: clears local account_id only.
    """
    account_id = await run_db(get_account_id)
    if not account_id:
        return (
            "No LinkedIn account is connected.\n\n"
            "Run setup_profile() to connect an account."
        )

    if is_backend_mode():
        try:
            client = get_linkedin_client()
            if isinstance(client, BackendClient):
                await client.unlink_account()
        except Exception as e:
            logger.warning(f"Backend unlink failed (clearing local anyway): {e}")
            # Still clear local so user isn't stuck

    await run_db(set_account_id, None)
    logger.info("Unlinked LinkedIn account (local account_id cleared)")

    return (
        "✅ LinkedIn account disconnected.\n\n"
        "Your campaigns and data are still in HeyLead. "
        "To connect again (same or different LinkedIn), run setup_profile()."
        + _daemon_warning()
    )


def _daemon_warning() -> str:
    """Warn while the scheduler daemon can still send from the old account.

    The daemon is a separate process that caches the account id at startup and
    does not re-read it, so clearing the setting here does not reach it.
    """
    try:
        from .. import daemon as daemon_mod

        status = daemon_mod.daemon_status()
        plist = daemon_mod._plist_path()
    except Exception as e:
        logger.debug(f"Could not read daemon status: {e}")
        return ""

    if not status.get("leader_alive") or status.get("leader_kind") != "daemon":
        return ""

    return (
        "\n\n⚠️  The HeyLead scheduler daemon (pid "
        f"{status.get('leader_pid')}) is still running and holds the previous "
        "account in memory — it will keep sending from it until it restarts.\n"
        f"Stop it now with:  launchctl unload {plist}"
    )
