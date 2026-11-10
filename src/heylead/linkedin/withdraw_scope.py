"""Which sent invitations HeyLead may withdraw (heylead-api#5601).

A seat's LinkedIn sent list holds every pending invitation, including the ones
the user sent by hand or through another tool. HeyLead withdraws only the ones
it sent, and only where their campaign allows it:

- the invitation must match an ``invited`` HeyLead outreach row
  (``db.queries.match_invitation_outreach``);
- the invitation must date from that row, the same rule as the hosted sweep
  (heylead-api ``_stale_match_verdict``, #5267 ruling b). An invitation sent
  more than a month before HeyLead recorded the row was sent by hand and only
  recorded as ours: the pre-send check marks a row ``invited`` when it finds
  the user's own pending invitation, and LinkedIn's "already invited" answer
  counts as a send. One sent more than STALE_INVITE_DAYS after the row is a
  later re-invite, not ours either;
- the campaign must be active (the hosted ``_campaign_active``), and its
  ``withdraw_stale_invites`` on. Unset means on, the default the #5267
  four-role review ruled.

When a person has several ``invited`` rows, one campaign that keeps its
invitations keeps this one: withdrawing is the act that cannot be undone.

Every client path that calls ``withdraw_invitation`` asks ``withdrawal_target``
first; tests/test_withdraw_scope_guard.py fails the suite if one does not.
"""

from __future__ import annotations

import json
from typing import Any

from ..constants import STALE_INVITE_DAYS
from ..flags import flag_enabled
from ..timeutil import to_epoch
from .sent_invitations import invitee_provider_id, invitee_public_id

# Why an invitation was left pending.
NOT_HEYLEAD = "not_heylead"  # no matching HeyLead invitation behind it
CAMPAIGN_OFF = "campaign_off"  # its campaign is not active, or withdrawal is off

# A matched invitation counts only between the row's invited_at minus one
# month bucket and invited_at plus the stale window: an old invitation's date
# is a fuzzy "Sent 5 months ago" resolved to the month (hosted
# STALE_MATCH_PREDATES_SECONDS).
MATCH_PREDATES_SECONDS = 31 * 86400
MATCH_POSTDATES_SECONDS = STALE_INVITE_DAYS * 86400


def invitation_sent_at(inv: dict[str, Any]) -> int:
    """When the invitation was sent, as epoch seconds; 0 when it carries no date."""
    return to_epoch(
        inv.get("parsed_datetime")
        or inv.get("timestamp")
        or inv.get("created_at")
        or inv.get("date")
        or 0
    ) or 0


def _campaign_allows(row: dict[str, Any]) -> bool:
    if (row.get("campaign_status") or "") != "active":
        return False
    try:
        config = json.loads(row.get("config_json") or "{}")
    except (TypeError, ValueError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    return flag_enabled(config, "withdraw_stale_invites", default=True)


def _dates_match(sent_at: int, recorded_at: int) -> bool:
    if not sent_at or not recorded_at:
        return False
    if sent_at < recorded_at - MATCH_PREDATES_SECONDS:
        return False  # sent by hand, then recorded as ours
    if sent_at > recorded_at + MATCH_POSTDATES_SECONDS:
        return False  # a later re-invite
    return True


def withdrawal_target(inv: dict[str, Any]) -> tuple[str | None, str]:
    """Return ``(outreach_id, "")`` when HeyLead may withdraw ``inv``.

    Otherwise ``(None, reason)``: ``NOT_HEYLEAD`` when no HeyLead invitation
    stands behind it (no ``invited`` row, or none that dates from it),
    ``CAMPAIGN_OFF`` when a campaign holding the person is not active or has
    withdrawal off. Synchronous (it reads the local database); call it
    through run_db.
    """
    from ..db.queries import match_invitation_outreach

    rows = match_invitation_outreach(invitee_provider_id(inv), invitee_public_id(inv))
    sent_at = invitation_sent_at(inv)
    ours = [row for row in rows if _dates_match(sent_at, int(row.get("recorded_at") or 0))]
    if not ours:
        # Dates first, so a hand-sent invitation is counted as not ours even
        # when the person also sits in a paused campaign (analyst-2-o1).
        return None, NOT_HEYLEAD
    if not all(_campaign_allows(row) for row in rows):
        return None, CAMPAIGN_OFF
    return ours[0]["id"], ""
