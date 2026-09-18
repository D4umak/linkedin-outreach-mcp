"""Tools for pausing and resuming campaigns.

Provides safety controls to stop outreach when needed
and resume it when ready.
"""

from __future__ import annotations

import copy

import json
import logging
import time

from .. import config
from ..config import get_scheduler_mode, is_scheduler_enabled, set_scheduler_mode
from ..constants import STATUS_ACTIVE, STATUS_DRAFT
from ..db.queries import (
    get_campaign,
    get_setting,
    list_campaigns,
    log_action,
    update_campaign,
)
from ..db import queries
from ..db.async_bridge import run_db
from ..services.dashboard_snapshot import status_footer
from ..services.cloud_sync import (
    PENDING_CLOUD_RESUME_AT,
    PENDING_CLOUD_RESUME_KEY,
    has_pending_cloud_resume,
    sync_campaign_status,
    sync_campaign_status_explained,
)

logger = logging.getLogger(__name__)

# A pause must be reversible. The queue is parked with a marker in
# last_attempt_error so resume can reverse exactly the rows pause touched and
# leave outreaches skipped for other reasons (segment pruning, dedup) alone.
_PAUSE_SKIP_MARKER = "campaign_paused"


async def _sync_active_to_host(
    campaign_id: str, *, caller: str, reason: str,
) -> tuple[bool, str]:
    """Resume on the host; if the row is missing, push just this campaign once.

    A full-account sync_to_cloud is what hung launch. create_campaign already
    pushed the draft; a 404 here means that push missed, so retry one campaign.
    A 409 (archived) is a refusal, not a miss — do not retry, return the
    backend's reason so the tool can show it.
    """
    synced, detail = await sync_campaign_status_explained(
        campaign_id, STATUS_ACTIVE, caller=caller, reason=reason,
    )
    if synced or not config.is_backend_mode():
        return synced, detail
    if detail:
        return False, detail
    try:
        from ..services.cloud_sync import sync_to_cloud
        result = await sync_to_cloud(campaign_id=campaign_id)
        if isinstance(result, dict) and result.get("error"):
            return False, str(result.get("error") or "")
        return await sync_campaign_status_explained(
            campaign_id, STATUS_ACTIVE, caller=caller, reason=reason,
        )
    except Exception as e:
        logger.warning("Targeted campaign push after resume miss failed: %s", e)
        return False, str(e)


def _pause_retry_warning(campaign_id: str) -> str:
    """The warning for a pause the cloud did not receive, naming a real retry.

    It used to send the user to show_status, which retries no cloud call.
    Pausing again does (run_pause_campaign re-sends an already-paused
    campaign's pause on hosted accounts).
    """
    return (
        "**Warning**: Could not sync to cloud scheduler, so the cloud may still "
        "send from this campaign. Retry with "
        f"campaign(action='pause', campaign_id='{campaign_id}')."
    )


def _resume_retry_warning(campaign_id: str) -> str:
    """The warning for a resume or launch the cloud did not receive.

    heylead-api #328 stopped the periodic push from moving a stopped cloud
    campaign to active, so nothing repairs this on its own. Resuming again
    re-issues the cloud resume, including for a campaign already active here.
    """
    return (
        "**Warning**: Could not sync to cloud scheduler, so this campaign is not "
        "live in the cloud and will not send from there. Retry with "
        f"campaign(action='resume', campaign_id='{campaign_id}')."
    )


# Cloud statuses a resume must not override when leaving observe: the backend
# refuses archived (409) and would restart completed, and neither is a
# campaign observe held back.
_CLOUD_STOPPED_FOR_GOOD = ("archived", "completed")


def _campaign_cfg(campaign: dict) -> dict | None:
    """config_json as a dict, or None when it is not a JSON object."""
    try:
        cfg = json.loads(campaign.get("config_json") or "{}")
    except (TypeError, ValueError):
        return None
    return cfg if isinstance(cfg, dict) else None


def _set_pending_cloud_resume(campaign_id: str, pending: bool) -> bool:
    """Set or clear the pending-cloud-resume flag on a campaign's config_json.

    Reads the row fresh so it never overwrites a config written in between.
    Returns True when the row changed. An unreadable config is left alone.
    """
    from ..db.queries import get_campaign as _get

    campaign = _get(campaign_id)
    if not campaign:
        return False
    cfg = _campaign_cfg(campaign)
    if cfg is None:
        return False
    # Two keys, written on their own: this used to put the whole document
    # back and revert anything saved since (heylead-api#482).
    if pending:
        queries.merge_campaign_config(campaign_id, {
            PENDING_CLOUD_RESUME_KEY: True,
            PENDING_CLOUD_RESUME_AT: int(time.time()),
        })
    elif PENDING_CLOUD_RESUME_KEY in cfg or PENDING_CLOUD_RESUME_AT in cfg:
        queries.merge_campaign_config(campaign_id, remove=[
            PENDING_CLOUD_RESUME_KEY, PENDING_CLOUD_RESUME_AT,
        ])
    else:
        return False
    return True


