"""Whose voice an ICP or a campaign is built in: the workspace's seat.

`generate_icp` and `create_campaign` used to build their "USER CONTEXT (the
sender)" from the local ``profile`` setting — the person who installed this
client. An editor setting up a customer's workspace from their own Mac
therefore had the ICP's relevance hook, and every campaign built on it,
present *the editor* as the sender (10 Sep 2026: a customer's campaign
carried "Denys Chumak is a 3x AI founder…" as its hook and sent from the
customer's LinkedIn).

In one's own workspace the local profile and expertise map are right. In
anyone else's workspace the sender is that workspace's connected seat: its
identity from the workspace-scoped accounts list, enriched by reading that
public profile. Not ``/users/me`` — through the hosted proxy that answers
for the caller's own seat whatever account id is passed. The laptop owner's
expertise map is left out.
When the seat cannot be read the context is empty: no sender is better than
the wrong one. The hosted twin of this rule is heylead-api's
``refresh_sender_profile`` job and the owner check in ``upsert_all_sync_data``.
"""

from __future__ import annotations

import logging
from typing import Any

from ..db.async_bridge import run_db
from ..db.queries import get_setting
from ..linkedin import get_linkedin_client

logger = logging.getLogger(__name__)

# What a seat's own profile contributes. Anything the local profile carries
# beyond these (expertise map, brand analysis) is about the laptop's owner.
_IDENTITY_KEYS = (
    "name", "first_name", "last_name", "headline", "title", "company",
    "location", "industry", "summary", "public_id", "provider_id", "profile_url",
)


async def fetch_active_role() -> str | None:
    """The caller's role in the active workspace; None when not hosted."""
    from ..tools.organization import fetch_active_role as _fetch
    return await _fetch()


async def sender_context() -> dict[str, Any]:
    """The sender the active workspace speaks as."""
    role = await fetch_active_role()
    if role in (None, "owner"):
        profile = await run_db(get_setting, "profile", {}) or {}
        expertise = await run_db(get_setting, "expertise_map", {}) or {}
        return {**profile, **expertise}
    return await _seat_identity()


def _identity_from_account(account: dict[str, Any]) -> dict[str, Any]:
    """The seat's identity as the workspace's accounts entry states it.

    ``/api/v1/accounts`` with X-Org-Id lists the workspace's seat as a raw
    Unipile Account: ``connection_params.im`` carries the LinkedIn provider
    id, the public identifier and the display name, and ``organizations``
    the company pages the seat administers.
    """
    im = (account.get("connection_params") or {}).get("im") or {}
    name = str(im.get("username") or account.get("name") or "").strip()
    public_id = str(im.get("publicIdentifier") or im.get("public_identifier") or "").strip()
    provider_id = str(im.get("id") or "").strip()
    orgs = im.get("organizations") or []
    company = ""
    if isinstance(orgs, list) and orgs and isinstance(orgs[0], dict):
        company = str(orgs[0].get("name") or "").strip()
    out: dict[str, Any] = {}
    if name:
        out["name"] = name
    if public_id:
        out["public_id"] = public_id
        out["profile_url"] = f"https://www.linkedin.com/in/{public_id}"
    if provider_id:
        out["provider_id"] = provider_id
    if company:
        out["company"] = company
    return out


async def _seat_identity() -> dict[str, Any]:
    """The workspace seat's identity: the accounts entry, enriched by its
    public profile. Never ``get_own_profile``: through the hosted proxy
    ``/users/me`` answers for the CALLER's own seat whatever account id is
    passed (verified 10 Sep 2026), which is exactly the wrong person here."""
    client = get_linkedin_client()
    try:
        accounts = await client.list_accounts()
        account = next(
            (a for a in (accounts or []) if isinstance(a, dict)
             and (a.get("id") or a.get("account_id"))),
            None,
        )
        if not account:
            logger.warning("sender_context: the active workspace has no connected seat")
            return {}
        identity = _identity_from_account(account)
        if not identity.get("name") and not identity.get("public_id"):
            logger.warning("sender_context: the workspace's seat states no identity")
            return {}
        account_id = str(account.get("id") or account.get("account_id"))
        if identity.get("public_id"):
            try:
                public = await client.get_profile(account_id, identity["public_id"])
            except Exception as e:
                logger.warning("sender_context: could not read the seat's public profile: %s", e)
                public = {}
            if isinstance(public, dict) and public.get("name"):
                for key in _IDENTITY_KEYS:
                    value = public.get(key)
                    if value not in (None, "") and key not in ("public_id", "provider_id"):
                        identity[key] = value
        return identity
    except Exception as e:
        logger.warning("sender_context: could not read the workspace's seat: %s", e)
        return {}
    finally:
        try:
            await client.close()
        except Exception:
            pass
