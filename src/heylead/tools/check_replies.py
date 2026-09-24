"""Tool 4: check_replies — Check for new replies across all campaigns.

Fetches new LinkedIn messages, classifies sentiment
(positive/negative/question/neutral), surfaces hot leads,
and auto-handles opt-outs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ..ai.sentiment import (
    classify_fast,
    classify_sentiment,
    detect_calendar_url,
    detect_meeting_agreement,
)
from ..db.async_bridge import run_db
from ..formatter import person_line, prospect_link
from ..author_identity import contact_provider_id
from ..db.queries import (
    get_inbound_signal_by_sender,
    get_messages_for_outreach,
    get_outreach,
    get_setting,
    save_setting,
    increment_accepted,
    list_campaigns,
    list_inbound_signals,
    log_action,
    mark_message_read,
    save_message,
    update_contact,
    update_outreach,
)
from ..db.schema import get_db
from ..linkedin.message_sender import message_is_ours
from ..services.connection_sync import mark_connected
from ..linkedin import (
    UnipileAuthError,
    UnipileError,
    get_account_id,
    get_linkedin_client,
)
from ..constants import AUTO_REPLY_MAX_AGE_DAYS
from ..linkedin.circuit_breaker import CircuitBreakerOpen, CollectorCircuitBreaker
from ..services.inbox_match import index_contacts_for_inbox
from ..timeutil import to_epoch

logger = logging.getLogger(__name__)

_cb = CollectorCircuitBreaker("check_replies")

# One page of get_chats (50) plus the server batch (100) is a recency window.
# A thread that scrolled off it — a reply that came a week after the opener,
# or during a busy inbox — was never looked at again on ANY sweep. Each sweep
# therefore also asks Unipile for a rotating slice of open rows by attendee
# (chat_attendees/{id}/messages has no recency horizon). Bounded per sweep so
# a large campaign spreads its revisits over the day rather than stalling one
# sweep; client twin of heylead-api #248.
REVISIT_THREADS_PER_SWEEP = 15
_REVISIT_STATUSES = ("invited", "connected", "messaged", "replied")
_REVISIT_CURSOR_KEY = "check_replies_revisit_cursor"


def _revisit_candidates(
    outreach_contacts: list, seen_provider_ids: set[str],
) -> list[tuple[str, str]]:
    """(outreach_id, provider_id) for open rows whose person was not on the page."""
    out: list[tuple[str, str]] = []
    for row in outreach_contacts:
        r = dict(row)
        if r.get("status") not in _REVISIT_STATUSES:
            continue
        if r.get("campaign_status") not in (None, "active", "paused"):
            continue
        # Both places the id is stored: a row whose id sits in the
        # linkedin_id column used to be dropped here, and this is the only
        # path that notices an accepted invitation.
        pid = contact_provider_id(r)
        if not pid or pid in seen_provider_ids:
            continue
        out.append((str(r.get("outreach_id") or ""), pid))
    out.sort()
    return out


async def _revisit_off_page_threads(
    client: Any,
    account_id: str,
    outreach_contacts: list,
    seen_provider_ids: set[str],
    our_provider_id: str,
) -> list[dict[str, Any]]:
    """Newest prospect message per revisited thread, in server_replies shape."""
    candidates = _revisit_candidates(outreach_contacts, seen_provider_ids)
    if not candidates or not hasattr(client, "get_messages_by_sender"):
        return []
    # Rotate: pick up where the last sweep stopped, wrap at the end.
    try:
        start = int(await run_db(get_setting, _REVISIT_CURSOR_KEY, 0) or 0)
    except (TypeError, ValueError):
        start = 0
    start %= len(candidates)
    ordered = candidates[start:] + candidates[:start]
    batch = ordered[:REVISIT_THREADS_PER_SWEEP]

    found: list[dict[str, Any]] = []
    asked = 0
    for _oid, pid in batch:
        try:
            msgs = await _cb.call(
                client.get_messages_by_sender(account_id, pid, limit=5),
                label="get_messages_by_sender",
            )
        except (CircuitBreakerOpen, asyncio.TimeoutError) as e:
            logger.info("Reply revisit stopped early (%s) after %d thread(s)", e, asked)
            break
        except Exception as e:
            logger.debug("Reply revisit: attendee lookup failed for %s: %s", pid[:12], e)
            continue
        asked += 1
        newest = None
        for m in msgs or []:
            sender = str(m.get("sender_id") or "")
            if not sender or message_is_ours(m, our_provider_id) or sender != pid:
                continue
            if not (m.get("text") or "").strip():
                continue
            ts = to_epoch(m.get("timestamp")) or 0
            if newest is None or ts >= (to_epoch(newest.get("timestamp")) or 0):
                newest = m
        if newest is not None:
            found.append({
                "sender_id": pid,
                "text": newest.get("text", ""),
                "chat_id": newest.get("chat_id") or "",
                "timestamp": newest.get("timestamp"),
                "_revisit": True,
            })
    if asked:
        await run_db(
            save_setting, _REVISIT_CURSOR_KEY, (start + asked) % len(candidates),
        )
    logger.info(
        "Reply revisit: %d of %d off-page thread(s) checked, %d with a prospect message",
        asked, len(candidates), len(found),
    )
    return found


def _within_auto_reply_window(msg: dict) -> bool:
    """Same 14-day bound as queued auto-reply — do not auto-book a stale calendar link."""
    ts = to_epoch(msg.get("timestamp")) or 0
    if ts <= 0:
        # Live inbox can omit a provider ts; a revisit already rejected timestamp=0.
        return not msg.get("_revisit")
    return ts >= int(time.time()) - AUTO_REPLY_MAX_AGE_DAYS * 86400

# Sentiment display config
SENTIMENT_ICONS = {
    "positive": "🔥",
    "negative": "👎",
    "question": "❓",
    "neutral": "💬",
    "engaged": "💬",
    "out_of_office": "✈️",
    "opt_out": "🚫",
}

# The status this sweep writes for a label; every other label stays "replied".
# SENTIMENT_ACTIONS below must not say an outreach IS closed for a label this
# table does not close (tests/test_client_mirrors_the_reply_policy.py).
STATUS_ON_DETECTION = {"opt_out": "opted_out", "positive": "hot_lead"}

SENTIMENT_ACTIONS = {
    "positive": "Auto-replying with booking link (if configured) or suggesting a time.",
    # Until 19 Sep 2026: "No action needed. Outreach closed." — while this
    # sweep wrote "replied". The cloud answers a no first (api #749).
    "negative": (
        "No action needed. HeyLead sends a short, polite close, then closes the "
        "outreach. If it is held for you, it shows under Needs attention."
    ),
    "question": "ACTION NEEDED: Answer their question.",
    "neutral": "No action needed. Monitor.",
    "engaged": "ACTION NEEDED: Continue the conversation — deepen rapport.",
    "out_of_office": "Will follow up when they're back.",
    "opt_out": "Auto-closed. They won't be contacted again.",
}


def _meeting_window(
    m_date: str,
    m_time: str,
    m_tz: str,
    duration_minutes: int,
) -> tuple[str, str]:
    """Build the start/end ISO stamps for an extracted meeting.

    The zone comes from the prospect's own message ("3pm my time"), not from
    the calendar the event lands in, so it has to be carried on the timestamps
    themselves — a naive stamp is read as the calendar's default zone and books
    the meeting hours away. An unrecognised zone falls back to naive stamps,
    which is what the calendar assumed anyway.
    """
    from datetime import datetime, timedelta

    try:
        start = datetime.fromisoformat(f"{m_date}T{m_time}:00")
    except (ValueError, TypeError):
        return f"{m_date}T{m_time}:00", f"{m_date}T{m_time}:00"

    if m_tz:
        try:
            from zoneinfo import ZoneInfo

            start = start.replace(tzinfo=ZoneInfo(m_tz))
        except Exception:
            logger.warning("Unknown meeting timezone %r — leaving the time naive", m_tz)

    end = start + timedelta(minutes=duration_minutes)
    return start.isoformat(), end.isoformat()


async def _append_inbound_invitations(
    output: list[str],
    invitations: list[dict[str, Any]],
    limit: int = 10,
) -> None:
    """Append inbound invitation lines with qualification badges if available."""
    output.append(f"📨 Inbound Invitations ({len(invitations)}):")
    for inv in invitations[:limit]:
        inv_name = inv.get("sender_name", "Someone")
        inv_headline = inv.get("headline", "")
        inv_msg = inv.get("message", "")

        # Check if we have qualification data for this sender
        sender_id = inv.get("sender_id", "")
        badge = ""
        if sender_id:
            from ..db.async_bridge import run_db as _run_db
            from ..db.queries import get_inbound_signal_by_sender as _get_sig
            sig = await _run_db(_get_sig, sender_id, "invitation")
            if sig and sig.get("intent"):
                conf = sig.get("confidence", 0) or 0
                intent = sig.get("intent", "unknown")
                if conf >= 0.7:
                    badge = f"  🟢 {conf:.0%} Lead ({intent})"
                elif conf >= 0.4:
                    badge = f"  🟡 {conf:.0%} ({intent})"
                else:
                    badge = f"  🔴 {conf:.0%} ({intent})"

        inv_line = f"   • {inv_name}"
        if inv_headline:
            inv_line += f" — {inv_headline}"
        inv_line += badge
        output.append(inv_line)
        if inv_msg:
            output.append(f"     \"{inv_msg[:100]}\"")
    if len(invitations) > limit:
        output.append(f"   ... and {len(invitations) - limit} more")


def _profile_viewer_lines(viewers: list[dict[str, Any]], limit: int) -> list[str]:
    """One `• [Name](url) — title at company` line per profile viewer.

    A viewer row carries the profile as `url` (LinkedIn's navigationUrl); an
    anonymous viewer ("Someone at ...") carries none and stays plain text.
    """
    lines: list[str] = []
    for v in viewers[:limit]:
        url = str(v.get("url") or "").strip()
        lines.append("   • " + person_line(
            v.get("name") or "Anonymous",
            url if url.startswith("http") else "",
            title=v.get("title") or "",
            company=v.get("company") or "",
        ))
    return lines


async def _sync_silent_connections(client: Any, account_id: str) -> list[dict]:
    """Detect connections that accepted without messaging.

    Calls get_relations() API and cross-references with outreaches
    that are still in 'invited' status. Updates them to 'connected'.

    Returns list of newly detected connections.
    """
    try:
        relations = await _cb.call(
            client.get_relations(account_id, limit=500),
            label="get_relations",
        )
    except (CircuitBreakerOpen, asyncio.TimeoutError) as e:
        logger.warning(f"get_relations timed out or circuit breaker open: {e}")
        return []
    except Exception as e:
        logger.warning(f"Failed to fetch relations for sync: {e}")
        return []

    if not relations:
        return []

    # Build multi-index of connected relations
    connected_provider_ids: set[str] = set()
    connected_public_ids: set[str] = set()
    connected_by_name: dict[str, dict] = {}
    relation_by_id: dict[str, dict] = {}

    import re as _re

    # Names are not identifiers: a name shared by two relations could belong
    # to either person, so it must never match. Ids collide only on data bugs.
    ambiguous_relation_names: set[str] = set()

    for rel in relations:
        pid = (rel.get("provider_id") or rel.get("member_id") or "").strip()
        pub_id = (rel.get("public_id") or rel.get("public_identifier") or "").strip()
        name = (rel.get("name") or "").strip()
        if not name and (rel.get("first_name") or rel.get("last_name")):
            name = f"{rel.get('first_name', '')} {rel.get('last_name', '')}".strip()

        if pid:
            connected_provider_ids.add(pid)
            relation_by_id[pid] = rel
        if pub_id:
            pub_norm = pub_id.lower()
            connected_public_ids.add(pub_norm)
            relation_by_id[pub_norm] = rel
        if name:
            norm_name = _re.sub(r"\s+", " ", name.lower()).strip()
            if norm_name in connected_by_name:
                ambiguous_relation_names.add(norm_name)
            connected_by_name[norm_name] = rel

    # Find outreaches stuck in 'invited' whose contact is now connected
    # Limit to 200 most recent to prevent unbounded memory usage
    def _fetch_invited() -> list:
        db = get_db()
        rows = db.execute(
            """SELECT o.id as outreach_id, o.contact_id, o.campaign_id,
                      c.name, c.title, c.company, c.linkedin_id, c.linkedin_url,
                      c.profile_json
               FROM outreaches o
               JOIN contacts c ON o.contact_id = c.id
               WHERE o.status = 'invited'
               ORDER BY o.created_at DESC
               LIMIT 200"""
        ).fetchall()
        db.close()
        return rows

    invited_outreaches = await run_db(_fetch_invited)

    # A name shared by two invited prospects is ambiguous from this side too:
    # the one relation row could be either of them.
    from collections import Counter
    invited_name_counts: Counter = Counter()
    for row in invited_outreaches:
        n = (dict(row).get("name") or "").strip()
        if n:
            invited_name_counts[_re.sub(r"\s+", " ", n.lower()).strip()] += 1

    newly_connected: list[dict] = []
    matched_ids: set[str] = set()
    for row in invited_outreaches:
        r = dict(row)
        linkedin_id = (r.get("linkedin_id") or "").strip()
        linkedin_id_norm = linkedin_id.lower()

        # Extract public_id from linkedin_url
        public_id = ""
        url = (r.get("linkedin_url") or "").strip()
        if "/in/" in url:
            public_id = url.split("/in/")[-1].split("?")[0].strip("/").lower()

        # Extract provider_id from profile_json
        contact_provider_id = ""
        try:
            profile_data = json.loads(r.get("profile_json") or "{}")
            contact_provider_id = str(profile_data.get("provider_id") or profile_data.get("id") or "").strip()
        except (json.JSONDecodeError, TypeError):
            profile_data = {}

        contact_name = (r.get("name") or "").strip()
        contact_name_norm = _re.sub(r"\s+", " ", contact_name.lower()).strip() if contact_name else ""

        # Check multi-index matches. The name tier only fires when the name is
        # unique among relations AND among invited prospects — a namesake on
        # either side means the match could be the wrong person.
        matched_rel = None
        matched_by = ""
        if contact_provider_id and contact_provider_id in connected_provider_ids:
            matched_rel = relation_by_id.get(contact_provider_id)
            matched_by = "provider_id"
        elif linkedin_id and linkedin_id in connected_provider_ids:
            matched_rel = relation_by_id.get(linkedin_id)
            matched_by = "provider_id"
        elif linkedin_id_norm and linkedin_id_norm in connected_public_ids:
            matched_rel = relation_by_id.get(linkedin_id_norm)
            matched_by = "public_id"
        elif public_id and public_id in connected_public_ids:
            matched_rel = relation_by_id.get(public_id)
            matched_by = "public_id"
        elif (
            contact_name_norm
            and contact_name_norm in connected_by_name
            and contact_name_norm not in ambiguous_relation_names
            and invited_name_counts[contact_name_norm] == 1
        ):
            matched_rel = connected_by_name.get(contact_name_norm)
            matched_by = "name"

        if matched_rel is not None:
            import time as _time
            accepted_ts = int(_time.time())
            rel_created = matched_rel.get("created_at")
            if rel_created:
                # Unipile timestamp in ms
                if rel_created > 1000000000000:
                    accepted_ts = int(rel_created // 1000)
                elif rel_created > 1000000000:
                    accepted_ts = int(rel_created)

            # Update outreach to connected
            await run_db(update_outreach, r["outreach_id"], status="connected", accepted_at=accepted_ts)
            await run_db(increment_accepted)
            await run_db(
                log_action,
                "silent_connection_detected",
                outreach_id=r["outreach_id"],
                result="success",
                details={
                    "name": r.get("name", ""),
                    "method": "relations_sync",
                    "matched_by": matched_by,
                },
            )

            # Backfill contact details if missing
            resolved_pid = matched_rel.get("provider_id") or matched_rel.get("member_id") or contact_provider_id
            resolved_pub = matched_rel.get("public_id") or matched_rel.get("public_identifier") or public_id
            resolved_url = matched_rel.get("profile_url") or matched_rel.get("public_profile_url") or url
            if not resolved_url and resolved_pub:
                resolved_url = f"https://www.linkedin.com/in/{resolved_pub}"

            if resolved_pid or resolved_pub or resolved_url:
                def _backfill_contact(cid: str, pid_: str, pub_: str, url_: str) -> None:
                    db = get_db()
                    try:
                        db.execute(
                            """UPDATE contacts SET
                                linkedin_id = COALESCE(NULLIF(linkedin_id, ''), ?),
                                linkedin_url = COALESCE(NULLIF(linkedin_url, ''), ?)
                               WHERE id = ?""",
                            (pub_ or pid_, url_, cid),
                        )
                        db.commit()
                    finally:
                        db.close()

                await run_db(_backfill_contact, r["contact_id"], str(resolved_pid or ""), str(resolved_pub or ""), str(resolved_url or ""))

            if resolved_pid:
                await run_db(
                    mark_connected, account_id, str(resolved_pid),
                    r.get("name", ""), str(resolved_pub or ""),
                )
            newly_connected.append(r)
            matched_ids.add(r["outreach_id"])

    # Fallback: individually check remaining invited contacts via API (in parallel)
    unmatched = [dict(row) for row in invited_outreaches
                 if dict(row)["outreach_id"] not in matched_ids]
    if unmatched and len(unmatched) <= 20:
        async def _check_relation(r: dict) -> dict | None:
            contact_provider_id = ""
            try:
                profile_data = json.loads(r.get("profile_json") or "{}")
                contact_provider_id = str(profile_data.get("provider_id", ""))
            except (json.JSONDecodeError, TypeError):
                pass

            if not contact_provider_id:
                return None

            try:
                relation = await client.check_existing_relation(
                    account_id, contact_provider_id
                )
                if relation.get("connected") or relation.get("has_chat"):
                    import time as _time
                    await run_db(update_outreach, r["outreach_id"], status="connected", accepted_at=int(_time.time()))
                    await run_db(increment_accepted)
                    await run_db(
                        log_action,
                        "silent_connection_detected",
                        outreach_id=r["outreach_id"],
                        result="success",
                        details={"name": r.get("name", ""), "method": "individual_check"},
                    )
                    if contact_provider_id:
                        await run_db(
                            mark_connected, account_id, contact_provider_id,
                            r.get("name", ""), "",
                        )
                    return r
            except Exception as e:
                logger.debug(
                    "Individual relation check failed for %s: %s",
                    r.get("linkedin_id") or r.get("provider_id") or "unknown", e,
                )
            return None

        results = await asyncio.gather(
            *[_check_relation(r) for r in unmatched],
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, dict):
                newly_connected.append(result)

    return newly_connected


def _first_outbound_touch(contact: dict[str, Any], existing_msgs: list[dict]) -> int:
    """When we first reached out to this prospect — invite sent, or message sent.

    0 means we never did.
    """
    touches = [to_epoch(contact.get("invited_at")) or 0]
    touches += [
        to_epoch(m.get("timestamp")) or 0
        for m in existing_msgs
        if m.get("role") == "sdr"
    ]
    positive = [t for t in touches if t > 0]
    return min(positive) if positive else 0


def _answers_our_outreach(
    contact: dict[str, Any], existing_msgs: list[dict], msg: dict[str, Any]
) -> bool:
    """Can this inbound message be a reply to something we sent?

    An inbox thread that predates our first touch is not a reply to it — it is
    history that happens to involve someone we added as a prospect later. On
    18 Aug 2026 a refill added 50 such people mid-campaign and every old thread
    they had ever started with us was counted as a same-day reply, two of them
    as hot leads, in a campaign that had sent nothing at all.

    First touch, not last: a reply that lands between our invite and a
    follow-up we send afterwards predates the LAST touch and is still a reply.
    """
    touch = _first_outbound_touch(contact, existing_msgs)
    if touch <= 0:
        return False
    # No provider timestamp: we did reach out, so take it as a reply.
    # Revisit rows are older history looked up by attendee. A parse miss
    # there is timestamp=0, which is not evidence the message is new.
    msg_ts = to_epoch(msg.get("timestamp")) or 0
    if msg.get("_revisit"):
        return msg_ts > 0 and msg_ts > touch
    return msg_ts == 0 or msg_ts > touch


async def _honor_gated_opt_out(outreach_id: str, name: str, text: str) -> bool:
    """Opt-outs are honored even when the message answers nothing we sent.

    A never-touched prospect who writes "remove me" is not replying — but
    dropping the request means the planner later invites the one person who
    explicitly asked us not to. Keyword check only: no LLM spend on messages
    the gate rejects, and gated messages are re-seen on every scan.
    """
    if classify_fast(text) != "opt_out":
        return False
    # A pre-outreach opt-out is not a reply: explicit NULLs keep the flip
    # from minting accepted_at/first_reply_at for a prospect we never touched.
    await run_db(
        update_outreach, outreach_id, status="opted_out",
        accepted_at=None, first_reply_at=None,
    )
    await run_db(
        log_action, "opt_out_detected", outreach_id=outreach_id,
        details={"text": text[:200], "note": "pre-outreach opt-out"},
    )
    logger.info("Pre-outreach opt-out honored: %s (outreach=%s)", name, outreach_id)
    return True


def _fetch_outreach_contacts() -> list:
    """Every outreach a reply could belong to, with what decides between them.

    One person can sit on several outreach rows, and inbox_match._match_key
    ranks them to pick which row an incoming reply attaches to. It scores on
    `invited_at`, `status` and `campaign_status` — so every one of those has to
    be selected here, in another module, one call away from the scorer that
    reads them.

    A missing column is not an error there: `row.get()` returns None, the term
    scores zero, and every row ties on it. Dropping `camp.status` alone would
    quietly send replies to paused and draft rows instead of the live campaign,
    which is the defect this column was added to fix (24fd52a). Module level so
    that contract can be tested — see test_inbox_match_query_contract.py; it
    used to be a closure inside run_check_replies, which is why it never was.
    """
    db = get_db()
    rows = db.execute(
        """SELECT o.id as outreach_id, o.contact_id, o.status, o.invited_at,
                  c.linkedin_id, c.name, c.title, c.company, c.campaign_id, c.fit_score,
                  c.profile_json, camp.status as campaign_status
           FROM outreaches o
           JOIN contacts c ON o.contact_id = c.id
           LEFT JOIN campaigns camp ON camp.id = o.campaign_id
           WHERE o.status NOT IN ('opted_out')"""
    ).fetchall()
    db.close()
    return rows


def _stored_prospect_state(existing_msgs: list[dict]) -> tuple[set[str], dict | None]:
    """Provider ids already stored for this outreach + the last prospect row."""
    ids = {
        str(m.get("external_message_id") or "").strip()
        for m in existing_msgs
        if m.get("role") == "prospect"
    }
    ids.discard("")
    last = None
    for m in reversed(existing_msgs):
        if m.get("role") == "prospect":
            last = m
            break
    return ids, last


async def _collect_new_prospect_turns(
    client: Any,
    account_id: str,
    chat_id: str,
    our_provider_id: str,
    existing_msgs: list[dict],
    limit: int = 15,
) -> list[dict]:
    """Every prospect turn in a chat that we have not stored yet.

    Before 9 Sep this scan took exactly ONE message per chat — the chat's
    embedded last message, or the newest turn of a ``limit=5`` fetch followed
    by ``break``. A prospect who sent A, B and C between two scans had only C
    stored: B never got a sentiment, never reached the reply prompt, and never
    reached the opt-out gate. Local DB 9 Sep: outreach
    a421d255-efe8-4c21-bd24-b2b116e06098 carries three consecutive prospect
    turns and no reply at all.

    Returns oldest-first ``{sender_id, text, timestamp, message_id, chat_id}``.
    """
    try:
        chat_msgs = await client.get_chat_messages(account_id, chat_id, limit=limit)
    except Exception as e:
        if getattr(getattr(e, "response", None), "status_code", None) == 404:
            # A gone chat has no new turns to collect.
            logger.info("Chat %s no longer exists (404); no new turns", str(chat_id)[:12])
            return []
        logger.warning("Could not fetch chat %s: %s", str(chat_id)[:12], e)
        return []

    stored_ids, last_stored = _stored_prospect_state(existing_msgs)
    last_ts = 0
    if last_stored:
        last_ts = to_epoch(last_stored.get("timestamp")) or 0
    last_text = (last_stored or {}).get("text", "").strip()

    out: list[dict] = []
    for cm in sorted(chat_msgs or [], key=lambda m: m.get("timestamp") or 0):
        sender = cm.get("sender_id") or ""
        text = (cm.get("text") or "").strip()
        if not sender or not text or message_is_ours(cm, our_provider_id):
            continue
        mid = str(cm.get("message_id") or cm.get("id") or "").strip()
        if mid and mid in stored_ids:
            continue
        ts = to_epoch(cm.get("timestamp")) or 0
        if not mid:
            # No provider id to compare — fall back to "newer than what we
            # stored", plus an exact-text guard for the equal-timestamp case.
            if last_ts and ts and ts < last_ts:
                continue
            if text == last_text:
                continue
        out.append({
            "sender_id": sender,
            "text": text,
            "timestamp": cm.get("timestamp") or 0,
            "message_id": mid,
            "chat_id": chat_id,
        })
    return out


async def run_check_replies() -> str:
    """Check for new replies across all active campaigns.

    Flow:
    1. Fetch recent LinkedIn inbox messages
    2. Match messages to active outreach contacts
    3. Classify sentiment for new messages
    4. Handle opt-outs automatically
    5. Surface hot leads first
    """

    # ── Pre-checks ──
    setup_done = await run_db(get_setting, "setup_complete", False)
    if not setup_done:
        return (
            "❌ Setup required before checking replies.\n\n"
            "Please run setup_profile first — it connects your LinkedIn account.\n\n"
            "Say 'set up my profile' and I'll walk you through it step by step."
        )

    account_id = await run_db(get_account_id)
    if not account_id:
        return "❌ No LinkedIn account connected. Run setup_profile first."

    try:
        client = get_linkedin_client()
    except UnipileError as e:
        return f"❌ {e}"

    # ── Fetch messages from LinkedIn via Unipile ──
    try:
        messages = await _cb.call(
            client.get_chats(account_id=account_id, limit=50),
            label="get_chats",
        )
    except UnipileAuthError:
        await client.close()
        return (
            "🔑 LinkedIn account disconnected.\n\n"
            "Run setup_profile() again to reconnect."
        )
    except (CircuitBreakerOpen, asyncio.TimeoutError) as e:
        logger.error(f"get_chats timed out or circuit breaker open: {e}")
        await client.close()
        return f"⏱️ LinkedIn API too slow — skipped this check. Will retry next cycle."
    except Exception as e:
        logger.error(f"Failed to check messages: {e}")
        await client.close()
        return f"❌ Failed to check LinkedIn messages: {e}"

    # ── Sync silent connections (accepted but no message) ──
    silent_connections = await _sync_silent_connections(client, account_id)

    # ── Fetch profile viewers (inbound signals) ──
    profile_viewers: list[dict[str, Any]] = []
    try:
        profile_viewers = await _cb.call(
            client.get_profile_viewers(account_id),
            label="get_profile_viewers",
        )
    except (CircuitBreakerOpen, asyncio.TimeoutError) as e:
        logger.debug(f"Profile viewers skipped (timeout/circuit breaker): {e}")
    except Exception as e:
        logger.debug(f"Profile viewers fetch failed (non-critical): {e}")

    # ── Fetch inbound invitations ──
    inbound_invitations: list[dict[str, Any]] = []
    try:
        inbound_invitations = await _cb.call(
            client.get_received_invitations(account_id),
            label="get_received_invitations",
        )
    except (CircuitBreakerOpen, asyncio.TimeoutError) as e:
        logger.debug(f"Inbound invitations skipped (timeout/circuit breaker): {e}")
    except Exception as e:
        logger.debug(f"Inbound invitations fetch failed (non-critical): {e}")

    await client.close()

    # NOTE: Inbound invitations are now detected and saved by the unified
    # inbound pipeline (services/inbound_pipeline.py). No need to save here.

    if not messages and not silent_connections:
        # Even with no messages, show inbound signals
        extra_output: list[str] = []
        if inbound_invitations:
            await _append_inbound_invitations(extra_output, inbound_invitations, limit=10)
            extra_output.append("   💡 Accept matching leads or let the scheduler auto-accept ICP matches!")
        if profile_viewers:
            if extra_output:
                extra_output.append("")
            extra_output.append(f"👀 Profile Viewers ({len(profile_viewers)}):")
            extra_output.extend(_profile_viewer_lines(profile_viewers, limit=10))
            if len(profile_viewers) > 10:
                extra_output.append(f"   ... and {len(profile_viewers) - 10} more")
            extra_output.append("   💡 These people checked out your profile — consider reaching out!")
        if extra_output:
            return "📭 No new messages found.\n\n" + "\n".join(extra_output)
        return (
            "📭 No new messages found.\n\n"
            "Tip: Replies usually take 1-3 days after invitations are accepted.\n"
            "Use show_status to see your campaign progress."
        )

    # ── Match messages to outreach contacts ──
    outreach_contacts = await run_db(_fetch_outreach_contacts)

    # sender_id from get_chats() is provider_id (ACoAAA...); contacts.linkedin_id
    # is often the public slug. Duplicate contacts for the same person must not
    # last-write-win onto a skipped row that was never invited.
    contact_lookup, name_lookup = index_contacts_for_inbox(outreach_contacts)

    # Get our own provider_id so we can detect chats where WE sent the last message
    our_provider_id = ""
    try:
        profile_json_str = await run_db(get_setting, "profile", "")
        if profile_json_str:
            our_profile = json.loads(profile_json_str) if isinstance(profile_json_str, str) else profile_json_str
            our_provider_id = our_profile.get("provider_id", "")
    except Exception:
        pass

    # ── Process matches ──
    matched_replies: list[dict[str, Any]] = []

    unsolicited_inbound: list[dict[str, Any]] = []

    pending_classifications: list[tuple[dict, dict, str]] = []  # (contact, msg, reply_text)

    # Phase 1: Process messages that have embedded content (direct Unipile mode)
    has_needs_fetch = False
    for msg in messages:
        if msg.get("_needs_fetch"):
            has_needs_fetch = True
            continue  # Will be handled in Phase 1b via server-side batch

        sender_id = msg.get("sender_id") or ""
        if not sender_id:
            continue
        if message_is_ours(msg, our_provider_id):
            continue  # Our own message is never a reply, whatever id it carries
        if sender_id not in contact_lookup:
            # Fallback: match by sender_name when provider_id is missing
            # from profile_json (common for DM-only / connections-only campaigns).
            sender_name_key = (msg.get("sender_name") or "").strip().lower()
            fallback = name_lookup.get(sender_name_key) if sender_name_key else None
            if fallback is not None:
                # Cache the resolved provider_id so future checks match directly
                contact_lookup[sender_id] = fallback
                logger.info(
                    "Reply matched by name fallback: %s (sender_id=%s, linkedin_id=%s)",
                    sender_name_key, sender_id, fallback.get("linkedin_id", ""),
                )
            else:
                # Unsolicited inbound DM — enroll if they fit an active campaign
                from ..services.live_thread_enroll import enrich_and_enroll_live_thread
                from ..services.referral_enroll import fill_sender_name, maybe_enroll_from_inbound

                existing_sig = await run_db(get_inbound_signal_by_sender, sender_id, "message")
                sender_name = await run_db(
                    fill_sender_name, sender_id, msg.get("sender_name") or "",
                )
                enrolled = await enrich_and_enroll_live_thread(
                    sender_id=sender_id,
                    name=sender_name or "Unknown",
                    text=msg.get("text") or "",
                    headline=msg.get("sender_headline") or msg.get("headline") or "",
                    company=msg.get("sender_company") or msg.get("company") or "",
                    message_id=msg.get("message_id") or "",
                    timestamp=to_epoch(msg.get("timestamp")),
                    client=client,
                    account_id=account_id,
                )
                try:
                    await maybe_enroll_from_inbound(
                        sender_id=sender_id,
                        text=msg.get("text") or "",
                        name=sender_name,
                        company=msg.get("sender_company") or msg.get("company") or "",
                    )
                except Exception:
                    logger.debug("Referral enroll from unsolicited inbound failed", exc_info=True)
                unsolicited_inbound.append({
                    "name": sender_name or msg.get("sender_name", "Unknown"),
                    "text": msg.get("text", ""),
                    "signal_id": existing_sig.get("id", "") if existing_sig else "",
                    "enrolled": bool(enrolled),
                    "campaign_id": (enrolled or {}).get("campaign_id", ""),
                })
                continue

        contact = contact_lookup[sender_id]
        reply_text = msg.get("text") or ""

        # Dedup
        existing_msgs = await run_db(get_messages_for_outreach, contact["outreach_id"])
        _stored_ids, last_prospect_msg_existing = _stored_prospect_state(existing_msgs)
        if last_prospect_msg_existing and last_prospect_msg_existing.get("text", "").strip() == reply_text.strip():
            continue

        if not _answers_our_outreach(contact, existing_msgs, msg):
            if await _honor_gated_opt_out(
                contact["outreach_id"], contact.get("name", ""), reply_text
            ):
                continue
            logger.debug(
                "Not a reply — no outreach of ours precedes it: outreach=%s",
                contact["outreach_id"],
            )
            continue

        # Detect connection acceptance
        if contact["status"] == "invited":
            import time as _time
            await run_db(update_outreach, contact["outreach_id"], status="connected", accepted_at=int(_time.time()))
            await run_db(log_action, "connection_accepted", outreach_id=contact["outreach_id"],
                         details={"name": contact.get("name", "")})
            contact["status"] = "connected"
            _pid = str((contact.get("provider_id") or contact.get("linkedin_id") or "")).strip()
            if _pid:
                await run_db(
                    mark_connected, await run_db(get_account_id) or "", _pid,
                    contact.get("name", ""), "",
                )

        # Re-activate dormant outreaches
        DORMANT_STATUSES = ("pending", "error", "closed_happy", "closed_unhappy", "exhausted")
        if contact["status"] in DORMANT_STATUSES:
            old_status = contact["status"]
            await run_db(update_outreach, contact["outreach_id"], status="replied")
            await run_db(log_action, "outreach_reactivated", outreach_id=contact["outreach_id"],
                         details={"name": contact.get("name", ""),
                                  "previous_status": old_status,
                                  "trigger": "inbound_message"})
            contact["status"] = "replied"

        # The listing hands back ONE message per chat. When a prospect sent
        # several since the last scan, the earlier ones are only reachable by
        # opening the chat — do that now that we know this chat has something
        # new in it, so the cost is bounded by chats that actually moved.
        _turns: list[dict] = []
        _chat_urn = msg.get("conversation_urn") or msg.get("chat_id") or ""
        if _chat_urn:
            _turns = await _collect_new_prospect_turns(
                client, account_id, _chat_urn, our_provider_id, existing_msgs,
            )
        if len(_turns) > 1:
            for _turn in _turns:
                pending_classifications.append((
                    contact,
                    {
                        "sender_id": sender_id,
                        "sender_name": msg.get("sender_name") or contact.get("name", ""),
                        "text": _turn["text"],
                        "timestamp": _turn["timestamp"],
                        "conversation_urn": _chat_urn,
                        "message_id": _turn["message_id"],
                    },
                    _turn["text"],
                ))
            logger.info(
                "Reply detection: %d unread prospect turns in chat %s (listing showed 1)",
                len(_turns), str(_chat_urn)[:12],
            )
        else:
            pending_classifications.append((contact, msg, reply_text))

    # Phase 1b: Server-side batch fetch for lightweight chat entries.
    # Instead of N+1 client-side calls, use POST /chats/with-replies
    # which fetches messages server-side and returns only prospect replies.
    # Defined on every path — the loop below consumes it for BOTH transports.
    server_replies: list[dict] = []
    if has_needs_fetch and not hasattr(client, "get_chats_with_replies"):
        # Direct-Unipile mode has no server-side batch endpoint. This whole
        # block used to be skipped there, so every chat the listing returned
        # without an embedded message was dropped — on this scan and every
        # later one. A prospect replying "stop messaging me" in an older chat
        # was never seen, never classified, and never reached the opt-out gate;
        # the outreach stayed 'messaged' and the scheduler sent the next
        # follow-up.
        #
        # Fetch those chats one at a time and hand the result to the SAME loop
        # below, rather than duplicating its dedup, opt-out and acceptance
        # handling. N+1 is the cost of not having the batch endpoint, bounded
        # by the chats the listing actually flagged.
        server_replies = []
        pending = [
            m for m in messages
            if m.get("_needs_fetch") and (m.get("chat_id") or m.get("id"))
        ]
        logger.info(
            "Reply detection: fetching %d chat(s) individually — no server-side "
            "batch endpoint on this transport", len(pending),
        )
        for _msg in pending:
            _chat_id = (
                _msg.get("conversation_urn") or _msg.get("chat_id")
                or _msg.get("id") or ""
            )
            # Resolve the contact first so the fetch can be diffed against
            # what we already stored — the loop used to keep the newest
            # prospect message and `break`, dropping everything before it.
            _contact = None
            for _pid in (_msg.get("attendee_ids") or []):
                if _pid in contact_lookup:
                    _contact = contact_lookup[_pid]
                    break
            _existing: list[dict] = []
            if _contact:
                _existing = await run_db(
                    get_messages_for_outreach, _contact["outreach_id"],
                )
            for _turn in await _collect_new_prospect_turns(
                client, account_id, _chat_id, our_provider_id, _existing,
            ):
                server_replies.append({
                    "sender_id": _turn["sender_id"],
                    "text": _turn["text"],
                    "chat_id": _chat_id,
                    "timestamp": _turn["timestamp"],
                    "message_id": _turn["message_id"],
                })

    elif has_needs_fetch and hasattr(client, "get_chats_with_replies"):
        all_provider_ids = [pid for pid in contact_lookup if pid != our_provider_id]
        try:
            reply_client = get_linkedin_client()
            logger.debug("Phase 1b: calling get_chats_with_replies with %d provider_ids", len(all_provider_ids))
            server_replies = await asyncio.wait_for(
                reply_client.get_chats_with_replies(
                    account_id=account_id,
                    provider_ids=all_provider_ids,
                    our_provider_id=our_provider_id,
                    limit=100,
                ),
                timeout=120,
            )
            await reply_client.close()
            logger.info("Reply detection: found %d prospect replies via server-side fetch", len(server_replies))
        except Exception as e:
            logger.warning("Server-side reply fetch failed: %s", e)
            server_replies = []

    # Phase 1c: rows whose thread is not on the recent page at all.
    seen_provider_ids: set[str] = set()
    for _m in messages:
        if _m.get("sender_id"):
            seen_provider_ids.add(str(_m["sender_id"]))
        for _aid in _m.get("attendee_ids") or []:
            seen_provider_ids.add(str(_aid))
    for _r in server_replies:
        _sid = _r.get("sender_id") or _r.get("prospect_provider_id") or ""
        if _sid:
            seen_provider_ids.add(str(_sid))
    try:
        server_replies.extend(await _revisit_off_page_threads(
            client, account_id, outreach_contacts, seen_provider_ids, our_provider_id,
        ))
    except Exception as e:
        logger.warning("Reply revisit failed: %s", e)

    for reply in server_replies:
        sender_id = reply.get("sender_id") or reply.get("prospect_provider_id") or ""
        if not sender_id or sender_id not in contact_lookup:
            continue
        contact = contact_lookup[sender_id]
        reply_text = (reply.get("text") or "").strip()
        if not reply_text:
            continue

        # Dedup — provider message id first, text against the last stored
        # prospect row only as a fallback for transports that give us no id.
        existing_msgs = await run_db(get_messages_for_outreach, contact["outreach_id"])
        _stored_ids, last_prospect_msg = _stored_prospect_state(existing_msgs)
        _reply_mid = str(reply.get("message_id") or "").strip()
        if _reply_mid and _reply_mid in _stored_ids:
            continue
        if (
            not _reply_mid
            and last_prospect_msg
            and last_prospect_msg.get("text", "").strip() == reply_text
        ):
            continue

        if not _answers_our_outreach(contact, existing_msgs, reply):
            if await _honor_gated_opt_out(
                contact["outreach_id"], contact.get("name", ""), reply_text
            ):
                continue
            logger.debug(
                "Not a reply — no outreach of ours precedes it: outreach=%s",
                contact["outreach_id"],
            )
            continue

        # Detect connection acceptance
        if contact["status"] == "invited":
            import time as _time
            await run_db(update_outreach, contact["outreach_id"], status="connected", accepted_at=int(_time.time()))
            await run_db(log_action, "connection_accepted", outreach_id=contact["outreach_id"],
                         details={"name": contact.get("name", "")})
            contact["status"] = "connected"
            _pid = str((contact.get("provider_id") or contact.get("linkedin_id") or "")).strip()
            if _pid:
                await run_db(
                    mark_connected, await run_db(get_account_id) or "", _pid,
                    contact.get("name", ""), "",
                )

        # Re-activate dormant outreaches
        DORMANT_STATUSES = ("pending", "error", "closed_happy", "closed_unhappy", "exhausted")
        if contact["status"] in DORMANT_STATUSES:
            old_status = contact["status"]
            await run_db(update_outreach, contact["outreach_id"], status="replied")
            await run_db(log_action, "outreach_reactivated", outreach_id=contact["outreach_id"],
                         details={"name": contact.get("name", ""),
                                  "previous_status": old_status,
                                  "trigger": "inbound_message"})
            contact["status"] = "replied"

        synthetic_msg = {
            "sender_id": sender_id,
            "sender_name": reply.get("sender_name") or contact.get("name", ""),
            "text": reply_text,
            "timestamp": reply.get("timestamp") or 0,
            "conversation_urn": reply.get("chat_id", ""),
            "message_id": str(reply.get("message_id") or ""),
        }
        pending_classifications.append((contact, synthetic_msg, reply_text))

    # Phase 2: Classify sentiment in parallel for all pending messages
    if pending_classifications:
        # Earlier turns of the same batch are context for the later ones: a
        # bare "sure" after "can you send pricing?" is not a neutral shrug.
        _prior_by_index: list[list[dict]] = []
        _seen_per_outreach: dict[str, list[dict]] = {}
        for _c, _m, _t in pending_classifications:
            _oid = _c.get("outreach_id") or ""
            _prior_by_index.append(list(_seen_per_outreach.get(_oid, [])))
            _seen_per_outreach.setdefault(_oid, []).append({
                "role": "prospect",
                "text": _t,
                "timestamp": to_epoch(_m.get("timestamp")) or 0,
            })
        sentiments = await asyncio.gather(
            *[
                classify_sentiment(
                    text,
                    prior_turns=_prior_by_index[_i],
                    outreach_id=contact.get("outreach_id") or "",
                    message_id=str(
                        msg.get("message_id") or msg.get("id")
                        or msg.get("conversation_urn") or ""
                    ),
                )
                for _i, (contact, msg, text) in enumerate(pending_classifications)
            ]
        )
    else:
        sentiments = []

    # Phase 2b: Reverse-pitch detection for engaged/question/neutral messages
    seller_flags: list[dict[str, Any]] = [{}] * len(sentiments)
    seller_check_indices = [
        i for i, s in enumerate(sentiments)
        if s in ("engaged", "question", "neutral")
    ]
    if seller_check_indices:
        try:
            from ..linkedin import get_linkedin_client as _get_client
            seller_client = _get_client()
            if hasattr(seller_client, "classify_seller"):
                seller_results = await asyncio.gather(
                    *[
                        seller_client.classify_seller(
                            messages=[pending_classifications[i][2]],
                            author_name=pending_classifications[i][0].get("name", ""),
                            author_headline=pending_classifications[i][0].get("title", ""),
                        )
                        for i in seller_check_indices
                    ],
                    return_exceptions=True,
                )
                await seller_client.close()
                for idx, result in zip(seller_check_indices, seller_results):
                    if isinstance(result, dict) and result.get("is_seller") and result.get("confidence", 0) >= 0.4:
                        seller_flags[idx] = result
            else:
                await seller_client.close()
        except Exception as e:
            logger.debug("Reverse-pitch detection failed (non-critical): %s", e)

    # Phase 2c: Detect calendar links in reply text (returns the actual URL or None)
    calendar_flags: list[str | None] = [
        detect_calendar_url(pending_classifications[i][2]) if i < len(pending_classifications) else None
        for i in range(len(sentiments))
    ]

    # Phase 3: Process results (save, update status, detect hot leads)
    for i, ((contact, msg, reply_text), sentiment) in enumerate(zip(pending_classifications, sentiments)):
        # Provider time this reply was actually sent — NOT ingestion time, or a
        # backfilled year-old message presents as a fresh reply.
        provider_ts = to_epoch(msg.get("timestamp"))

        # Mark prior SDR messages as read (prospect replied → they read our messages)
        try:
            prior_msgs = await run_db(get_messages_for_outreach, contact["outreach_id"])
            reply_ts = to_epoch(msg.get("timestamp")) or int(time.time())
            for pm in prior_msgs:
                if pm.get("role") == "sdr" and not pm.get("read_at"):
                    await run_db(mark_message_read, pm["id"], read_at=reply_ts)
        except Exception:
            pass  # Non-critical

        # Save the reply
        await run_db(
            save_message,
            outreach_id=contact["outreach_id"],
            role="prospect",
            text=reply_text,
            sentiment=sentiment,
            timestamp=provider_ts,
            # Store the provider id so the next scan dedups exactly instead of
            # comparing text against the single newest stored prospect row.
            external_message_id=str(msg.get("message_id") or "") or None,
        )

        try:
            from ..services.referral_enroll import maybe_enroll_from_reply
            await maybe_enroll_from_reply(contact, reply_text)
        except Exception:
            logger.debug("Referral enroll from reply failed", exc_info=True)

        # Check reverse-pitch flag
        is_seller = bool(seller_flags[i]) if i < len(seller_flags) else False
        calendar_url = calendar_flags[i] if i < len(calendar_flags) else None
        has_calendar = bool(calendar_url)

        # Handle opt-outs
        if sentiment == "opt_out":
            await run_db(update_outreach, contact["outreach_id"], status=STATUS_ON_DETECTION["opt_out"])
            await run_db(log_action, "opt_out_detected", outreach_id=contact["outreach_id"],
                         details={"text": reply_text[:200]})

        # Reverse pitch — prospect is selling to us
        elif is_seller:
            await run_db(update_outreach, contact["outreach_id"], status="reverse_pitch")
            await run_db(log_action, "reverse_pitch_detected", outreach_id=contact["outreach_id"],
                         details={
                             "text": reply_text[:200],
                             "seller_type": seller_flags[i].get("seller_type", ""),
                             "confidence": seller_flags[i].get("confidence", 0),
                         })

        # Hot lead detection
        elif sentiment == "positive":
            await run_db(update_outreach, contact["outreach_id"], status=STATUS_ON_DETECTION["positive"])
            details: dict[str, Any] = {"text": reply_text[:200]}
            if calendar_url:
                details["prospect_calendar_url"] = calendar_url
                # Store the prospect's inbound calendar URL in the outreach
                await run_db(
                    update_outreach, contact["outreach_id"],
                    next_action=json.dumps({
                        "type": "book_meeting",
                        "prospect_calendar_url": calendar_url,
                        "prospect_name": contact.get("name", ""),
                    }),
                )
            await run_db(log_action, "hot_lead_detected", outreach_id=contact["outreach_id"],
                         details=details)
            # Bump fit score on positive reply
            current_fit = contact.get("fit_score", 0.5)
            new_fit = min(1.0, current_fit + 0.1)
            if contact.get("contact_id"):
                await run_db(update_contact, contact["contact_id"], fit_score=new_fit)

            # Dispatch real-time hot lead notification
            await _dispatch_hot_lead_alert(contact, reply_text, sentiment, calendar_url)

        # Question — needs attention
        elif sentiment == "question":
            await run_db(update_outreach, contact["outreach_id"], status="replied")
            await _dispatch_hot_lead_alert(contact, reply_text, sentiment, calendar_url)

        # Engaged conversation
        elif sentiment == "engaged":
            await run_db(update_outreach, contact["outreach_id"], status="replied")
            await _dispatch_hot_lead_alert(contact, reply_text, sentiment, calendar_url)

        # Other
        else:
            await run_db(update_outreach, contact["outreach_id"], status="replied")

        matched_replies.append({
            "name": contact.get("name", "Unknown"),
            "title": contact.get("title", ""),
            "company": contact.get("company", ""),
            "text": reply_text,
            "sentiment": sentiment,
            "conversation_urn": msg.get("conversation_urn", ""),
            "is_seller": is_seller,
            "has_calendar": has_calendar,
            "prospect_calendar_url": calendar_url or "",
            "outreach_id": contact.get("outreach_id", ""),
        })

    # ── Job-search campaigns: never book on the prospect's behalf ──
    # Replies there are answered by hand, so both booking paths below skip them.
    # The reply itself is still recorded and surfaced above.
    job_search_ids: set[str] = set()
    # Fail closed: if job-search campaigns cannot be identified, book nothing.
    job_search_lookup_failed = False
    try:
        from ..services.job_search_guard import job_search_campaign_ids
        job_search_ids = await run_db(job_search_campaign_ids)
    except Exception as e:
        job_search_lookup_failed = True
        logger.warning("Job-search campaign lookup failed, auto-booking skipped: %s", e)
    job_search_booking_skipped: set[str] = set()

    def _is_job_search_contact(contact: dict) -> bool:
        if job_search_lookup_failed:
            return True
        if (contact.get("campaign_id") or "") in job_search_ids:
            job_search_booking_skipped.add(contact.get("outreach_id") or contact.get("name", ""))
            return True
        return False

    # ── Auto-book meetings when prospect shared calendar link ──
    auto_booked: list[dict[str, Any]] = []
    calendar_replies = [
        (i, contact, calendar_flags[i])
        for i, ((contact, msg, reply_text), sentiment) in enumerate(zip(pending_classifications, sentiments))
        if calendar_flags[i] and _within_auto_reply_window(msg)
        and not _is_job_search_contact(contact)
    ]
    if calendar_replies:
        try:
            from ..services.calendar_booker import book_meeting, format_booking_result

            # Get user's name, email, and timezone for booking
            booker_name = ""
            booker_email = ""
            user_tz = "UTC"
            try:
                profile = await run_db(get_setting, "profile", {})
                booker_name = profile.get("name", "") if isinstance(profile, dict) else ""
            except Exception:
                pass
            try:
                from ..config import get_timezone, is_backend_mode
                user_tz = get_timezone() or "UTC"
            except Exception:
                pass
            # Get email from backend if available
            if not booker_email:
                try:
                    from ..config import is_backend_mode
                    if is_backend_mode():
                        from ..linkedin import get_linkedin_client as _get_bc
                        bc = _get_bc()
                        user_info = await bc.get_user_info()
                        booker_email = user_info.get("email", "")
                        await bc.close()
                except Exception as e:
                    logger.debug("Could not get user email for booking: %s", e)

            if booker_name and booker_email:
                for idx, contact, cal_url in calendar_replies:
                    try:
                        result = await book_meeting(
                            calendar_url=cal_url,
                            booker_name=booker_name,
                            booker_email=booker_email,
                            user_tz=user_tz,
                        )
                        auto_booked.append({
                            "name": contact.get("name", "Unknown"),
                            "linkedin_url": contact.get("linkedin_url", "") or "",
                            "result": result,
                            "outreach_id": contact.get("outreach_id", ""),
                        })
                        if result.get("success"):
                            # Update outreach with booking confirmation
                            await run_db(
                                update_outreach, contact["outreach_id"],
                                next_action=json.dumps({
                                    "type": "meeting_booked",
                                    "prospect_calendar_url": cal_url,
                                    "booked_time": result.get("booked_time", ""),
                                    "provider": result.get("provider", ""),
                                }),
                            )
                            await run_db(
                                log_action, "meeting_auto_booked",
                                outreach_id=contact["outreach_id"],
                                result="success",
                                details={
                                    "prospect_calendar_url": cal_url,
                                    "booked_time": result.get("booked_time", ""),
                                    "provider": result.get("provider", ""),
                                },
                            )
                            logger.info(
                                "Auto-booked meeting with %s at %s",
                                contact.get("name", ""), result.get("booked_time", ""),
                            )
                        else:
                            logger.info(
                                "Auto-booking failed for %s: %s",
                                contact.get("name", ""), result.get("error", ""),
                            )
                    except Exception as e:
                        logger.warning("Auto-booking error for %s: %s", contact.get("name", ""), e)
            else:
                logger.info("Skipping auto-booking: missing user name=%s email=%s", bool(booker_name), bool(booker_email))
        except Exception as e:
            logger.warning("Auto-booking batch failed: %s", e)

    # ── Auto-create Google Calendar events for meeting agreements ──
    calendar_events_created: list[dict[str, Any]] = []
    try:
        from ..config import is_backend_mode
        if is_backend_mode():
            # Find replies with meeting agreement signals (positive sentiment + scheduling language)
            meeting_candidates = []
            for i, ((contact, msg, reply_text), sentiment) in enumerate(zip(pending_classifications, sentiments)):
                if (
                    sentiment in ("positive", "engaged")
                    and detect_meeting_agreement(reply_text)
                    and not _is_job_search_contact(contact)
                ):
                    meeting_candidates.append((i, contact, msg, reply_text))

            if meeting_candidates:
                from datetime import datetime
                bc = get_linkedin_client()
                try:
                    user_tz = "UTC"
                    try:
                        from ..config import get_timezone
                        user_tz = get_timezone() or "UTC"
                    except Exception:
                        pass
                    # "Thursday" is resolved against this date, so it has to be
                    # today in the user's zone, not on the host's clock.
                    try:
                        from zoneinfo import ZoneInfo
                        current_date = datetime.now(ZoneInfo(user_tz)).date().isoformat()
                    except Exception:
                        current_date = datetime.now().date().isoformat()

                    for idx, contact, msg, reply_text in meeting_candidates:
                        try:
                            # Get conversation history for context
                            conv_messages = []
                            try:
                                prior = await run_db(get_messages_for_outreach, contact["outreach_id"])
                                for pm in prior:
                                    conv_messages.append({
                                        "role": pm.get("role", "unknown"),
                                        "text": pm.get("text", ""),
                                        "timestamp": pm.get("timestamp", ""),
                                    })
                            except Exception:
                                pass
                            # Add the current reply
                            conv_messages.append({
                                "role": "prospect",
                                "text": reply_text,
                                "timestamp": msg.get("timestamp", ""),
                            })

                            # Extract meeting details via LLM
                            meeting = await bc.extract_meeting(conv_messages, current_date, user_tz)

                            if meeting.get("meeting_agreed") and meeting.get("confidence", 0) >= 0.7:
                                # Build ISO datetime strings
                                m_date = meeting.get("date", "")
                                m_time = meeting.get("time", "")
                                m_tz = meeting.get("timezone", user_tz)
                                m_duration = meeting.get("duration_minutes", 30)

                                if m_date and m_time:
                                    start_dt, end_dt = _meeting_window(
                                        m_date, m_time, m_tz, m_duration,
                                    )

                                    prospect_name = contact.get("name", "Unknown")
                                    attendee_email = meeting.get("attendee_email", "")
                                    summary = f"Meeting with {prospect_name}"
                                    description = (
                                        f"Auto-scheduled by HeyLead from LinkedIn conversation.\n"
                                        f"Prospect: {prospect_name}"
                                    )
                                    if contact.get("title"):
                                        description += f" — {contact['title']}"
                                    if contact.get("company"):
                                        description += f" at {contact['company']}"

                                    # Create calendar event via backend
                                    result = await bc.create_calendar_event(
                                        summary=summary,
                                        start_datetime=start_dt,
                                        end_datetime=end_dt,
                                        attendee_email=attendee_email,
                                        description=description,
                                    )

                                    if result.get("success"):
                                        calendar_events_created.append({
                                            "name": prospect_name,
                                            "linkedin_url": contact.get("linkedin_url", "") or "",
                                            "date": m_date,
                                            "time": m_time,
                                            "event_link": result.get("event_link", ""),
                                        })
                                        # Update outreach with calendar event info
                                        await run_db(
                                            update_outreach, contact["outreach_id"],
                                            next_action=json.dumps({
                                                "type": "calendar_event_created",
                                                "event_link": result.get("event_link", ""),
                                                "event_id": result.get("event_id", ""),
                                                "meeting_date": m_date,
                                                "meeting_time": m_time,
                                                "attendee_email": attendee_email,
                                            }),
                                        )
                                        await run_db(
                                            log_action, "calendar_event_created",
                                            outreach_id=contact["outreach_id"],
                                            result="success",
                                            details={
                                                "meeting_date": m_date,
                                                "meeting_time": m_time,
                                                "event_link": result.get("event_link", ""),
                                                "attendee_email": attendee_email,
                                                "prospect_name": prospect_name,
                                            },
                                        )
                                        logger.info(
                                            "Calendar event created for %s on %s at %s",
                                            prospect_name, m_date, m_time,
                                        )
                                    else:
                                        logger.info(
                                            "Calendar event creation failed for %s: %s",
                                            prospect_name, result.get("error", ""),
                                        )
                        except Exception as e:
                            logger.warning(
                                "Meeting extraction/calendar error for %s: %s",
                                contact.get("name", ""), e,
                            )
                finally:
                    await bc.close()
    except Exception as e:
        logger.debug("Calendar event auto-creation skipped: %s", e)

    # Auto-replies are handled exclusively by the scheduler (JOB_AUTO_REPLY)
    # to prevent duplicate replies from concurrent check_replies + scheduler execution.
    # The scheduler's planner picks up positive/engaged replies and creates auto-reply
    # jobs with proper dedup (pending job check + 30-min cooldown + DB message guard).
    auto_replied_names: list[str] = []

    # ── Mark processed chats as read (non-blocking) ──
    try:
        chat_ids_to_mark = set()
        for reply in matched_replies:
            curn = reply.get("conversation_urn", "")
            if curn:
                chat_ids_to_mark.add(curn)
        if chat_ids_to_mark:
            mark_account_id = await run_db(get_account_id)
            if mark_account_id:
                try:
                    mark_client = get_linkedin_client()
                except Exception:
                    mark_client = None
                if mark_client:
                    try:
                        for cid in chat_ids_to_mark:
                            await mark_client.mark_chat_read(cid)
                    finally:
                        await mark_client.close()
    except Exception:
        pass  # Non-critical — inbox hygiene

    # ── Format output ──
    if not matched_replies and not silent_connections:
        return (
            f"📬 Checked {len(messages)} messages — none from campaign contacts.\n\n"
            "Replies from non-campaign contacts are not tracked.\n"
            "Use show_status to see campaign progress."
        )

    if not matched_replies and silent_connections:
        # Only silent connections, no message replies
        output = [f"📬 No new message replies, but:\n"]
        sc_count = len(silent_connections)
        output.append(f"🤝 {sc_count} silent connection{'s' if sc_count != 1 else ''} detected:")
        for sc in silent_connections[:5]:
            name = sc.get("name", "Unknown")
            title = sc.get("title", "")
            company = sc.get("company", "")
            role = title
            if company:
                role += f" at {company}" if role else company
            output.append(f"   └── {name} ({role}) — accepted your invite!")
        if sc_count > 5:
            output.append(f"   ... and {sc_count - 5} more")
        output.append('   → Use send_message(action="followup") to reach out!')
        return "\n".join(output)

    # Sort: hot leads first, then questions, then rest
    priority_order = {"positive": 0, "engaged": 1, "question": 2, "neutral": 3, "negative": 4, "out_of_office": 5, "opt_out": 6}
    matched_replies.sort(key=lambda r: priority_order.get(r["sentiment"], 99))

    output = [f"📬 {len(matched_replies)} new repl{'y' if len(matched_replies) == 1 else 'ies'}:\n"]

    for i, reply in enumerate(matched_replies):
        is_seller = reply.get("is_seller", False)
        has_calendar = reply.get("has_calendar", False)

        if is_seller:
            icon = "🔄"
            action = "REVERSE PITCH — they're selling to you. Auto-reply skipped."
        elif has_calendar:
            icon = "📅"
            cal_url = reply.get("prospect_calendar_url", "")
            action = f"Shared their calendar — book here: {cal_url}" if cal_url else "Shared their calendar link — book a time!"
        else:
            icon = SENTIMENT_ICONS.get(reply["sentiment"], "💬")
            action = SENTIMENT_ACTIONS.get(reply["sentiment"], "")

        hold_note = ""
        oid = reply.get("outreach_id") or ""
        if oid:
            rec = await run_db(get_outreach, oid)
            if rec:
                from ..services.reply_agent import parse_operator_hold
                hold = parse_operator_hold(rec.get("next_action") or "")
                if hold:
                    hold_note = f"HELD FOR OPERATOR — {hold.get('reason') or 'needs a human'}"

        name = reply["name"]
        role = reply.get("title", "")
        if reply.get("company"):
            role += f" at {reply['company']}" if role else reply["company"]

        text_preview = reply["text"][:150]
        if len(reply["text"]) > 150:
            text_preview += "..."

        output.append(f"{icon} **{name}** ({role}):")
        output.append(f"   \"{text_preview}\"")
        if hold_note:
            output.append(f"   → {hold_note}")
        elif action:
            output.append(f"   → {action}")
        output.append("")

    # Summary
    hot_count = sum(1 for r in matched_replies if r["sentiment"] == "positive")
    question_count = sum(1 for r in matched_replies if r["sentiment"] == "question")
    opt_out_count = sum(1 for r in matched_replies if r["sentiment"] == "opt_out")
    seller_count = sum(1 for r in matched_replies if r.get("is_seller"))
    calendar_count = sum(1 for r in matched_replies if r.get("has_calendar"))

    if hot_count > 0:
        if auto_replied_names:
            names_str = ", ".join(auto_replied_names)
            output.append(f"🔥 {hot_count} hot lead{'s' if hot_count > 1 else ''}! Auto-replied to: {names_str}")
        else:
            output.append(f"🔥 {hot_count} hot lead{'s' if hot_count > 1 else ''}! Reply to them ASAP.")
    if calendar_count > 0:
        if auto_booked:
            from ..services.calendar_booker import format_booking_result
            booked_ok = [b for b in auto_booked if b["result"].get("success")]
            booked_fail = [b for b in auto_booked if not b["result"].get("success")]
            if booked_ok:
                output.append(f"📅 Auto-booked {len(booked_ok)} meeting{'s' if len(booked_ok) > 1 else ''}:")
                for b in booked_ok:
                    booked_time = b["result"].get("booked_time", "")
                    provider = b["result"].get("provider", "")
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        dt = _dt.fromisoformat(booked_time.replace("Z", "+00:00"))
                        time_str = dt.strftime("%a %b %d, %I:%M %p")
                    except (ValueError, TypeError):
                        time_str = booked_time
                    output.append(f"   {prospect_link(b['name'], b.get('linkedin_url', ''))} — {time_str} ({provider.title()})")
            if booked_fail:
                for b in booked_fail:
                    cal_url = b["result"].get("url", "")
                    output.append(f"   {prospect_link(b['name'], b.get('linkedin_url', ''))} — auto-book failed, book manually: {cal_url}")
        else:
            cal_urls = [r["prospect_calendar_url"] for r in matched_replies if r.get("prospect_calendar_url")]
            if cal_urls:
                output.append(f"📅 {calendar_count} prospect{'s' if calendar_count > 1 else ''} shared their calendar:")
                for url in cal_urls:
                    output.append(f"   → {url}")
            else:
                output.append(f"📅 {calendar_count} prospect{'s' if calendar_count > 1 else ''} shared their calendar — book a time!")
    # Calendar events auto-created from meeting agreements
    if calendar_events_created:
        output.append(f"\n📅 Auto-created {len(calendar_events_created)} Google Calendar event{'s' if len(calendar_events_created) > 1 else ''}:")
        for evt in calendar_events_created:
            output.append(f"   {prospect_link(evt['name'], evt.get('linkedin_url', ''))} — {evt['date']} at {evt['time']}")
            if evt.get("event_link"):
                output.append(f"      {evt['event_link']}")

    if job_search_lookup_failed:
        output.append(
            "📅 Auto-booking skipped this run: job-search campaigns could not be "
            "checked. Book any meetings by hand."
        )
    elif job_search_booking_skipped:
        n = len(job_search_booking_skipped)
        output.append(
            f"📅 Booking skipped for {n} job-search repl{'ies' if n > 1 else 'y'}: "
            "answer by hand."
        )

    if question_count > 0:
        output.append(f"❓ {question_count} question{'s' if question_count > 1 else ''} to answer.")
    if seller_count > 0:
        output.append(f"🔄 {seller_count} reverse pitch{'es' if seller_count > 1 else ''} detected (auto-reply skipped).")
    if opt_out_count > 0:
        output.append(f"🚫 {opt_out_count} opt-out{'s' if opt_out_count > 1 else ''} (auto-closed).")

    # ── Silent connections ──
    if silent_connections:
        output.append("")
        sc_count = len(silent_connections)
        output.append(f"🤝 {sc_count} silent connection{'s' if sc_count != 1 else ''} detected:")
        for sc in silent_connections[:5]:
            name = sc.get("name", "Unknown")
            title = sc.get("title", "")
            company = sc.get("company", "")
            role = title
            if company:
                role += f" at {company}" if role else company
            output.append(f"   └── {name} ({role}) — accepted your invite!")
        if sc_count > 5:
            output.append(f"   ... and {sc_count - 5} more")
        output.append('   → Use send_message(action="followup") to reach out!')

    # ── Profile viewers (inbound signals) ──
    if profile_viewers:
        output.append("")
        output.append(f"👀 Profile Viewers ({len(profile_viewers)}):")
        output.extend(_profile_viewer_lines(profile_viewers, limit=5))
        if len(profile_viewers) > 5:
            output.append(f"   ... and {len(profile_viewers) - 5} more")
        output.append("   💡 These people checked your profile — warm leads!")

    # ── Inbound invitations with qualification badges ──
    if inbound_invitations:
        output.append("")
        await _append_inbound_invitations(output, inbound_invitations, limit=5)
        output.append("   💡 Accept matching leads or let the scheduler handle it!")

    # ── Unsolicited inbound DMs ──
    if unsolicited_inbound:
        output.append("")
        output.append(f"📩 Unsolicited Inbound DMs ({len(unsolicited_inbound)}):")
        enrolled = [ub for ub in unsolicited_inbound if ub.get("enrolled")]
        for ub in unsolicited_inbound[:5]:
            name = ub.get("name", "Unknown")
            text = (ub.get("text") or "")[:100]
            tag = " — enrolled on a matching campaign" if ub.get("enrolled") else ""
            output.append(f"   • {name}: \"{text}\"{tag}")
        if len(unsolicited_inbound) > 5:
            output.append(f"   ... and {len(unsolicited_inbound) - 5} more")
        if enrolled:
            names = ", ".join(ub.get("name", "?") for ub in enrolled[:5])
            output.append(f"   Enrolled {len(enrolled)} live thread(s): {names}")
        else:
            output.append("   💡 New inbound messages detected — pipeline will classify and respond!")

    # ── Inbound Pipeline Status ──
    recent_signals = await run_db(list_inbound_signals, limit=30)
    if recent_signals:
        by_status: dict[str, int] = {}
        for s in recent_signals:
            st = s.get("status", "unknown")
            by_status[st] = by_status.get(st, 0) + 1
        pipeline_parts = []
        for status_name in ("new", "classified", "accepted", "ignored", "engaged", "dismissed"):
            count = by_status.get(status_name, 0)
            if count:
                pipeline_parts.append(f"{count} {status_name}")
        if pipeline_parts:
            output.append("")
            output.append(f"📊 Inbound Pipeline: {', '.join(pipeline_parts)}")

    return "\n".join(output)


async def _dispatch_hot_lead_alert(
    contact: dict[str, Any],
    reply_text: str,
    sentiment: str,
    calendar_url: str | None = None,
) -> None:
    """Send immediate notification to configured webhook when a hot lead responds."""
    try:
        from ..config import load_config
        cfg = load_config()
        webhook_url = cfg.get("alert_webhook_url") or cfg.get("slack_webhook_url") or cfg.get("lead_webhook_url") or ""
        if webhook_url:
            import httpx
            name = contact.get("name", "Prospect")
            company = contact.get("company", "")
            title = contact.get("title", "")
            msg = f"🔥 *Hot Lead Response*: {name} ({title} at {company})\n*Sentiment*: `{sentiment}`\n*Message*: {reply_text[:300]}"
            if calendar_url:
                msg += f"\n*Calendar Link*: {calendar_url}"
            payload = {
                "text": msg,
                "event": "hot_lead_detected",
                "name": name,
                "company": company,
                "sentiment": sentiment,
                "message": reply_text,
                "calendar_url": calendar_url or "",
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(webhook_url, json=payload)
    except Exception as e:
        logger.debug("Lead alert webhook failed: %s", e)