async def _resume_flagged_on_host(
    campaign_id: str, *, caller: str, reason: str,
) -> tuple[str, str]:
    """Resume one flagged campaign in the cloud; ("resumed"|"refused"|"failed", detail).

    Like _sync_active_to_host, including the one targeted push when no
    workspace holds the row, but it keeps a definitive refusal apart from a
    failure worth retrying, because only the latter keeps the flag.
    """
    from ..services.cloud_sync import resume_campaign_in_cloud_classified, sync_to_cloud

    kind, detail = await resume_campaign_in_cloud_classified(
        campaign_id, caller=caller, reason=reason,
    )
    if kind == "not_found":
        try:
            pushed = await sync_to_cloud(campaign_id=campaign_id)
        except Exception as e:  # noqa: BLE001 - reported as a retryable failure
            return "failed", str(e)
        if isinstance(pushed, dict) and pushed.get("error"):
            return "failed", str(pushed.get("error") or "")
        kind, detail = await resume_campaign_in_cloud_classified(
            campaign_id, caller=caller, reason=reason,
        )
    if kind == "ok":
        return "resumed", ""
    if kind == "refused":
        return "refused", detail
    return "failed", detail


async def resume_observed_campaigns_in_cloud(
    *, caller: str, reason: str,
) -> list[dict]:
    """Resume, in the cloud, the campaigns observe activated without telling it.

    Resume, launch and monitor in observe flip the local row to active, send
    no /resume, and flag the row pending_cloud_resume. Since heylead-api #328
    a push cannot move a paused cloud campaign to active, so this issues the
    resume each flagged one is missing. It must run BEFORE any push: the
    flag is what stops the push's status refresh from reading the stale cloud
    "paused" as a dashboard pause, and an unflagged active campaign the
    dashboard paused is deliberately not resumed here.

    Per local active campaign carrying the flag, by its status in the cloud list:
      active               nothing to do ("already_live"), flag cleared
      archived, completed  left alone and reported, never resumed, flag cleared
      anything else        resumed ("resumed", flag cleared), refused for good
                           ("refused", e.g. a 409, flag cleared) or failed
                           ("failed", network or 5xx, flag KEPT so a retry
                           still knows the resume is owed)
    A flagged copilot campaign is not resumed (the backend schedules
    autopilot only); its flag is dropped.
    An unreadable list falls back to resuming every flagged campaign: the
    backend still refuses archived itself, and the alternative strands them.

    Hosted accounts only; returns [] otherwise. Never raises.
    """
    if not config.is_backend_mode():
        return []
    local = []
    for c in await run_db(list_campaigns, status=STATUS_ACTIVE):
        if not has_pending_cloud_resume(_campaign_cfg(c)):
            continue
        if c.get("mode") != "autopilot":
            await run_db(_set_pending_cloud_resume, c["id"], False)
            continue
        local.append(c)
    if not local:
        return []

    listed: dict = {}
    try:
        from ..services.cloud_sync import _fetch_cloud_campaigns

        listed = await _fetch_cloud_campaigns() or {}
    except Exception as e:  # noqa: BLE001 - fall back to resuming
        logger.warning("Could not list cloud campaigns before resuming: %r", e)

    outcomes: list[dict] = []
    for campaign in local:
        cid = campaign["id"]
        cloud_status = str((listed.get(cid) or {}).get("status") or "").strip().lower()
        row = {
            "id": cid,
            "name": campaign.get("name") or cid,
            "cloud_status": cloud_status,
            "detail": "",
        }
        if cloud_status == STATUS_ACTIVE:
            await run_db(_set_pending_cloud_resume, cid, False)
            outcomes.append({**row, "outcome": "already_live"})
            continue
        if cloud_status in _CLOUD_STOPPED_FOR_GOOD:
            logger.info(
                "Leaving observe: campaign %s is %s in the cloud, not resuming",
                cid, cloud_status,
            )
            await run_db(_set_pending_cloud_resume, cid, False)
            outcomes.append({**row, "outcome": cloud_status})
            continue
        try:
            outcome, detail = await _resume_flagged_on_host(
                cid, caller=caller, reason=reason,
            )
        except Exception as e:  # noqa: BLE001 - report, never raise
            outcome, detail = "failed", str(e)
        if outcome != "failed":
            await run_db(_set_pending_cloud_resume, cid, False)
        outcomes.append({**row, "outcome": outcome, "detail": detail})
    return outcomes


