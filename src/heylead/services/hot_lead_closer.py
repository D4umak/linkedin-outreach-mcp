"""Book a meeting when a book intent is already grounded in the thread."""

from __future__ import annotations

from heylead.services.booking_links import offered_link

from .campaign_naming import cut_at_word

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from ..ai.agent_loop import LOOP_FAILED_REASON, AgentBudget, run_agent_loop
from . import model_failure
from ..ai.hot_lead_closer import CLOSER_AGENT_SYSTEM, build_closer_context
from ..ai.schemas import CLOSER_AGENT_STEP
from ..constants import (
    HOT_LEAD_CLOSER_MAX_STEPS,
    HOT_LEAD_CLOSER_MAX_TOKENS,
    HOT_LEAD_CLOSER_RESULT_CHARS,
    HOT_LEAD_CLOSER_TIMEOUT_SECONDS,
    HOT_LEAD_CLOSER_VALID_DECISIONS,
)
from ..db.async_bridge import run_db
from ..db.queries import get_messages_for_outreach, get_outreach_with_contact, log_action
from ..flags import flag_enabled
from . import agent_decisions
from .agent_commons import async_commons_tools
from .agent_context import numbers_for
from .coordinator import after_sibling_loop
from ..services.prospect_email import extract_profile_email

logger = logging.getLogger(__name__)

_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?")
_AFFIRM = re.compile(
    r"\b(yes|yeah|yep|yup|ok|okay|sure|confirmed|confirm|works|perfect|"
    r"sounds good|let'?s do (?:it|that)|book it|that works|that'?s fine|"
    r"go ahead)\b",
    re.I,
)
CloserKind = Literal["continue", "hold", "not_yet"]

# The kernel words that mean "I could not finish my step this turn".
_KERNEL_NOT_YET = frozenset({"hold", "none"})


def beat_decision(kernel_decision: str) -> str:
    """What the beat says the closer DID, not the word its kernel used.

    The coordinator reads these beats and may hold the WHOLE campaign for
    human review. On 22 Sep 2026 one prospect who expressed interest without
    naming a time put a campaign under a coordinator hold -- reason "closer
    agent is on hold, requiring human review" -- and blocked 21 sends to other
    people until an operator cleared it. Nothing the closer decides for itself
    is a human hold.
    """
    return "not_yet" if (kernel_decision or "") in _KERNEL_NOT_YET else kernel_decision


@dataclass
class HotLeadCloserOutcome:
    kind: CloserKind
    reason: str = ""
    message: str = ""
    applied: bool = False


def hot_lead_closer_mode(config: dict[str, Any] | None) -> Literal["off", "observe", "act"]:
    """Campaign config → off | observe | act. Unset defaults to observe.

    Same order as the hosted closer: the explicit mode, then the legacy
    flag, then observe. Until 27 Oct 2026 an unset mode read act here while
    the API read observe (api #4564).
    """
    cfg = config or {}
    raw_mode = cfg.get("hot_lead_closer_mode")
    if isinstance(raw_mode, str) and raw_mode.strip():
        text = raw_mode.strip().lower()
        if text in {"off", "observe", "act"}:
            return text  # type: ignore[return-value]
        if text in {"on", "true", "yes", "1"}:
            return "act"
        if text in {"false", "no", "0"}:
            return "off"
    if "enable_hot_lead_closer" in cfg and cfg.get("enable_hot_lead_closer") not in (None, ""):
        return "act" if flag_enabled(cfg, "enable_hot_lead_closer", default=False) else "off"
    return "observe"


def booking_is_grounded(
    email: str,
    start: str,
    evidence: str,
    known_emails: list[str] | tuple[str, ...] = (),
    *,
    messages: list[dict[str, Any]] | None = None,
    reply_text: str = "",
) -> bool:
    """True when the email is known or in the thread, and the ISO is a prospect's."""
    addr = (email or "").strip().lower()
    start_n = (start or "").strip()
    blob = evidence or ""
    if not addr or "@" not in addr:
        return False
    known = {e.strip().lower() for e in known_emails if e and "@" in e}
    if addr not in known and addr not in blob.lower():
        return False
    match = _ISO.search(start_n)
    if not match:
        return False
    token = match.group(0)
    if messages is None:
        return token in blob or token[:16] in blob
    return _iso_grounded_in_prospect(token, messages, reply_text)


def _iso_grounded_in_prospect(
    token: str, messages: list[dict[str, Any]], reply_text: str,
) -> bool:
    """ISO must appear in a prospect line, or a later affirmative after our ISO."""
    short = token[:16]

    def has_iso(text: str) -> bool:
        body = text or ""
        return token in body or short in body

    if has_iso(reply_text):
        return True
    last_sdr_iso = -1
    for i, message in enumerate(messages or []):
        role = (message.get("role") or "").lower()
        text = message.get("text") or ""
        if role == "prospect" and has_iso(text):
            return True
        if role == "sdr" and has_iso(text):
            last_sdr_iso = i
    if last_sdr_iso < 0:
        return False
    for message in (messages or [])[last_sdr_iso + 1:]:
        if (message.get("role") or "").lower() != "prospect":
            continue
        if _AFFIRM.search(message.get("text") or ""):
            return True
    return bool(reply_text and _AFFIRM.search(reply_text))


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

