"""Account-wide action health — skip vs send across every campaign.

The planner used to write the same skip_* row every 60s tick. After debounce,
those rows should be one per (campaign, action, reason) until a send lands.
This snapshot is the ongoing check that the firehose stayed off.
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..db.queries import log_action
from ..db.schema import get_db

WINDOW_SECONDS = 86400
ALERT_WINDOW_SECONDS = 7200
SNAPSHOT_ACTION = "action_health_snapshot"
ALERT_ACTION = "action_health_alerted"
SNAPSHOT_MIN_GAP_SECONDS = 50 * 60

SEND_ACTIONS = ("invitation_sent", "dm_sent", "inmail_sent", "email_sent")
SEND_LABELS = {
    "invitation_sent": "invite",
    "dm_sent": "DM",
    "inmail_sent": "InMail",
    "email_sent": "email",
}

_EMPTY_SENDS = {name: 0 for name in SEND_ACTIONS}


@dataclass
class CampaignHealth:
    campaign_id: str
    campaign_name: str
    skip_rows: int = 0
    skip_reasons: int = 0
    sends: dict[str, int] = field(default_factory=lambda: dict(_EMPTY_SENDS))
    top_skip_reasons: list[tuple[str, str, int]] = field(default_factory=list)
    signal_not_activated: int = 0


@dataclass
class ActionHealth:
    now: int
    window_seconds: int
    campaign_id: str = ""
    total: int = 0
    skip_rows: int = 0
    skip_reasons: int = 0
    sends: dict[str, int] = field(default_factory=lambda: dict(_EMPTY_SENDS))
    results: dict[str, int] = field(default_factory=dict)
    signal_not_activated: int = 0
    verdict: str = "healthy"
    campaigns: list[CampaignHealth] = field(default_factory=list)


def summarize_action_health(
    *,
    now: int | None = None,
    window_seconds: int = WINDOW_SECONDS,
    campaign_id: str = "",
) -> ActionHealth:
    """Last-window skip/send mix for the whole account, or one campaign."""
    now = int(now if now is not None else time.time())
    since = now - window_seconds
    names = _campaign_names()

    db = get_db()
    try:
        if campaign_id:
            rows = db.execute(
                """SELECT action_type, result, campaign_id, details_json
                     FROM actions_log
                    WHERE timestamp >= ? AND timestamp <= ?
                      AND campaign_id = ?""",
                (since, now, campaign_id),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT action_type, result, campaign_id, details_json
                     FROM actions_log
                    WHERE timestamp >= ? AND timestamp <= ?""",
                (since, now),
            ).fetchall()
    finally:
        db.close()

    sends = dict(_EMPTY_SENDS)
    results: Counter[str] = Counter()
    skip_keys: set[tuple[str, str, str]] = set()
    skip_reason_counts: Counter[tuple[str, str, str]] = Counter()
    per: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "skip_rows": 0,
        "skip_keys": set(),
        "sends": dict(_EMPTY_SENDS),
        "reason_counts": Counter(),
        "signal_not_activated": 0,
    })
    skip_rows = 0
    signal_not_activated = 0
    total = 0

    for row in rows:
        action_type = row["action_type"] or ""
        if action_type == SNAPSHOT_ACTION:
            continue
        total += 1
        cid = row["campaign_id"] or ""
        result = row["result"] or ""
        if result:
            results[result] += 1
        if action_type in sends:
            sends[action_type] += 1
            per[cid]["sends"][action_type] += 1
        if action_type == "signal_not_activated":
            signal_not_activated += 1
            per[cid]["signal_not_activated"] += 1
        if action_type.startswith("skip_"):
            skip_rows += 1
            reason = _reason(row["details_json"])
            key = (cid, action_type, reason)
            skip_keys.add(key)
            skip_reason_counts[key] += 1
            per[cid]["skip_rows"] += 1
            per[cid]["skip_keys"].add(key)
            per[cid]["reason_counts"][(action_type, reason)] += 1

    campaigns = []
    for cid, stats in per.items():
        if campaign_id and cid != campaign_id:
            continue
        reason_counts: Counter = stats["reason_counts"]
        campaigns.append(CampaignHealth(
            campaign_id=cid,
            campaign_name=names.get(cid) or cid or "(account)",
            skip_rows=stats["skip_rows"],
            skip_reasons=len(stats["skip_keys"]),
            sends=stats["sends"],
            top_skip_reasons=[
                (action, reason, count)
                for (action, reason), count in reason_counts.most_common(5)
            ],
            signal_not_activated=stats["signal_not_activated"],
        ))
    campaigns.sort(key=lambda c: (-c.skip_rows, -sum(c.sends.values()), c.campaign_name))

    skip_reasons = len(skip_keys)
    return ActionHealth(
        now=now,
        window_seconds=window_seconds,
        campaign_id=campaign_id,
        total=total,
        skip_rows=skip_rows,
        skip_reasons=skip_reasons,
        sends=sends,
        results=dict(results),
        signal_not_activated=signal_not_activated,
        verdict=_verdict(skip_rows, skip_reasons),
        campaigns=campaigns,
    )


