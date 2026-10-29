"""Actual direct-install settings; hosted accounts use the remote authority (#4626)."""
from __future__ import annotations

import json
from typing import Any

from .. import config
from ..db.queries import get_setting, list_campaigns
from ..flags import flag_enabled
from .coordinator import coordinator_mode
from .count_words import count_noun
from .hot_lead_closer import hot_lead_closer_mode
from .icp_research import icp_research_mode
from .reply_agent import reply_agent_mode
from .strategist_replan import strategist_replan_mode

_READERS = {
    "Reply exception agent": (reply_agent_mode, "reply_agent_mode", "enable_reply_agent"),
    "Strategist": (strategist_replan_mode, "strategist_replan_mode", "enable_strategist_replan_agent"),
    "Meeting closer": (hot_lead_closer_mode, "hot_lead_closer_mode", "enable_hot_lead_closer"),
    "Campaign guard": (coordinator_mode, "coordinator_agent_mode", "enable_coordinator_agent"),
}
_MODE_INPUTS = frozenset({"off", "observe", "act", "on", "true", "yes", "1", "false", "no", "0"})


def _mode_line(label, reader, cfg, mode_key, enable_key, scope):
    raw = cfg.get(mode_key)
    supplied = isinstance(raw, str) and raw.strip().lower() in _MODE_INPUTS
    supplied = supplied or cfg.get(enable_key) not in (None, "")
    return f"• {label}: {reader(cfg)} · source: {scope if supplied else 'default'}"


def _config(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        loaded = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def inspect_local(campaign_id: str, outreach_id: str, limit: int) -> str:
    if outreach_id:
        return "Autonomy is campaign-scoped. Pass campaign_id without outreach_id."
    try:
        return _inventory(campaign_id, max(1, min(int(limit), 100)))
    except Exception:
        return "Agent authority unavailable. Could not read this installation's settings. No settings changed."


def _inventory(campaign_id: str, limit: int) -> str:
    campaigns = sorted(list_campaigns(), key=lambda c: str(c["id"]))
    if campaign_id:
        exact = [c for c in campaigns if c["id"] == campaign_id]
        campaigns = exact or [c for c in campaigns if str(c["id"]).startswith(campaign_id)]
        if not campaigns:
            return "Campaign not found in this installation."
        if len(campaigns) != 1:
            return "Campaign selection is ambiguous. Pass the full campaign ID."
    profile = {key: get_setting(key, "") or ""
               for key in ("icp_research_mode", "enable_icp_research_agent")}
    mode = config.get_scheduler_mode()
    host = config.get_sending_host()
    lines = ["Agent authority · this installation", f"Sending host: {host}", f"Scheduler: {mode}"]
    if host == "cloud":
        lines.extend(["Local scheduled sending: blocked · cloud is responsible.",
                      "Cloud agent authority: not established by this local view."])
    elif mode != "full":
        lines.append("Local scheduled sending: blocked · scheduler is not in full mode.")
    else:
        lines.append("Local scheduler mode gate: passed; runtime eligibility is not evaluated here.")
    lines.extend(["Approval setting: hosted message-approval policy is unavailable here.",
                  "Standing delegation: not evaluated; existing settings govern this view.", "",
                  "Installation agent settings:",
                  _mode_line("Audience research", icp_research_mode, profile, "icp_research_mode",
                             "enable_icp_research_agent", "installation setting"),
                  "• Inbound triage: unavailable here (hosted agent)",
                  f"Showing {min(limit, len(campaigns))} of {count_noun(len(campaigns), 'campaign')}."])
    for campaign in campaigns[:limit]:
        cfg = _config(campaign.get("config_json"))
        status = str(campaign.get("status") or "unknown")
        lines.extend(["", f"Campaign {campaign['id']}: {status}",
                      f"Campaign mode: {campaign.get('mode') or 'unknown'}"])
        if status != "active":
            lines.append("New campaign planning: blocked · campaign is not active.")
        elif campaign.get("mode") != "autopilot":
            lines.append("New campaign planning: blocked · campaign is not in autopilot.")
        if status == "completed":
            lines.append("Completed campaigns may still schedule owed follow-ups and opening messages for recent late accepters.")
        for label, (reader, mode_key, enable_key) in _READERS.items():
            lines.append(_mode_line(label, reader, cfg, mode_key, enable_key, "campaign"))
        lines.extend(["• Send-fit gate: unavailable here (hosted agent)",
                      "Normal automatic replies: " + ("on" if flag_enabled(cfg, "enable_auto_replies") else "off")
                      + " — separate from the reply exception agent."])
    if not campaigns:
        lines.append("No campaigns found. Campaign-specific settings are unavailable.")
    if len(campaigns) > limit:
        lines.append("More campaigns omitted. Pass campaign_id for a complete campaign view.")
    lines.extend(["", "Act applies only to each agent's implemented actions.",
                  "Runtime quality, recipient and provider checks, approvals and conversation/campaign holds still apply.",
                  "These settings do not show that a message was sent or delivered.", "No settings changed."])
    return "\n".join(lines)