# Why a run readied no call, in the user's words (api #4564, designer round 1).
WHY_ADDRESS = "their email address isn't in their own messages or their contact record"
WHY_TIME = "they haven't named a time themselves, and HeyLead can't see your calendar"
WHY_UNSETTLED = "they haven't settled on a call yet"
WHY_UNFINISHED = "the meeting check didn't finish"
COORDINATOR_HELD = "Held for operator — this campaign is paused for your review. Nothing was sent to them."


def _prospect_lines(messages: list[dict[str, Any]] | None, reply_text: str = "") -> str:
    """Their words only: the reply and every prospect line, never ours."""
    lines = [str(m.get("text") or "") for m in messages or [] if (m.get("role") or "").lower() == "prospect"]
    return "\n".join([reply_text or "", *lines])


def address_source(email: str, prospect_text: str, known_email: str = "") -> str:
    """Where the address came from: "contact record", "their message", or "" when not from them.

    A whole address only: one that is merely part of an address they wrote
    is not theirs. Our own lines never count (api #4564, hosted #3388 P4).
    """
    addr = (email or "").strip().lower()
    if not addr or "@" not in addr:
        return ""
    if addr == (known_email or "").strip().lower():
        return "contact record"
    found = {m.group(0).lower().rstrip(".") for m in _EMAIL.finditer(prospect_text or "")}
    return "their message" if addr in found else ""


def start_in_prospect_line(start: str, messages: list[dict[str, Any]] | None, reply_text: str = "") -> bool:
    """The ISO start is in the prospect's own words, not only a yes to ours (hosted G-1).

    The client never reads the owner's free time, so a time only we
    proposed was never checked against the calendar.
    """
    match = _ISO.search((start or "").strip())
    if not match:
        return False
    token = match.group(0)
    text = _prospect_lines(messages, reply_text)
    return token in text or token[:16] in text


