"""How long a person took to answer us: one definition, here and in the api.

A response time is the time from our message to the reply that answers it:
for each prospect message whose previous message in the thread is ours, the
seconds between the two. A reply that follows another reply, or comes before
we wrote at all, answers nothing of ours and is not counted.

Both repos measured "time to reply" as ``first_reply_at - accepted_at``, the
time from the acceptance to the first reply. For a connection accepted months
before we ever wrote, that is the connection's age: the dashboard's 30-day
Replies read "Avg Response Time 2214.4h", about 92 days, and 27 of the 51
replies in that window came more than 30 days after their acceptance
(heylead-api#1726, UI QA round 2 of #1578, F14, 28 Sep 2026).

The api (D4umak/heylead-api) has the same file at app/services/response_time.py; keep the two the same.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

OURS = frozenset({"sdr", "assistant"})
THEIRS = frozenset({"prospect"})


def responses(
    messages: Iterable[tuple],
    since: int | float | None = None,
) -> dict[Any, int]:
    """Each reply that answers one of our messages, by its key, with the
    seconds it took. ``messages`` are (outreach id, role, when[, key]); the
    key defaults to the message's place in ``messages``.

    With ``since``, only replies to messages we sent at or after it, which
    are then received after it too: a window's average cannot be longer than
    the window.
    """
    threads: dict[str, list[tuple[float, int, str, Any]]] = defaultdict(list)
    for i, row in enumerate(messages):
        outreach_id, role, at = row[0], row[1], row[2]
        key = row[3] if len(row) > 3 else i
        if at:
            threads[outreach_id].append((float(at), i, role, key))
    out: dict[Any, int] = {}
    for rows in threads.values():
        rows.sort(key=lambda r: (r[0], r[1]))
        previous_role = ""
        ours_at = 0.0
        for at, _i, role, key in rows:
            if role in OURS:
                ours_at = at
            elif role in THEIRS and previous_role in OURS:
                if since is None or ours_at >= since:
                    out[key] = int(at - ours_at)
            previous_role = role
    return out


def response_seconds(
    messages: Iterable[tuple],
    since: int | float | None = None,
) -> list[int]:
    """The response times in ``messages``, as ``responses`` finds them."""
    return list(responses(messages, since).values())
