"""Tool: organization — list, switch, and manage hosted HeyLead orgs."""

from __future__ import annotations

from typing import Any

from ..config import get_active_org_id, is_backend_mode, set_active_org_id
from ..linkedin import UnipileError, get_linkedin_client


VIEWER_BLOCKED = (
    "This organization is view-only for you. Switch to an org where you are "
    "owner or editor, or ask the owner to upgrade your role."
)

HOSTED_SEND_BLOCKED = (
    "HeyLead sends from the cloud, not from this machine. Your active "
    "campaigns send on their own; use campaign(action='launch') to start a "
    "campaign, or the dashboard to send something one-off. Nothing was sent."
)


def next_step_hint() -> str:
    """The one truthful onboarding instruction for the configured sender."""
    from ..services.cloud_sync import local_scheduler_engine_enabled

    if not local_scheduler_engine_enabled():
        return (
            "**Next:** create_campaign(...) → "
            "campaign(action='launch') → check_replies."
        )
    return "**Next:** create_campaign(...) → generate_and_send → check_replies."


async def _orgs_payload() -> dict[str, Any]:
    if not is_backend_mode():
        raise UnipileError("Organizations require a hosted HeyLead account.")
    client = get_linkedin_client()
    url = f"{client.base_url.rstrip('/')}/api/v1/orgs"
    resp = await client._client.get(url, headers=client._headers())
    resp.raise_for_status()
    return resp.json()


async def fetch_active_role() -> str | None:
    try:
        data = await _orgs_payload()
    except Exception:
        return None
    active = get_active_org_id()
    orgs = data.get("orgs") or []
    if not orgs:
        return None
    if active:
        for org in orgs:
            if org.get("id") == active:
                return org.get("role")
    default = data.get("default_org_id")
    for org in orgs:
        if org.get("id") == default:
            return org.get("role")
    return orgs[0].get("role")


async def refuse_if_viewer() -> str | None:
    role = await fetch_active_role()
    if role == "viewer":
        return VIEWER_BLOCKED
    return None


async def refuse_if_hosted_send() -> str | None:
    """Refuse a send that would run here when the cloud owns sending.

    Every "whose identity does the laptop use" defect -- the own-profile
    cache shared across workspaces (heylead#350) among them -- exists
    because a send can run here at all. When the cloud owns sending, it is
    the only sender, so the question never arises.

    The question is NOT ``is_backend_mode()``. A hosted account can opt this
    machine back in with ``sending_host='local'``, and then the daemon runs
    SchedulerEngine, whose executors call run_generate_and_send themselves
    (scheduler/executors.py). Refusing there would both break a setup the
    user deliberately chose and be recorded as a SEND: the executors classify
    an unrecognised result string as success (ops_log.classify_result), so a
    refusal would be logged as outreach that never happened.

    ``local_scheduler_engine_enabled`` is the same predicate the daemon uses
    to decide whether to start that engine, so the two cannot drift apart.
    """
    from ..services.cloud_sync import local_scheduler_engine_enabled

    if not local_scheduler_engine_enabled():
        return HOSTED_SEND_BLOCKED
    return None


async def run_organization(
    action: str = "list",
    org_id: str = "",
    email: str = "",
    role: str = "editor",
    user_id: str = "",
) -> str:
    action = (action or "list").lower().strip()
    if not is_backend_mode():
        return "Organizations are a hosted HeyLead feature. Sign in with setup_profile first."

    client = get_linkedin_client()
    base = client.base_url.rstrip("/")
    headers = client._headers()

    if action == "list":
        data = await _orgs_payload()
        active = get_active_org_id() or data.get("default_org_id") or ""
        lines = ["Organizations:\n"]
        for org in data.get("orgs") or []:
            mark = " <-- current" if org.get("id") == active else ""
            li = "LinkedIn connected" if org.get("linkedin_connected") else "no LinkedIn"
            lines.append(
                f"  {org.get('id')} | {org.get('name')} | {org.get('role')} | {li}{mark}"
            )
        if not data.get("orgs"):
            lines.append("  (none)")
        return "\n".join(lines)

    if action == "switch":
        if not org_id:
            return "Provide org_id to switch. Use organization(action='list') first."
        data = await _orgs_payload()
        match = next((o for o in data.get("orgs") or [] if o.get("id") == org_id), None)
        if not match:
            return "Organization not found, or you are not a member."
        set_active_org_id(org_id)
        return f"Switched to {match.get('name')} as {match.get('role')}."

    if action == "members":
        target = org_id or get_active_org_id()
        if not target:
            return "Provide org_id or switch to an organization first."
        resp = await client._client.get(f"{base}/api/v1/orgs/{target}/members", headers=headers)
        resp.raise_for_status()
        body = resp.json()
        lines = ["Members:\n"]
        for m in body.get("members") or []:
            lines.append(f"  {m.get('email')} | {m.get('role')} | {m.get('user_id')}")
        pending = body.get("invites") or []
        if pending:
            lines.append("\nPending invites:")
            for inv in pending:
                lines.append(f"  {inv.get('email')} | {inv.get('role')}")
        return "\n".join(lines)

    if action == "invite":
        blocked = await refuse_if_viewer()
        if blocked:
            return blocked
        target = org_id or get_active_org_id()
        if not target or not email:
            return "Provide org_id (or switch first) and email."
        if role not in ("editor", "viewer"):
            return "role must be editor or viewer."
        resp = await client._post(
            f"{base}/api/v1/orgs/{target}/invites",
            headers=headers,
            json={"email": email, "role": role},
        )
        if resp.status_code >= 400:
            return f"Invite failed: {resp.text[:300]}"
        return f"Invited {email} as {role}."

    if action == "remove_member":
        blocked = await refuse_if_viewer()
        if blocked:
            return blocked
        target = org_id or get_active_org_id()
        if not target or not user_id:
            return "Provide org_id (or switch first) and user_id."
        resp = await client._client.delete(
            f"{base}/api/v1/orgs/{target}/members/{user_id}",
            headers=headers,
        )
        if resp.status_code >= 400:
            return f"Remove failed: {resp.text[:300]}"
        return "Member removed."

    if action == "create":
        name = email or org_id
        if not name:
            return "Provide a name via email= or org_id= for create."
        resp = await client._post(
            f"{base}/api/v1/orgs",
            headers=headers,
            json={"name": name},
        )
        if resp.status_code >= 400:
            return f"Create failed: {resp.text[:300]}"
        created = resp.json()
        created = created if isinstance(created, dict) else {}
        new_id = str(created.get("id") or "").strip()
        created_name = str(created.get("name") or "").strip() or name
        # Only write a workspace we actually have. set_active_org_id("") clears
        # the selection, so an id-less create response used to un-select the
        # user's workspace and then report a switch that never happened —
        # action="switch" above writes only after finding the org, and so do we.
        if not new_id:
            return (
                f"Created {created_name}, but HeyLead didn't get its id back, so it "
                "hasn't switched — you are still in your current workspace. Run "
                "organization(action='list'), then "
                "organization(action='switch', org_id='...')."
            )
        set_active_org_id(new_id)
        return f"Created {created_name} and switched to it."

    return "Unknown action. Use: list, switch, members, invite, remove_member, create."
