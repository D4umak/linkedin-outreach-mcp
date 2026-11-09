"""The ICP research agent's hold, kept until the owner answers (heylead-api#5380).

Before this, a hold was a summary line in the generate_icp reply: nothing
stored it, so launch, resume and the hourly refill went ahead on the target the
agent had just said finds the wrong people.

The record is one setting, ``icp_holds`` = {icp_id: hold}. A hold is written
only in act mode (observe gates nothing, as the agent's decision row says).

* Direct mode: the local record is the gate. Launch, resume and refill read
  it through the campaign's ``config_json.icp_id`` (create_campaign writes it
  when a saved ICP is used).
* Hosted: the hold is posted to ``POST /api/v1/icps/{icp_id}/hold``, and the
  api gates (its /resume answers 409 ``icp_held``). The api answers 404 until
  the ICP is ingested, so the hold stays ``pending_post`` and the next sync
  push retries it; while it is pending this machine gates too.

Only an ``launch_anyway`` answer clears it; ``fix_audience`` leaves the gate
standing. An edit of the ICP's persona lists (their hash) lifts it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

import httpx

from .. import config
from ..db import queries
from ..db.async_bridge import run_db

logger = logging.getLogger(__name__)

SETTING = "icp_holds"
ANSWERS = ("launch_anyway", "fix_audience")
_LIST_FIELDS = ("job_titles", "locations", "industries")


def persona_lists_hash(icp_json: Any) -> str:
    """A hash of every persona's include/exclude lists; "" when unreadable."""
    data = icp_json
    if isinstance(data, str):
        try:
            data = json.loads(data or "{}")
        except (TypeError, ValueError):
            return ""
    if not isinstance(data, dict):
        return ""
    lists = []
    for persona in data.get("icps") or []:
        if not isinstance(persona, dict):
            continue
        row = {}
        for field in _LIST_FIELDS:
            bucket = persona.get(field) if isinstance(persona.get(field), dict) else {}
            row[field] = {
                side: sorted(str(v).strip().lower() for v in (bucket.get(side) or []))
                for side in ("include", "exclude")
            }
        lists.append(row)
    return hashlib.sha256(json.dumps(lists, sort_keys=True).encode()).hexdigest()[:16]


def _all() -> dict[str, dict]:
    stored = queries.get_setting(SETTING, {}) or {}
    return stored if isinstance(stored, dict) else {}


def _put(icp_id: str, hold: dict | None) -> None:
    stored = _all()
    if hold is None:
        stored.pop(icp_id, None)
    else:
        stored[icp_id] = hold
    queries.save_setting(SETTING, stored)


def get_hold(icp_id: str) -> dict | None:
    """The stored hold for this ICP, answered or not; None when there is none."""
    hold = _all().get(icp_id or "")
    return dict(hold) if isinstance(hold, dict) else None


def _current_hash(icp_id: str) -> str | None:
    record = queries.get_icp(icp_id)
    if not record:
        return None
    return persona_lists_hash(record.get("icp_json"))


def _store_hold(icp_id: str, *, reason: str, mode: str, at: int, pending_post: bool) -> dict:
    hold = {
        "icp_id": icp_id,
        "reason": reason,
        "mode": mode,
        "at": at,
        "answered": None,
        "answered_at": None,
        "lists_hash": _current_hash(icp_id) or "",
        "pending_post": pending_post,
    }
    _put(icp_id, hold)
    return hold


async def record_icp_hold(
    icp_id: str, *, reason: str, mode: str, decided_at: int | None = None,
) -> str:
    """Record the research agent's hold. Returns what happened to it.

    "observe" (nothing stored: observe gates nothing), "local" (direct mode,
    the local record gates), "hosted" (the api recorded it) or "pending" (the
    api has not got it yet; the next sync push retries).
    """
    if mode != "act" or not icp_id:
        return "observe"
    at = int(decided_at or time.time())
    hosted = config.is_backend_mode()
    await run_db(_store_hold, icp_id, reason=reason, mode=mode, at=at, pending_post=hosted)
    if not hosted:
        return "local"
    return "hosted" if await post_icp_hold(icp_id) else "pending"