def format_action_health_lines(health: ActionHealth) -> list[str]:
    """Dashboard / digest lines. Empty health still shows a quiet headline."""
    title = (
        "Action health (last 24h):"
        if health.campaign_id
        else "Action health (last 24h, all campaigns):"
    )
    row_word = "row" if health.skip_rows == 1 else "rows"
    reason_word = "reason" if health.skip_reasons == 1 else "reasons"
    if health.skip_rows == 0:
        skip_line = "Planner skips: none"
    else:
        skip_line = (
            f"Planner skips: {health.skip_rows} {row_word} / "
            f"{health.skip_reasons} {reason_word} — {health.verdict}"
        )
    sends_line = f"Sends: {_format_sends(health.sends)}"

    body = [skip_line, sends_line]
    if health.signal_not_activated:
        body.append(f"Other skips: {health.signal_not_activated} signal_not_activated")
    for camp in health.campaigns[:8]:
        if camp.skip_rows <= 0 and not any(camp.sends.values()):
            continue
        bits: list[str] = []
        if camp.skip_rows:
            skip_word = "skip" if camp.skip_rows == 1 else "skips"
            reasons = ", ".join(
                _skip_label(action, reason)
                for action, reason, _n in camp.top_skip_reasons
            )
            bits.append(f"{camp.skip_rows} {skip_word}" + (f" ({reasons})" if reasons else ""))
        send_txt = _format_sends(camp.sends)
        if send_txt != "none":
            bits.append(send_txt)
        if bits:
            body.append(f"{camp.campaign_name} — {'; '.join(bits)}")

    lines = [title]
    for i, item in enumerate(body):
        prefix = "└──" if i == len(body) - 1 else "├──"
        lines.append(f"{prefix} {item}")
    lines.append("")
    return lines


def action_health_anomalies(*, now: int | None = None) -> list[Any]:
    """Critical anomalies for the last 2 hours only — leftover 24h firehose is ignored."""
    from .anomaly_detector import Anomaly

    health = summarize_action_health(now=now, window_seconds=ALERT_WINDOW_SECONDS)
    if health.verdict != "firehose":
        return []
    top = health.campaigns[0] if health.campaigns else None
    top_name = top.campaign_name if top else "account"
    top_reasons = ", ".join(
        _skip_label(action, reason) for action, reason, _n in (top.top_skip_reasons if top else [])
    )
    where = f"{top_name}" + (f" ({top_reasons})" if top_reasons else "")
    return [Anomaly(
        anomaly_type="action_health_firehose",
        severity="critical",
        summary=(
            f"Planner skip firehose: {health.skip_rows} rows / "
            f"{health.skip_reasons} reasons in 2h — {where}"
        ),
        details={
            "skip_rows": health.skip_rows,
            "skip_reasons": health.skip_reasons,
            "sends": health.sends,
            "campaign_id": top.campaign_id if top else "",
            "campaign_name": top_name,
        },
        detected_at=health.now,
    )]