def format_cloud_resume_report(outcomes: list[dict]) -> str:
    """One line per campaign, saying what the cloud did with it."""
    if not outcomes:
        return ""
    lines = ["**Cloud resumes held back by observe:**"]
    for o in outcomes:
        name, cid = o["name"], o["id"]
        retry = f"campaign(action='resume', campaign_id='{cid}')"
        outcome = o["outcome"]
        if outcome == "resumed":
            lines.append(f"- {name}: resumed in the cloud.")
        elif outcome == "already_live":
            lines.append(f"- {name}: already live in the cloud, nothing to resume.")
        elif outcome == "archived":
            lines.append(
                f"- {name}: archived in the cloud, so it was not resumed and "
                "will not send. Unarchive it on the dashboard (it comes back "
                f"paused), then run {retry}."
            )
        elif outcome == "completed":
            lines.append(
                f"- {name}: completed in the cloud, so it was not restarted and "
                f"will not send. Run {retry} if you want it to send again."
            )
        elif outcome == "refused":
            why = o.get("detail") or "refused by the backend"
            lines.append(
                f"- {name}: NOT resumed in the cloud ({why}), so it will not "
                f"send from there. Fix that, then retry with {retry}."
            )
        else:
            why = o.get("detail") or "no answer from the backend"
            lines.append(
                f"- {name}: NOT resumed in the cloud ({why}), so it will not "
                "send from there yet. It stays marked as waiting for a cloud "
                f"resume. Retry with {retry}."
            )
    return "\n".join(lines) + "\n\n"


def _config_without_cloud_stop(
    campaign: dict, *, pending_cloud_resume: bool = False,
) -> str:
    """The campaign's config_json with any cloud-stop mark removed.

    The cloud status refresh marks a stop it brought down (pause_reason
    "cloud_status_refresh" plus a cloud_stop record) so a later cloud resume
    can lift it. Once the user launches, monitors or resumes locally, that
    mark no longer describes the row and would mislabel the next pause in
    show_status and the audit log. Anything unreadable is returned as is.

    pending_cloud_resume=True also flags the row as activated here without a
    cloud resume (observe on a hosted account); see
    cloud_sync.PENDING_CLOUD_RESUME_KEY.
    """
    raw = campaign.get("config_json") or "{}"
    try:
        cfg = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(cfg, dict):
        return raw
    cfg.pop("cloud_stop", None)
    if cfg.get("pause_reason") == "cloud_status_refresh":
        cfg.pop("pause_reason", None)
        cfg.pop("paused_at", None)
    if pending_cloud_resume:
        cfg[PENDING_CLOUD_RESUME_KEY] = True
        cfg[PENDING_CLOUD_RESUME_AT] = int(time.time())
    return json.dumps(cfg)


def _park_pending_outreaches(campaign_id: str) -> int:
    """Park a campaign's pending outreaches so resume can bring them back."""
    from ..db.schema import get_db

    db = get_db()
    cursor = db.execute(
        "UPDATE outreaches SET status = 'skipped', last_attempt_error = ?, "
        "updated_at = strftime('%s','now') "
        "WHERE campaign_id = ? AND status = 'pending'",
        (_PAUSE_SKIP_MARKER, campaign_id),
    )
    count = cursor.rowcount
    db.commit()
    db.close()
    return count


def _unpark_paused_outreaches(campaign_id: str) -> int:
    """Return parked outreaches to 'pending'. Returns count restored."""
    from ..db.schema import get_db

    db = get_db()
    cursor = db.execute(
        "UPDATE outreaches SET status = 'pending', last_attempt_error = NULL, "
        "updated_at = strftime('%s','now') "
        "WHERE campaign_id = ? AND status = 'skipped' AND last_attempt_error = ?",
        (campaign_id, _PAUSE_SKIP_MARKER),
    )
    count = cursor.rowcount
    db.commit()
    db.close()
    return count


