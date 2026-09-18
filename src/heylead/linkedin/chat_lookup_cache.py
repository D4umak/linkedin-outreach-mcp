"""Process-local TTL cache for find_chat_for_user hits and misses.

The same provider IDs were scanned 3 pages + attendee ~80 times each overnight.
A short TTL (hits and None) keyed by (account_id, provider_id) stops that.
"""

from __future__ import annotations

import time
from typing import Optional

_TTL_SECONDS = 20 * 60
_cache: dict[tuple[str, str], tuple[float, Optional[str]]] = {}


def get_cached_chat(account_id: str, provider_id: str) -> tuple[bool, Optional[str]]:
    """Return (hit, chat_id). chat_id may be None on a cached miss."""
    key = (account_id or "", provider_id or "")
    if not key[1]:
        return False, None
    row = _cache.get(key)
    if not row:
        return False, None
    ts, chat_id = row
    if time.monotonic() - ts > _TTL_SECONDS:
        _cache.pop(key, None)
        return False, None
    return True, chat_id


def store_cached_chat(account_id: str, provider_id: str, chat_id: Optional[str]) -> None:
    key = (account_id or "", provider_id or "")
    if not key[1]:
        return
    _cache[key] = (time.monotonic(), chat_id)


def clear_cached_chats() -> None:
    _cache.clear()