def was_action_health_alerted(since: int, anomaly_type: str = "action_health_firehose") -> bool:
    """True if we already emailed this anomaly type after ``since``."""
    db = get_db()
    try:
        rows = db.execute(
            """SELECT details_json FROM actions_log
               WHERE action_type = ? AND result = 'sent' AND timestamp >= ?""",
            (ALERT_ACTION, since),
        ).fetchall()
    finally:
        db.close()
    for row in rows:
        try:
            details = json.loads(row["details_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(details, dict) and details.get("anomaly_type") == anomaly_type:
            return True
    return False


def mark_action_health_alerted(anomaly: Any, *, now: int | None = None) -> None:
    """Record that the firehose email was accepted by the backend."""
    details = dict(getattr(anomaly, "details", None) or {})
    log_action(
        ALERT_ACTION,
        result="sent",
        details={
            "anomaly_type": getattr(anomaly, "anomaly_type", "action_health_firehose"),
            "summary": getattr(anomaly, "summary", ""),
            "skip_rows": details.get("skip_rows"),
            "skip_reasons": details.get("skip_reasons"),
            "campaign_name": details.get("campaign_name", ""),
        },
        timestamp=now,
    )


def persist_action_health_snapshot(
    health: ActionHealth | None = None,
    *,
    now: int | None = None,
) -> bool:
    """Write at most one snapshot per hour. Returns True when a row was added."""
    now = int(now if now is not None else time.time())
    last = _last_snapshot_ts()
    if last and now - last < SNAPSHOT_MIN_GAP_SECONDS:
        return False
    if health is None:
        health = summarize_action_health(now=now)
    log_action(
        SNAPSHOT_ACTION,
        result=health.verdict,
        details={
            "window_seconds": health.window_seconds,
            "total": health.total,
            "skip_rows": health.skip_rows,
            "skip_reasons": health.skip_reasons,
            "sends": health.sends,
            "results": health.results,
            "signal_not_activated": health.signal_not_activated,
            "verdict": health.verdict,
            "campaigns": [
                {
                    "id": c.campaign_id,
                    "name": c.campaign_name,
                    "skip_rows": c.skip_rows,
                    "skip_reasons": c.skip_reasons,
                    "sends": c.sends,
                }
                for c in health.campaigns
            ],
        },
        timestamp=now,
    )
    return True


def _verdict(skip_rows: int, skip_reasons: int) -> str:
    if skip_rows == 0 or skip_rows <= skip_reasons:
        return "healthy"
    if skip_rows >= 50 and skip_rows > skip_reasons * 5:
        return "firehose"
    return "repeating"


def _skip_label(action: str, reason: str) -> str:
    short = action.removeprefix("skip_")
    return f"{short}:{reason}" if reason else short


def _format_sends(sends: dict[str, int]) -> str:
    parts = [
        f"{sends[name]} {label}"
        for name, label in SEND_LABELS.items()
        if sends.get(name)
    ]
    return ", ".join(parts) if parts else "none"


def _reason(details_json: str | None) -> str:
    try:
        details = json.loads(details_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(details, dict):
        return ""
    return str(details.get("reason") or details.get("skip_reason") or "")


def _campaign_names() -> dict[str, str]:
    db = get_db()
    try:
        rows = db.execute("SELECT id, name FROM campaigns").fetchall()
    finally:
        db.close()
    return {row["id"]: (row["name"] or row["id"]) for row in rows}


def _last_snapshot_ts() -> int:
    db = get_db()
    try:
        row = db.execute(
            "SELECT MAX(timestamp) AS ts FROM actions_log WHERE action_type = ?",
            (SNAPSHOT_ACTION,),
        ).fetchone()
    finally:
        db.close()
    return int(row["ts"] or 0) if row and row["ts"] else 0