async def run_pause_campaign(campaign_id: str = "") -> str:
    """Pause an active campaign, stopping all outreach.

    If no campaign_id is provided, pauses the first active campaign found.
    """

    setup_done = await run_db(get_setting, "setup_complete", False)
    if not setup_done:
        return (
            "Setup required before managing campaigns.\n\n"
            "Please run setup_profile first."
        )

    # Find the campaign to pause
    if campaign_id:
        campaign = await run_db(get_campaign, campaign_id)
        if not campaign:
            return f"Campaign not found: {campaign_id}"
    else:
        campaigns = await run_db(list_campaigns, status="active")
        if not campaigns:
            return (
                "No active campaigns to pause.\n\n"
                "Use show_status() to see all campaigns."
            )
        campaign = campaigns[0]
        campaign_id = campaign["id"]

    # Check current status
    status = campaign.get("status", "")
    if status == "paused":
        if config.is_backend_mode():
            # Pausing again is the retry the failure warning names, so it has
            # to reach the cloud. A pause is idempotent there.
            synced = await sync_campaign_status(
                campaign_id, "paused", caller="user", reason="manual_pause_retry",
            )
            cloud_line = (
                "The pause was sent to the cloud scheduler again."
                if synced else _pause_retry_warning(campaign_id)
            )
            return (
                f"Campaign '{campaign['name']}' is already paused.\n\n"
                f"{cloud_line}\n\n"
                "Use resume_campaign() to resume it."
            )
        return (
            f"Campaign '{campaign['name']}' is already paused.\n\n"
            "Use resume_campaign() to resume it."
        )
    if status not in ("active", "draft"):
        return (
            f"Campaign '{campaign['name']}' has status '{status}' and cannot be paused.\n"
            "Only active or draft campaigns can be paused."
        )

    # Pause it, park pending outreaches (resume restores them), record reason
    skipped = await run_db(_park_pending_outreaches, campaign_id)
    cfg = json.loads(campaign.get("config_json") or "{}")
    cfg_before = copy.deepcopy(cfg)
    cfg["pause_reason"] = "user"
    cfg["paused_at"] = int(time.time())
    # A resume observe still owed the cloud is withdrawn by the pause.
    cfg.pop(PENDING_CLOUD_RESUME_KEY, None)
    cfg.pop(PENDING_CLOUD_RESUME_AT, None)
    # Only the keys this pause changed, and the status in the same statement
    # (heylead-api#482: writing the whole document reverted concurrent keys).
    await run_db(queries.merge_campaign_config_delta, campaign_id, cfg_before,
                 cfg, status="paused")

    # Audit log
    await run_db(
        log_action, "campaign_status_change",
        result="paused",
        details={
            "campaign_id": campaign_id,
            "campaign_name": campaign["name"],
            "old_status": status,
            "new_status": "paused",
            "changed_by": "user",
            "reason": "manual_pause",
            "pending_skipped": skipped,
        },
    )

    # Push the parked queue (skipped + campaign_paused) before the next
    # 15-minute tick. /pause only flips campaign status; without this the
    # host never sees the marker and cannot unpark on resume.
    if config.is_backend_mode():
        try:
            from ..services.cloud_sync import sync_to_cloud
            await sync_to_cloud(campaign_id=campaign_id)
        except Exception as e:
            logger.warning(
                "Pause parked %s locally but the queue push failed: %s",
                campaign_id, e,
            )

    # Sync to backend so cloud scheduler also stops
    synced = await sync_campaign_status(
        campaign_id, "paused", caller="user", reason="manual_pause",
    )
    logger.info(
        "Paused campaign %s: %s (%d pending parked, cloud_synced=%s)",
        campaign_id, campaign["name"], skipped, synced,
    )

    cloud_note = ""
    if not synced:
        cloud_note = "\n\n" + _pause_retry_warning(campaign_id)

    return (
        f"Campaign '{campaign['name']}' is now paused.\n\n"
        # Not "marked as skipped": that reads as dropped. The rows carry the
        # pause marker and _unpark_paused_outreaches returns exactly them.
        f"{skipped} pending outreaches parked. Resume puts them back in the queue.\n"
        "No invitations or messages will be sent.\n\n"
        f"Use resume_campaign() when you're ready to continue.{cloud_note}"
        + "\n".join(["", *status_footer("campaign", campaign_id, fresh=True)])
    )


