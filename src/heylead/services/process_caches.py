"""Re-read the identity settings that long-lived processes memoize.

``get_account_id`` and ``get_email_account_id`` cache in module globals because
they are sync DB reads reached from async call sites, and ``get_db`` refuses to
run on the event loop thread. That trade is right for the hot path and wrong
over hours: the launchd daemon holds whatever it read at startup, so an account
switched or an email account connected from an MCP session never reaches it.

Refreshing per scheduler tick keeps the hot path unchanged and bounds the
staleness to one tick. Every function here is SYNC and must be called through
``run_db`` so the reads land on the worker thread.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def refresh_process_caches() -> None:
    """Re-read cached identity settings from the database.

    Safe to call when nothing has changed: each read is a single indexed
    lookup, and the caches are only rewritten with what the DB now says.
    """
    from ..db.queries import get_setting
    from ..linkedin import unipile
    from . import channel_selector

    account_id = get_setting("unipile_account_id", None)
    if account_id != unipile._cached_account_id:
        logger.info(
            "Account id changed underneath this process: %s -> %s",
            unipile._cached_account_id, account_id,
        )
        # Pending invitations belong to the previous account.
        from ..linkedin.rate_limiter import invalidate_pending_cache
        invalidate_pending_cache()
    unipile._cached_account_id = account_id
    unipile._cached_account_id_set = True

    email_account_id = get_setting("email_account_id", "") or ""
    if email_account_id != channel_selector._EMAIL_ACCOUNT_CACHE:
        logger.info(
            "Email account changed underneath this process: %s -> %s",
            channel_selector._EMAIL_ACCOUNT_CACHE, email_account_id,
        )
    channel_selector._EMAIL_ACCOUNT_CACHE = email_account_id
