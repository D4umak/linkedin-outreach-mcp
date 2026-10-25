"""Tool: telegram — the workspace's own Telegram, for the operator (heylead-api #3213).

Reads chats and sends messages from a Telegram account the workspace
connected through Unipile. Every send is two steps: ``queue`` holds the exact
text and sends nothing; ``approve`` sends that text, verbatim, after the user
has said yes to it. There are no campaigns and no messages to strangers here.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ACTIONS = ("connect", "chats", "read", "queue", "outbox", "approve", "discard")


def _client():
    from ..linkedin import get_linkedin_client
    from ..linkedin.backend_client import BackendClient

    client = get_linkedin_client()
    if not isinstance(client, BackendClient):
        return None
    return client


async def _call(method: str, path: str, *, json: dict[str, Any] | None = None,
                params: dict[str, Any] | None = None) -> tuple[int, Any]:
    client = _client()
    if client is None:
        return 0, {"detail": "Telegram works through the HeyLead cloud. Run setup_profile first."}
    url = f"{client.base_url}/api/v1{path}"
    try:
        if method == "GET":
            resp = await client._client.get(url, params=params, headers=client._headers(), timeout=30)
        else:
            resp = await client._post(url, json=json, params=params, headers=client._headers(), timeout=60)
    except httpx.HTTPError as exc:
        return 0, {"detail": f"HeyLead did not answer ({type(exc).__name__}). Try again in a minute.", "network_error": type(exc).__name__}
    try:
        body = resp.json()
        if not isinstance(body, dict):
            body = {"detail": f"HTTP {resp.status_code} from HeyLead."}
    except ValueError:
        body = {"detail": f"HTTP {resp.status_code} from HeyLead."}
    return resp.status_code, body


def _detail(body: Any) -> str:
    return str((body or {}).get("detail") or "something went wrong") if isinstance(body, dict) else "something went wrong"


def _held_line(item: dict[str, Any]) -> str:
    where = item.get("chat_name") or item.get("chat_id")
    err = f" — {item.get('error')}" if item.get("status") == "failed" and item.get("error") else ""
    return f"[{item.get('status')}] {item.get('id')} → {where}{err}\n{item.get('text')}"


async def run_telegram(
    action: str,
    chat_id: str = "",
    text: str = "",
    item_id: str = "",
    chat_name: str = "",
    status: str = "",
    limit: int = 30,
) -> str:
    action = (action or "").strip().lower()
    if action not in ACTIONS:
        return f"Error: action is one of {', '.join(ACTIONS)}."

    if action == "connect":
        code, body = await _call("POST", "/telegram/connect")
        if code == 200 and (body or {}).get("url"):
            return (
                "Open this link and sign in to the Telegram account the workspace should use "
                f"(Unipile shows a QR code or asks for the phone number):\n{body['url']}\n\n"
                "Then run telegram(action='chats')."
            )
        return f"Could not create the Telegram link: {_detail(body)}"

    if action == "chats":
        code, body = await _call("GET", "/telegram/chats", params={"limit": limit})
        if code != 200:
            return f"Could not read Telegram chats: {_detail(body)}"
        chats = (body or {}).get("chats") or []
        if not chats:
            return "No Telegram chats yet."
        lines = [f"- {c.get('name') or '(no name)'} [{c.get('kind')}] chat_id={c.get('chat_id')}"
                 + (f", {c.get('unread')} unread" if c.get("unread") else "") for c in chats]
        return "Telegram chats:\n" + "\n".join(lines)

    if action == "read":
        if not chat_id:
            return "Error: chat_id is required (from telegram(action='chats'))."
        code, body = await _call("GET", f"/telegram/chats/{chat_id}/messages", params={"limit": limit})
        if code != 200:
            return f"Could not read that chat: {_detail(body)}"
        messages = (body or {}).get("messages") or []
        if not messages:
            return "No messages in that chat."
        lines = [f"[{m.get('at')}] {'Me' if m.get('from_me') else (m.get('sender') or 'Them')}: {m.get('text')}"
                 for m in reversed(messages)]
        return "\n".join(lines)

    if action == "queue":
        if not chat_id or not (text or "").strip():
            return "Error: chat_id and text are required."
        code, body = await _call("POST", "/telegram/outbox",
                                 json={"chat_id": chat_id, "text": text, "chat_name": chat_name})
        if code != 200:
            return f"Not queued: {_detail(body)}"
        item = (body or {}).get("held") or {}
        return (
            "Held, NOT sent:\n" + _held_line(item) + "\n\n"
            "Show the user this exact text. Only after they say yes to it, call "
            f"telegram(action='approve', item_id='{item.get('id')}'). Otherwise "
            f"telegram(action='discard', item_id='{item.get('id')}')."
        )

    if action == "outbox":
        code, body = await _call("GET", "/telegram/outbox", params={"status": status} if status else None)
        if code != 200:
            return f"Could not read the outbox: {_detail(body)}"
        items = (body or {}).get("items") or []
        return "\n\n".join(_held_line(i) for i in items) if items else "The Telegram outbox is empty."

    if not item_id:
        return "Error: item_id is required (from telegram(action='queue') or 'outbox')."
    code, body = await _call("POST", f"/telegram/outbox/{item_id}/{action}")
    if code != 200:
        if action == "approve" and code == 0 and isinstance(body, dict) and body.get("network_error"):
            return f"Not sent: network error talking to HeyLead ({body['network_error']}). Run telegram(action='outbox') to check message {item_id}: if it is still [held], it was not sent and you can run telegram(action='approve', item_id='{item_id}') again."
        return f"{action.capitalize()} refused: {_detail(body)}"
    result = (body or {}).get("result") or {}
    if action == "discard":
        return f"Discarded {item_id}; nothing was sent."
    if result.get("status") == "sent":
        return f"Sent to {result.get('chat_name') or result.get('chat_id')}."
    return f"Not sent: {result.get('error') or result.get('status')}"