async def _select_campaign_to_start(
    campaign_id: str, action: str,
) -> tuple[dict | None, str, str]:
    """Resolve which campaign 'launch' or 'monitor' should act on.

    Both actions move a draft (or paused) campaign to 'active' and differ only
    in what they do to the scheduler afterwards, so the lookup, the ambiguity
    prompt and the status checks are shared.

    Returns (campaign, campaign_id, error). When error is non-empty it is the
    complete tool response and campaign is None.
    """
    setup_done = await run_db(get_setting, "setup_complete", False)
    if not setup_done:
        return None, "", (
            "Setup required before managing campaigns.\n\n"
            "Please run setup_profile first."
        )

    if campaign_id:
        campaign = await run_db(get_campaign, campaign_id)
        if not campaign:
            return None, "", f"Campaign not found: {campaign_id}"
    else:
        drafts = await run_db(list_campaigns, status=STATUS_DRAFT)
        if not drafts:
            return None, "", (
                f"No draft campaigns to {action}.\n\n"
                "Use create_campaign() to build one, or show_status() to see "
                "existing campaigns."
            )
        if len(drafts) > 1:
            # Full ids, not the 8-char prefix this used to print. get_campaign
            # matches on `id = ?` and nothing resolves prefixes, so the command
            # handed to the user came back "Campaign not found" — leaving both
            # launch and monitor unusable for anyone with two drafts.
            names = "\n".join(
                f"  {c['name']} — campaign(action='{action}', campaign_id='{c['id']}')"
                for c in drafts
            )
            return None, "", f"{len(drafts)} draft campaigns — which one?\n\n{names}"
        campaign = drafts[0]
        campaign_id = campaign["id"]

    status = campaign.get("status", "")
    if status == STATUS_ACTIVE:
        # Worded off the mode, not the action: in observe an active campaign is
        # being read from, and calling that "running" is what sent users looking
        # for progress that by definition cannot exist.
        if get_scheduler_mode() == "observe":
            return None, "", (
                f"Campaign '{campaign['name']}' is already active. In observe "
                "mode that means the scheduler collects from it and sends "
                "nothing.\n\n"
                "Use show_status() to see what has come in."
            )
        return None, "", (
            f"Campaign '{campaign['name']}' is already running.\n\n"
            "Use show_status() to see its progress."
        )
    if status not in (STATUS_DRAFT, "paused"):
        return None, "", (
            f"Campaign '{campaign['name']}' has status '{status}' and cannot be {action}ed.\n"
            f"Only draft or paused campaigns can be {action}ed."
        )

    return campaign, campaign_id, ""


