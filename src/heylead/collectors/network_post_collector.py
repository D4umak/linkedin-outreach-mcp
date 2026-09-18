"""Scan 1st-degree connections' posts for signals, outside any campaign.

``prospect_post_collector`` scans contacts inside campaigns. That is the right
surface for a sales campaign and the wrong one for a job search, where the
valuable signal is someone in the network announcing a role. On 10 Aug 2026 the
CEO of a venture builder publicly asked for Head of Marketing recommendations
and the product had no way to see it: he was one of 16,067 connections, not one
of 11 campaign contacts.

Scanning everyone every cycle is not affordable, so selection is prioritised by
headline and rotated by last_scanned_at. Signals are saved at network level with
no campaign_id, and nobody is enrolled as a contact — this collector only reads.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..constants import SIGNAL_PROSPECT_POST, SIGNAL_PROSPECT_POST_LOOKBACK_DAYS
from ..db.async_bridge import run_db
from ..db.queries import get_connections_to_scan, mark_connection_scanned
from ..db.signal_queries import save_signal, signal_exists
from ..linkedin import get_account_id, get_linkedin_client
from ..timeutil import to_epoch

logger = logging.getLogger(__name__)

NETWORK_SCAN_SOURCE = "network_scan"


async def collect_network_posts(limit: int = 25) -> str:
    """Scan a bounded slice of connections for recent posts.

    Args:
        limit: How many connections to scan this run.

    Returns:
        Summary string.
    """
    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected — skipping network post scan."

    connections = await run_db(get_connections_to_scan, limit)
    if not connections:
        return "No connections matched the network watch criteria."

    client = get_linkedin_client()
    scanned = 0
    saved = 0
    errors = 0
    cutoff = int(time.time()) - (SIGNAL_PROSPECT_POST_LOOKBACK_DAYS * 86400)

    try:
        for conn in connections:
            provider_id = conn.get("provider_id") or ""
            if not provider_id:
                continue
            try:
                posts = await client.get_user_posts(account_id, provider_id, limit=5)
            except Exception as e:
                errors += 1
                logger.debug("Network scan failed for %s: %s", provider_id, e)
                continue

            scanned += 1
            for post in posts or []:
                text = (post.get("text") or "").strip()
                if not text:
                    continue
                posted_at = to_epoch(post.get("date") or post.get("timestamp"))
                if posted_at and posted_at < cutoff:
                    continue
                post_id = str(post.get("id") or "")
                # Rotation re-reads the same connection on later runs, so without
                # this the same post was saved once per scan: 2,229 prospect_post
                # signals over 1,450 distinct post_ids, one post stored 36 times,
                # each copy re-sent to the classifier. That is what exhausted the
                # LLM quota (199 provider 429s in a day).
                # The bool(post_id) test is load-bearing: signal_exists() drops
                # a falsy post_id from its WHERE clause, so passing "" would
                # match ANY prospect_post signal and silently stop the
                # collector saving anything at all.
                try:
                    seen = bool(post_id) and await run_db(
                        signal_exists, SIGNAL_PROSPECT_POST, post_id=post_id
                    )
                except Exception as e:
                    # Fail open: a dedup read that fails costs one duplicate
                    # row; letting it escape would abandon the whole scan.
                    seen = False
                    logger.debug("Dedup check failed for post %s: %s", post_id, e)
                if seen:
                    continue
                try:
                    await run_db(
                        save_signal,
                        SIGNAL_PROSPECT_POST,
                        NETWORK_SCAN_SOURCE,
                        prospect_name=conn.get("name") or "",
                        prospect_title=conn.get("headline") or "",
                        linkedin_id=provider_id,
                        # Deliberately no campaign_id: these people are not
                        # campaign contacts and must not become them.
                        campaign_id=None,
                        content=text[:4000],
                        post_id=post_id,
                    )
                    saved += 1
                except Exception as e:
                    errors += 1
                    logger.debug("Saving network signal failed for %s: %s", provider_id, e)

            await run_db(mark_connection_scanned, provider_id)
    finally:
        try:
            await client.close()
        except Exception:
            pass

    return (
        f"Network scan: {scanned} connections scanned, {saved} post signals saved"
        + (f", {errors} errors" if errors else "")
    )