async def post_icp_hold(icp_id: str) -> bool:
    """POST the stored hold to the api. True once the api has it (or never will).

    404 (the ICP is not in the workspace yet), a 5xx or no answer keep it
    pending for the next push. Any other refusal drops the pending mark: the
    same body would be refused again.
    """
    hold = await run_db(get_hold, icp_id)
    if not hold or not hold.get("pending_post"):
        return True
    from .cloud_sync import _base_url, _headers

    body = {"reason": hold.get("reason") or "", "mode": hold.get("mode") or "act",
            "decided_at": int(hold.get("at") or 0)}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_base_url()}/api/v1/icps/{icp_id}/hold", headers=_headers(), json=body,
            )
    except httpx.HTTPError as e:
        logger.warning("ICP hold post for %s failed, retried on the next push: %s", icp_id, e)
        return False
    status = int(getattr(resp, "status_code", 0) or 0)
    if status == 404 or status >= 500:
        logger.info("ICP hold post for %s answered %s; retried on the next push", icp_id, status)
        return False
    if status >= 400:
        logger.warning("ICP hold post for %s refused: %s %s", icp_id, status,
                       str(getattr(resp, "text", ""))[:200])
    await run_db(_mark_posted, icp_id)
    return True


def _mark_posted(icp_id: str) -> None:
    hold = get_hold(icp_id)
    if hold:
        hold["pending_post"] = False
        _put(icp_id, hold)


async def retry_pending_icp_holds() -> int:
    """Re-post the holds the api has not got yet. Returns how many landed."""
    if not config.is_backend_mode():
        return 0
    landed = 0
    for icp_id, hold in (await run_db(_all)).items():
        if isinstance(hold, dict) and hold.get("pending_post"):
            if await post_icp_hold(icp_id):
                landed += 1
    return landed


def answer_icp_hold(icp_id: str, answer: str) -> dict | None:
    """Record the owner's answer. Only launch_anyway clears the gate."""
    if answer not in ANSWERS:
        raise ValueError(f"unknown answer: {answer}")
    hold = get_hold(icp_id)
    if not hold:
        return None
    hold["answered"] = answer
    hold["answered_at"] = int(time.time())
    _put(icp_id, hold)
    return hold


def active_hold(icp_id: str) -> dict | None:
    """The hold that stops this machine from launching or refilling, or None.

    None when there is no hold, it was answered launch_anyway, the persona
    lists changed since (the hold is lifted and removed), or a hosted
    workspace's api has recorded it (the api gates from there).
    """
    hold = get_hold(icp_id)
    if not hold or hold.get("answered") == "launch_anyway":
        return None
    current = _current_hash(icp_id)
    if current is not None and hold.get("lists_hash") and current != hold["lists_hash"]:
        _put(icp_id, None)
        logger.info("ICP %s: persona lists changed since the hold; hold lifted", icp_id)
        return None
    if config.is_backend_mode() and not hold.get("pending_post"):
        return None
    return hold


def held_icp_for_config(cfg: dict | None) -> dict | None:
    """active_hold for a campaign's config_json (its icp_id), or None."""
    icp_id = str((cfg or {}).get("icp_id") or "").strip() if isinstance(cfg, dict) else ""
    return active_hold(icp_id) if icp_id else None


def _sentence(reason: str) -> str:
    text = (reason or "").strip() or "the people the search finds are not who you described."
    return text if text[-1] in ".!?" else text + "."


def not_started_text(hold: dict, *, action: str = "launch") -> str:
    """The refusal for launch or resume on a held target (designer round 2)."""
    reason = _sentence(str(hold.get("reason") or ""))
    if action == "resume":
        head = "Not resumed yet."
        stays = "this campaign stays paused and asks again if resumed"
    else:
        head = "Not launched yet."
        stays = "this draft stays in Drafts and asks again if launched"
    return (
        f"{head} HeyLead ran this campaign's target as a search and held it: {reason}\n"
        "Ask the user: \"Change the target, or launch anyway?\" To change it, generate_icp "
        f"with a corrected description and create_campaign from the new ICP; {stays}. "
        f"To launch anyway, campaign(action='{action}', icp_hold_ok=True). "
        "Nothing is sent until they choose."
    )


def held_reply_summary(reason: str) -> str:
    """generate_icp's line for an act-mode hold, after "ICP research " (designer round 2)."""
    return (
        f"held this target: {_sentence(reason)} "
        "Launch will ask you to change the target or launch anyway."
    )


def icp_held_refusal(detail: Any) -> dict | None:
    """The api's 409 ``icp_held`` body ({code, icp_id, reason, message}), or None.

    The client sees the FastAPI detail as a dict or as its JSON text.
    """
    data: Any = detail
    if not isinstance(data, dict):
        text = str(detail or "")
        if "icp_held" not in text:
            return None
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return None
    if isinstance(data, dict) and isinstance(data.get("detail"), dict):
        data = data["detail"]
    if not isinstance(data, dict) or data.get("code") != "icp_held":
        return None
    return data


def refusal_message(refusal: dict, *, action: str = "launch") -> str:
    """The api's own message, or ours from its reason when it sent none."""
    message = str(refusal.get("message") or "").strip()
    return message or not_started_text(refusal, action=action)