async def run_launch_campaign(campaign_id: str = "") -> str:
    """Start outreach for a draft campaign.

    Campaigns are created as drafts so that building one never sends anything.
    This is the step where the user explicitly says "go". Hosted accounts
    commission the cloud; this machine's sender stays off unless the user
    later runs send_from host=local. Direct-mode still switches the local
    scheduler on if it was off.

    In observe mode it is not that step. observe means "never sends, invites,
    engages, or enrols anyone", so launching there activates the campaign for
    collection and touches neither the scheduler mode nor the cloud: the
    /campaigns/{id}/resume POST is the backend's instruction to send from this
    campaign every 5 minutes, which is the one thing observe promises not to do.

    If no campaign_id is provided and there is exactly one draft, that one is
    launched; more than one is ambiguous and the user is asked which.
    """

    campaign, campaign_id, error = await _select_campaign_to_start(campaign_id, "launch")
    if error:
        return error
    assert campaign is not None  # guaranteed when error is empty

    from ..services.project_brief import refuse_without_project_brief
    missing = refuse_without_project_brief(campaign)
    if missing:
        return missing

    status = campaign.get("status", "")
    observing = get_scheduler_mode() == "observe"
    hosted = config.is_backend_mode()

    await run_db(update_campaign, campaign_id, status=STATUS_ACTIVE,
                 config_json=_config_without_cloud_stop(
                     campaign, pending_cloud_resume=observing and hosted,
                 ))

    # Hosted sending is the cloud's. Direct-mode still has to turn this
    # machine on — there is no other sender.
    if not observing and hosted:
        config.set_sending_host(
            "cloud",
            caller="campaign_launch",
            reason=f"Launched campaign {campaign['name']}",
        )
    scheduler_was_off = (
        not observing and not hosted and not is_scheduler_enabled()
    )
    if scheduler_was_off:
        set_scheduler_mode(
            "full", caller="campaign_launch",
            reason=f"Launched campaign {campaign['name']}",
        )

    await run_db(
        log_action, "campaign_status_change",
        result=STATUS_ACTIVE,
        details={
            "campaign_id": campaign_id,
            "campaign_name": campaign["name"],
            "old_status": status,
            "new_status": STATUS_ACTIVE,
            "changed_by": "user",
            "reason": (
                "manual_launch (observe mode — collection only)"
                if observing else "manual_launch"
            ),
            "scheduler_mode": get_scheduler_mode(),
        },
    )

    if observing:
        logger.info(
            "Launched campaign %s: %s in observe mode "
            "(scheduler untouched, no cloud resume)",
            campaign_id, campaign["name"],
        )
        return "\n".join([
            f"👀 Campaign '{campaign['name']}' is active, but nothing will be sent.",
            "",
            "The scheduler is in observe mode, so this campaign is only read from:",
            "├── 🔎 Its prospects' posts scanned for signals",
            "├── 🧠 Signals classified and scored",
            "└── 📬 Replies checked",
            "",
            "No invitations, DMs or engagements go out. The scheduler mode was "
            "left alone and the cloud scheduler was not asked to resume this "
            "campaign — that request is what makes the backend send from it "
            "every 5 minutes.",
            "",
            "├── scheduler(action='toggle', enabled=True) — leave observe and start sending",
            "├── show_status() — watch what comes in",
            "└── campaign(action='pause') — stop collecting from this campaign",
            *status_footer("campaign", campaign_id, fresh=True),
        ])

    synced, _host_detail = await _sync_active_to_host(
        campaign_id, caller="user", reason="manual_launch",
    )
    if synced:
        await run_db(_set_pending_cloud_resume, campaign_id, False)
    logger.info(
        "Launched campaign %s: %s (scheduler_was_off=%s, cloud_synced=%s)",
        campaign_id, campaign["name"], scheduler_was_off, synced,
    )

    # Launching is the opt-in to autonomous sending. Hosted accounts send
    # from the cloud; this machine stays silent unless the user later runs
    # send_from host=local. Best-effort: a refused commission must not
    # fail the launch.
    from ..services.cloud_sync import commission_cloud_sending

    cloud_commissioned, cloud_detail = await commission_cloud_sending(
        campaign_id,
    )

    # Queue the first connection request now. Waiting for the next 60s tick
    # plus a 22-minute invite delay is why "launch" used to look idle.
    from ..scheduler.planner import plan_campaign_work

    try:
        await plan_campaign_work(campaign_id)
    except Exception:
        logger.warning("Launch planning failed for %s", campaign_id, exc_info=True)

    lines = [
        f"🚀 Campaign '{campaign['name']}' is now live.",
        "",
        "The first connection request is queued now:",
        "├── 🤝 Invite anyone who clears the send line",
        "├── 💬 Warm-up continues between invites",
        "├── 📩 Follow-up DMs once connections are accepted",
        "└── 📬 Replies checked every 5 minutes",
    ]
    if scheduler_was_off:
        lines.append("")
        lines.append("⚡ The scheduler was off and has been enabled.")
    if cloud_commissioned:
        lines.extend([
            "",
            "☁️ Sending from the cloud — this campaign keeps going every 5 "
            "minutes with your laptop closed. This machine sends only after "
            "`scheduler(action='send_from', host='local')`.",
        ])
    elif config.is_backend_mode():
        lines.extend([
            "",
            f"⚠️ 24/7 cloud sending could not be switched on ({cloud_detail}). "
            "This machine will not take over. Retry later, or move sending "
            "here with `scheduler(action='send_from', host='local')`.",
        ])
    lines.extend([
        "",
        "├── show_status() — watch progress",
        "└── campaign(action='pause') — stop at any time",
    ])
    if not synced:
        # Launching again answers "already running", so the retry is resume,
        # which re-issues the cloud resume for an active campaign.
        lines.append("\n" + _resume_retry_warning(campaign_id))
    lines.extend(status_footer("campaign", campaign_id, fresh=True))
    return "\n".join(lines)


