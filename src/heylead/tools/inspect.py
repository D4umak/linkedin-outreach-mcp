"""Tool: inspect — read-only digest of in-process agent ops.

Reads operator holds through services.waiting_on_you (the one reader of who
is waiting) and ``actions_log`` decisions. Never writes, never calls
LinkedIn, never calls an LLM.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from ..config import get_scheduler_mode, get_sending_host
from ..db.async_bridge import run_db
from ..db.schema import get_db
from ..formatter import prospect_link
from ..services.agent_commons import (
    beat_is_stale,
    get_digest,
    get_hold,
    list_beats,
    list_coordinator_holds,
    list_live_notes,
)
from ..services.coordinator import coordinator_mode
from ..services.hot_lead_closer import hot_lead_closer_mode
from ..services.icp_research import icp_research_mode
from ..services.product_agent import product_agent_mode
from ..services.reply_agent import reply_agent_mode
from ..services.waiting_on_you import HOLD, Waiting, render_waiting, who_is_waiting
from ..services.strategist_replan import strategist_replan_mode
from ..db.queries import get_campaign, get_setting

logger = logging.getLogger(__name__)

VALID_ACTIONS = (
    "agents", "holds", "replans", "closer", "skips", "jobs", "commons", "journal", "review",
    "waiting",
)

_PLAN_SKIP_REASONS = frozenset({
    "gated_create_refused",
    "cloud_owned",
    "message_gap",
    "daily_cap",
    "queue_full",
    "total_daily_cap",
})

_SKIP_TYPES = (
    "reply_cap_reached",
    "reply_dedup_blocked",
    "reply_dedup_blocked_chat_scoped",
    # Written until 19 Sep 2026; kept so older rows still show.
    "auto_reply_skipped_negative",
    "auto_reply_skipped_decline_keywords",
    "reply_left_to_cloud_decline",
    "reverse_pitch_keyword",
    "reverse_pitch_detected",
)

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


async def _review_from_host(campaign_id: str, limit: int) -> str:
    """Hosted campaign-review digest. Self-hosted uses local inspect jobs."""
    from ..config import is_backend_mode
    from ..services.cloud_sync import BackendAuthError, get_hosted_json

    if not is_backend_mode():
        return (
            "Campaign review is the hosted watch that adjusts in-window stalls. "
            "This machine is self-hosted — use inspect() and scheduler(action='status')."
        )
    params: dict[str, Any] = {"action": "review"}
    if campaign_id:
        params["campaign_id"] = campaign_id
    try:
        cap = int(limit or _DEFAULT_LIMIT)
    except (TypeError, ValueError):
        cap = _DEFAULT_LIMIT
    params["limit"] = max(1, min(cap, _MAX_LIMIT))
    try:
        data = await get_hosted_json("/api/v1/scheduler/inspect", params=params)
    except BackendAuthError:
        return (
            "Hosted inspect needs a valid HeyLead token. "
            "Paste the token message and call setup_profile(backend_jwt='...')."
        )
    except Exception as exc:
        return f"Inspect review failed: {exc}"
    return str(data.get("text") or "Campaign review · last 24h · 0 stalls")


_KIND_LABELS = {"dm": "opening message", "followup": "follow-up"}


def _waiting_line(i: int, draft: dict[str, Any]) -> list[str]:
    """One held message: who, what kind, which campaign, and the text."""
    from ..formatter import person_line

    slug = str(draft.get("linkedin_id") or "").strip()
    # A provider id (ACo..., AEm...) is not a profile slug; only a slug links.
    url = f"https://www.linkedin.com/in/{slug}" if slug and not slug.startswith(("ACo", "AEm")) else ""
    kind = _KIND_LABELS.get(str(draft.get("kind") or ""), str(draft.get("kind") or "message"))
    n = int(draft.get("followup_number") or 0)
    if draft.get("kind") == "followup" and n:
        kind = f"follow-up {n}"
    who = person_line(str(draft.get("name") or "Unknown"), url,
                      title=str(draft.get("title") or ""), company=str(draft.get("company") or ""))
    campaign = str(draft.get("campaign_id") or "")[:8]
    text = " ".join(str(draft.get("text") or "").split())
    return [f"{i}. {who} · {kind} · campaign {campaign}", f"   {text}"]


async def _waiting(campaign_id: str, limit: int) -> str:
    """Who is waiting on you, then the messages waiting for your approval.

    The people come from services.waiting_on_you, the reader Needs attention,
    inspect(action='holds') and check_replies share; until 25 Sep 2026 this
    action listed drafts only and named nobody on a self-hosted install.
    """
    try:
        cap = int(limit or _DEFAULT_LIMIT)
    except (TypeError, ValueError):
        cap = _DEFAULT_LIMIT
    cap = max(1, min(cap, _MAX_LIMIT))
    try:
        people = await run_db(
            who_is_waiting, campaign_id=(campaign_id or "").strip(), limit=cap,
        )
        head = render_waiting(people)
    except Exception as exc:
        logger.warning("inspect waiting: people failed: %s", exc)
        head = f"Who is waiting could not be read: {exc}"
    return head + "\n\n" + await _waiting_from_host(campaign_id, cap)


async def _waiting_from_host(campaign_id: str, cap: int) -> str:
    """Opening messages and follow-ups held for approval (hosted).

    The same list the dashboard's Approvals page reads
    (GET /api/v1/scheduler/outreach-drafts). The server's instructions
    advertised inspect(action='waiting') before it existed (24 Sep 2026).
    """
    from ..config import is_backend_mode
    from ..services.cloud_sync import BackendAuthError, get_hosted_json

    if not is_backend_mode():
        return (
            "Nothing waits for approval on a self-hosted install: messages are "
            "sent as written."
        )
    try:
        data = await get_hosted_json(
            "/api/v1/scheduler/outreach-drafts", params={"limit": _MAX_LIMIT},
        )
    except BackendAuthError:
        return (
            "Hosted inspect needs a valid HeyLead token. "
            "Paste the token message and call setup_profile(backend_jwt='...')."
        )
    except Exception as exc:
        return f"Inspect waiting failed: {exc}"
    drafts = [d for d in (data.get("drafts") or []) if isinstance(d, dict)]
    cid = (campaign_id or "").strip()
    if cid:
        drafts = [d for d in drafts if str(d.get("campaign_id") or "").startswith(cid)]
    mode = str(data.get("mode") or "")
    try:
        from ..dashboard_links import dashboard_url

        where = f"Approve, edit or discard them on the Approvals page: {dashboard_url('approvals')}"
    except Exception:
        where = "Approve, edit or discard them on the dashboard's Approvals page."
    if not drafts:
        if mode == "autopilot":
            return ("Nothing is waiting: this workspace is on autopilot, so opening "
                    "messages and follow-ups are sent inside your window without review.")
        return "Nothing is waiting for your approval."
    lines = [f"Waiting for your approval: {len(drafts)}"]
    for i, draft in enumerate(drafts[:cap], start=1):
        lines.extend(_waiting_line(i, draft))
    if len(drafts) > cap:
        lines.append(f"... and {len(drafts) - cap} more")
    lines.extend(["", "Nothing here has been sent. " + where])
    return "\n".join(lines)


async def _journal_from_host(
    campaign_id: str, outreach_id: str, limit: int,
) -> str:
    """Hosted agent diary. Self-hosted has no agent_journal table."""
    from ..config import is_backend_mode
    from ..services.cloud_sync import BackendAuthError, get_hosted_json

    if not is_backend_mode():
        return (
            "Agent journal is the hosted diary of cloud agents. "
            "This machine is self-hosted — use inspect() for local holds, "
            "or inspect(action='commons')."
        )
    params: dict[str, Any] = {"action": "journal"}
    if campaign_id:
        params["campaign_id"] = campaign_id
    if outreach_id:
        params["outreach_id"] = outreach_id
    try:
        cap = int(limit or _DEFAULT_LIMIT)
    except (TypeError, ValueError):
        cap = _DEFAULT_LIMIT
    params["limit"] = max(1, min(cap, _MAX_LIMIT))
    try:
        data = await get_hosted_json("/api/v1/scheduler/inspect", params=params)
    except BackendAuthError:
        return (
            "Hosted inspect needs a valid HeyLead token. "
            "Paste the token message and call setup_profile(backend_jwt='...')."
        )
    except Exception as exc:
        return f"Inspect journal failed: {exc}"
    return str(data.get("text") or "Agent journal · last 7d · needs you: 0")


async def run_inspect(
    action: str = "agents",
    campaign_id: str = "",
    outreach_id: str = "",
    limit: int = _DEFAULT_LIMIT,
) -> str:
    """Dispatch a read-only inspect action."""
    action = (action or "agents").strip().lower()
    if action == "journal":
        return await _journal_from_host(campaign_id, outreach_id, limit)
    if action == "review":
        return await _review_from_host(campaign_id, limit)
    if action == "waiting":
        return await _waiting(campaign_id, limit)
    if action not in VALID_ACTIONS:
        return (
            f"Unknown inspect action: '{action}'.\n\n"
            f"inspect() supports: {', '.join(VALID_ACTIONS)}"
        )
    try:
        cap = int(limit or _DEFAULT_LIMIT)
    except (TypeError, ValueError):
        cap = _DEFAULT_LIMIT
    cap = max(1, min(cap, _MAX_LIMIT))
    try:
        return await run_db(
            _render, action, (campaign_id or "").strip(),
            (outreach_id or "").strip(), cap,
        )
    except Exception as exc:
        logger.warning("inspect failed: %s", exc)
        return f"Inspect failed: {exc}"


def _utc_day_start() -> int:
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(now.timestamp())


def _scope_sql(campaign_col: str, outreach_col: str, campaign_id: str, outreach_id: str,
               params: list[Any]) -> str:
    parts: list[str] = []
    if campaign_id:
        parts.append(f"({campaign_col} = ? OR {campaign_col} LIKE ?)")
        params.extend([campaign_id, f"{campaign_id}%"])
    if outreach_id:
        parts.append(f"({outreach_col} = ? OR {outreach_col} LIKE ?)")
        params.extend([outreach_id, f"{outreach_id}%"])
    return (" AND " + " AND ".join(parts)) if parts else ""


def _details(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _name_of(row: dict[str, Any]) -> str:
    """The person as a LinkedIn link when the row carries the URL."""
    name = (row.get("name") or "Unknown").strip() or "Unknown"
    return prospect_link(name, (row.get("linkedin_url") or "").strip())


def _campaign_of(row: dict[str, Any]) -> str:
    return (row.get("campaign_name") or "").strip()


def _short_id(row: dict[str, Any]) -> str:
    return str(row.get("outreach_id") or "")[:8]


def _remaining_labels(details: dict[str, Any]) -> str:
    remaining = details.get("remaining") or []
    if not isinstance(remaining, list):
        return ""
    labels: list[str] = []
    for item in remaining:
        if isinstance(item, dict) and item.get("action_type"):
            labels.append(str(item["action_type"]))
    return ", ".join(labels)


def _list_holds(campaign_id: str, outreach_id: str, limit: int) -> list[Waiting]:
    """The hold subset of who is waiting (services.waiting_on_you)."""
    return who_is_waiting(
        kinds=(HOLD,), campaign_id=campaign_id, outreach_id=outreach_id, limit=limit,
    )


def _list_log_rows(
    action_types: tuple[str, ...],
    *,
    campaign_id: str,
    outreach_id: str,
    since: int,
    limit: int,
    extra_where: str = "",
    extra_params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    db = get_db()
    params: list[Any] = [since, *action_types, *extra_params]
    placeholders = ",".join("?" * len(action_types))
    scope = _scope_sql(
        "COALESCE(al.campaign_id, o.campaign_id)",
        "al.outreach_id",
        campaign_id,
        outreach_id,
        params,
    )
    rows = db.execute(
        f"""SELECT al.timestamp, al.action_type, al.result, al.details_json,
                   al.outreach_id, al.campaign_id,
                   c.name, c.linkedin_url, ca.name AS campaign_name
            FROM actions_log al
            LEFT JOIN outreaches o ON o.id = al.outreach_id
            LEFT JOIN contacts c ON c.id = o.contact_id
            LEFT JOIN campaigns ca ON ca.id = COALESCE(al.campaign_id, o.campaign_id)
            WHERE al.timestamp >= ?
              AND al.action_type IN ({placeholders})
              {extra_where}
              {scope}
            ORDER BY al.timestamp DESC
            LIMIT ?""",
        [*params, limit],
    ).fetchall()
    db.close()
    return [dict(row) for row in rows]


def _list_replans(campaign_id: str, outreach_id: str, limit: int) -> list[dict[str, Any]]:
    rows = _list_log_rows(
        ("strategist_replan_decision",),
        campaign_id=campaign_id,
        outreach_id=outreach_id,
        since=_utc_day_start(),
        limit=limit,
    )
    if not rows:
        return rows
    ids = [r["outreach_id"] for r in rows if r.get("outreach_id")]
    steps = _latest_steps(ids, "strategist_replan_step")
    for row in rows:
        row["last_step"] = steps.get(row.get("outreach_id") or "")
    return rows


def _list_closer(campaign_id: str, outreach_id: str, limit: int) -> list[dict[str, Any]]:
    return _list_log_rows(
        ("hot_lead_closer_decision",),
        campaign_id=campaign_id,
        outreach_id=outreach_id,
        since=_utc_day_start(),
        limit=limit,
    )


def _mode_lines() -> list[str]:
    mode = get_scheduler_mode()
    host = ""
    try:
        host = (get_sending_host() or "").strip().lower()
    except Exception:
        host = ""
    bits = [f"Mode: {mode}"]
    if host == "cloud":
        bits.append("cloud owns sending")
    elif host == "local":
        bits.append("sending from this machine")
    lines = [" · ".join(bits)]
    if mode == "observe":
        lines.append("Observe is why send jobs are not queued.")
    return lines


def _list_pending_jobs(campaign_id: str, outreach_id: str, limit: int) -> list[dict[str, Any]]:
    db = get_db()
    params: list[Any] = []
    scope = _scope_sql("sj.campaign_id", "sj.outreach_id", campaign_id, outreach_id, params)
    rows = db.execute(
        f"""SELECT sj.id, sj.job_type, sj.status, sj.scheduled_at,
                   sj.outreach_id, sj.campaign_id,
                   c.name, c.linkedin_url, ca.name AS campaign_name
            FROM scheduler_jobs sj
            LEFT JOIN outreaches o ON o.id = sj.outreach_id
            LEFT JOIN contacts c ON c.id = o.contact_id
            LEFT JOIN campaigns ca ON ca.id = sj.campaign_id
            WHERE sj.status IN ('pending', 'running')
              {scope}
            ORDER BY sj.scheduled_at ASC
            LIMIT ?""",
        [*params, limit],
    ).fetchall()
    db.close()
    return [dict(row) for row in rows]


def _list_skip_log(campaign_id: str, outreach_id: str, limit: int) -> list[dict[str, Any]]:
    since = int(datetime.now(timezone.utc).timestamp()) - 86400
    db = get_db()
    params: list[Any] = [since]
    scope = _scope_sql(
        "COALESCE(al.campaign_id, o.campaign_id)",
        "al.outreach_id",
        campaign_id,
        outreach_id,
        params,
    )
    rows = db.execute(
        f"""SELECT al.timestamp, al.action_type, al.result, al.details_json,
                   al.outreach_id, al.campaign_id,
                   c.name, c.linkedin_url, ca.name AS campaign_name
            FROM actions_log al
            LEFT JOIN outreaches o ON o.id = al.outreach_id
            LEFT JOIN contacts c ON c.id = o.contact_id
            LEFT JOIN campaigns ca ON ca.id = COALESCE(al.campaign_id, o.campaign_id)
            WHERE al.timestamp >= ?
              AND al.action_type LIKE 'skip_%'
              {scope}
            ORDER BY al.timestamp DESC
            LIMIT ?""",
        [*params, limit],
    ).fetchall()
    db.close()
    return [dict(row) for row in rows]


def _list_plan_skips(campaign_id: str, outreach_id: str, limit: int) -> list[dict[str, Any]]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db = get_db()
    params: list[Any] = [today]
    scope = _scope_sql("p.campaign_id", "p.outreach_id", campaign_id, outreach_id, params)
    rows = db.execute(
        f"""SELECT p.outreach_id, p.campaign_id, p.executed_actions,
                   c.name, c.linkedin_url, ca.name AS campaign_name
            FROM prospect_daily_plans p
            LEFT JOIN outreaches o ON o.id = p.outreach_id
            LEFT JOIN contacts c ON c.id = o.contact_id
            LEFT JOIN campaigns ca ON ca.id = p.campaign_id
            WHERE p.plan_date = ?
              {scope}""",
        params,
    ).fetchall()
    db.close()
    found: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            executed = json.loads(item.get("executed_actions") or "[]")
        except (json.JSONDecodeError, TypeError):
            executed = []
        if not isinstance(executed, list):
            continue
        for entry in executed:
            if not isinstance(entry, dict):
                continue
            if (entry.get("status") or "") != "skipped":
                continue
            reason = str(entry.get("skip_reason") or "")
            if reason not in _PLAN_SKIP_REASONS:
                continue
            found.append({
                **item,
                "action_type": entry.get("action_type") or "plan",
                "result": "skipped",
                "details_json": json.dumps({"reason": reason}),
                "timestamp": int(entry.get("skipped_at") or 0),
            })
            if len(found) >= limit:
                return found
    return found


def _list_job_refusals(campaign_id: str, outreach_id: str, limit: int) -> list[dict[str, Any]]:
    merged = _list_skip_log(campaign_id, outreach_id, limit) + _list_plan_skips(
        campaign_id, outreach_id, limit,
    )
    merged.sort(key=lambda r: int(r.get("timestamp") or 0), reverse=True)
    return merged[:limit]


def _list_skips(campaign_id: str, outreach_id: str, limit: int) -> list[dict[str, Any]]:
    since = int(datetime.now(timezone.utc).timestamp()) - 86400
    typed = _list_log_rows(
        _SKIP_TYPES,
        campaign_id=campaign_id,
        outreach_id=outreach_id,
        since=since,
        limit=limit,
    )
    decisions = _list_log_rows(
        ("reply_agent_decision",),
        campaign_id=campaign_id,
        outreach_id=outreach_id,
        since=since,
        limit=limit,
        extra_where="AND al.result = 'skip'",
    )
    scheduler = _list_log_rows(
        ("auto_reply_sent",),
        campaign_id=campaign_id,
        outreach_id=outreach_id,
        since=since,
        limit=limit,
        extra_where="AND al.result = 'skipped'",
    )
    merged = typed + decisions + scheduler
    merged.sort(key=lambda r: int(r.get("timestamp") or 0), reverse=True)
    return merged[:limit]


def _latest_steps(outreach_ids: list[str], action_type: str) -> dict[str, dict[str, Any]]:
    if not outreach_ids:
        return {}
    db = get_db()
    placeholders = ",".join("?" * len(outreach_ids))
    rows = db.execute(
        f"""SELECT al.outreach_id, al.result, al.details_json, al.timestamp
            FROM actions_log al
            WHERE al.action_type = ?
              AND al.outreach_id IN ({placeholders})
              AND al.timestamp >= ?
            ORDER BY al.timestamp DESC""",
        [action_type, *outreach_ids, _utc_day_start()],
    ).fetchall()
    db.close()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        oid = row["outreach_id"]
        if oid and oid not in latest:
            latest[oid] = dict(row)
    return latest


def _format_holds(holds: list[Waiting]) -> list[str]:
    if not holds:
        return ["No operator holds."]
    lines = [f"Operator holds ({len(holds)}):", ""]
    for hold in holds:
        who = prospect_link(hold.name or "Unknown", hold.linkedin_url)
        camp = f" · {hold.campaign_name}" if hold.campaign_name else ""
        lines.append(f"• **{who}** — {hold.held_because or 'needs a human'}")
        lines.append(f"  outreach `{hold.outreach_id[:8]}`{camp}")
        lines.append("")
    return lines


def _format_replans(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["No strategist replans today."]
    lines = [f"Strategist replans today ({len(rows)}):", ""]
    for row in rows:
        details = _details(row.get("details_json"))
        decision = (row.get("result") or "keep").strip()
        mode = str(details.get("mode") or "")
        if decision == "revise" and mode == "act":
            applied = "applied"
        elif decision == "revise":
            applied = "observe"
        else:
            applied = mode or "logged"
        reason = (details.get("reason") or "").strip()
        leftover = _remaining_labels(details)
        campaign = _campaign_of(row)
        camp = f" · {campaign}" if campaign else ""
        suffix = f" — {reason}" if reason else ""
        lines.append(f"• **{_name_of(row)}** — {decision} ({applied}){suffix}")
        step = row.get("last_step") or {}
        step_name = (step.get("result") or "").strip()
        bits = [x for x in (f"remaining: {leftover}" if leftover else "", f"last step: {step_name}" if step_name else "") if x]
        lines.append(f"  outreach `{_short_id(row)}`{camp}" + (f" · {'; '.join(bits)}" if bits else ""))
        lines.append("")
    return lines


def _format_closer(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["No hot-lead closer decisions today."]
    lines = [f"Hot-lead closer today ({len(rows)}):", ""]
    for row in rows:
        details = _details(row.get("details_json"))
        result = (row.get("result") or "hold").strip()
        reason = (details.get("reason") or "").strip()
        email = (details.get("email") or "").strip()
        start = (details.get("start") or "").strip()
        mode = str(details.get("mode") or "")
        campaign = _campaign_of(row)
        camp = f" · {campaign}" if campaign else ""
        suffix = f" — {reason}" if reason else ""
        lines.append(f"• **{_name_of(row)}** — {result}{suffix}")
        grounded = " · ".join(x for x in (email, start, mode) if x)
        lines.append(f"  outreach `{_short_id(row)}`{camp}" + (f" · {grounded}" if grounded else ""))
        lines.append("")
    return lines


def _format_pending_jobs(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["No pending jobs."]
    lines = [f"Pending jobs ({len(rows)}):", ""]
    for row in rows:
        kind = (row.get("job_type") or "job").strip()
        status = (row.get("status") or "pending").strip()
        campaign = _campaign_of(row)
        camp = f" · {campaign}" if campaign else ""
        lines.append(f"• **{_name_of(row)}** — {kind} ({status})")
        lines.append(f"  outreach `{_short_id(row)}`{camp}")
        lines.append("")
    return lines


def _format_refusals(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["No recent gated-job refusals."]
    lines = [f"Recent refusals ({len(rows)}):", ""]
    for row in rows:
        details = _details(row.get("details_json"))
        kind = (row.get("action_type") or "").strip()
        reason = (details.get("reason") or "").strip()
        campaign = _campaign_of(row)
        camp = f" · {campaign}" if campaign else ""
        label = kind or "refused"
        if reason:
            label = f"{label} — {reason}"
        lines.append(f"• **{_name_of(row)}** — {label}")
        lines.append(f"  outreach `{_short_id(row)}`{camp}")
        lines.append("")
    return lines


def _format_jobs(pending: list[dict[str, Any]], refusals: list[dict[str, Any]]) -> list[str]:
    lines = []
    lines.extend(_mode_lines())
    lines.append("")
    lines.extend(_format_pending_jobs(pending))
    lines.append("")
    lines.extend(_format_refusals(refusals))
    return lines


def _format_skips(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["No recent reply skips."]
    lines = [f"Reply skips ({len(rows)}):", ""]
    for row in rows:
        details = _details(row.get("details_json"))
        kind = (row.get("action_type") or "").strip()
        result = (row.get("result") or "skipped").strip()
        reason = (details.get("reason") or "").strip()
        campaign = _campaign_of(row)
        camp = f" · {campaign}" if campaign else ""
        label = f"{kind} ({result})"
        if reason:
            label = f"{label} — {reason}"
        lines.append(f"• **{_name_of(row)}** — {label}")
        lines.append(f"  outreach `{_short_id(row)}`{camp}")
        lines.append("")
    return lines


def _age_label(ts: int, now: int) -> str:
    delta = max(0, now - int(ts or 0))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    return f"{delta // 3600}h ago"


def _commons_mode(agent: str, campaign_id: str) -> str:
    if agent == "icp_research":
        return icp_research_mode({
            "icp_research_mode": get_setting("icp_research_mode", "") or "",
            "enable_icp_research_agent": get_setting("enable_icp_research_agent", "") or "",
        })
    if agent == "product":
        return product_agent_mode({
            "product_agent_mode": get_setting("product_agent_mode", "") or "",
            "enable_product_agent": get_setting("enable_product_agent", "") or "",
        })
    config: dict[str, Any] = {}
    if campaign_id:
        camp = get_campaign(campaign_id) or {}
        raw = camp.get("config_json") or "{}"
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                config = loaded
        except (json.JSONDecodeError, TypeError):
            config = {}
    if agent == "reply":
        return reply_agent_mode(config)
    if agent == "strategist":
        return strategist_replan_mode(config)
    if agent == "closer":
        return hot_lead_closer_mode(config)
    if agent == "coordinator":
        return coordinator_mode(config)
    return "observe"


def _commons_summary_line(campaign_id: str, outreach_id: str) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    notes = list_live_notes(campaign_id=campaign_id, outreach_id=outreach_id, now=now)
    beats = list_beats(campaign_id=campaign_id)
    newest = max((int(b.get("created_at") or 0) for b in beats), default=0)
    agent = ""
    if newest:
        for beat in beats:
            if int(beat.get("created_at") or 0) == newest:
                agent = str(beat.get("agent") or "")
                break
    age = _age_label(newest, now) if newest else "never"
    who = f"{agent} beat {age}" if newest else "no beats"
    coord = next((b for b in beats if str(b.get("agent") or "") == "coordinator"), None)
    if coord and agent != "coordinator":
        who = f"{who}, coordinator beat {_age_label(int(coord.get('created_at') or 0), now)}"
    return f"commons: {len(notes)} live notes, {who}"


def _format_commons(campaign_id: str, outreach_id: str, limit: int) -> list[str]:
    now = int(datetime.now(timezone.utc).timestamp())
    beats = list_beats(campaign_id=campaign_id)[:limit]
    notes = list_live_notes(campaign_id=campaign_id, outreach_id=outreach_id, now=now)[:limit]
    digest = get_digest(campaign_id) if campaign_id else None
    hold = get_hold(campaign_id) if campaign_id else None
    lines = ["Agent commons", ""]
    lines.append("Digest:")
    if digest and (digest.get("body") or "").strip():
        lines.append((digest.get("body") or "").strip())
    else:
        lines.append("No digest.")
    lines.extend(["", "Beats:"])
    if not beats:
        lines.append("No agent beats.")
    for beat in beats:
        agent = str(beat.get("agent") or "?")
        decision = str(beat.get("decision") or "?")
        reason = (beat.get("reason") or "").strip()
        age = _age_label(int(beat.get("created_at") or 0), now)
        suffix = f" — {reason}" if reason else ""
        lines.append(f"• **{agent}** — {decision} ({age}){suffix}")
        mode = _commons_mode(agent, str(beat.get("campaign_id") or campaign_id or ""))
        if beat_is_stale(beat, mode=mode, now=now):
            lines.append(f"  STALE ({mode}, beat older than 6h)")
    lines.extend(["", "Notes:"])
    if not notes:
        lines.append("No live notes.")
    for note in notes:
        agent = str(note.get("agent") or "?")
        body = (note.get("body") or "").strip()
        lines.append(f"• **{agent}** — {body}")
    if hold and ((hold.get("reason") or hold.get("body") or "").strip()):
        reason = (hold.get("reason") or hold.get("body") or "").strip()
        lines.extend(["", "Coordinator hold:", f"• {reason}"])
    return lines


def _format_coordinator_holds(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    lines = [f"Coordinator holds ({len(rows)}):", ""]
    for row in rows:
        reason = (row.get("reason") or row.get("body") or "").strip() or "needs a human"
        cid = str(row.get("campaign_id") or "")
        camp = get_campaign(cid) if cid else None
        campaign_label = ((camp or {}).get("name") or "").strip() or (cid[:8] if cid else "campaign")
        lines.append(f"• **{campaign_label}** — {reason}")
        if cid:
            lines.append(f"  campaign `{cid[:8]}`")
        lines.append("")
    return lines


def _render(action: str, campaign_id: str, outreach_id: str, limit: int) -> str:
    if action == "commons":
        return "\n".join(_format_commons(campaign_id, outreach_id, limit)).rstrip()
    if action == "holds":
        lines = _format_holds(_list_holds(campaign_id, outreach_id, limit))
        extra = _format_coordinator_holds(list_coordinator_holds(campaign_id=campaign_id))
        if extra:
            lines.append("")
            lines.extend(extra)
        return "\n".join(lines).rstrip()
    if action == "replans":
        return "\n".join(_format_replans(_list_replans(campaign_id, outreach_id, limit))).rstrip()
    if action == "closer":
        return "\n".join(_format_closer(_list_closer(campaign_id, outreach_id, limit))).rstrip()
    if action == "skips":
        return "\n".join(_format_skips(_list_skips(campaign_id, outreach_id, limit))).rstrip()
    if action == "jobs":
        pending = _list_pending_jobs(campaign_id, outreach_id, limit)
        refusals = _list_job_refusals(campaign_id, outreach_id, limit)
        return "\n".join(_format_jobs(pending, refusals)).rstrip()

    holds = _list_holds(campaign_id, outreach_id, limit)
    replans = _list_replans(campaign_id, outreach_id, limit)
    closer = _list_closer(campaign_id, outreach_id, limit)
    skips = _list_skips(campaign_id, outreach_id, limit)
    pending = _list_pending_jobs(campaign_id, outreach_id, limit)
    refusals = _list_job_refusals(campaign_id, outreach_id, limit)
    lines = [
        "Agent ops",
        "",
        f"Holds: {len(holds)}",
        f"Replans today: {len(replans)}",
        f"Closer today: {len(closer)}",
        f"Reply skips: {len(skips)}",
        f"Jobs: {len(pending)} pending, {len(refusals)} refused",
        _commons_summary_line(campaign_id, outreach_id),
        "",
    ]
    lines.extend(_format_holds(holds))
    extra = _format_coordinator_holds(list_coordinator_holds(campaign_id=campaign_id))
    if extra:
        lines.append("")
        lines.extend(extra)
    lines.append("")
    lines.extend(_format_replans(replans))
    lines.append("")
    lines.extend(_format_closer(closer))
    lines.append("")
    lines.extend(_format_skips(skips))
    lines.append("")
    lines.extend(_format_jobs(pending, refusals))
    return "\n".join(lines).rstrip()
