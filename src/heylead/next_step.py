"""Who a follow-up may go to: one rule for the sender and for the words.

heylead-api#1723 (28 Sep 2026): the dashboard said "Follow-up · next run" to
people who had replied, because four api sites each kept their own list of
follow-up statuses and the list printed a pointer none of them agreed on. The
api now keeps the rule in ``app/services/next_step.py``; this is the client's
copy of the same rule. The follow-up candidates, the executor's re-check, the
``send_followup`` claim and the stale-lead suggestions all read it.
"""

from __future__ import annotations

# A connected row whose opener went out and a messaged row still waiting for
# an answer. A person who answered (replied, hot_lead) gets a reply, never a
# drip; a closed or opted-out row gets nothing.
FOLLOWUP_STATUSES: tuple[str, ...] = ("connected", "messaged")

FOLLOWUP_COMMAND = 'send_message(action="followup")'
REPLY_COMMAND = 'send_message(action="reply")'


def followup_allowed(status: str | None) -> bool:
    """True when a follow-up may be planned, claimed or sent on this row."""
    return (status or "") in FOLLOWUP_STATUSES


def statuses_sql(statuses: tuple[str, ...] = FOLLOWUP_STATUSES) -> str:
    """A fixed status tuple from this module as a SQL list. Never input."""
    return ", ".join(f"'{s}'" for s in statuses)


def reengage_command(status: str | None) -> str:
    """The tool call that re-opens a quiet conversation with this person:
    a follow-up while they have not answered, a reply once they have."""
    return FOLLOWUP_COMMAND if followup_allowed(status) else REPLY_COMMAND
