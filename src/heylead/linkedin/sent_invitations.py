"""Name the person on a LinkedIn sent-invitation item.

Twin of heylead-api's app/services/sent_invitations.py (#2257). An item's
``id`` is the INVITATION's own id (what a withdrawal takes); only the
``invited_user_*`` keys name the person. Reading ``inv.get("id")`` as the
person matched nothing: the hosted report called all 783 pending invites
untracked on 6 Oct 2026, and check_existing_relation fell back to it here.
"""

from __future__ import annotations

from typing import Any

# The keys on a sent-list item that name the invited person.
INVITEE_KEYS = ("invited_user_id", "invited_user_public_id", "invited_user_provider_id")


def invitee_ids(item: dict[str, Any]) -> list[str]:
    """Every identifier the item gives for the invited person."""
    ids = [str(item.get(key) or "").strip() for key in INVITEE_KEYS]
    return [i for i in dict.fromkeys(ids) if i]


def invitee_provider_id(item: dict[str, Any]) -> str:
    """The person's ACoAA provider id, or ""."""
    return str(item.get("invited_user_id") or item.get("invited_user_provider_id") or "").strip()


def invitee_public_id(item: dict[str, Any]) -> str:
    """The person's public profile slug, or ""."""
    return str(item.get("invited_user_public_id") or "").strip()
