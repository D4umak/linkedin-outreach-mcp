"""Tool: prospect — Manage prospects (skip, close, view conversation).

Thin dispatcher that routes to existing run_* functions based on the action parameter.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def run_prospect(
    action: str,
    outreach_id: str = "",
    campaign_id: str = "",
    outcome: str = "won",
    reason: str = "",
    meeting_link: str = "",
    confirm: bool = False,
    reason_code: str = "",
    reason_note: str = "",
) -> str:
    """Manage a prospect's outreach status.

    Skip and Stop are different decisions and this is the one place both are
    stated (9 Sep 2026):

      Skip — leave this person out of THIS campaign. No other effect: they
             are not suppressed anywhere else and another campaign may still
             reach them.
      Stop — close(outcome='opt_out'). Stops all outreach to this person
             across the whole workspace, cancels their queued jobs, and
             records why. That feedback improves targeting.

    Actions:
      skip         — Skip a prospect, removing them from this campaign's queue
      close        — Record an outcome (won/lost/opt_out) for an outreach
      dismiss      — Clear a lead off Needs attention (closes it as lost)
      conversation — View the full conversation thread with a prospect
      timeline     — View chronological journey of all actions for a prospect

    Args:
        action: What to do: 'skip', 'close', 'conversation', 'timeline'.
        outreach_id: The outreach ID. Auto-selects if empty (except 'conversation'/'timeline').
        campaign_id: Which campaign (for 'skip'). Uses active if empty.
        outcome: 'won', 'lost', or 'opt_out' (for 'close' action).
        reason: Optional notes for the outcome (for 'close' action).
        meeting_link: Meeting/calendar URL if outcome is 'won' (for 'close' action).
            Could be the user's booking page or the prospect's shared calendar.
        confirm: Required True to actually dismiss (for 'dismiss' action).
        reason_code: Why, for 'skip' and 'close'. One of 'not_a_fit',
            'negative_reply', 'asked_to_stop', 'handled_elsewhere', 'other'.
            Only 'not_a_fit' and 'negative_reply' are evidence about
            targeting; the other three are facts about that one person and
            never move a segment's ranking.
        reason_note: Free text alongside the code.
    """
    action = action.lower().strip()

    if action == "timeline":
        if not outreach_id:
            return "Error: 'outreach_id' is required for action='timeline'."
        from .prospect_timeline import run_prospect_timeline
        return await run_prospect_timeline(outreach_id)

    if action == "skip":
        from .skip_prospect import run_skip_prospect
        return await run_skip_prospect(
            outreach_id, campaign_id,
            reason_code=reason_code, reason_note=reason_note,
        )

    if action == "close":
        from .close_outreach import run_close_outreach
        return await run_close_outreach(
            outreach_id=outreach_id,
            outcome=outcome,
            reason=reason,
            meeting_link=meeting_link,
            reason_code=reason_code,
            reason_note=reason_note,
        )

    if action == "dismiss":
        from .dismiss_lead import run_dismiss_lead
        return await run_dismiss_lead(
            outreach_id=outreach_id,
            reason=reason,
            confirm=confirm,
        )

    if action == "conversation":
        if not outreach_id:
            return (
            "Error: 'outreach_id' is required for action='conversation'.\n"
            "show_status() prints an id under each Needs attention lead, and "
            "suggest_next_action() prints one for each recommendation."
        )
        from .show_conversation import run_show_conversation
        return await run_show_conversation(outreach_id)

    return (
        f"Unknown action: '{action}'. "
        "Use 'skip', 'close', 'dismiss', 'conversation', or 'timeline'."
    )
