"""Shared helpers for inbound pipeline and inbound service.

Extracted from inbound_pipeline.py and inbound_service.py to eliminate
exact duplicates of _has_prior_conversation() and _resolve_campaign_for_signal().
"""

from __future__ import annotations

import logging
from typing import Any

from ..db.queries import list_campaigns

logger = logging.getLogger(__name__)


async def has_prior_conversation(
    client: Any,
    account_id: str,
    sender_id: str,
    signal_created_at: int,
    buffer_seconds: int = 300,
) -> bool:
    """Return True if a pre-existing conversation exists with this person.

    Fetches the chat and checks if any messages predate the inbound signal's
    ``created_at`` timestamp (minus a small buffer for clock skew). If older
    messages exist, this is a friend or ongoing conversation — skip the DM.

    Fails open: if the check errors out, returns ``False`` (allow the DM)
    rather than silently dropping a lead.
    """
    try:
        chat_id = await client.find_chat_for_user(account_id, sender_id)
        if not chat_id:
            return False

        messages = await client.get_chat_messages(account_id, chat_id, limit=10)
        if not messages:
            return False

        cutoff = signal_created_at - buffer_seconds
        for msg in messages:
            ts = msg.get("timestamp", 0)
            if ts and ts < cutoff:
                return True

        return False
    except Exception as e:
        logger.debug(
            "Prior conversation check failed for sender %s: %s — allowing DM",
            sender_id, e,
        )
        return False


def resolve_campaign_for_signal(signal: dict[str, Any]) -> str:
    """Resolve the best campaign for a qualified inbound signal.

    Uses ICP keyword overlap matching (same algorithm as signal_linker).
    Skips paused campaigns. Returns empty string if no match.
    """
    from .signal_linker import _match_best_campaign

    active_campaigns = list_campaigns(status="active")
    if not active_campaigns:
        return ""

    signal_for_matching = {
        "content": (signal.get("content") or ""),
        "prospect_title": (signal.get("sender_headline") or ""),
        "metadata_json": "",
    }

    campaign_id = _match_best_campaign(signal_for_matching, active_campaigns)
    return campaign_id or ""
