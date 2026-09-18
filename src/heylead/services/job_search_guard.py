"""Recognise a job-search campaign, so nothing acts on its replies unattended.

A job-search campaign messages people such as the CEO of a company the founder
has applied to. Their replies are answered by hand: no automatic booking, no
calendar invitation, no autonomous reply.

Reads the raw ``config_json.campaign_type`` key. Once VALID_CAMPAIGN_TYPES /
CAMPAIGN_TYPE_JOB_SEARCH land on main (PR #306), switch to those constants.
"""

from __future__ import annotations

import json
from typing import Any

from ..db.schema import get_db

JOB_SEARCH_HELD_MESSAGE = "Held for operator: job-search campaign. No reply will be sent."


def is_job_search_campaign(config: dict[str, Any] | None) -> bool:
    """True when a campaign config marks a job-search campaign."""
    if not isinstance(config, dict):
        return False
    return str(config.get("campaign_type") or "").strip().lower() == "job_search"


def is_job_search_config_json(config_json: Any) -> bool:
    """Same check on a campaign row's raw config_json value."""
    if isinstance(config_json, dict):
        return is_job_search_campaign(config_json)
    try:
        return is_job_search_campaign(json.loads(config_json or "{}"))
    except (json.JSONDecodeError, TypeError):
        return False


def job_search_campaign_ids() -> set[str]:
    """Ids of every job-search campaign. Sync: call through run_db."""
    db = get_db()
    try:
        rows = db.execute("SELECT id, config_json FROM campaigns").fetchall()
    finally:
        db.close()
    return {row["id"] for row in rows if is_job_search_config_json(row["config_json"])}
