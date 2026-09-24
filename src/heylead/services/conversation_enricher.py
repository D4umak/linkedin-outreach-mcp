"""Conversation enrichment — merge local DB messages with live LinkedIn chat history.

Before drafting any DM (follow-up, reply, or initial outreach to existing connections),
this service fetches the actual LinkedIn conversation via get_chat_messages() and merges
it with locally tracked messages. This gives the AI full context: prior conversations
before the campaign started, prospect replies that happened outside HeyLead, etc.
"""

from __future__ import annotations

import logging
from typing import Any

from ..db.async_bridge import run_db
from ..linkedin.message_sender import message_is_ours
from ..db.queries import get_messages_for_outreach

logger = logging.getLogger(__name__)

# Default number of LinkedIn messages to fetch for context.
LINKEDIN_HISTORY_LIMIT = 15

# Two messages are considered duplicates if they share the same role
# and their timestamps are within this window (seconds).
DEDUP_WINDOW_SECONDS = 60


async def fetch_linkedin_history(
    client: Any,
    account_id: str,
    chat_id: str,
    sender_provider_id: str,
    limit: int = LINKEDIN_HISTORY_LIMIT,
) -> list[dict]:
    """Fetch LinkedIn messages and normalize to ``{role, text, timestamp}`` format.

    Args:
        client: UnipileClient or BackendClient instance.
        account_id: The Unipile account ID.
        chat_id: The LinkedIn chat/conversation ID.
        sender_provider_id: The SDR's provider_id (ACoAAA format) to distinguish
            sender vs prospect messages.
        limit: Max messages to fetch.

    Returns:
        List of dicts with keys ``role`` ("sdr" | "prospect"), ``text``, ``timestamp``.
        Sorted chronologically (oldest first).
    """
    raw_messages = await client.get_chat_messages(account_id, chat_id, limit=limit)

    normalized: list[dict] = []
    for msg in raw_messages:
        text = msg.get("text", "")
        if not text or not text.strip():
            continue
        # Unipile's is_sender first; the profile id is only the fallback.
        role = "sdr" if message_is_ours(msg, sender_provider_id) else "prospect"
        timestamp = msg.get("timestamp", 0)
        if isinstance(timestamp, str):
            try:
                timestamp = int(timestamp)
            except (ValueError, TypeError):
                timestamp = 0
        normalized.append({
            "role": role,
            "text": text.strip(),
            "timestamp": int(timestamp),
            # Provider message id — the only reliable dedup key against local
            # rows (messages.external_message_id). Timestamps drift: a local
            # row stamped when we saved it can be minutes off the provider's.
            "message_id": str(msg.get("id") or msg.get("message_id") or ""),
        })

    # get_chat_messages may return newest-first; sort chronologically.
    normalized.sort(key=lambda m: m["timestamp"])
    return normalized


def merge_conversation_history(
    local_messages: list[dict],
    linkedin_messages: list[dict],
    dedup_window_seconds: int = DEDUP_WINDOW_SECONDS,
) -> list[dict]:
    """Merge local DB messages with LinkedIn messages, deduplicating overlaps.

    Deduplication: a LinkedIn message is considered a duplicate of a local message
    if they share the same role and their timestamps differ by at most
    ``dedup_window_seconds``.

    Args:
        local_messages: Messages from ``get_messages_for_outreach()``.
        linkedin_messages: Messages from ``fetch_linkedin_history()``.
        dedup_window_seconds: Timestamp tolerance for dedup.

    Returns:
        Merged list sorted by timestamp ascending.
    """
    if not linkedin_messages:
        return list(local_messages)

    # Provider message ids first: exact, and immune to the timestamp drift
    # that made the 60s window duplicate a manually-sent LinkedIn message.
    local_ids = {
        str(m.get("external_message_id") or m.get("message_id") or "").strip()
        for m in local_messages
    }
    local_ids.discard("")

    # Build lookup from local messages for the fallback dedup.
    local_entries = [
        (m.get("role", ""), m.get("timestamp", 0))
        for m in local_messages
        if not str(
            m.get("external_message_id") or m.get("message_id") or ""
        ).strip()
    ]

    unique_linkedin: list[dict] = []
    for li_msg in linkedin_messages:
        li_id = str(li_msg.get("message_id") or "").strip()
        if li_id and li_id in local_ids:
            continue
        # No id match: fall back to the timestamp window, but only against
        # local rows that carry no provider id of their own — an id-bearing
        # local row has already been compared exactly.
        li_role = li_msg.get("role", "")
        li_ts = li_msg.get("timestamp", 0)
        is_dup = False
        for loc_role, loc_ts in local_entries:
            if loc_role == li_role and abs(loc_ts - li_ts) <= dedup_window_seconds:
                is_dup = True
                break
        if not is_dup:
            unique_linkedin.append(li_msg)

    merged = unique_linkedin + list(local_messages)
    merged.sort(key=lambda m: m.get("timestamp", 0))
    return merged


async def get_enriched_conversation(
    client: Any,
    account_id: str,
    chat_id: str | None,
    outreach_id: str,
    sender_provider_id: str,
) -> list[dict]:
    """High-level helper: fetch local + LinkedIn history, merge, return enriched list.

    Falls back to local-only on any LinkedIn API error.

    Args:
        client: UnipileClient or BackendClient.
        account_id: Unipile account ID.
        chat_id: LinkedIn chat ID (may be None if no chat exists yet).
        outreach_id: Local outreach ID for DB message lookup.
        sender_provider_id: SDR's provider_id for role mapping.

    Returns:
        Merged conversation history sorted chronologically.
    """
    local_messages = await run_db(get_messages_for_outreach, outreach_id)

    if not chat_id or not sender_provider_id:
        return local_messages

    try:
        linkedin_messages = await fetch_linkedin_history(
            client, account_id, chat_id, sender_provider_id,
        )
    except Exception as e:
        if getattr(getattr(e, "response", None), "status_code", None) == 404:
            raise
        logger.warning("Failed to fetch LinkedIn chat history, using local-only: %s", e)
        return local_messages

    return merge_conversation_history(local_messages, linkedin_messages)
