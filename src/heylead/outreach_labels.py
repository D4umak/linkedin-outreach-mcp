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

#: The reason a job_failed carries when it was LinkedIn asking for a pause
#: (heylead-api scheduler_store._last_action_reason, #2782).
LINKEDIN_PAUSE_REASON = "linkedin_pause"
PAUSED_BY_LINKEDIN = "Paused by LinkedIn"

#: The api's map, key for key (heylead-api#2922 F7): this one said "Added to
#: campaign", lacked the LinkedIn pause codes and send_fit_followups_ended.
#: tests/test_outreach_labels_parity_2922.py compares it with
#: tests/fixtures/last_action_labels.json, written from the api's map.
LAST_ACTION_LABELS: dict[str, str] = {
    "agent_journal": "Agent note",
    "approval_discarded": "Discarded: not sent",
    "approval_queued": "Approved: queued to send",
    "approval_released": "Released to autopilot",
    "approval_replaced": "Replaced by a newer draft",
    "approval_sent": "Sent after your approval",
    "approval_waiting": "Waiting: your approval",
    "approval_withdrawn": "Withdrawn: yours to answer",
    "archived_outreach_disarmed": "Stopped (campaign archived)",
    "below_min_fit": "Held: fit below the campaign floor",
    "campaign_review": "Campaign review",
    "close_before_contact_reopened": "Close undone: closed before any contact",
    "closed_thread_replied": "They replied after the conversation closed",
    "competitor_excluded": "Skipped: works at a competitor",
    "decline_closed": "Closed: they declined",
    "discovered": "Added to a campaign",
    "dm_prior_conversation_skipped": "Message skipped: you've talked before",
    "dm_skipped_already_messaged": "Message skipped: already messaged",
    "dm_skipped_already_visible": "Message skipped: already in the thread",
    "dm_skipped_prospect_replied": "Message skipped: they replied",
    "dm_waits_after_invite_note": "Waiting: opening message one working day after they accepted",
    "duplicate_message_removed": "Removed a duplicate message",
    "email_fallback_no_mailbox": "Email skipped: no mailbox connected",
    "email_fallback_sent": "Email sent",
    "engagement_invite_reserve": "Waiting: invites kept for people who engaged",
    "first_degree_excluded": "Skipped: already connected",
    "first_reply_restamped": "Reply time corrected",
    "fit_unscored": "Held: fit not scored yet",
    "followup_prior_conversation_skipped": "Follow-up skipped: you've talked before",
    "followup_rate_limited": "Paused by LinkedIn",
    "followup_skipped_prospect_replied": "Follow-up skipped: they replied",
    "hot_lead_alerted": "Hot lead: you were alerted",
    "hot_lead_closer_decision": "Hot lead: next step decided",
    "inbound_agent_decision": "Their message considered",
    "invite_duplicate_person_skipped": "Skipped: already in another campaign",
    "invite_failed": "Invite failed",
    "invite_linkedin_paused": "Paused by LinkedIn",
    "invite_llm_cooling": "Waiting: HeyLead retries the invitation shortly",
    "invite_no_message_detected": "Invitation went without a note",
    "invite_prior_conversation_skipped": "Invite skipped: you've talked before",
    "invite_rate_limited": "Paused by LinkedIn",
    "invite_skipped_already_connected": "Skipped: already connected",
    "invite_to_connection_retired": "Already connected: invitation step closed",
    "invite_weekly_cap_blocked": "Waiting: weekly invite cap",
    "invite_withdrawn": "Invitation withdrawn",
    "invite_withdrawn_to_free_spot": "Invitation withdrawn to make room",
    "invited": "Invited",
    "job_completed": "Step completed",
    "job_failed": "Step failed",
    "job_seeker_excluded": "Skipped: open to work",
    "job_trace_error": "Step failed",
    "lead_alert_emailed": "Emailed you about their reply",
    "lead_decayed_stale": "Gone quiet: no activity for a while",
    "linkedin_limit_observed": "Paused by LinkedIn",
    "model_failed": "Not sent: the check before sending didn't finish",
    "nameless_profile_skipped": "Skipped: no name on the profile",
    "night_pause": "Waiting: night pause",
    "night_pause_blocked": "Waiting: night pause",
    "not_now_hold_released": "Check-in due after \"not now\"",
    "not_now_reopened": "Reopened: it was a \"not now\"",
    "ooo_deferred": "Waiting: they're out of office",
    "operator_hold_released": "Released by you",
    "operator_mcp_send": "Sent by you, not by autopilot",
    "pipeline_advance": "Moved to next step",
    "profile_read_budget_spent": "Waiting: today's profile reads are used up",
    "profile_view_warmup": "Viewed their profile",
    "referral_not_enrolled": "Not added: referral",
    "referral_not_enrolled_job_search": "Not added: referral",
    "reply_agent_decision": "Reply considered",
    "reply_received": "They replied",
    "reply_restamped": "Reply time corrected",
    "reply_role_repaired": "Conversation record corrected",
    "reply_status_repaired": "Status corrected",
    "send_blocked": "Blocked",
    "send_deferred_no_sender_profile": "Waiting: your profile is still being read",
    "send_dm_rate_limited": "Paused by LinkedIn",
    "send_fit_agent_decision": "Draft checked before sending",
    "send_fit_followups_ended": "Follow-ups stopped: each draft was held",
    "send_fit_fulfilled": "Research for the draft done",
    "send_fit_requested": "Waiting: more research before sending",
    "send_fit_skipped": "Held: the draft did not fit the conversation",
    "send_fit_unfit": "Held: the draft did not fit the conversation",
    "send_held_for_approval": "Waiting: your approval",
    "skip_auto_reply": "Auto-reply skipped",
    "skip_email_fallback": "Email skipped",
    "skip_engage": "Post engagement skipped",
    "skip_engage_post": "Post engagement skipped",
    "skip_engagement": "Post engagement skipped",
    "skip_follow": "Follow skipped",
    "skip_follow_profile": "Follow skipped",
    "skip_followup": "Follow-up skipped",
    "skip_hot_lead_closer": "Hot-lead next step skipped",
    "skip_inmail": "InMail skipped",
    "skip_invite": "Invite skipped",
    "skip_profile_view_warmup": "Profile view skipped",
    "skip_send_discovery_dms": "Message skipped",
    "skip_send_dm": "Message skipped",
    "skip_send_email": "Email skipped",
    "skip_send_follow_up": "Follow-up skipped",
    "skip_send_followup": "Follow-up skipped",
    "skip_send_inmail": "InMail skipped",
    "skip_send_invitation": "Invite skipped",
    "skip_send_invite": "Invite skipped",
    "skip_send_reply": "Reply skipped",
    "skip_view_profile": "Profile view skipped",
    "skip_withdraw_invite": "Invitation withdrawal skipped",
    "strategist_replan_decision": "Campaign plan revisited",
    "sync_version_disagreement": "Syncing",
    "unanswered_no_reopened": "Reopened: their \"no\" gets an answer",
    "visible_dm_recorded": "Answered outside HeyLead",
    "voice_dm_fell_back_to_text": "Sent as text",
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


#: Words a code writes in lower case that are written otherwise ("DM", not
#: "Dm"): heylead-api's outreach_labels.WORD_FORMS; change them together.
WORD_FORMS: dict[str, str] = {
    "icp": "ICP", "rag": "RAG", "dm": "DM", "dms": "DMs", "api": "API", "crm": "CRM",
    "llm": "LLM", "ai": "AI", "sn": "SN", "url": "URL", "urls": "URLs", "id": "ID",
    "ids": "IDs", "mcp": "MCP", "ooo": "OOO", "seo": "SEO", "csv": "CSV", "http": "HTTP",
    "json": "JSON", "smtp": "SMTP", "ui": "UI", "b2b": "B2B", "faq": "FAQ",
    "linkedin": "LinkedIn", "inmail": "InMail", "inmails": "InMails", "hubspot": "HubSpot",
    "unipile": "Unipile", "slack": "Slack", "gmail": "Gmail",
}


def readable_code(code: str | None, fallback: str = "Unknown") -> str:
    """``"some_new_code"`` as ``"Some new code"``, ``"dm_waits"`` as ``"DM waits"``; ``fallback`` for nothing."""
    words = re.sub(r"\s+", " ", re.sub(r"[_-]+", " ", str(code or ""))).strip().lower()
    if not words:
        return fallback
    spelled = " ".join(WORD_FORMS.get(w, w) for w in words.split(" "))
    return spelled[0].upper() + spelled[1:]


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
    if action == "job_failed" and reason == LINKEDIN_PAUSE_REASON:
        return PAUSED_BY_LINKEDIN
    known = LAST_ACTION_LABELS.get(action)
    if known:
        return known
    if _SYNC_INTERNALS.search(action):
        return "Syncing"
    return _family_label(action) or readable_code(action, "")


#: The step a ``<lane>_held_offer_unconfirmed`` code names: always a first touch.
_FIRST_TOUCH_STEP: dict[str, str] = {
    "dm": "Opening message",
    "followup": "Opening message",
    "inmail": "InMail",
    "email": "Email",
}


def _family_label(action: str) -> str:
    """Words for a code built from a pattern, or ``""``: the api's, word for word (#2922 F7)."""
    if action.endswith("_held_offer_unconfirmed"):
        step = _FIRST_TOUCH_STEP.get(action[: -len("_held_offer_unconfirmed")], "First message")
        return f"{step} held: offer not confirmed yet"
    if action.endswith("_backoff_skipped"):
        return PAUSED_BY_LINKEDIN
    if action.endswith("_blocked_recipient_park"):
        return "Stopped: they can't be messaged"
    if action.startswith("skip_"):
        return "Step skipped"
    return ""


def next_step_label(action: str | None) -> str:
    """The Next step column's words; ``""`` when nothing is next."""
    if not action or not isinstance(action, str):
        return ""
    known = NEXT_STEP_LABELS.get(action)
    if known:
        return known
    words = action.replace("_", " ").strip()
    return words[:1].upper() + words[1:]
