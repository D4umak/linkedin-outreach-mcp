"""LLM prompts for the Communication Strategist (per-prospect daily planning)."""

from __future__ import annotations

import json
from typing import Any

STRATEGIST_SYSTEM = """You are an expert LinkedIn SDR strategist planning daily outreach actions.

For each prospect, you decide what actions to take TODAY based on:
1. Their current stage in the outreach funnel
2. What warm-up actions have already been done
3. Any signals or engagement history
4. What has worked for similar prospects (feedback data)
5. Natural pacing — avoid appearing robotic

Available actions:
- profile_view: View their LinkedIn profile (lightest touch, triggers notification)
- follow: Follow their profile (triggers "started following you" notification)
- endorse: Endorse their skills (triggers high-visibility notification)
- engage_comment: Comment on one of their recent posts
- engage_react: React (Like) to one of their recent posts
- invite: Send a LinkedIn connection request with personalized message
- inmail: Send InMail as first touch when credits or Open Profile allow it
- send_dm: Send a DM to an existing 1st-degree connection
- email: Continue the same story by email after a quiet LinkedIn first touch (never day-0)
- followup: Send a follow-up DM (only for connected prospects who already received a first message)
- voice_memo: Send a LinkedIn voice memo (only for connected prospects)
- skip_today: Do nothing today (rest, let previous actions breathe)

Rules:
- Max 3 actions per prospect per day
- Profile views and follows should precede engagement by at least 1 day
- Warm-up actions (profile_view, follow, engage) are recommended before inviting but NEVER required. Always include the core action (invite/send_dm/followup) in the plan — warm-up must not delay outreach.
- For connected prospects, space follow-ups 3-7+ days apart
- skip_today is valid and encouraged — not every prospect needs daily action
- Timing: morning (8-11 AM), afternoon (12-3 PM), evening (4-6 PM), anytime
- Consider the prospect's seniority: C-levels prefer less frequent, higher-quality touches
- Commenting on posts brings more value than reacting — prefer comments when posts have substantial text

Scoring context (from previous plans):
- A feedback score of +3 means the prospect replied after those actions
- A feedback score of +2 means they accepted the invitation
- A feedback score of -1 means they declined/ignored"""


STRATEGIST_BATCH_PROMPT = """Plan today's actions for each prospect below.

## Campaign Context
Campaign: {campaign_name}
ICP: {icp_summary}
Voice:
{voice_summary}

## Previous Plan Feedback (best & worst from last 7 days)
{feedback_section}

## Prospects to Plan ({prospect_count} total)
{prospects_section}

---

Return ONLY valid JSON:
{{
    "plans": [
        {{
            "outreach_id": "the_outreach_id",
            "actions": [
                {{
                    "action_type": "profile_view | follow | endorse | engage_comment | engage_react | invite | inmail | send_dm | email | followup | voice_memo | skip_today",
                    "priority": 1,
                    "timing_preference": "morning | afternoon | evening | anytime",
                    "params": {{}},
                    "rationale": "brief reason for this action"
                }}
            ]
        }}
    ]
}}

Important:
- Return a plan for EVERY prospect listed
- Order actions within each prospect by execution sequence (priority 1 first)
- Max 3 actions per prospect
- Include "skip_today" as sole action if no action is warranted today"""


def format_prospect_for_llm(prospect: dict[str, Any]) -> str:
    """Format a single prospect's context for the batch prompt.

    Concise one-line format to minimize tokens:
    ID | Name | Title @ Company | Status | Engagements | Messages | FitScore | Days
    """
    outreach_id = prospect.get("outreach_id", "?")
    name = prospect.get("name", "Unknown")
    title = prospect.get("title", "")
    company = prospect.get("company", "")
    status = prospect.get("status", "pending")
    eng_count = prospect.get("engagement_count", 0)
    eng_types = prospect.get("engagement_types") or "none"
    msg_count = prospect.get("message_count", 0)
    fit_score = prospect.get("fit_score") or 0.0
    followup_count = prospect.get("followup_count", 0)

    # Calculate days since last relevant event
    import time
    now = int(time.time())
    last_msg = prospect.get("last_message_at") or 0
    invited_at = prospect.get("invited_at") or 0
    accepted_at = prospect.get("accepted_at") or 0
    latest_event = max(last_msg, invited_at, accepted_at)
    days_since = (now - latest_event) // 86400 if latest_event else 0

    parts = [
        f"ID:{outreach_id}",
        f"{name}",
        f"{title} @ {company}" if company else title,
        f"status={status}",
        f"engagements={eng_count}({eng_types})",
        f"messages={msg_count}",
        f"followups={followup_count}",
        f"fit={fit_score:.1f}",
        f"days_since_event={days_since}",
    ]
    if prospect.get("prospect_timezone"):
        parts.append(f"tz={prospect['prospect_timezone']}")
    return " | ".join(parts)


def format_feedback_section(feedback_data: dict[str, Any]) -> str:
    """Format the best/worst plans from the last N days."""
    if not feedback_data:
        return "No feedback data available yet (first day of planning)."

    best = feedback_data.get("best", [])
    worst = feedback_data.get("worst", [])

    lines = []

    if best:
        lines.append("### Top performing plans (score = prospect outcome):")
        for p in best[:5]:
            actions = p.get("planned_actions", [])
            action_types = [a.get("action_type", "?") for a in actions]
            title = p.get("title", "?")
            score = p.get("feedback_score", 0)
            lines.append(f"  {title}: {' → '.join(action_types)} → score={score}")

    if worst:
        lines.append("### Lowest performing plans:")
        for p in worst[:5]:
            actions = p.get("planned_actions", [])
            action_types = [a.get("action_type", "?") for a in actions]
            title = p.get("title", "?")
            score = p.get("feedback_score", 0)
            lines.append(f"  {title}: {' → '.join(action_types)} → score={score}")

    vs = feedback_data.get("voice_stats") or {}
    if vs.get("voice_sent") or vs.get("text_sent"):
        lines.append(
            "### Channel format (this campaign): "
            f"voice memos {int(vs.get('voice_sent') or 0)} sent, "
            f"{(vs.get('voice_reply_rate') or 0) * 100:.0f}% reply rate; "
            f"text DMs {int(vs.get('text_sent') or 0)} sent, "
            f"{(vs.get('text_reply_rate') or 0) * 100:.0f}% reply rate. "
            "Prefer the format that is earning replies; use voice_memo where it wins."
        )

    return "\n".join(lines) if lines else "No feedback data available yet."
