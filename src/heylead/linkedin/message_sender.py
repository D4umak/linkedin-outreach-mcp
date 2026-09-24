"""Who sent a LinkedIn message: one rule for the client.

Unipile answers the question itself: ``is_sender`` is 1 on a message the
connected seat sent and 0 on one it received. That answer wins. Comparing the
message's ``sender_id`` with the seat's ``profile.provider_id`` is only the
fallback, for a message that carries no flag: the profile is one setting per
install, not per seat, and when it is empty or stale -- or LinkedIn puts our
message under another id form -- the comparison turns our own messages into
the prospect's. heylead-api did exactly that until 9 Sep 2026 (its copy
compared a member id with the Unipile account id) and stored our follow-ups
as replies; its rule is ``scheduler_executor._message_is_ours``, and this one
gives the same answers.

Every normaliser in ``linkedin/`` keeps the flag with ``sender_flag``; every
caller asks ``message_is_ours``. ``.semgrep/who-sent-it-decided-by-hand.yaml``
fails a copy.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_OURS = (1, True, "1", "true", "True")


def sender_flag(raw: Mapping[str, Any]) -> int | None:
    """Unipile's ``is_sender`` as 1 or 0, or None when the message has none."""
    flag = raw.get("is_sender")
    if flag is None or flag == "":
        return None
    return 1 if flag in _OURS else 0


def message_is_ours(msg: Mapping[str, Any], own_provider_id: str = "") -> bool:
    """Whether the connected seat sent this message.

    ``is_sender`` wins whenever present. Without it, the message is ours only
    if its sender id is the seat's own provider id -- never when we do not know
    that id.
    """
    flag = sender_flag(msg)
    if flag is not None:
        return flag == 1
    sender = msg.get("sender") if isinstance(msg.get("sender"), Mapping) else {}
    sender_id = str(msg.get("sender_id") or sender.get("provider_id") or "")
    return bool(own_provider_id) and bool(sender_id) and sender_id == own_provider_id