async def run_monitor_campaign(campaign_id: str = "") -> str:
    """Activate a campaign for read-only signal collection.

    'active' is the only status the planner collects from, so watching a
    campaign without sending from it used to need a raw SQL UPDATE — which
    skipped the actions_log audit row that campaign(action='status_history')
    reads. This does the same activation through the normal path, and touches
    nothing else: no scheduler config write, no cloud resume.

    Only observe mode can honour that promise, so this refuses in any other
    mode rather than activating a campaign that would immediately send.
    """

    mode = get_scheduler_mode()

    campaign, campaign_id, error = await _select_campaign_to_start(campaign_id, "monitor")
    if error:
        return error
    assert campaign is not None  # guaranteed when error is empty

    if mode != "observe":
        return (
            f"Campaign '{campaign['name']}' was not activated — the scheduler "
            f"is in '{mode}' mode.\n\n"
            "monitor promises collection without sending, and only observe mode "
            "delivers that. In 'full' the planner sends from any active "
            "campaign; in 'off' a linked cloud account is still handed active "
            "campaigns by the 15-minute push and sends from them.\n\n"
            "├── scheduler(action='observe') — switch to observe, then run this again\n"
            "└── campaign(action='launch') — activate and send from this campaign"
        )

    status = campaign.get("status", "")
    await run_db(update_campaign, campaign_id, status=STATUS_ACTIVE,
                 config_json=_config_without_cloud_stop(
                     campaign, pending_cloud_resume=config.is_backend_mode(),
                 ))

    await run_db(
        log_action, "campaign_status_change",
        result=STATUS_ACTIVE,
        details={
            "campaign_id": campaign_id,
            "campaign_name": campaign["name"],
            "old_status": status,
            "new_status": STATUS_ACTIVE,
            "changed_by": "user",
            "reason": "manual_monitor (observe mode — collection only)",
            "scheduler_mode": mode,
        },
    )

    logger.info(
        "Monitoring campaign %s: %s (observe mode, scheduler untouched, "
        "no cloud resume)",
        campaign_id, campaign["name"],
    )

    return "\n".join([
        f"👀 Campaign '{campaign['name']}' is now collecting.",
        "",
        "The scheduler is in observe mode, so this campaign is only read from:",
        "├── 🔎 Its prospects' posts scanned for signals",
        "├── 🧠 Signals classified and scored",
        "└── 📬 Replies checked",
        "",
        "Nothing was sent and nothing will be: no invitation, DM or engagement "
        "goes out while the mode is observe. The scheduler mode was not changed "
        "and no cloud resume was issued.",
        "",
        "├── signals(action='show') — what has been collected",
        "├── scheduler(action='toggle', enabled=True) — leave observe and start sending",
        "└── campaign(action='pause') — stop collecting from this campaign",
        *status_footer("campaign", campaign_id, fresh=True),
    ])


