"""Book a meeting when a book intent is already grounded in the thread."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from ..ai.agent_loop import AgentBudget, run_agent_loop
from ..ai.hot_lead_closer import CLOSER_AGENT_SYSTEM, build_closer_context
from ..ai.schemas import CLOSER_AGENT_STEP
from ..constants import (
    HOT_LEAD_CLOSER_MAX_BOOKS_PER_DAY,
    HOT_LEAD_CLOSER_MAX_STEPS,
    HOT_LEAD_CLOSER_MAX_TOKENS,
    HOT_LEAD_CLOSER_RESULT_CHARS,
    HOT_LEAD_CLOSER_TIMEOUT_SECONDS,
    HOT_LEAD_CLOSER_VALID_DECISIONS,
)
from ..db.async_bridge import run_db
from ..db.queries import get_messages_for_outreach, get_outreach_with_contact, log_action
from ..db.schema import get_db
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
CloserKind = Literal["continue", "hold", "not_yet", "booked"]

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
    return "act"


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


def _parse_duration(raw: str) -> int:
    try:
        minutes = int(str(raw).strip() or "30")
    except (TypeError, ValueError):
        minutes = 30
    return max(15, min(minutes, 120))


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
    book_fn: Any = None,
) -> HotLeadCloserOutcome:
    """Place a meeting or hold. Failure → hold. Off → continue into the reply pipeline."""
    try:
        mode = hot_lead_closer_mode(campaign_config)
        if mode == "off":
            return HotLeadCloserOutcome(kind="continue", reason="agent off")

        if mode == "act":
            if await run_db(_has_closer_today, outreach_id):
                return await _not_yet(
                    outreach_id, "already ran today", last_message_ts, last_message_id,
                    prospect_calendar_url,
                )
            if await run_db(_books_today) >= HOT_LEAD_CLOSER_MAX_BOOKS_PER_DAY:
                return await _not_yet(
                    outreach_id, "daily book cap reached", last_message_ts, last_message_id,
                    prospect_calendar_url,
                )

        contact = await run_db(get_outreach_with_contact, outreach_id) or {}
        campaign_id = str(contact.get("campaign_id") or "")
        from .coordinator import coordinator_blocks_send
        if campaign_id and await run_db(coordinator_blocks_send, campaign_id):
            if mode == "observe":
                return HotLeadCloserOutcome(kind="continue", reason="coordinator hold")
            return await _not_yet(
                outreach_id, "coordinator hold", last_message_ts, last_message_id,
                prospect_calendar_url,
            )
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
            link = (campaign_config or {}).get("booking_link") or prospect_calendar_url or ""
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
        await after_sibling_loop(
            agent="closer",
            campaign_id=campaign_id,
            decision=beat_decision(result.decision),
            reason=result.reason,
            config=campaign_config,
        )

        messages = (await run_db(get_messages_for_outreach, outreach_id) or [])[-12:]
        thread = await run_db(_format_thread, outreach_id)
        evidence = f"{reply_text or ''}\n{thread}"
        email = (result.extras.get("attendee_email") or known_email or "").strip()
        start = (result.extras.get("start_iso") or "").strip()
        duration = _parse_duration(result.extras.get("duration_minutes") or "30")
        decision = result.decision if result.decision in {"book", "hold"} else "hold"
        reason = result.reason or decision

        for step in result.steps:
            await run_db(
                log_action, "hot_lead_closer_step",
                outreach_id=outreach_id,
                result=str(step.get("action") or ""),
                details=step,
            )

        grounded = booking_is_grounded(
            email, start, evidence,
            known_emails=[known_email] if known_email else [],
            messages=messages,
            reply_text=reply_text or "",
        )
        if decision == "book" and not grounded:
            decision = "hold"
            reason = "email or ISO start not in evidence"
        decision_id = await run_db(
            agent_decisions.record_decision, actor="closer", kind=decision, campaign_id=campaign_id,
            outreach_ids=[outreach_id], applied=decision == "book" and mode == "act", numbers=numbers,
        )

        if decision == "book" and mode == "act":
            booked = await _place_booking(
                email, start, duration, contact.get("name") or "", book_fn,
            )
            if _booking_failed(booked):
                await run_db(
                    log_action, "hot_lead_closer_decision",
                    outreach_id=outreach_id,
                    result="hold",
                    details={
                        "reason": booked[:240],
                        "email": email,
                        "start": start,
                        "mode": mode,
                    },
                )
                return await _not_yet(
                    outreach_id, booked[:240], last_message_ts, last_message_id,
                    prospect_calendar_url,
                )
            await run_db(agent_decisions.stamp_rows, [outreach_id], decision_id)
            await run_db(
                log_action, "hot_lead_closer_decision",
                outreach_id=outreach_id,
                result="booked",
                details={"reason": reason, "email": email, "start": start, "mode": mode},
            )
            return HotLeadCloserOutcome(
                kind="booked",
                reason=reason,
                message=booked,
                applied=True,
            )

        await run_db(
            log_action, "hot_lead_closer_decision",
            outreach_id=outreach_id,
            result=decision,
            details={
                "reason": reason,
                "mode": mode,
                "email": email,
                "start": start,
                "observe": mode == "observe",
            },
        )
        if mode == "observe":
            return HotLeadCloserOutcome(
                kind="continue",
                reason=f"observe — {reason}",
                message=f"observe — {reason}",
            )
        return await _not_yet(
            outreach_id, reason, last_message_ts, last_message_id, prospect_calendar_url,
        )
    except Exception as e:
        logger.warning("Hot-lead closer failed: %s", e)
        if hot_lead_closer_mode(campaign_config) == "observe":
            return HotLeadCloserOutcome(
                kind="continue",
                reason=str(e)[:240],
                message=f"observe — closer could not run: {e}",
            )
        try:
            return await _not_yet(
                outreach_id, str(e)[:240], last_message_ts, last_message_id,
                prospect_calendar_url,
            )
        except Exception:
            return HotLeadCloserOutcome(
                kind="not_yet",
                reason=str(e)[:240],
                message="No booking yet — the closer could not run. The conversation continues.",
            )


async def _place_booking(
    email: str,
    start: str,
    duration: int,
    name: str,
    book_fn: Any,
) -> str:
    if book_fn is None:
        from ..tools.book_meeting import run_book_meeting
        book_fn = run_book_meeting
    result = book_fn(
        attendee_email=email,
        start=start,
        duration_minutes=duration,
        summary=f"Intro call with {name}" if name else "",
    )
    if hasattr(result, "__await__"):
        result = await result
    return str(result)


def _booking_failed(result: str) -> bool:
    text = (result or "").strip().lower()
    return text.startswith("could not") or text.startswith("error:")


async def _not_yet(
    outreach_id: str,
    reason: str,
    message_ts: int,
    message_id: str,
    prospect_calendar_url: str,
) -> HotLeadCloserOutcome:
    """"I could not finish MY step this turn" — which is not a hold.

    Until 22 Sep 2026 every one of these wrote ``hold_for_operator`` on the
    row, and reply_to_prospect reads that field before anything else: the
    closer needs a time to book, and the reply that would have ASKED for one
    was the thing the hold silenced. The closer owns the booking step only, so
    it now stands aside and the reply lane keeps the conversation. The field
    is left to work only a person can do (reply_agent).
    """
    return HotLeadCloserOutcome(
        kind="not_yet",
        reason=reason,
        message=f"No booking yet — {reason}. The conversation continues.",
    )


def _format_thread(outreach_id: str) -> str:
    messages = get_messages_for_outreach(outreach_id)[-12:]
    lines = []
    for message in messages:
        role = message.get("role") or "?"
        text = (message.get("text") or "").replace("\n", " ")[:280]
        lines.append(f"{role}: {text}")
    return "\n".join(lines) or "(no messages)"


def _day_start() -> int:
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(now.timestamp())


def _has_closer_today(outreach_id: str) -> bool:
    db = get_db()
    row = db.execute(
        """SELECT 1 FROM actions_log
           WHERE action_type = 'hot_lead_closer_decision'
             AND result = 'booked'
             AND outreach_id = ?
             AND timestamp >= ?
           LIMIT 1""",
        (outreach_id, _day_start()),
    ).fetchone()
    db.close()
    return row is not None


def _books_today() -> int:
    db = get_db()
    row = db.execute(
        """SELECT COUNT(*) AS c FROM actions_log
           WHERE action_type = 'hot_lead_closer_decision'
             AND result = 'booked'
             AND timestamp >= ?""",
        (_day_start(),),
    ).fetchone()
    db.close()
    return int(row["c"] if row else 0)
