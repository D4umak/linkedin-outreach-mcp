"""Shared enqueue gate — do not create a job the executor will immediately defer.

Planner (60s) and the daily strategist (15m) both call ``can_enqueue_outreach``
before writing a scheduler_jobs row. Cap, 24h message gap, and cloud ownership
live here so a second caller cannot bypass the first.
"""

from __future__ import annotations

import time
from typing import Any

from ..constants import (
    JOB_EMAIL_INVITE,
    JOB_ENGAGE,
    JOB_FOLLOW,
    JOB_FOLLOWUP,
    JOB_INMAIL,
    JOB_INVITE,
    JOB_PROFILE_VIEW,
    JOB_SEND_DM,
)

# Shared with generate_send — one day between SDR messages to the same person.
MIN_MESSAGE_GAP_SECONDS = 86400

JOB_TO_CAP_ACTION: dict[str, str] = {
    JOB_INVITE: "invite",
    JOB_SEND_DM: "dm",
    JOB_FOLLOWUP: "followup",
    JOB_INMAIL: "inmail",
    JOB_EMAIL_INVITE: "email",
    JOB_ENGAGE: "engage",
    JOB_PROFILE_VIEW: "profile_view",
    JOB_FOLLOW: "follow",
}

GAP_JOB_TYPES = frozenset({JOB_SEND_DM, JOB_FOLLOWUP})


def last_sdr_message_ts(outreach_id: str) -> int:
    """Latest real chat-DM timestamp for this outreach, or 0.

    Invitation notes are excluded — they are not a conversation message.
    """
    if not outreach_id:
        return 0
    from ..db.queries import last_real_sdr_message_ts

    return last_real_sdr_message_ts(outreach_id)


def message_gap_blocks(outreach_id: str, now: int | None = None) -> tuple[bool, dict[str, Any]]:
    """True when a real chat DM landed inside MIN_MESSAGE_GAP_SECONDS."""
    last_ts = last_sdr_message_ts(outreach_id)
    if last_ts <= 0:
        return False, {}
    now = int(now if now is not None else time.time())
    seconds_since = now - last_ts
    if seconds_since >= MIN_MESSAGE_GAP_SECONDS:
        return False, {}
    return True, {
        "seconds_since": seconds_since,
        "min_gap": MIN_MESSAGE_GAP_SECONDS,
        "last_sdr_ts": last_ts,
    }


async def can_enqueue_outreach(
    job_type: str,
    campaign_id: str = "",
    outreach_id: str = "",
) -> tuple[bool, str, dict[str, Any]]:
    """Whether a send-side job should be created.

    Returns ``(ok, reason, details)``. ``reason`` is empty when ``ok`` is True.
    """
    from ..db.async_bridge import run_db
    from ..services.cloud_sync import cloud_sends_this_job

    if cloud_sends_this_job(job_type, campaign_id or "", outreach_id or ""):
        return False, "cloud_owned", {}

    cap_reason, cap_details = await _cap_blocks(job_type)
    if cap_reason:
        return False, cap_reason, cap_details

    if job_type in GAP_JOB_TYPES and outreach_id:
        blocked, details = await run_db(message_gap_blocks, outreach_id)
        if blocked:
            return False, "message_gap", details

    return True, "", {}


async def _cap_blocks(job_type: str) -> tuple[str, dict[str, Any]]:
    action = JOB_TO_CAP_ACTION.get(job_type)
    if not action:
        return "", {}
    from ..linkedin.rate_limiter import check_daily_cap, get_daily_cap_summary

    can, current, cap, _block = await check_daily_cap(action, reserve=False)
    if not can:
        return "daily_cap", {"current": current, "cap": cap}
    summary = await get_daily_cap_summary()
    remaining = int((summary.get("_total") or {}).get("remaining", 1))
    if remaining <= 0:
        return "total_daily_cap", {
            "current": int((summary.get("_total") or {}).get("current", current)),
            "cap": int((summary.get("_total") or {}).get("cap", cap)),
        }
    return "", {}