async def run_resume_campaign(campaign_id: str = "") -> str:
    """Resume a paused campaign, re-enabling outreach.

    If no campaign_id is provided, resumes the first paused campaign found.

    In observe mode the local status change still happens — 'active' is the
    only status the collectors read from — but the cloud resume does not. The
    /campaigns/{id}/resume POST is the backend's instruction to send from this
    campaign every 5 minutes, and observe means "never sends". This is the same
    door launch closes; pause steers users straight here, so leaving it open
    made the guarantee one tool call from false.
    """

    setup_done = await run_db(get_setting, "setup_complete", False)
    if not setup_done:
        return (
            "Setup required before managing campaigns.\n\n"
            "Please run setup_profile first."
        )

    # Find the campaign to resume
    if campaign_id:
        campaign = await run_db(get_campaign, campaign_id)
        if not campaign:
            return f"Campaign not found: {campaign_id}"
    else:
        campaigns = await run_db(list_campaigns, status="paused")
        if not campaigns:
            return (
                "No paused campaigns to resume.\n\n"
                "Use show_status() to see all campaigns."
            )
        campaign = campaigns[0]
        campaign_id = campaign["id"]

    # Check current status
    observing = get_scheduler_mode() == "observe"
    status = campaign.get("status", "")
    if status == "active":
        if observing:
            return (
                f"Campaign '{campaign['name']}' is already active. In observe "
                "mode that means the scheduler collects from it and sends "
                "nothing.\n\n"
                "Use show_status() to see what has come in."
            )
        if config.is_backend_mode():
            # Resuming again is the retry every failed launch or resume names,
            # and since heylead-api #328 the periodic push cannot lift a cloud
            # pause, so this must reach the cloud. Resume is idempotent there,
            # and an archived campaign comes back as a 409 with its reason.
            synced, host_detail = await _sync_active_to_host(
                campaign_id, caller="user", reason="manual_resume_retry",
            )
            if synced:
                await run_db(_set_pending_cloud_resume, campaign_id, False)
                cloud_line = "The resume was sent to the cloud scheduler again."
            elif host_detail:
                cloud_line = f"The cloud refused the resume: {host_detail}"
            else:
                cloud_line = _resume_retry_warning(campaign_id)
            return (
                f"Campaign '{campaign['name']}' is already active.\n\n"
                f"{cloud_line}"
            )
        return (
            f"Campaign '{campaign['name']}' is already active.\n\n"
            "Outreach is running normally."
        )
    if status == "archived":
        return (
            f"Campaign '{campaign['name']}' is archived and cannot be resumed.\n"
            "Unarchive it first; it comes back paused."
        )
    if status != "paused":
        return (
            f"Campaign '{campaign['name']}' has status '{status}' and cannot be resumed.\n"
            "Only paused campaigns can be resumed."
        )

    from ..services.project_brief import refuse_without_project_brief
    missing = refuse_without_project_brief(campaign)
    if missing:
        return missing

    # Resume it, restore the parked queue, and clear pause metadata
    restored = await run_db(_unpark_paused_outreaches, campaign_id)
    cfg = json.loads(campaign.get("config_json") or "{}")
    cfg_before = copy.deepcopy(cfg)
    old_pause_reason = cfg.get("pause_reason", "unknown")
    cfg.pop("pause_reason", None)
    cfg.pop("paused_at", None)
    cfg.pop("cloud_stop", None)
    if observing and config.is_backend_mode():
        # The cloud stays paused until leaving observe sends the resume; the
        # flag keeps the status refresh from undoing this one meanwhile.
        cfg[PENDING_CLOUD_RESUME_KEY] = True
        cfg[PENDING_CLOUD_RESUME_AT] = int(time.time())
    await run_db(queries.merge_campaign_config_delta, campaign_id, cfg_before,
                 cfg, status="active")

    # Audit log
    await run_db(
        log_action, "campaign_status_change",
        result="active",
        details={
            "campaign_id": campaign_id,
            "campaign_name": campaign["name"],
            "old_status": "paused",
            "new_status": "active",
            "changed_by": "user",
            "reason": (
                f"manual_resume (observe mode — collection only, was paused by: "
                f"{old_pause_reason})"
                if observing
                else f"manual_resume (was paused by: {old_pause_reason})"
            ),
            "scheduler_mode": get_scheduler_mode(),
        },
    )

    if observing:
        logger.info(
            "Resumed campaign %s: %s in observe mode (no cloud resume)",
            campaign_id, campaign["name"],
        )
        if config.is_backend_mode():
            cloud_lines = [
                "It stays paused in the cloud. No cloud resume was issued, "
                "because that request is what makes the backend send from this "
                "campaign every 5 minutes, and the periodic sync cannot resume "
                "it either.",
                "",
                "Leaving observe with scheduler(action='toggle', enabled=True) "
                "sends that resume for this campaign and reports whether the "
                "cloud accepted it.",
            ]
        else:
            cloud_lines = [
                "Leaving observe with scheduler(action='toggle', enabled=True) "
                "starts sending from it on this machine.",
            ]
        return "\n".join([
            f"👀 Campaign '{campaign['name']}' is collecting again, but nothing "
            "will be sent.",
            "",
            "The scheduler is in observe mode, so this campaign is only read "
            "from: its prospects' posts are scanned, signals classified, and "
            "replies checked.",
            "",
            *cloud_lines,
            "",
            "├── scheduler(action='toggle', enabled=True): leave observe and start sending",
            "├── show_status(): watch what comes in",
            "└── campaign(action='pause'): stop collecting from this campaign",
            *status_footer("campaign", campaign_id, fresh=True),
        ])

    if config.is_backend_mode():
        config.set_sending_host(
            "cloud",
            caller="campaign_resume",
            reason=f"Resumed campaign {campaign['name']}",
        )

    # Sync to backend so cloud scheduler resumes
    synced, host_detail = await _sync_active_to_host(
        campaign_id, caller="user", reason="manual_resume",
    )
    if synced:
        await run_db(_set_pending_cloud_resume, campaign_id, False)
    logger.info(
        "Resumed campaign %s: %s (cloud_synced=%s)",
        campaign_id, campaign["name"], synced,
    )

    # Same promise as launch: outreach the user just switched back on should not
    # stop again the moment the lid closes. Best-effort — see _launch.
    from ..services.cloud_sync import commission_cloud_sending

    cloud_commissioned, cloud_detail = await commission_cloud_sending(
        campaign_id,
    )

    cloud_note = ""
    if not synced and host_detail:
        cloud_note = f"\n\nThe cloud refused: {host_detail}"
    elif not synced:
        cloud_note = "\n\n" + _resume_retry_warning(campaign_id)
    if cloud_commissioned:
        cloud_note += (
            "\n\n☁️ Sending from the cloud — this campaign keeps going with "
            "your laptop closed. This machine sends only after "
            "`scheduler(action='send_from', host='local')`."
        )
    elif config.is_backend_mode():
        cloud_note += (
            f"\n\n⚠️ 24/7 cloud sending could not be switched on ({cloud_detail}). "
            "This machine will not take over. Move sending here with "
            "`scheduler(action='send_from', host='local')` if you want it to."
        )

    return (
        f"Campaign '{campaign['name']}' is now active again.\n\n"
        f"{restored} parked outreaches returned to the queue.\n"
        f"Outreach will resume. Use generate_and_send() to send the next message.{cloud_note}"
        + "\n".join(["", *status_footer("campaign", campaign_id, fresh=True)])
    )
