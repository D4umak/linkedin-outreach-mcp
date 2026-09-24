"""When was the LinkedIn post a signal cites published?

Discovery time is not publication time. A post found today can be months old
(provider feeds return a person's last N posts, whatever their age), and an
opener that cites it as news reads as a bot. So every signal that cites a post
carries the post's publication time in ``metadata_json["published_at"]``
(epoch seconds), and activation declines one whose post is older than
PROSPECT_POST_MAX_AGE_DAYS ("stale_source") or whose time is unknown
("missing_source_time").

The date a provider hands over is often useless: Unipile's ``date`` is an age
("3mo"), which one collector's private parser read as None and then waved
through as fresh. The post id itself is better. A LinkedIn activity id is a
snowflake whose top bits are the creation time: ``int(id) >> 22`` is epoch
milliseconds. On 16 of 16 production rows carrying both, the decoded time
equalled the provider's own ``published_at`` to the second. ugcPost and share
ids are believed to use the same scheme; that is not verified.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ..constants import PROSPECT_POST_MAX_AGE_DAYS, SIGNAL_PROSPECT_POST
from ..timeutil import to_epoch

MISSING_SOURCE_TIME = "missing_source_time"
STALE_SOURCE = "stale_source"

# 2003-01-01 UTC: LinkedIn did not exist before it, so an earlier time is a
# misread (a 16-18 digit number that is not a post id decodes to the 1970s).
EARLIEST_POST_EPOCH = 1_041_379_200
# A post cannot be from the future; allow for clock skew between machines.
FUTURE_SLACK_SECONDS = 300

# A post id inside whatever the collector stored in post_id: a bare id, an
# ``urn:li:activity:<id>`` URN, or a dedup key such as ``pi:<id>:<type>``. The
# boundaries keep a run of digits inside a hash or a provider id from counting.
_POST_ID_RE = re.compile(r"(?<![0-9A-Za-z])(\d{16,20})(?![0-9A-Za-z])")


def _now(now: int | None) -> int:
    return int(now if now is not None else time.time())


def _in_range(value: int | None, now: int | None) -> int | None:
    if value is None:
        return None
    if EARLIEST_POST_EPOCH <= value <= _now(now) + FUTURE_SLACK_SECONDS:
        return value
    return None


def _metadata(signal: dict[str, Any]) -> dict[str, Any]:
    raw = signal.get("metadata_json")
    if isinstance(raw, dict):
        return raw
    try:
        meta = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def names_a_post(post_id: Any) -> bool:
    """True when ``post_id`` carries a LinkedIn post id (not merely any key)."""
    return bool(_POST_ID_RE.search(str(post_id or "")))


def post_id_published_at(post_id: Any, now: int | None = None) -> int | None:
    """Publication time (epoch seconds) encoded in a LinkedIn post id, or None."""
    match = _POST_ID_RE.search(str(post_id or ""))
    if not match:
        return None
    return _in_range((int(match.group(1)) >> 22) // 1000, now)


def post_item_published_at(
    post_id: Any, date_value: Any, now: int | None = None,
) -> int | None:
    """For a collector holding a post: when was it published?

    The id first, because it is exact; the provider's date string second, read
    as an age from ``now`` when it is one ("2d", "3mo"). None when neither
    gives a time LinkedIn could have produced; the caller must then treat the
    post as undated, never as fresh.
    """
    decoded = post_id_published_at(post_id, now)
    if decoded is not None:
        return decoded
    return _in_range(to_epoch(date_value, now=_now(now)), now)


def post_published_at(signal: dict[str, Any], now: int | None = None) -> int | None:
    """Publication time of the post a saved signal cites, or None.

    ``published_at`` in the metadata (what the collectors write) wins; then the
    time decoded from the post id; then a legacy ``post_date`` / ``timestamp``
    string, whose relative ages are counted from when the row was saved.
    """
    meta = _metadata(signal)
    stated = _in_range(to_epoch(meta.get("published_at"), now=_now(now)), now)
    if stated is not None:
        return stated
    decoded = post_id_published_at(signal.get("post_id"), now)
    if decoded is not None:
        return decoded
    saved_at = int(signal.get("detected_at") or 0) or _now(now)
    for key in ("post_date", "timestamp"):
        parsed = _in_range(to_epoch(meta.get(key), now=saved_at), now)
        if parsed is not None:
            return parsed
    return None


def cites_a_post(signal: dict[str, Any]) -> bool:
    """True when the signal's hook is a LinkedIn post.

    A prospect_post always is. Any other type is when its post_id carries a
    post id, or its collector recorded a ``published_at`` (even an empty one).
    A non-empty post_id alone is not enough: the client stores dedup keys
    there for news (an md5), company followers and web results.
    """
    if (signal.get("signal_type") or "") == SIGNAL_PROSPECT_POST:
        return True
    if names_a_post(signal.get("post_id")):
        return True
    return "published_at" in _metadata(signal)


def post_date_decline(signal: dict[str, Any], now: int | None = None) -> str | None:
    """The reason activation must not act on this signal, or None.

    ``missing_source_time`` when it cites a post whose time is unknown,
    ``stale_source`` when that post is older than PROSPECT_POST_MAX_AGE_DAYS.
    """
    if not cites_a_post(signal):
        return None
    published = post_published_at(signal, now)
    if published is None:
        return MISSING_SOURCE_TIME
    if _now(now) - published > PROSPECT_POST_MAX_AGE_DAYS * 86400:
        return STALE_SOURCE
    return None
