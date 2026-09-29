"""An outreach's status, last action and next step, in the dashboard's words.

The prospects table on heylead.dev says "Closed: lost", "Stopped (campaign
archived)" and "No follow-up: they replied" where the store holds
``closed_unhappy``, ``archived_outreach_disarmed`` and ``replied``. The
exports wrote the codes, so a file disagreed with the page it came from
(D4umak/heylead-api#1926). This is the twin of heylead-api's
``app/services/outreach_labels.py``; ``export_campaign`` reads it here.

The words are the dashboard's, ported from heylead-dashboard:
``src/lib/outreachLabels.ts`` (status, ``readableCode``),
``src/features/campaigns/detail/lastAction.ts`` (last action),
``src/features/campaigns/detail/prospectLine.ts`` (next step) and
``src/lib/deferral.ts`` (a deferral's reason). Change them together. A code
neither side has seen still reads as words ("Some new code"), never blank
and never the code.
"""

from __future__ import annotations

import re

STATUS_LABELS: dict[str, str] = {
    "pending": "Pending",
    "invited": "Invited",
    "connected": "Connected",
    "messaged": "Messaged",
    "messaged_without_dm": "Connected",
    "replied": "Replied",
    "replied_without_dm": "Replied",
    "hot_lead": "Hot lead",
    "won": "Won",
    "skipped": "Skipped",
    "opted_out": "Opted out",
    "closed_happy": "Closed: won",
    "closed_unhappy": "Closed: lost",
    "unsubscribed": "Unsubscribed",
    "bounced": "Bounced",
    "expired": "Expired",
    "error": "Error",
    "engaged": "Engaged",
    "qualified": "Qualified",
}

LAST_ACTION_LABELS: dict[str, str] = {
    "below_min_fit": "Held: below min fit",
    "job_failed": "Step failed",
    "invite_llm_cooling": "Invite cooling (LLM)",
    "invite_failed": "Invite failed",
    "invited": "Invited",
    "discovered": "Added to campaign",
    "send_blocked": "Blocked",
    "pipeline_advance": "Moved to next step",
    "job_completed": "Step completed",
    "skip_invite": "Invite skipped",
    "sync_version_disagreement": "Syncing",
    "archived_outreach_disarmed": "Stopped (campaign archived)",
    "invite_weekly_cap_blocked": "Waiting: weekly invite cap",
    "night_pause": "Waiting: night pause",
    "night_pause_blocked": "Waiting: night pause",
    "campaign_review": "Campaign review",
    "job_trace_error": "Step failed",
    "reply_received": "They replied",
    "followup_skipped_prospect_replied": "Follow-up skipped: they replied",
    "dm_skipped_prospect_replied": "Message skipped: they replied",
    "decline_closed": "Closed: they declined",
    "send_fit_agent_decision": "Draft checked before sending",
    "send_fit_skipped": "Held: the draft did not fit the conversation",
    "reply_agent_decision": "Reply considered",
    "strategist_replan_decision": "Campaign plan revisited",
    # The journal's event type as a label; this module reads no journal rows.
    "agent_journal": "Agent note",
    "profile_view_warmup": "Viewed their profile",
    "skip_profile_view_warmup": "Profile view skipped",
    "skip_inmail": "InMail skipped",
    "skip_auto_reply": "Auto-reply skipped",
    "skip_followup": "Follow-up skipped",
    "skip_email_fallback": "Email skipped",
    "first_degree_excluded": "Skipped: already connected",
    "invite_to_connection_retired": "Already connected: invitation step closed",
    "invite_duplicate_person_skipped": "Skipped: already in another campaign",
    "engagement_invite_reserve": "Waiting: invites kept for people who engaged",
    "fit_unscored": "Held: fit not scored yet",
    "reply_restamped": "Reply time corrected",
    "reply_role_repaired": "Conversation record corrected",
    "reply_status_repaired": "Status corrected",
    "first_reply_restamped": "Reply time corrected",
    "duplicate_message_removed": "Removed a duplicate message",
}

# The executor's vocabulary for next_action_type, plus the codes
# next_step.next_step answers where no follow-up will run (#1723).
NEXT_STEP_LABELS: dict[str, str] = {
    "replied": "No follow-up: they replied",
    "check_in": "Check-in",
    "opted_out": "Nothing more: they opted out",
    "closed_won": "Nothing more: closed as won",
    "closed_lost": "Nothing more: closed as lost",
    "invite": "Invite",
    "send_invite": "Invite",
    "send_dm": "Opening message",
    "dm": "Opening message",
    "wait_accept": "Waiting for the invite to be accepted",
    "followup": "Follow-up",
    "follow_up": "Follow-up",
    "follow": "Follow-up",
    "inmail": "InMail",
    "email": "Email",
    "email_sent": "Email",
}

# business_hours_deferred is one event type for three different waits.
DEFERRAL_REASONS: dict[str, str] = {
    "outside_active_days": "active days",
    "outside_prospect_hours": "prospect's hours",
    "outside_workspace_hours": "workspace hours",
}

# Nothing about sync or versions reaches a reader: to them it is "Syncing".
_SYNC_INTERNALS = re.compile(r"sync|version", re.IGNORECASE)


def readable_code(code: str | None, fallback: str = "Unknown") -> str:
    """``"some_new_code"`` as ``"Some new code"``; ``fallback`` for nothing."""
    words = re.sub(r"\s+", " ", re.sub(r"[_-]+", " ", str(code or ""))).strip().lower()
    return words[0].upper() + words[1:] if words else fallback


def status_label(status: str | None) -> str:
    """The Status column's words; an empty status is a pending row."""
    s = str(status or "").strip().lower() or "pending"
    return STATUS_LABELS.get(s) or readable_code(s)


def deferral_label(reason: str | None) -> str:
    """``"Deferred: active days"``, or ``"Deferred"`` when nothing says why."""
    why = DEFERRAL_REASONS.get(str(reason or ""), "")
    return f"Deferred: {why}" if why else "Deferred"


def last_action_label(action: str | None, reason: str | None = None) -> str:
    """The Last Action column's words; ``""`` when there was none."""
    if not action or not isinstance(action, str):
        return ""
    if action == "business_hours_deferred":
        return deferral_label(reason)
    known = LAST_ACTION_LABELS.get(action)
    if known:
        return known
    if _SYNC_INTERNALS.search(action):
        return "Syncing"
    return readable_code(action, "")


def next_step_label(action: str | None) -> str:
    """The Next step column's words; ``""`` when nothing is next."""
    if not action or not isinstance(action, str):
        return ""
    known = NEXT_STEP_LABELS.get(action)
    if known:
        return known
    words = action.replace("_", " ").strip()
    return words[:1].upper() + words[1:]