def _when(start: str) -> str:
    """'Tue 3 Nov, 10:00 (Europe/London)' in the user's own time zone."""
    from ..config import get_timezone

    try:
        at = datetime.fromisoformat(str(start or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return str(start or "").strip()
    zone = get_timezone()
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(zone)
    except Exception:
        tz, zone = timezone.utc, "UTC"
    if at.tzinfo is None:
        at = at.replace(tzinfo=tz)
    return at.astimezone(tz).strftime(f"%a %-d %b, %H:%M ({zone})")


def closer_line(mode: str, ready: bool, why: str = "", start: str = "") -> str:
    """The line a closer run leaves, written by code (api #4564).

    The client never sends a calendar invite (Denys, 27 Oct 2026: "Reply
    goes on"), so every line says none was sent and that the reply goes on.
    """
    if ready:
        when = _when(start)
        if mode == "act":
            return (f"Ready to book: they agreed to a call on {when}. HeyLead can't put it on your "
                    "Google Calendar from this computer, so no calendar invite was sent. Book it from "
                    "your calendar; the reply goes on as usual.")
        return (f"Would have marked a call on {when} as ready for you to book. No calendar invite was "
                "sent; the conversation goes on as usual.")
    if mode == "act":
        return f"No calendar invite yet — {why}. The reply goes on as usual."
    return f"Would have waited: {why}. No calendar invite was sent; the conversation goes on as usual."


async def maybe_run_hot_lead_closer(
    *,
    outreach_id: str,
    campaign_config: dict[str, Any] | None,
    reply_text: str,
    sentiment: str,
    prospect_calendar_url: str = "",
    last_message_ts: int = 0,
    last_message_id: str = "",
    call_llm_fn: Any = None,
) -> HotLeadCloserOutcome:
    """Note the call a hot lead is ready for. Never sends a calendar invite.

    On this computer the closer cannot book: the local engine runs only on
    an install with no backend sign-in, and book_meeting needs one. Until
    27 Oct 2026 act called it anyway, book_meeting answered "Not connected
    to the HeyLead backend", that answer was logged as booked and the reply
    the prospect was owed never went out (api #4564). Denys chose that the
    reply goes on, so every run that sends no invite returns ``continue``;
    the only exception is a coordinator hold in act, which stops the reply.
    """
    try:
        mode = hot_lead_closer_mode(campaign_config)
        if mode == "off":
            return HotLeadCloserOutcome(kind="continue", reason="agent off")

        contact = await run_db(get_outreach_with_contact, outreach_id) or {}
        campaign_id = str(contact.get("campaign_id") or "")
        from .coordinator import coordinator_blocks_send
        if campaign_id and await run_db(coordinator_blocks_send, campaign_id):
            if mode == "observe":
                return HotLeadCloserOutcome(kind="continue", reason="coordinator hold")
            return HotLeadCloserOutcome(kind="not_yet", reason="coordinator hold", message=COORDINATOR_HELD)
        known_email = extract_profile_email(contact, contact.get("profile_json") or "")
        numbers = await numbers_for(campaign_id, actor="closer")
        context = build_closer_context(
            name=str(contact.get("name") or ""),
            sentiment=sentiment,
            reply_text=reply_text,
            numbers=numbers,
        )

        async def read_thread() -> str:
            return await run_db(_format_thread, outreach_id)

        async def read_contact() -> str:
            return f"name: {contact.get('name') or ''}\nemail: {known_email or '(none)'}"

        async def read_calendar_target() -> str:
            link = offered_link(campaign_config) or prospect_calendar_url or ""
            return f"booking_link: {link or '(none)'}\nprospect_calendar_url: {prospect_calendar_url or '(none)'}"

        result = await run_agent_loop(
            system=CLOSER_AGENT_SYSTEM,
            context=context,
            tools={
                "read_thread": read_thread,
                "read_contact": read_contact,
                "read_calendar_target": read_calendar_target,
                **async_commons_tools(
                    agent="closer", campaign_id=campaign_id, outreach_id=outreach_id,
                ),
            },
            schema=CLOSER_AGENT_STEP,
            budget=AgentBudget(
                max_steps=HOT_LEAD_CLOSER_MAX_STEPS,
                max_tokens=HOT_LEAD_CLOSER_MAX_TOKENS,
                timeout_seconds=float(HOT_LEAD_CLOSER_TIMEOUT_SECONDS),
                result_chars=HOT_LEAD_CLOSER_RESULT_CHARS,
            ),
            call_llm_fn=call_llm_fn,
            valid_decisions=HOT_LEAD_CLOSER_VALID_DECISIONS,
        )
        if result.failure:
            # Not a hold (heylead-api#3390): no decision row, no sibling woken. The closer
            # stands aside and the reply lane, which reads its own failure, keeps the thread.
            await model_failure.loop_failed(actor="closer", failure=result.failure, outreach_id=outreach_id,
                                            campaign_id=campaign_id, detail=result.error)
            return HotLeadCloserOutcome(
                kind="continue", reason=LOOP_FAILED_REASON, message=closer_line(mode, False, WHY_UNFINISHED),
            )
        await after_sibling_loop(
            agent="closer",
            campaign_id=campaign_id,
            decision=beat_decision(result.decision),
            reason=result.reason,
            config=campaign_config,
        )

        messages = (await run_db(get_messages_for_outreach, outreach_id) or [])[-12:]
        email = (result.extras.get("attendee_email") or known_email or "").strip()
        start = (result.extras.get("start_iso") or "").strip()
        decision = result.decision if result.decision in {"book", "hold"} else "hold"

        for step in result.steps:
            await run_db(
                log_action, "hot_lead_closer_step",
                outreach_id=outreach_id,
                result=str(step.get("action") or ""),
                details={k: v for k, v in step.items() if k != "attendee_email"},
            )

        # Their words only: an address or a time WE wrote is not theirs
        # (api #4564, hosted #3388 P4 and G-1; the client reads no free time).
        source = address_source(email, _prospect_lines(messages, reply_text), known_email)
        why = WHY_UNSETTLED
        if decision == "book" and not source:
            decision, why = "hold", WHY_ADDRESS
        elif decision == "book" and not start_in_prospect_line(start, messages, reply_text):
            decision, why = "hold", WHY_TIME
        line = closer_line(mode, decision == "book", why, start)
        # Recorded unapplied: nothing is sent from here (api #4564).
        await run_db(
            agent_decisions.record_decision, actor="closer", kind=decision, campaign_id=campaign_id,
            outreach_ids=[outreach_id], applied=False, numbers=numbers,
        )
        await run_db(
            log_action, "hot_lead_closer_decision",
            outreach_id=outreach_id,
            result=decision,
            details={
                "reason": line,
                "notes": result.reason or "",
                "mode": mode,
                "address_source": source,
                "start": start,
                "observe": mode == "observe",
            },
        )
        return HotLeadCloserOutcome(kind="continue", reason=line, message=line)
    except Exception as e:
        logger.warning("Hot-lead closer failed: %s", e)
        return HotLeadCloserOutcome(
            kind="continue",
            reason=LOOP_FAILED_REASON,
            message=closer_line(hot_lead_closer_mode(campaign_config), False, WHY_UNFINISHED),
        )


def _format_thread(outreach_id: str) -> str:
    messages = get_messages_for_outreach(outreach_id)[-12:]
    lines = []
    for message in messages:
        role = message.get("role") or "?"
        text = cut_at_word(message.get("text") or "", 280)
        lines.append(f"{role}: {text}")
    return "\n".join(lines) or "(no messages)"
