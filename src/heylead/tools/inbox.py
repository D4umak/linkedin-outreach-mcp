"""Inbox tool — browse and read LinkedIn messages directly."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..db.async_bridge import run_db
from ..ai.inbound_qualifier import _classify_fast
from ..db import aio as db
from ..linkedin import get_account_id, get_linkedin_client

logger = logging.getLogger(__name__)


def _contact_provider_id(contact: dict) -> str:
    """Extract LinkedIn provider_id (ACoAAA...) from a global contact row.

    Delegates, because a row stores that id in ``profile_json`` or in the
    ``linkedin_id`` column and this reader used to know only the first. The
    fallback below skips a contact with no id, so the miss surfaced as "No
    conversation found" -- the same sentence as a chat that does not exist.
    """
    from ..author_identity import contact_provider_id

    return contact_provider_id(contact)


def _relative_time(ts: int) -> str:
    """Convert a unix timestamp to a human-readable relative time string."""
    if not ts:
        return ""
    now = int(time.time())
    diff = now - ts
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    days = diff // 86400
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days}d ago"
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d")
    except (ValueError, OSError):
        return ""


def _format_timestamp(ts: int) -> str:
    """Format a unix timestamp as a readable date/time string."""
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d, %H:%M")
    except (ValueError, OSError):
        return ""


def _parse_ts(ts_raw: Any) -> int:
    """Parse a timestamp from various formats to unix epoch seconds."""
    if isinstance(ts_raw, (int, float)):
        return int(ts_raw)
    if isinstance(ts_raw, str) and ts_raw:
        try:
            return int(datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError):
            pass
    return 0


class InboxError(Exception):
    """Raised when inbox fetch fails with a clear reason."""


async def _fetch_raw_chats(
    client: Any, account_id: str, limit: int,
) -> list[dict[str, Any]]:
    """Fetch raw chat objects from Unipile/backend API with pagination."""
    all_items: list[dict[str, Any]] = []
    cursor: str | None = None
    PAGE_SIZE = 30

    while len(all_items) < limit:
        page_limit = min(limit - len(all_items), PAGE_SIZE)
        if hasattr(client, "_retry_request"):
            # BackendClient
            url = f"{client.base_url}/api/v1/chats"
            params: dict[str, str] = {"limit": str(page_limit)}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = await client._retry_request("GET", url, params=params)
            except Exception as e:
                raise InboxError(f"Connection error fetching inbox: {e}") from e
        elif hasattr(client, "base_url") and hasattr(client, "_headers"):
            # UnipileClient
            qs = f"account_id={account_id}&limit={page_limit}"
            if cursor:
                qs += f"&cursor={cursor}"
            url = f"{client.base_url}/api/v1/chats?{qs}"
            try:
                resp = await client._client.get(url, headers=client._headers())
            except Exception as e:
                raise InboxError(f"Connection error fetching inbox: {e}") from e
        else:
            raise InboxError("No LinkedIn client available.")

        if resp.status_code in (401, 403):
            raise InboxError(
                "LinkedIn session expired or unauthorized (HTTP "
                f"{resp.status_code}). Please reconnect your LinkedIn account "
                "at https://heylead.dev/auth/login-url"
            )
        if resp.status_code == 400:
            body = ""
            try:
                body = resp.text[:200]
            except Exception:
                pass
            raise InboxError(
                f"Bad request fetching inbox (HTTP 400). "
                f"This usually means no LinkedIn account is connected. "
                f"Details: {body}"
            )
        if resp.status_code != 200:
            body = ""
            try:
                body = resp.text[:200]
            except Exception:
                pass
            logger.warning("Inbox fetch failed: HTTP %s — %s", resp.status_code, body)
            raise InboxError(
                f"Failed to fetch inbox (HTTP {resp.status_code}). Details: {body}"
            )

        data = resp.json()
        items = data.get("items") or data.get("chats") or data.get("data") or []
        if isinstance(data, list):
            items = data
        page = [c for c in items if isinstance(c, dict)]
        if not page:
            break
        all_items.extend(page)
        # Check for pagination cursor
        cursor = (
            data.get("cursor")
            or data.get("next_cursor")
            or (data.get("paging") or {}).get("cursor")
        ) if isinstance(data, dict) else None
        if not cursor:
            break
    return all_items[:limit]


async def _resolve_profile(client: Any, account_id: str, provider_id: str) -> dict[str, str]:
    """Resolve a LinkedIn provider_id to name + headline via profile lookup."""
    try:
        profile = await client.get_profile(account_id, provider_id)
        if not profile:
            return {"name": "", "headline": ""}
        name = (
            profile.get("name")
            or profile.get("display_name")
            or f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
        )
        headline = profile.get("headline") or profile.get("occupation") or ""
        return {"name": name, "headline": headline}
    except Exception as e:
        logger.debug("Profile resolve failed for %s: %s", provider_id, e)
        return {"name": "", "headline": ""}


def _extract_chat_info(chat: dict) -> dict[str, Any]:
    """Extract basic chat metadata from a raw chat object."""
    chat_id = chat.get("id") or chat.get("chat_id") or ""
    ts = _parse_ts(chat.get("timestamp") or "")
    unread = chat.get("unread_count", 0) or 0

    # Try to get contact info from attendees (Unipile direct mode)
    attendees = chat.get("attendees") or []
    contact_name = ""
    contact_headline = ""
    contact_provider_id = (
        chat.get("attendee_provider_id")
        or chat.get("attendees_provider_id")
        or ""
    )
    # Also try attendees_ids list
    if not contact_provider_id:
        att_ids = chat.get("attendees_ids") or chat.get("attendees_provider_ids") or []
        if isinstance(att_ids, list) and att_ids:
            contact_provider_id = str(att_ids[0])

    for att in attendees:
        if not isinstance(att, dict):
            continue
        name = att.get("display_name") or att.get("name") or ""
        if name:
            contact_name = name
            contact_headline = att.get("headline") or ""
            contact_provider_id = att.get("provider_id") or att.get("id") or contact_provider_id
            break

    # Try embedded last message
    chat_messages = chat.get("messages") or chat.get("last_messages") or []
    last_text = ""
    if isinstance(chat_messages, list) and chat_messages:
        last_msg = chat_messages[-1] if isinstance(chat_messages[-1], dict) else {}
        last_text = last_msg.get("text") or last_msg.get("body") or ""
        if not contact_name:
            contact_name = last_msg.get("sender_name") or last_msg.get("sender", {}).get("display_name", "")

    # Fallback: chat name field
    if not contact_name:
        contact_name = chat.get("name") or ""

    return {
        "chat_id": chat_id,
        "contact_name": contact_name,
        "contact_headline": contact_headline,
        "contact_provider_id": contact_provider_id,
        "timestamp": ts,
        "unread": unread,
        "last_text": last_text,
    }


async def _resolve_chat(
    client: Any,
    account_id: str,
    chat_id: str,
    name: str,
) -> dict[str, str] | str:
    """Resolve a chat by chat_id or name search.

    Returns a dict with keys chat_id, contact_name, contact_headline,
    contact_provider_id on success, or an error string on failure.
    """
    contact_headline = ""
    contact_name_resolved = ""
    contact_provider_id = ""

    if not chat_id and name:
        raw_chats = await _fetch_raw_chats(client, account_id, 50)
        chat_infos = [_extract_chat_info(c) for c in raw_chats]

        name_lower = name.lower()
        for ci in chat_infos:
            if ci["contact_name"] and name_lower in ci["contact_name"].lower():
                chat_id = ci["chat_id"]
                contact_headline = ci["contact_headline"]
                contact_name_resolved = ci["contact_name"]
                contact_provider_id = ci["contact_provider_id"]
                break

        if not chat_id:
            unresolved = [ci for ci in chat_infos if not ci["contact_name"] and ci["contact_provider_id"]]
            if unresolved:
                tasks = [
                    _resolve_profile(client, account_id, ci["contact_provider_id"])
                    for ci in unresolved[:30]
                ]
                profiles = await asyncio.gather(*tasks, return_exceptions=True)
                for ci, profile in zip(unresolved[:30], profiles):
                    if isinstance(profile, dict) and profile.get("name"):
                        ci["contact_name"] = profile["name"]
                        ci["contact_headline"] = profile.get("headline", "")
                        if name_lower in profile["name"].lower():
                            chat_id = ci["chat_id"]
                            contact_headline = ci["contact_headline"]
                            contact_name_resolved = ci["contact_name"]
                            contact_provider_id = ci["contact_provider_id"]
                            break

        # Fallback: search the global contact base, then ask Unipile to
        # resolve (or create) a chat for that contact's provider_id.
        # Handles the case where the target chat is not in the recent
        # inbox page returned by Unipile (e.g. stale sort, inbound-only DM).
        if not chat_id:
            try:
                candidates = await db.search_global_contacts(query=name, limit=5)
            except Exception as e:
                logger.debug("global contact search failed for '%s': %s", name, e)
                candidates = []
            for contact in candidates:
                pid = _contact_provider_id(contact)
                if not pid:
                    continue
                try:
                    found = await client.find_chat_for_user(account_id, pid)
                except Exception as e:
                    logger.debug("find_chat_for_user failed for %s: %s", pid[:20], e)
                    continue
                if found:
                    chat_id = found
                    contact_name_resolved = contact.get("name") or ""
                    contact_headline = contact.get("title") or ""
                    contact_provider_id = pid
                    break

        if not chat_id:
            return (
                f"No conversation found matching '{name}'.\n"
                "Use `inbox(action='list')` to see all conversations."
            )

    elif not chat_id:
        return "Please provide a `chat_id` or `name` to read a conversation."

    # Resolve contact name/headline if we only have chat_id
    if not contact_name_resolved and chat_id:
        raw_chats = await _fetch_raw_chats(client, account_id, 50)
        for c in raw_chats:
            cid = c.get("id") or c.get("chat_id") or ""
            if cid == chat_id:
                ci = _extract_chat_info(c)
                contact_provider_id = ci["contact_provider_id"]
                contact_name_resolved = ci["contact_name"]
                contact_headline = ci["contact_headline"]
                break
        if not contact_name_resolved and contact_provider_id:
            profile = await _resolve_profile(client, account_id, contact_provider_id)
            contact_name_resolved = profile.get("name", "")
            contact_headline = profile.get("headline", "")

    return {
        "chat_id": chat_id,
        "contact_name": contact_name_resolved,
        "contact_headline": contact_headline,
        "contact_provider_id": contact_provider_id,
    }


async def run_inbox(
    action: str = "list",
    chat_id: str = "",
    name: str = "",
    limit: int = 30,
    text: str = "",
) -> str:
    """Browse and read LinkedIn inbox messages."""
    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected. Run setup_profile first."

    client = get_linkedin_client()
    try:
        if action == "list":
            return await _list_inbox(client, account_id, limit)
        elif action == "read":
            return await _read_conversation(client, account_id, chat_id, name, limit)
        elif action == "reply":
            return await _reply_conversation(client, account_id, chat_id, name, text)
        elif action == "comment_drafts":
            return await _list_comment_drafts(client)
        elif action == "approve_draft":
            return await _approve_comment_draft(client, chat_id, text)
        elif action == "discard_draft":
            return await _discard_comment_draft(client, chat_id)
        else:
            return (
                f"Unknown action: '{action}'. Use 'list', 'read', 'reply', "
                "'comment_drafts', 'approve_draft', or 'discard_draft'."
            )
    except InboxError as e:
        return f"⚠️ **Inbox error**: {e}"
    finally:
        await client.close()


# ── Comment reply drafts ──
#
# Replies drafted for comments on the user's own posts. They live here because
# the inbox is already where things waiting on the user are, and a 34th tool
# for four rows would be worse. Nothing is sent until approve_draft.


def _render(text: str, author_name: str) -> str:
    """Show a draft the way LinkedIn will render it.

    {{0}} is a mention token: on LinkedIn it becomes a link to the person, and
    it carries their first name because "Thanks, Yana Pampukha!" reads like a
    form letter.
    """
    first = (author_name or "").strip().split(" ")[0]
    return (text or "").replace("{{0}}", first or "there")


async def _list_comment_drafts(client: Any) -> str:
    drafts = await client.list_comment_drafts()
    if not drafts:
        return "No comment replies waiting. Nothing has been drafted since the last check."

    lines = [f"**{len(drafts)} reply draft(s) waiting**", ""]
    for d in drafts:
        who = d.get("author_name") or "someone"
        lines.append(f"— {who} commented:")
        lines.append(f'    "{(d.get("comment_text") or "").strip()}"')
        lines.append(f'  your reply: "{_render(d.get("draft_text", ""), who)}"')
        lines.append(f"  approve: inbox(action='approve_draft', chat_id='{d.get('id')}')")
        lines.append("")
    lines.append("Edit before sending by passing text= to approve_draft.")
    return "\n".join(lines)


async def _approve_comment_draft(client: Any, draft_id: str, text: str) -> str:
    if not draft_id:
        return "Which draft? Pass chat_id with the draft id from comment_drafts."
    result = await client.approve_comment_draft(draft_id, text=text)
    if result.get("success"):
        return "Reply sent."
    return f"Could not send: {result.get('error') or 'unknown error'}"


async def _discard_comment_draft(client: Any, draft_id: str) -> str:
    if not draft_id:
        return "Which draft? Pass chat_id with the draft id from comment_drafts."
    await client.discard_comment_draft(draft_id)
    return "Draft discarded. Nothing was sent."


async def _list_inbox(client: Any, account_id: str, limit: int) -> str:
    """List recent LinkedIn conversations with enriched contact info."""
    raw_chats = await _fetch_raw_chats(client, account_id, limit)
    if not raw_chats:
        return "No conversations found in your inbox."

    # Extract basic info from each chat
    chat_infos = [_extract_chat_info(c) for c in raw_chats]

    # Resolve names and fetch last messages for chats missing this data
    BATCH = 10

    needs_profile = [
        ci for ci in chat_infos
        if not ci["contact_name"] and ci["contact_provider_id"]
    ]
    needs_message = [ci for ci in chat_infos if not ci["last_text"] and ci["chat_id"]]

    # Batch resolve profiles (all, in groups of BATCH)
    for start in range(0, len(needs_profile), BATCH):
        batch = needs_profile[start : start + BATCH]
        tasks = [
            _resolve_profile(client, account_id, ci["contact_provider_id"])
            for ci in batch
        ]
        profiles = await asyncio.gather(*tasks, return_exceptions=True)
        for ci, profile in zip(batch, profiles):
            if isinstance(profile, dict):
                ci["contact_name"] = profile.get("name", "")
                ci["contact_headline"] = profile.get("headline", "")

    # Batch fetch last messages (all, in groups of BATCH)
    for start in range(0, len(needs_message), BATCH):
        batch = needs_message[start : start + BATCH]
        tasks = [
            client.get_chat_messages(account_id, ci["chat_id"], limit=1)
            for ci in batch
        ]
        msg_results = await asyncio.gather(*tasks, return_exceptions=True)
        for ci, msgs in zip(batch, msg_results):
            if isinstance(msgs, list) and msgs:
                ci["last_text"] = msgs[0].get("text", "")
                # Also extract sender_id for provider_id fallback
                if not ci["contact_provider_id"]:
                    sid = msgs[0].get("sender_id", "")
                    if sid and sid != account_id:
                        ci["contact_provider_id"] = sid

    # Fallback: resolve chats that got a provider_id from message fetching
    needs_profile_2 = [
        ci for ci in chat_infos
        if not ci["contact_name"] and ci["contact_provider_id"]
    ]
    for start in range(0, len(needs_profile_2), BATCH):
        batch = needs_profile_2[start : start + BATCH]
        tasks = [
            _resolve_profile(client, account_id, ci["contact_provider_id"])
            for ci in batch
        ]
        profiles = await asyncio.gather(*tasks, return_exceptions=True)
        for ci, profile in zip(batch, profiles):
            if isinstance(profile, dict):
                ci["contact_name"] = profile.get("name", "")
                ci["contact_headline"] = profile.get("headline", "")

    # Format output
    lines = [f"**LinkedIn Inbox** ({len(chat_infos)} conversations)\n"]

    for i, ci in enumerate(chat_infos, 1):
        contact_name = ci["contact_name"] or "Unknown"
        headline = ci["contact_headline"]
        text = ci["last_text"]
        ts = ci["timestamp"]
        unread = ci["unread"]
        cid = ci["chat_id"]

        preview = text[:80].replace("\n", " ") if text else "(no preview)"
        if text and len(text) > 80:
            preview += "..."

        time_str = _relative_time(ts)
        unread_badge = " [UNREAD]" if unread else ""

        line = f"{i}. **{contact_name}**{unread_badge}"
        if headline:
            line += f" — {headline[:60]}"
        if time_str:
            line += f" ({time_str})"
        line += f"\n   {preview}"
        line += f"\n   `chat_id: {cid}`"
        lines.append(line)

    lines.append(
        "\nTo read a conversation: `inbox(action='read', name='...')` "
        "or `inbox(action='read', chat_id='...')`"
    )
    return "\n".join(lines)


async def _read_conversation(
    client: Any,
    account_id: str,
    chat_id: str,
    name: str,
    limit: int,
) -> str:
    """Read full conversation thread."""
    resolved = await _resolve_chat(client, account_id, chat_id, name)
    if isinstance(resolved, str):
        return resolved

    chat_id = resolved["chat_id"]
    contact_name_resolved = resolved["contact_name"]
    contact_headline = resolved["contact_headline"]
    contact_provider_id = resolved["contact_provider_id"]

    # Fetch full message thread
    try:
        messages = await client.get_chat_messages(account_id, chat_id, limit=limit)
    except Exception as e:
        if getattr(getattr(e, "response", None), "status_code", None) == 404:
            return (
                f"Chat `{chat_id}` no longer exists on LinkedIn: it was deleted, "
                "or the person disconnected."
            )
        raise
    if not messages:
        return f"No messages found in chat `{chat_id}`."

    # Resolve sender names if empty (backend mode returns empty sender_name)
    sender_ids = {m.get("sender_id", "") for m in messages if not m.get("sender_name")}
    sender_ids.discard("")
    sender_names: dict[str, str] = {}

    if sender_ids:
        # We know the contact's provider_id — use it to label messages
        if contact_provider_id:
            for sid in sender_ids:
                if sid == contact_provider_id:
                    sender_names[sid] = contact_name_resolved or "Contact"
                else:
                    sender_names[sid] = "You"
        else:
            # Resolve the first unknown sender as the contact
            tasks = [_resolve_profile(client, account_id, sid) for sid in list(sender_ids)[:3]]
            profiles = await asyncio.gather(*tasks, return_exceptions=True)
            for sid, profile in zip(sender_ids, profiles):
                if isinstance(profile, dict) and profile.get("name"):
                    sender_names[sid] = profile["name"]
                    if not contact_name_resolved:
                        contact_name_resolved = profile["name"]
                        contact_headline = profile.get("headline", "")

    # Build conversation display
    display_name = contact_name_resolved or "contact"
    header = f"**Conversation with {display_name}**"
    if contact_headline:
        header += f"\n_{contact_headline}_"
    header += f"\n({len(messages)} messages)\n"

    lines = [header, "---"]
    prospect_texts: list[str] = []

    for msg in messages:
        sender_id = msg.get("sender_id", "")
        sender = msg.get("sender_name") or sender_names.get(sender_id, "Unknown")
        text = msg.get("text") or ""
        ts = msg.get("timestamp", 0)

        time_str = _format_timestamp(ts)
        time_part = f" ({time_str})" if time_str else ""

        lines.append(f"**{sender}**{time_part}")
        lines.append(text)
        lines.append("")

        # Collect contact's messages for qualification
        is_contact_msg = (
            (contact_provider_id and sender_id == contact_provider_id)
            or (contact_name_resolved and sender and contact_name_resolved.lower() in sender.lower())
            or sender_names.get(sender_id) not in ("You", None)
        )
        if is_contact_msg:
            prospect_texts.append(text)

    # Run fast-path qualification
    lines.append("---")
    combined_text = " ".join(prospect_texts)
    qualification = _classify_fast(contact_headline or "", combined_text)

    if qualification:
        emoji = {"vendor_pitch": "🛑", "spam": "🚫", "job_seeking": "🔍"}.get(
            qualification.intent, "ℹ️"
        )
        lines.append(
            f"{emoji} **Qualification**: {qualification.intent} "
            f"(confidence: {qualification.confidence:.0%}, action: {qualification.recommended_action})"
        )
        lines.append(f"   Reason: {qualification.reasoning}")
    else:
        lines.append(
            "ℹ️ **Qualification**: No fast-path match — "
            "would need LLM classification (could be a lead, networking, or unknown)"
        )

    return "\n".join(lines)


async def _reply_conversation(
    client: Any,
    account_id: str,
    chat_id: str,
    name: str,
    text: str,
) -> str:
    """Send a direct reply to any inbox conversation."""
    if not text:
        return "Please provide `text` — the message to send."

    resolved = await _resolve_chat(client, account_id, chat_id, name)
    if isinstance(resolved, str):
        return resolved

    chat_id = resolved["chat_id"]
    contact_name = resolved["contact_name"] or "contact"

    from ..ops_log import log_event, message_hash
    log_event(
        "inbox_reply_sent",
        channel="dm",
        text_hash=message_hash(text),
        message_len=len(text),
        outcome="attempt",
        **({"chat_id": chat_id} if chat_id else {}),
    )
    result = await client.send_message(account_id, chat_id, text)
    log_event(
        "inbox_reply_sent",
        channel="dm",
        text_hash=message_hash(text),
        message_len=len(text),
        outcome="success" if result.get("success") else "error",
        error_type=(result.get("error") or "")[:80] or None,
        **({"chat_id": chat_id} if chat_id else {}),
    )
    if result.get("success"):
        return (
            f"✅ **Message sent to {contact_name}**\n\n"
            f"> {text}"
        )
    error = result.get("error", "Unknown error")
    return f"❌ **Failed to send message to {contact_name}**: {error}"
