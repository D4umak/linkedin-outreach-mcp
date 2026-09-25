"""Tool: suggest_next_action — Temperature-scored next action advisor.

Analyzes all active campaigns and recommends the highest-priority
action using a 4-rule decision engine ported from AI in Charge:

Temperature classification (cold/warm/hot):
- COLD: <MIN_WARMUP_ENGAGEMENTS engagements, no invitation sent
- WARM: Invited or connected, some engagement
- HOT: Active conversation, replied, or multiple exchanges

Priority rules:
R1: Mandatory warm-up for cold prospects (block invitations/follow-ups)
R2: Conversation-driven constraints (must reply if interested, close if not)
R3: Filter invalid actions based on outreach state
R4: Score remaining actions: EngagementFit + SequenceFit + TimingFit (0-9)

Priority order:
1. Hot leads (reply ASAP)
2. Questions to answer
3. Pending approvals
4. Follow-ups due
5. Warm-up engagement for invited prospects
6. Invitations ready (warm-up complete)
7. Warm-up needed (engage before inviting)
8. Stale leads needing attention
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..flags import flag_enabled
from ..config import get_tier
from ..constants import (
    MIN_WARMUP_ENGAGEMENTS,
    PRO_FOLLOWUP_SCHEDULE_DAYS,
    TIER_PRO,
)
from ..db.queries import (
    get_campaign,
    get_engagement_count_for_outreach,
    get_last_activity_timestamp,
    get_messages_for_outreach,
    get_rate_limit_today,
    get_sending_days_7d,
    get_setting,
    get_stale_outreaches,
    get_weekly_invitation_sum,
    get_campaign_stats,
    list_campaigns,
    list_inbound_signals,
)
from ..db.schema import get_db
from ..formatter import stars
from ..linkedin import get_account_id, get_linkedin_client, UnipileError
from ..services.health_score import coerce_daily_limit, compute_health_score
from ..db.async_bridge import run_db

logger = logging.getLogger(__name__)

# Don't suggest follow-up if contacted less than this many days ago
MIN_FOLLOWUP_DAYS = 3

# For invited prospects, suggest engagement after this many days
INVITED_ENGAGE_AFTER_DAYS = 3

# Max recommendations to show
MAX_RECOMMENDATIONS = 5

# Temperature thresholds (ported from original AI in Charge R1)
WARM_UP_COMPLETE_THRESHOLD = MIN_WARMUP_ENGAGEMENTS  # Engagements needed to leave COLD
HOT_THRESHOLD_MESSAGES = 2  # Messages exchanged to reach HOT

# Icons for each action type
ACTION_ICONS = {
    "hot_lead": "\U0001f525",
    "question": "\u2753",
    "approval": "\U0001f440",
    "followup": "\U0001f4ac",
    "engage_invited": "\U0001f3af",
    "invite_ready": "\U0001f4e4",
    "warmup": "\U0001f3af",
    "new_campaign": "\U0001f680",
    "stale": "\u23f0",
    "errored": "\u26a0\ufe0f",
}

# Voice memo advice: minimum sends before judging, and how far below text
# the voice reply rate must sit before we recommend turning it off.
VOICE_ADVICE_MIN_SENT = 10
VOICE_ADVICE_UNDERPERFORM_RATIO = 0.5


def _voice_mode_advice(stats: dict | None) -> str | None:
    """Return "text_only" when voice memos are clearly not earning replies.

    Compares the voice reply rate with the text reply rate from
    get_voice_memo_stats. The old check read a ``voice_accepted`` key that
    the stats never contained, so it always saw zero and nagged every
    campaign after its 10th voice memo regardless of outcome.
    """
    stats = stats or {}
    voice_sent = int(stats.get("voice_sent") or 0)
    if voice_sent < VOICE_ADVICE_MIN_SENT:
        return None
    voice_rate = float(stats.get("voice_reply_rate") or 0.0)
    text_rate = float(stats.get("text_reply_rate") or 0.0)
    if voice_rate <= 0.0:
        return "text_only"
    if text_rate > 0.0 and voice_rate < text_rate * VOICE_ADVICE_UNDERPERFORM_RATIO:
        return "text_only"
    return None


# Temperature labels
TEMP_COLD = "cold"
TEMP_WARM = "warm"
TEMP_HOT = "hot"
TEMP_ICONS = {TEMP_COLD: "\u2744\ufe0f", TEMP_WARM: "\U0001f321\ufe0f", TEMP_HOT: "\U0001f525"}


def _classify_temperature(
    status: str, engagement_count: int, message_count: int,
) -> str:
    """Classify a prospect's temperature based on engagement and conversation depth.

    Returns: "cold", "warm", or "hot"
    """
    # HOT: Active conversation (replied/hot_lead) or 2+ message exchanges
    if status in ("hot_lead", "replied") or message_count >= HOT_THRESHOLD_MESSAGES:
        return TEMP_HOT

    # WARM: Invited/connected with some engagement, or has messages
    if status in ("connected", "invited", "messaged") or engagement_count >= WARM_UP_COMPLETE_THRESHOLD:
        return TEMP_WARM

    # COLD: No engagement, pending
    return TEMP_COLD


def _score_action(
    priority: int,
    temperature: str,
    engagement_count: int,
    days_since_activity: int,
    fit_score: float,
) -> float:
    """Score an action using R4: EngagementFit + SequenceFit + TimingFit.

    Returns a composite score (0-9) for tie-breaking within priority tiers.
    Higher is better.
    """
    # EngagementFit (0-3): How well the action matches the engagement stage
    if temperature == TEMP_HOT:
        engagement_fit = 3.0 if priority <= 2 else 1.0  # Hot leads → reply first
    elif temperature == TEMP_WARM:
        engagement_fit = 3.0 if priority in (4, 5, 6) else 1.5  # Warm → follow-up/engage
    else:
        engagement_fit = 3.0 if priority == 7 else 0.5  # Cold → warm-up only

    # SequenceFit (0-3): Natural progression in the outreach cadence
    # Higher engagement count = further in sequence = higher fit for advanced actions
    if engagement_count >= 5:
        sequence_fit = 3.0 if priority in (4, 6) else 2.0  # Ready for follow-up/invite
    elif engagement_count >= WARM_UP_COMPLETE_THRESHOLD:
        sequence_fit = 2.5 if priority in (5, 6) else 1.5  # Ready for invite
    else:
        sequence_fit = 2.0 if priority == 7 else 1.0  # Needs warm-up

    # TimingFit (0-3): Appropriate timing given activity recency
    if days_since_activity == 0:
        timing_fit = 1.0  # Just active — might be too soon
    elif 1 <= days_since_activity <= 3:
        timing_fit = 3.0  # Sweet spot
    elif 4 <= days_since_activity <= 7:
        timing_fit = 2.0  # Good window
    elif 8 <= days_since_activity <= 14:
        timing_fit = 1.5  # Getting stale
    else:
        timing_fit = 0.5  # Very stale

    # Fit score bonus (0-1 scale → 0-0.5 bonus). The old divisor of 7 and cap
    # of 1.5 were left from a scale fit_score no longer uses: on a fraction a
    # perfect fit earned 0.14 of the 0.5 this comment promises, and the cap was
    # unreachable.
    fit_bonus = min(max(fit_score, 0.0) * 0.5, 0.5) if fit_score else 0.0

    return engagement_fit + sequence_fit + timing_fit + fit_bonus


async def run_suggest_next_action(campaign_id: str = "") -> str:
    """Suggest the best next action for your outreach.

    Analyzes all active campaigns and recommends the highest-priority
    action: reply to hot leads, approve pending messages, send follow-ups,
    warm up prospects with engagement, or send invitations.
    """

    # ── Pre-checks ──
    setup_done = await run_db(get_setting, "setup_complete", False)
    if not setup_done:
        return (
            "Setup required before suggesting actions.\n\n"
            "Please run setup_profile first."
        )

    # ── Account connectivity check ──
    try:
        from ..linkedin import get_account_id, get_linkedin_client
        account_id = await run_db(get_account_id)
        if account_id:
            client = get_linkedin_client()
            try:
                connected, msg = await client.verify_account(account_id)
                if not connected:
                    return (
                        "⚠️ **Priority 1: Reconnect LinkedIn Account**\n\n"
                        f"Your account is not connected: {msg}\n\n"
                        "All outreach is paused until you reconnect.\n"
                        "Go to https://heylead.dev/auth/login-url to reconnect,\n"
                        "then run setup_profile(backend_jwt='YOUR_TOKEN')."
                    )
            finally:
                await client.close()
    except Exception:
        pass  # Non-fatal, continue with suggestions

    # ── Load campaigns ──
    if campaign_id:
        campaign = await run_db(get_campaign, campaign_id)
        if not campaign:
            return f"Campaign not found: {campaign_id}"
        campaigns = [campaign]
    else:
        campaigns = await run_db(list_campaigns, status="active")
        if not campaigns:
            # Check for paused campaigns before suggesting "create one"
            paused = await run_db(list_campaigns, status="paused")
            if paused:
                lines = ["All campaigns are currently paused.\n"]
                lines.append("Resume one to restart outreach:")
                for c in paused[:5]:
                    name = c.get("name", "Unnamed")[:40]
                    cid = c["id"][:8]
                    lines.append(f"  campaign(action='resume', campaign_id='{cid}...')"
                                 f"  — {name}")
                return "\n".join(lines)
            return (
                "No active campaigns.\n\n"
                "Create one first: create_campaign(\"your target description\")"
            )

    tier = get_tier()
    now = int(time.time())
    from ..services.campaign_plan import effective_max_followups

    # ── Scan all outreaches across campaigns ──
    recommendations: list[dict[str, Any]] = []

    for camp in campaigns:
        cid = camp["id"]
        camp_name = camp["name"]
        # This campaign's own count, as its plan promises: its max_followups
        # under the tier ceiling, 0 when follow-ups are off (#1414).
        max_followups = effective_max_followups(camp.get("config_json"), tier)

        def _get_outreaches():
            db = get_db()
            rows = db.execute(
                """SELECT o.id as outreach_id, o.campaign_id, o.contact_id,
                          o.status, o.followup_count, o.next_action, o.updated_at,
                          o.invited_at, o.created_at,
                          o.last_attempt_error,
                          c.name, c.title, c.company, c.fit_score, c.linkedin_url, c.source
                   FROM outreaches o
                   JOIN contacts c ON o.contact_id = c.id
                   WHERE o.campaign_id = ?
                     AND o.status NOT IN ('opted_out', 'closed_happy', 'closed_unhappy', 'skipped')
                   ORDER BY c.fit_score DESC""",
                (cid,),
            ).fetchall()
            db.close()
            return rows
        outreaches = await run_db(_get_outreaches)
        # Held for a person: the hold subset of who is waiting, fresh by the
        # rule inspect and Needs attention share. Until 25 Sep 2026 this
        # judged holds itself and kept one a person had answered since.
        from ..services.waiting_on_you import HOLD, who_is_waiting
        held = {w.outreach_id: w for w in await run_db(who_is_waiting, kinds=(HOLD,), campaign_id=cid)}

        # A campaign can hold hundreds of failed rows; three is enough to tell
        # the user the vertical needs attention without burying everything else.
        errored_shown = 0

        for row in outreaches:
            r = dict(row)
            status = r["status"]
            name = r.get("name", "Unknown")
            title = r.get("title", "")
            company = r.get("company", "")
            fit_score = r.get("fit_score", 0)
            outreach_id = r["outreach_id"]
            followup_count = r.get("followup_count", 0) or 0

            role_str = title
            if company:
                role_str += f" at {company}" if role_str else company

            # Source tag for non-search prospects
            _src = r.get("source", "search")
            if _src in ("signal_discovery", "inbound_invitation", "inbound_dm", "inbound_comment", "csv_import"):
                from ..constants import SOURCE_LABELS
                _src_tag = f" [{SOURCE_LABELS.get(_src, _src)}]"
            else:
                _src_tag = ""

            # Compute temperature for this prospect
            eng_count = await run_db(get_engagement_count_for_outreach, outreach_id)
            messages = await run_db(get_messages_for_outreach, outreach_id)
            msg_count = len(messages) if messages else 0
            temperature = _classify_temperature(status, eng_count, msg_count)
            temp_badge = TEMP_ICONS.get(temperature, "")

            # Compute days since last activity (reused across checks)
            last_activity = await run_db(get_last_activity_timestamp, outreach_id)
            days_since = (now - last_activity) // 86400 if last_activity else 999

            # Check if auto-reply is handling this campaign
            _camp_config = {}
            try:
                import json as _json
                _camp_config = _json.loads(camp.get("config_json", "{}") or "{}")
            except (ValueError, TypeError):
                pass
            _auto_reply_on = flag_enabled(_camp_config, "enable_auto_replies")
            _auto_reply_note = ""
            if _auto_reply_on and camp.get("mode") == "autopilot":
                _auto_reply_note = " (auto-reply enabled — scheduler will handle)"

            # Priority 1: Operator hold (exception agent)
            _held = held.get(outreach_id)
            if _held is not None:
                score = _score_action(1, temperature, eng_count, days_since, fit_score)
                recommendations.append({
                    "priority": 1,
                    "score": score + 1.0,
                    "icon": ACTION_ICONS.get("approval", ACTION_ICONS["hot_lead"]),
                    "text": (
                        f"Held for operator: **{name}** ({role_str}){_src_tag} — "
                        f"{_held.held_because}"
                    ),
                    "action": f'Run: send_message(action="reply", outreach_id="{outreach_id}")',
                    "fit_score": fit_score,
                    "campaign": camp_name,
                    "temp": temp_badge,
                })
                continue

            # Priority 1: Hot leads
            if status == "hot_lead":
                score = _score_action(1, temperature, eng_count, days_since, fit_score)
                # Check if prospect shared a calendar link (stored in next_action)
                _next_action_raw = r.get("next_action", "")
                _cal_note = ""
                if _next_action_raw:
                    try:
                        import json as _json2
                        _na = _json2.loads(_next_action_raw)
                        if isinstance(_na, dict) and _na.get("type") == "book_meeting":
                            # prospect_calendar_url (new) with calendar_url fallback (legacy data)
                            _cal_url = _na.get("prospect_calendar_url") or _na.get("calendar_url", "")
                            if _cal_url:
                                _cal_note = f"\n   📅 Book meeting: {_cal_url}"
                    except (ValueError, TypeError):
                        pass
                recommendations.append({
                    "priority": 1,
                    "score": score,
                    "icon": ACTION_ICONS["hot_lead"],
                    "text": f"Reply to **{name}** ({role_str}){_src_tag} — they're interested!{_auto_reply_note}{_cal_note}",
                    "action": f'Run: send_message(action="reply", outreach_id="{outreach_id}")',
                    "fit_score": fit_score,
                    "campaign": camp_name,
                    "temp": temp_badge,
                })

            # Priority 2: Questions to answer
            elif status == "replied":
                last_msg = messages[-1] if messages else None
                if last_msg and last_msg.get("sentiment") == "question":
                    score = _score_action(2, temperature, eng_count, days_since, fit_score)
                    recommendations.append({
                        "priority": 2,
                        "score": score,
                        "icon": ACTION_ICONS["question"],
                        "text": f"Answer **{name}**'s question ({role_str}){_src_tag}{_auto_reply_note}",
                        "action": f'Run: send_message(action="reply", outreach_id="{outreach_id}")',
                        "fit_score": fit_score,
                        "campaign": camp_name,
                        "temp": temp_badge,
                    })

            # Priority 4: Follow-ups due (R1: blocked for COLD prospects)
            elif status == "connected" and followup_count < max_followups:
                # R1: Cold prospects must warm up before follow-ups
                if temperature == TEMP_COLD:
                    score = _score_action(7, temperature, eng_count, days_since, fit_score)
                    recommendations.append({
                        "priority": 7,
                        "score": score,
                        "icon": ACTION_ICONS["warmup"],
                        "text": f"Warm up **{name}** before follow-up ({eng_count}/{WARM_UP_COMPLETE_THRESHOLD} touches) {temp_badge}",
                        "action": "Run: engage_prospect()",
                        "fit_score": fit_score,
                        "campaign": camp_name,
                        "temp": temp_badge,
                    })
                    continue

                # Check pro schedule
                if tier == TIER_PRO and followup_count > 0:
                    schedule_idx = min(followup_count, len(PRO_FOLLOWUP_SCHEDULE_DAYS) - 1)
                    required_days = PRO_FOLLOWUP_SCHEDULE_DAYS[schedule_idx]
                else:
                    required_days = MIN_FOLLOWUP_DAYS

                if days_since >= required_days:
                    score = _score_action(4, temperature, eng_count, days_since, fit_score)
                    recommendations.append({
                        "priority": 4,
                        "score": score,
                        "icon": ACTION_ICONS["followup"],
                        "text": f"Send follow-up #{followup_count + 1} to **{name}** ({role_str}) {temp_badge}",
                        "action": 'Run: send_message(action="followup")',
                        "fit_score": fit_score,
                        "campaign": camp_name,
                        "temp": temp_badge,
                    })

            # Priority 5: Engaged invited prospects — warm them up
            elif status == "invited":
                # invited_at, not updated_at: the row's mtime moves whenever
                # any column changes (a sync echo, a warm-up, a version bump),
                # so "invited Nd ago" counted row edits rather than days.
                invited_at = r.get("invited_at") or r.get("created_at") or 0
                days_since_invite = (now - invited_at) // 86400
                if days_since_invite >= INVITED_ENGAGE_AFTER_DAYS and eng_count < 3:
                    score = _score_action(5, temperature, eng_count, days_since, fit_score)
                    recommendations.append({
                        "priority": 5,
                        "score": score,
                        "icon": ACTION_ICONS["engage_invited"],
                        "text": f"Engage with **{name}**'s posts (invited {days_since_invite}d ago) {temp_badge}",
                        "action": "Run: engage_prospect()",
                        "fit_score": fit_score,
                        "campaign": camp_name,
                        "temp": temp_badge,
                    })

            # Priority 6 & 7: Pending prospects (R1 enforcement)
            elif status == "pending":
                if eng_count >= MIN_WARMUP_ENGAGEMENTS:
                    # Warm-up complete → ready to invite
                    score = _score_action(6, temperature, eng_count, days_since, fit_score)
                    recommendations.append({
                        "priority": 6,
                        "score": score,
                        "icon": ACTION_ICONS["invite_ready"],
                        "text": f"Send invitation to **{name}** ({role_str}){_src_tag} — warm-up done ({eng_count} touches) {temp_badge}",
                        "action": "Run: generate_and_send()",
                        "fit_score": fit_score,
                        "campaign": camp_name,
                        "temp": temp_badge,
                    })
                else:
                    # R1: Needs warm-up first (COLD)
                    score = _score_action(7, temperature, eng_count, days_since, fit_score)
                    recommendations.append({
                        "priority": 7,
                        "score": score,
                        "icon": ACTION_ICONS["warmup"],
                        "text": f"Engage with **{name}**'s posts first ({eng_count}/{MIN_WARMUP_ENGAGEMENTS} warm-up) {temp_badge}",
                        "action": "Run: engage_prospect()",
                        "fit_score": fit_score,
                        "campaign": camp_name,
                        "temp": temp_badge,
                    })

            # Priority 7: A send that failed. 'error' is retryable — that is
            # what separates it from 'skipped' — but nothing retries it on its
            # own: run_retry_failed has no scheduled caller, only
            # campaign(action='retry_failed'). Until this branch existed these
            # rows were selected, iterated and dropped, so the tool reported
            # "All caught up" over an outreach that had failed days earlier.
            #
            # The stored reason is quoted, not interpreted. These strings cover
            # both the recoverable (an id not yet backfilled, a prospect who
            # has not accepted yet) and the permanent (a company page, a bare
            # numeric id), and reading a cause out of an error string is how
            # this codebase has been wrong before. The user can tell them
            # apart; a keyword match cannot.
            elif status == "error" and errored_shown < 3:
                errored_shown += 1
                reason = (r.get("last_attempt_error") or "").strip()
                detail = f" — {reason}" if reason else ""
                score = _score_action(7, temperature, eng_count, days_since, fit_score)
                recommendations.append({
                    "priority": 7,
                    "score": score,
                    "icon": ACTION_ICONS["errored"],
                    "text": f"Send to **{name}** ({role_str}) failed{detail}",
                    "action": f"Run: campaign(action='retry_failed', campaign_id='{cid}')",
                    "fit_score": fit_score,
                    "campaign": camp_name,
                    "temp": temp_badge,
                })

        # Priority 8: Close stale outreaches (no activity for 14+ days)
        stale = await run_db(get_stale_outreaches, cid, stale_days=14)
        for s in stale[:3]:  # Cap at 3 stale recommendations per campaign
            recommendations.append({
                "priority": 8,
                "score": 0.0,
                "icon": ACTION_ICONS["stale"],
                "text": f"**{s['name']}** has been inactive for {s['days_stale']}d \u2014 close or re-engage?",
                "action": f'prospect(action="close", outreach_id="{s["outreach_id"]}") or send_message(action="followup")',
                "fit_score": s.get("fit_score", 0),
                "campaign": camp_name,
                "temp": "",
            })

    # ── Qualified inbound leads (priority 0-1, above everything) ──
    try:
        from ..constants import INBOUND_ENGAGE_CONFIDENCE, INBOUND_ASK_PURPOSE_CONFIDENCE
        qualified_inbound = await run_db(list_inbound_signals, status="qualified", limit=10)
        for sig in qualified_inbound:
            sig_name = sig.get("sender_name", "Unknown")
            sig_headline = sig.get("sender_headline", "")
            sig_intent = sig.get("intent", "unknown")
            sig_conf = sig.get("confidence", 0) or 0
            sig_action = sig.get("recommended_action", "ask_purpose")

            role_str = sig_headline or ""

            if sig_intent == "buying_signal" and sig_conf >= INBOUND_ENGAGE_CONFIDENCE:
                # Priority 0: High-confidence buying signal
                recommendations.append({
                    "priority": 0,
                    "score": 9.0 + sig_conf,
                    "icon": "📥",
                    "text": f"**INBOUND LEAD** — {sig_name} ({role_str}) — buying signal ({sig_conf:.0%} match)",
                    "action": "Check check_replies() — discovery DM auto-sent",
                    "fit_score": sig_conf * 10,
                    "campaign": "",
                    "temp": "",
                })
            elif sig_conf >= INBOUND_ASK_PURPOSE_CONFIDENCE:
                # Priority 1: Medium-confidence — worth engaging
                recommendations.append({
                    "priority": 1,
                    "score": 7.0 + sig_conf,
                    "icon": "📥",
                    "text": f"Inbound connection: {sig_name} ({role_str}) — {sig_intent} ({sig_conf:.0%})",
                    "action": "Check check_replies() — discovery DM auto-sent",
                    "fit_score": sig_conf * 10,
                    "campaign": "",
                    "temp": "",
                })
    except Exception as e:
        logger.debug("Inbound lead suggestions failed: %s", e)

    # ── Signal-heavy prospects (priority 2, high-score signal accounts) ──
    try:
        from ..db.signal_queries import list_signal_accounts
        from ..services.signal_scorer import compute_prospect_signal_score

        top_signal_accounts = await run_db(list_signal_accounts, min_score=0.3, limit=5)
        for acct in top_signal_accounts:
            acct_name = acct.get("prospect_name", "Unknown")
            acct_company = acct.get("company") or ""
            acct_score = acct.get("composite_score", 0)
            acct_total = acct.get("total_signals", 0)
            acct_top_type = (acct.get("top_signal_type") or "").replace("_", " ")
            acct_linkedin = acct.get("linkedin_id", "")

            # Check if this prospect is already in a campaign outreach
            # (avoid duplicate recommendations)
            already_recommended = False
            for rec in recommendations:
                if acct_name in rec.get("text", ""):
                    already_recommended = True
                    break
            if already_recommended:
                continue

            role_str = acct_company or ""
            if acct_top_type:
                role_str += f", {acct_top_type}" if role_str else acct_top_type

            # Score tier determines priority
            if acct_score >= 0.7:
                # Very hot signal — prioritize above most actions
                sig_priority = 2
                action_score = 8.0 + acct_score
                icon = "\U0001f4e1"  # satellite — signal
                text = f"**HIGH SIGNAL** — {acct_name} ({role_str}) — Score: {acct_score:.2f} ({acct_total} signals)"
                action = f"Run: show_signals() to review, then engage_prospect() or generate_and_send()"
            elif acct_score >= 0.5:
                # Warm signal — worth acting on
                sig_priority = 5
                action_score = 5.0 + acct_score
                icon = "\U0001f4e1"
                text = f"Signal detected: {acct_name} ({role_str}) — Score: {acct_score:.2f} ({acct_total} signals)"
                action = "Run: show_signals() to review, then engage_prospect()"
            else:
                # Moderate signal — monitor
                sig_priority = 7
                action_score = 3.0 + acct_score
                icon = "\U0001f4e1"
                text = f"Emerging signal: {acct_name} ({role_str}) — Score: {acct_score:.2f} ({acct_total} signals)"
                action = "Run: show_signals() to monitor"

            recommendations.append({
                "priority": sig_priority,
                "score": action_score,
                "icon": icon,
                "text": text,
                "action": action,
                "fit_score": acct_score * 10,
                "campaign": "",
                "temp": "",
            })
    except Exception as e:
        logger.debug("Signal account suggestions failed: %s", e)

    # ── Profile viewers & inbound invitations (priority 2, between hot leads and approvals) ──
    try:
        account_id = await run_db(get_account_id)
        if account_id:
            client = get_linkedin_client()
            try:
                viewers = await client.get_profile_viewers(account_id)
                if viewers:
                    viewer_names = [v.get("name", "Someone") for v in viewers[:3]]
                    viewer_summary = ", ".join(viewer_names)
                    if len(viewers) > 3:
                        viewer_summary += f" and {len(viewers) - 3} more"
                    recommendations.append({
                        "priority": 2,
                        "score": 5.0,  # High visibility
                        "icon": "👀",
                        "text": f"{len(viewers)} profile viewer{'s' if len(viewers) != 1 else ''}: {viewer_summary}",
                        "action": "Check check_replies() for details — warm leads!",
                        "fit_score": 0,
                        "campaign": "",
                        "temp": "",
                    })
                # Inbound invitations
                inbound = await client.get_received_invitations(account_id)
                if inbound:
                    inv_names = [inv.get("sender_name", "Someone") for inv in inbound[:3]]
                    inv_summary = ", ".join(inv_names)
                    if len(inbound) > 3:
                        inv_summary += f" and {len(inbound) - 3} more"
                    recommendations.append({
                        "priority": 2,
                        "score": 6.0,  # Higher than profile viewers
                        "icon": "📨",
                        "text": f"{len(inbound)} inbound invitation{'s' if len(inbound) != 1 else ''}: {inv_summary}",
                        "action": "Check check_replies() or let the scheduler auto-accept ICP matches!",
                        "fit_score": 0,
                        "campaign": "",
                        "temp": "",
                    })
            finally:
                await client.close()
    except (UnipileError, Exception) as e:
        logger.debug("Profile viewers/invitations fetch failed in suggest_next_action: %s", e)

    # ── Strategy engine recommendations (priority 1-2) ──
    try:
        from ..db.strategy_queries import get_strategy_summary, list_strategy_patterns
        strat = await run_db(get_strategy_summary)
        if strat.get("active_patterns", 0) > 0:
            top_patterns = await run_db(list_strategy_patterns, status="active", limit=2)
            for p in top_patterns:
                if p.get("estimated_revenue_impact", 0) > 5000:
                    recommendations.append({
                        "priority": 1,
                        "score": p.get("estimated_revenue_impact", 0) / 1000,
                        "icon": "\U0001f9e0",
                        "text": f"Strategy: {p.get('pattern_key', '')}",
                        "action": f"High-revenue pattern detected — {p.get('description', '')[:80]}. "
                                  "Run show_strategy() for details.",
                        "fit_score": p.get("confidence", 0),
                        "campaign": "",
                        "temp": "",
                    })
            if strat.get("spawned_campaigns", 0) > 0:
                recommendations.append({
                    "priority": 2,
                    "score": 5,
                    "icon": "\U0001f680",
                    "text": "Auto-spawned campaigns active",
                    "action": f"{strat['spawned_campaigns']} campaign(s) auto-created by strategy engine. "
                              "Run show_strategy() to review.",
                    "fit_score": 0,
                    "campaign": "",
                    "temp": "",
                })
    except Exception:
        pass

    # ── Sort by priority, then R4 score (desc), then fit_score (desc) ──
    recommendations.sort(key=lambda r: (r["priority"], -r.get("score", 0), -r.get("fit_score", 0)))

    from ..services.dashboard_snapshot import status_footer

    # get_campaign matched campaign_id exactly, so it is the real id.
    footer = (
        status_footer("campaign", campaign_id) if campaign_id
        else status_footer("overview")
    )

    if not recommendations:
        return (
            "✅ All caught up! No immediate actions needed.\n\n"
            "Your campaigns are running smoothly.\n"
            "Use show_status() for your dashboard, or\n"
            "create_campaign(\"target\") to find new prospects."
        ) + "\n".join(["", *footer])

    # ── Health check ──
    health_warning = ""
    hs = None
    try:
        rate_data = await run_db(get_rate_limit_today)
        daily_sent = rate_data.get("sent", 0)
        # Cumulative, from the campaign rows. This used to be
        # accepted_today / sent_today, which compares two unrelated groups —
        # an invitation accepted today was sent days ago, and one sent today
        # cannot have been accepted yet — so it read 0% against a real 24%
        # and contradicted show_status, which #236 already moved off it.
        from .show_status import _pooled_acceptance

        _camps = await run_db(list_campaigns)
        _rows = [await run_db(get_campaign_stats, c["id"]) for c in _camps]
        acc_rate, _ = _pooled_acceptance(_rows)
        from ..linkedin.rate_limiter import invite_limits_for_display

        _sna_weekly_cap, daily_limit = await invite_limits_for_display(rate_data)
        weekly_sent = await run_db(get_weekly_invitation_sum)
        sending_days = await run_db(get_sending_days_7d)

        # Fetch total sent lifetime
        total_lifetime = 0
        try:
            def _get_total_lifetime():
                _db = get_db()
                _row = _db.execute("SELECT COALESCE(SUM(sent), 0) as total FROM rate_limits").fetchone()
                val = _row["total"] if _row else 0
                _db.close()
                return val
            total_lifetime = await run_db(_get_total_lifetime)
        except Exception:
            pass

        hs = compute_health_score(
            acceptance_rate=acc_rate,
            total_sent=total_lifetime,
            daily_sent=daily_sent,
            daily_limit=daily_limit,
            weekly_sent=weekly_sent,
            weekly_limit=_sna_weekly_cap,
            sending_days_7d=sending_days,
        )
        if hs.level == "red":
            health_warning = f"🔴 **Health Score: {hs.total}/100 — DANGER** — Consider pausing outreach\n"
            if hs.warnings:
                health_warning += "\n".join(f"   ⚠️  {w}" for w in hs.warnings) + "\n"
            health_warning += "\n"
        elif hs.level == "orange":
            health_warning = f"🟠 **Health Score: {hs.total}/100 — WARNING** — Slow down sending\n"
            if hs.warnings:
                health_warning += "\n".join(f"   ⚠️  {w}" for w in hs.warnings) + "\n"
            health_warning += "\n"
    except Exception:
        pass

    # ── Weekly limit notice ──
    try:
        from ..linkedin.rate_limiter import (
            can_send_now as _csn,
            estimate_weekly_limit_reset as _ewlr,
            BLOCK_WEEKLY as _BW,
            BLOCK_DAILY as _BD,
        )
        import json as _sna_json

        _can_send, _csn_reason, _csn_block = await _csn(reserve=False)
        if _csn_block == _BW:
            _, _, _eta = await _ewlr()
            # Informational notice for all campaigns
            recommendations.append({
                "priority": 0,
                "score": 100,  # Always show first
                "icon": "⚠️",
                "text": f"Weekly LinkedIn invitation limit reached — invitations resume in {_eta}",
                "action": "No action needed — engagements, follows, and email overflow continue.",
                "fit_score": 0,
                "campaign": "",
                "temp": "",
            })
            # Check for copilot campaigns paused due to weekly limits
            _paused_camps = await run_db(list_campaigns, status="paused")
            for _pc in _paused_camps:
                _pc_cfg = _sna_json.loads(_pc.get("config_json") or "{}")
                if _pc_cfg.get("pause_reason") == "weekly_limit" and _pc.get("mode") != "autopilot":
                    # Copilot campaign paused by weekly limit — suggest resume
                    if not _can_send:
                        # Still limited — just inform
                        recommendations.append({
                            "priority": 1,
                            "score": 8.0,
                            "icon": "⏸️",
                            "text": f"**{_pc['name']}** paused (weekly limit) — will be ready to resume in {_eta}",
                            "action": f"Resume when ready: resume_campaign(campaign_id='{_pc['id'][:8]}...')",
                            "fit_score": 0,
                            "campaign": _pc["name"],
                            "temp": "",
                        })
                elif _pc_cfg.get("pause_reason") == "weekly_limit" and _pc.get("mode") == "autopilot":
                    # Autopilot should auto-resume — this shouldn't happen, but just in case
                    pass
        elif not _can_send and _csn_block == _BD:
            recommendations.append({
                "priority": 0,
                "score": 100,
                "icon": "⚠️",
                "text": "Daily LinkedIn invitation limit reached — invitations resume tomorrow",
                "action": "No action needed — engagements and email overflow continue.",
                "fit_score": 0,
                "campaign": "",
                "temp": "",
            })

        # Check for copilot campaigns paused by weekly limit where limits HAVE cleared
        if _can_send or _csn_block not in (_BW, _BD):
            _paused_camps2 = await run_db(list_campaigns, status="paused")
            for _pc2 in _paused_camps2:
                _pc2_cfg = _sna_json.loads(_pc2.get("config_json") or "{}")
                if _pc2_cfg.get("pause_reason") == "weekly_limit" and _pc2.get("mode") != "autopilot":
                    recommendations.append({
                        "priority": 0,
                        "score": 99,
                        "icon": "✅",
                        "text": f"**{_pc2['name']}** — weekly limit cleared! Ready to resume sending invitations.",
                        "action": f"Resume now: resume_campaign(campaign_id='{_pc2['id'][:8]}...')",
                        "fit_score": 0,
                        "campaign": _pc2["name"],
                        "temp": "",
                    })
    except Exception as _e:
        logger.debug("Weekly limit notice failed: %s", _e)

    # ── Brand strategy suggestion ──
    try:
        from ..config import is_scheduler_enabled as _sched_enabled
        from ..services.brand_service import load_brand_analysis, load_brand_plan
        _brand_analysis = await run_db(load_brand_analysis)
        _brand_plan = await run_db(load_brand_plan)
        _scheduler_on = _sched_enabled()

        if hs and (hs.total < 60 or (total_lifetime >= 10 and acc_rate < 0.25)):
            if not _brand_analysis:
                recommendations.append({
                    "priority": 9,
                    "score": 2.0,
                    "icon": "\U0001f3af",
                    "text": "Your metrics suggest a brand audit could improve campaign results",
                    "action": 'Run: brand_strategy(action="analyze")',
                    "fit_score": 0,
                    "campaign": "",
                    "temp": "",
                })
        elif _brand_plan:
            _bp_total = sum(len(w.get("actions", [])) for w in _brand_plan.get("weeks", []))
            _bp_done = sum(
                1 for w in _brand_plan.get("weeks", [])
                for a in w.get("actions", []) if a.get("status") == "completed"
            )
            if _bp_done < _bp_total:
                if _scheduler_on:
                    # Scheduler handles execution automatically — suggest progress check
                    recommendations.append({
                        "priority": 9,
                        "score": 1.0,
                        "icon": "\U0001f4cb",
                        "text": f"Brand strategy: {_bp_done}/{_bp_total} actions (auto-executing via scheduler)",
                        "action": 'Run: brand_strategy(action="progress") to check improvement',
                        "fit_score": 0,
                        "campaign": "",
                        "temp": "",
                    })
                else:
                    recommendations.append({
                        "priority": 9,
                        "score": 1.0,
                        "icon": "\U0001f4cb",
                        "text": f"Brand strategy: {_bp_done}/{_bp_total} actions completed",
                        "action": 'Run: brand_strategy(action="execute")',
                        "fit_score": 0,
                        "campaign": "",
                        "temp": "",
                    })
    except Exception:
        pass

    # ── Experiment insight suggestion ──
    try:
        from ..services.experiment_service import get_experiment_summary
        # Sync DB read — get_db() raises on the event loop thread and the
        # except below would swallow it, so the insight never appeared.
        exp_summary = await run_db(get_experiment_summary)
        if exp_summary:
            recommendations.append({
                "priority": 10,
                "score": 0.5,
                "icon": "\U0001f52c",
                "text": f"Experiment insight: {exp_summary}",
                "action": "Run: campaign_report() for full experiment details",
                "fit_score": 0,
                "campaign": "",
                "temp": "",
            })
    except Exception:
        pass

    # ── Voice memo suggestion ──
    try:
        from ..config import is_voice_memo_enabled
        from ..db.queries import get_voice_memo_stats

        if is_voice_memo_enabled():
            for camp in campaigns:
                cid = camp["id"]
                camp_config = {}
                try:
                    import json as _json
                    camp_config = _json.loads(camp.get("config_json", "{}") or "{}")
                except (ValueError, TypeError):
                    pass
                voice_mode = camp_config.get("voice_mode", "text_only")
                if voice_mode != "text_only":
                    # Voice is on — check if voice acceptance is low and suggest disabling
                    v_stats = await run_db(get_voice_memo_stats, cid)
                    if _voice_mode_advice(v_stats) == "text_only":
                        recommendations.append({
                            "priority": 9,
                            "score": 1.5,
                            "icon": "\U0001f3a4",
                            "text": f"Voice memos not getting responses for **{camp['name']}** — consider switching to text only",
                            "action": f"Run: edit_campaign(campaign_id='{cid[:8]}...', voice_mode='text_only')",
                            "fit_score": 0,
                            "campaign": camp["name"],
                            "temp": "",
                        })
                        break  # Only suggest once
    except Exception:
        pass

    # ── Format output ──
    top = recommendations[:MAX_RECOMMENDATIONS]
    remaining = len(recommendations) - MAX_RECOMMENDATIONS

    output = []
    if health_warning:
        output.append(health_warning)
    try:
        from ..services.unipile_email import mailbox_disconnect_banner
        output.extend(await mailbox_disconnect_banner())
    except Exception:
        pass
    output.append("🎯 **Recommended Next Actions:**\n")

    for i, rec in enumerate(top, 1):
        output.append(f"{i}. {rec['icon']} {rec['text']}")
        output.append(f"   → {rec['action']}")
        if len(campaigns) > 1:
            output.append(f"   Campaign: {rec['campaign']}")
        output.append("")

    if remaining > 0:
        output.append(f"... and {remaining} more action{'s' if remaining != 1 else ''} queued.")
        output.append("")

    # ── Summary stats ──
    hot = sum(1 for r in recommendations if r["priority"] == 1)
    questions = sum(1 for r in recommendations if r["priority"] == 2)
    followups = sum(1 for r in recommendations if r["priority"] == 4)
    warmups = sum(1 for r in recommendations if r["priority"] in (5, 7))
    ready = sum(1 for r in recommendations if r["priority"] == 6)

    summary_parts = []
    if hot:
        summary_parts.append(f"{hot} hot lead{'s' if hot != 1 else ''}")
    if questions:
        summary_parts.append(f"{questions} question{'s' if questions != 1 else ''}")
    if followups:
        summary_parts.append(f"{followups} follow-up{'s' if followups != 1 else ''} due")
    if ready:
        summary_parts.append(f"{ready} ready to invite")
    if warmups:
        summary_parts.append(f"{warmups} need{'s' if warmups == 1 else ''} warm-up")
    stale_count = sum(1 for r in recommendations if r["priority"] == 8)
    if stale_count:
        summary_parts.append(f"{stale_count} stale lead{'s' if stale_count != 1 else ''}")
    signal_count = sum(1 for r in recommendations if r.get("icon") == "\U0001f4e1")
    if signal_count:
        summary_parts.append(f"{signal_count} signal{'s' if signal_count != 1 else ''} detected")

    if summary_parts:
        output.append("Summary: " + " · ".join(summary_parts))

    # Temperature breakdown
    temp_counts = {TEMP_COLD: 0, TEMP_WARM: 0, TEMP_HOT: 0}
    for r in recommendations:
        t = r.get("temp", "")
        if TEMP_ICONS[TEMP_HOT] in t:
            temp_counts[TEMP_HOT] += 1
        elif TEMP_ICONS[TEMP_WARM] in t:
            temp_counts[TEMP_WARM] += 1
        elif TEMP_ICONS[TEMP_COLD] in t:
            temp_counts[TEMP_COLD] += 1
    temp_parts = []
    if temp_counts[TEMP_HOT]:
        temp_parts.append(f"{TEMP_ICONS[TEMP_HOT]} {temp_counts[TEMP_HOT]} hot")
    if temp_counts[TEMP_WARM]:
        temp_parts.append(f"{TEMP_ICONS[TEMP_WARM]} {temp_counts[TEMP_WARM]} warm")
    if temp_counts[TEMP_COLD]:
        temp_parts.append(f"{TEMP_ICONS[TEMP_COLD]} {temp_counts[TEMP_COLD]} cold")
    if temp_parts:
        output.append("Pipeline: " + " · ".join(temp_parts))

    output.extend(footer)
    return "\n".join(output)
