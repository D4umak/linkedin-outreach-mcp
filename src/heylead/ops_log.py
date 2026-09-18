"""High-signal operational events for outreach debugging.

File logs used to drop identity fields: ``_JsonFormatter`` only copied a
handful of scheduler extras, and nobody passed ``extra=``. MCP tool calls
also never set a correlation id. Every P0 debug event goes through
``log_event`` so the JSON allowlist and the call sites stay in lockstep.

Do not put full message bodies in these events — hash them.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from .correlation import clear_correlation_id, get_correlation_id, new_correlation_id

logger = logging.getLogger("heylead.ops")

# Copied onto every JSON log line when present on the LogRecord.
JSON_LOG_EXTRA_FIELDS: tuple[str, ...] = (
    "job_id",
    "campaign_id",
    "correlation_id",
    "error_category",
    "duration_ms",
    "tick",
    "job_type",
    "event",
    "outreach_id",
    "prospect_id",
    "linkedin_id",
    "skip_reason",
    "channel",
    "request_id",
    "tool",
    "action",
    "outcome",
    "source",
    "owner",
    "error_type",
    "classifier",
    "sentiment",
    "text_hash",
    "predicates",
    "http_status",
    "step_index",
    "message_len",
    "unipile_id",
    "verified",
    "retry",
    "success",
    "prompt_name",
    "plan_id",
    "chat_id",
    "sample_rate",
    "signal_id",
    "reason",
    "message_id",
)

# Stdlib LogRecord attributes. Passing these as extra= raises KeyError.
_RESERVED_LOG_RECORD_KEYS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
}

_GREP_KEYS = (
    "channel",
    "skip_reason",
    "tool",
    "outcome",
    "owner",
    "source",
    "sentiment",
    "classifier",
    "http_status",
    "success",
    "step_index",
    "outreach_id",
    "campaign_id",
    "signal_id",
    "linkedin_id",
    "reason",
    "message_id",
)

_PROFILE_FIELD_KEYS = (
    "headline",
    "title",
    "company",
    "provider_id",
    "is_open_profile",
    "public_id",
    "name",
)


def message_hash(text: str | None) -> str:
    """Short, stable hash of message text. Empty input → empty hash."""
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def present_fields(data: Mapping[str, Any] | None, keys: Sequence[str] | None = None) -> list[str]:
    """Names of keys that have a real value — for enrichment completeness."""
    if not data:
        return []
    wanted = keys or _PROFILE_FIELD_KEYS
    present: list[str] = []
    for key in wanted:
        value = data.get(key)
        if value in (None, "", [], {}):
            continue
        present.append(key)
    return present


def classify_unipile_error_type(error: str | None) -> str:
    """Short code for outbound_send error_type — never the raw Unipile JSON."""
    text = str(error or "")
    lower = text.lower()
    if (
        "no_connection_with_recipient" in lower
        or "cannot be reached" in lower
        or "does not allow incoming" in lower
        or "not to be first degree" in lower
    ):
        return "unipile_422_no_connection"
    if "422" in text:
        return "unipile_422"
    if "429" in text or "rate limit" in lower:
        return "unipile_429"
    if "402" in text or "no inmail credits" in lower:
        return "unipile_402"
    return ""


def classify_result(result: Any) -> tuple[str, str | None]:
    """Map a tool/job return value to (outcome, skip_reason).

    ``JobResult.outcome`` wins. String markers are the MCP contract — tools
    return emoji lines instead of raising. Never copy the human message into
    skip_reason (prospect names leak).
    """
    if hasattr(result, "outcome") and result.outcome:
        return str(result.outcome), None
    if isinstance(result, (list, tuple)):
        # Status tools return [text, Image]; classify on the text block.
        for item in result:
            if isinstance(item, str):
                result = item
                break
            if isinstance(getattr(item, "text", None), str):
                result = item.text
                break
        else:
            result = ""
    text = str(result or "")
    stripped = text.lstrip()
    if stripped.startswith("⏭️") or stripped.startswith("ℹ️"):
        return "skipped", "skipped"
    if "excluded" in text.lower():
        return "skipped", "excluded"
    if "already replied" in text.lower():
        return "skipped", "already_replied"
    if stripped.startswith("❌") or stripped.startswith("Error:"):
        return "error", None
    if stripped.startswith("🔑"):
        return "deferred", None
    return "success", None


async def run_traced(tool: str, coro, **ids: Any):
    """Await a tool coroutine, log start/end with an outcome from the result."""
    existing = get_correlation_id()
    owned = not existing
    cid = existing or new_correlation_id()
    started = time.monotonic()
    log_event("tool_start", tool=tool, request_id=cid, **ids)
    try:
        result = await coro
    except Exception as exc:
        log_event(
            "tool_end",
            tool=tool,
            request_id=cid,
            outcome="error",
            error_type=type(exc).__name__,
            duration_ms=int((time.monotonic() - started) * 1000),
            **ids,
        )
        raise
    else:
        outcome, skip = classify_result(result)
        log_event(
            "tool_end",
            tool=tool,
            request_id=cid,
            outcome=outcome,
            skip_reason=skip,
            duration_ms=int((time.monotonic() - started) * 1000),
            **ids,
        )
        return result
    finally:
        if owned:
            clear_correlation_id()


def log_event(event: str, *, log: logging.Logger | None = None, **fields: Any) -> None:
    """Info-level event with structured extras (and a grep-friendly message)."""
    extra: dict[str, Any] = {"event": event}
    for key, value in fields.items():
        if value is None or key in _RESERVED_LOG_RECORD_KEYS:
            continue
        extra[key] = value
    parts = [event]
    for key in _GREP_KEYS:
        if key in extra:
            parts.append(f"{key}={extra[key]}")
    (log or logger).info(" ".join(parts), extra=extra)


@asynccontextmanager
async def tool_span(tool: str, **ids: Any):
    """Correlate one MCP tool call: set CID if missing, log start/end."""
    existing = get_correlation_id()
    owned = not existing
    cid = existing or new_correlation_id()
    started = time.monotonic()
    log_event("tool_start", tool=tool, request_id=cid, **ids)
    try:
        yield cid
    except Exception as exc:
        log_event(
            "tool_end",
            tool=tool,
            request_id=cid,
            outcome="error",
            error_type=type(exc).__name__,
            duration_ms=int((time.monotonic() - started) * 1000),
            **ids,
        )
        raise
    else:
        log_event(
            "tool_end",
            tool=tool,
            request_id=cid,
            outcome="success",
            duration_ms=int((time.monotonic() - started) * 1000),
            **ids,
        )
    finally:
        if owned:
            clear_correlation_id()


def record_inbound_enrol_failure(
    *,
    campaign_id: str = "",
    source: str = "",
    exc: BaseException | None = None,
) -> None:
    """Enrol failed — usually a missing campaign/contact FK. No prospect names."""
    err = str(exc or "")
    reason = "fk_missing_parent" if "FOREIGN KEY" in err.upper() else "enrol_failed"
    log_event(
        "enrol_rejected",
        campaign_id=campaign_id,
        source=source,
        reason=reason,
        skip_reason=reason,
    )
    logger.warning("Failed to create outreach for inbound: %s", reason)


async def record_skip_excluded(
    *,
    outreach_id: str = "",
    campaign_id: str = "",
    contact_id: str = "",
    reason: str = "do_not_automate_or_do_not_contact",
) -> None:
    log_event(
        "skip_excluded",
        outreach_id=outreach_id,
        campaign_id=campaign_id,
        prospect_id=contact_id,
        skip_reason=reason,
    )
    from .db import aio as adb

    await adb.log_action(
        "skip_excluded",
        outreach_id=outreach_id,
        campaign_id=campaign_id,
        result="skipped",
        details={"contact_id": contact_id, "reason": reason},
    )


async def record_channel_skip(
    action_type: str,
    *,
    outreach_id: str = "",
    campaign_id: str = "",
    skip_reason: str,
    details: dict[str, Any] | None = None,
) -> None:
    """InMail / email skip that must show up in actions_log."""
    log_event(
        action_type,
        outreach_id=outreach_id,
        campaign_id=campaign_id,
        skip_reason=skip_reason,
    )
    from .db import aio as adb

    payload = {"skip_reason": skip_reason}
    if details:
        payload.update(details)
    await adb.log_action(
        action_type,
        outreach_id=outreach_id,
        campaign_id=campaign_id,
        result="skipped",
        details=payload,
    )


def log_outbound_send(
    phase: str,
    *,
    outreach_id: str = "",
    campaign_id: str = "",
    channel: str = "",
    step_index: str = "",
    text: str | None = None,
    provider_id: str = "",
    chat_id: str = "",
    prompt_name: str = "",
    success: bool | None = None,
    http_status: Any = None,
    unipile_id: str = "",
    verified: bool | None = None,
    retry: str = "",
    error_type: str = "",
    msg_format: str = "",
) -> None:
    """Attempt or result around a LinkedIn/Unipile send. Never logs the body."""
    event = f"outbound_send_{phase}"
    log_event(
        event,
        outreach_id=outreach_id,
        campaign_id=campaign_id,
        channel=channel,
        step_index=step_index,
        text_hash=message_hash(text) if text is not None else None,
        message_len=len(text) if text is not None else None,
        linkedin_id=provider_id or None,
        prompt_name=prompt_name or None,
        success=success,
        http_status=http_status,
        unipile_id=unipile_id or None,
        verified=verified,
        retry=retry or None,
        error_type=error_type or None,
        source=msg_format or None,
        **({"chat_id": chat_id} if chat_id else {}),
    )


FIT_SAMPLE_RATE = 0.1


def should_sample_kept(linkedin_id: str, sample_rate: float = FIT_SAMPLE_RATE) -> bool:
    """Deterministic sample: same linkedin_id always keeps or drops at a given rate."""
    if not linkedin_id or sample_rate <= 0:
        return False
    if sample_rate >= 1:
        return True
    digest = int(hashlib.sha256(linkedin_id.encode("utf-8")).hexdigest()[:8], 16)
    return digest % 10_000 < int(round(sample_rate * 10_000))


def log_prospects_below_threshold(
    prospects: Sequence[Mapping[str, Any]],
    *,
    threshold: float,
    sample_rate: float = FIT_SAMPLE_RATE,
) -> None:
    """Summary of the fit gate, plus a sample of dropped prospects."""
    dropped: list[tuple[str, float]] = []
    kept = 0
    for prospect in prospects:
        score = prospect.get("fit_score") or 0
        try:
            score_f = float(score)
        except (TypeError, ValueError):
            score_f = 0.0
        if score_f >= threshold:
            kept += 1
            continue
        dropped.append((str(prospect.get("linkedin_id") or ""), score_f))
    log_event(
        "prospects_scored_summary",
        predicates={"dropped": len(dropped), "kept": kept, "threshold": threshold},
    )
    for linkedin_id, score_f in dropped:
        if not should_sample_kept(linkedin_id, sample_rate):
            continue
        log_event(
            "prospect_scored",
            linkedin_id=linkedin_id,
            outcome="dropped",
            skip_reason="below_threshold",
            predicates={"fit_score": score_f, "threshold": threshold},
            sample_rate=sample_rate,
        )


def log_prospects_sampled(
    prospects: Sequence[Mapping[str, Any]],
    *,
    threshold: float,
    sample_rate: float = FIT_SAMPLE_RATE,
) -> None:
    """Info line for a sample of kept prospects — scores without flooding."""
    for prospect in prospects:
        score = prospect.get("fit_score") or 0
        try:
            score_f = float(score)
        except (TypeError, ValueError):
            score_f = 0.0
        if score_f < threshold:
            continue
        linkedin_id = prospect.get("linkedin_id") or ""
        if not should_sample_kept(linkedin_id, sample_rate):
            continue
        log_event(
            "prospect_scored",
            linkedin_id=linkedin_id,
            outcome="kept",
            predicates={"fit_score": score_f, "threshold": threshold},
            sample_rate=sample_rate,
        )


def log_signal_not_activated(
    *,
    signal_id: str = "",
    linkedin_id: str = "",
    campaign_id: str = "",
    outreach_id: str = "",
    skip_reason: str,
    score: float | None = None,
    signal_type: str = "",
    signal_types: Sequence[str] | None = None,
) -> None:
    """Buying signal seen but not turned into outreach."""
    types = list(signal_types) if signal_types is not None else (
        [signal_type] if signal_type else []
    )
    preds: dict[str, Any] = {}
    if score is not None:
        preds["score"] = score
    if types:
        preds["signal_types"] = types
    log_event(
        "signal_not_activated",
        signal_id=signal_id or None,
        linkedin_id=linkedin_id or None,
        campaign_id=campaign_id or None,
        outreach_id=outreach_id or None,
        skip_reason=skip_reason,
        predicates=preds or None,
    )
