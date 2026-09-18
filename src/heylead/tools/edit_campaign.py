"""Tool: edit_campaign — Edit campaign settings after creation.

Allows changing the campaign name, mode, voice settings, warm-up sequence,
follow-up cadence, engagement behavior, and timing preferences.
"""

from __future__ import annotations

import json
import logging

from ..config import is_backend_mode
from ..db import aio as db
from ..services.cloud_sync import sync_campaign_settings

logger = logging.getLogger(__name__)

# Valid values for new settings
_VALID_ENGAGEMENT_MODES = frozenset({"auto", "comment_only", "react_only"})
# Mirrors campaign_scorecard.WEEKLY_TARGET_MAX on the backend, which clamps
# whatever arrives; refusing here gives the operator the error instead.
_WEEKLY_TARGET_MAX = 50
_VALID_ACTIVE_DAYS = frozenset(range(7))  # 0=Mon ... 6=Sun
_VALID_AGENT_FLAGS = frozenset({"on", "off", "observe"})
_AGENT_FLAG_SPECS = (
    ("enable_reply_agent", "reply_agent_mode", "Reply agent"),
    ("enable_strategist_replan_agent", "strategist_replan_mode", "Strategist replan"),
    ("enable_hot_lead_closer", "hot_lead_closer_mode", "Hot-lead closer"),
    ("enable_coordinator_agent", "coordinator_agent_mode", "Coordinator"),
)


async def run_edit_campaign(
    campaign_id: str = "",
    name: str = "",
    mode: str = "",
    booking_link: str = "",
    offerings: str = "",
    case_studies: str = "",
    social_proofs: str = "",
    campaign_preferences: str = "",
    project_brief: str = "",
    product: str = "",
    go_live: str = "",
    volume: str = "",
    must_confirm: str = "",
    voice_mode: str = "",
    voice_noise: str = "",
    voice_humanize: str = "",
    open_to_work_mode: str = "",
    # Warm-up sequence toggles
    enable_profile_views: str = "",
    enable_follows: str = "",
    enable_endorsements: str = "",
    enable_engagements: str = "",
    enable_followups: str = "",
    enable_auto_replies: str = "",
    enable_invitations: str = "",
    enable_discovery: str = "",
    # Never message a pre-existing 1st-degree connection (9 Sep 2026)
    exclude_connections: str = "",
    connections_only: str = "",
    exclude_competitors: str = "",
    competitor_companies: str = "",
    # Engagement settings
    engagement_mode: str = "",
    # Follow-up settings
    max_followups: int = 0,
    followup_delay_days: str = "",
    weekly_meeting_target: int = -1,
    # Invite settings
    withdraw_stale_invites: str = "",
    stale_invite_days: int = 0,
    # InMail escalation settings
    inmail_fallback: str = "",
    inmail_fallback_days: int = 0,
    inmail_first_touch: str = "",
    # Send timing
    send_in_business_hours: str = "",
    active_days: str = "",
    # Message stance
    campaign_intent: str = "",
    # Prompt family: outbound (default) or job_search
    campaign_type: str = "",
    # In-process agents (act is the default when unset)
    enable_reply_agent: str = "",
    enable_strategist_replan_agent: str = "",
    enable_hot_lead_closer: str = "",
    enable_coordinator_agent: str = "",
) -> str:
    """Edit a campaign's settings.

    Args:
        campaign_id: Which campaign to edit. Edits the first active campaign if empty.
        name: New campaign name. Leave empty to keep current name.
        mode: New mode: "copilot" or "autopilot". Leave empty to keep current mode.
        booking_link: Calendar/booking URL for positive reply auto-responses.
        offerings: What you offer (products, services, value props).
        case_studies: Brief case studies or success stories.
        social_proofs: Social proof (logos, metrics, testimonials).
        campaign_preferences: Custom messaging preferences (tone, topics to avoid, etc.).
        project_brief: Full project paste the model sees (what you are building,
            go-live, volume, what a vendor must confirm). Required before launch.
        product: Optional structured fact: product / what you buy or sell.
        go_live: Optional structured fact: go-live date.
        volume: Optional structured fact: volume model.
        must_confirm: Optional comma-separated list of questions a vendor must confirm.
        voice_mode: Voice memo mode: "text_only", "voice_only", "mixed", or "ab_test".
        voice_noise: Ambient noise type: "office", "cafe", "street", "quiet", "none", "auto".
        voice_humanize: Voice text humanization: "on" or "off".
        open_to_work_mode: OTW badge control: "off", "on_for_inbound", "off_for_outbound".
        enable_follows: Follow prospects before inviting: "on" or "off".
        enable_endorsements: Endorse skills before inviting: "on" or "off".
        enable_engagements: Comment/react on posts before inviting: "on" or "off".
        enable_followups: Send follow-up DMs after connection: "on" or "off".
        enable_invitations: Send connection invitations: "on" or "off".
        enable_discovery: Find and enrol new prospects automatically: "on" or "off".
            Turn off for curated campaigns with a fixed, hand-picked target list.
            When off, campaign only DMs existing connections (no invitations sent).
        exclude_competitors: Never first-touch people at competing companies:
            "on" or "off". Default on.
        competitor_companies: Comma-separated employer names to skip.
        engagement_mode: Engagement style: "auto" (30% react/70% comment),
            "comment_only", or "react_only".
        max_followups: Max follow-up messages (1-5). 0 to keep current.
        weekly_meeting_target: Meetings this campaign should book per week.
            The daily report reads it as the Key Result and says whether the
            campaign is on track. 0 means no goal this week; -1 (the default)
            keeps the current value.
        followup_delay_days: Custom day intervals as comma-separated list
            (e.g., "1,3,7,14"). Leave empty to keep current.
        withdraw_stale_invites: Auto-withdraw stale invites: "on" or "off".
        stale_invite_days: Days before withdrawing stale invites (7-60). 0 to keep current.
        inmail_fallback: Escalate quiet invitations with one InMail: "on" or "off".
            Free tier sends only to Open Profile members (zero credits).
        inmail_fallback_days: Quiet days before the InMail (1-20; must precede the day-21 withdrawal). 0 to keep current.
        inmail_first_touch: InMail as the first touch for Premium / Open Profile
            strangers: "on" or "off". Unset follows inmail_fallback.
        send_in_business_hours: Respect prospect's business hours: "on" or "off".
        active_days: Active send days as comma-separated numbers (0=Mon, 6=Sun).
            E.g., "0,1,2,3,4" for weekdays. Leave empty to keep current.
        campaign_type: Prompt family: "outbound" (default) or "job_search".
            job_search selects the invitation note and the first DM that may
            name the recipient's company and the role. InMail is not routed
            by this switch. Empty keeps the current value.
            job_search replaces the intent-specific first touch; campaign_intent
            still selects the system prompt.
        enable_reply_agent: Reply exception agent: "on" (act), "off", or
            "observe". Empty keeps the current value. Unset defaults to act.
        enable_strategist_replan_agent: Strategist replan agent: "on", "off",
            or "observe". Empty keeps the current value.
        enable_hot_lead_closer: Hot-lead closer: "on", "off", or "observe".
            Empty keeps the current value.
        enable_coordinator_agent: Coordinator digest/hold: "on", "off", or
            "observe". Empty keeps the current value.
    """

    # ── Pre-checks ──
    setup_done = await db.get_setting("setup_complete", False)
    if not setup_done:
        return (
            "Setup required before editing campaigns.\n\n"
            "Please run setup_profile first."
        )

    has_context_fields = any([
        offerings, case_studies, social_proofs, campaign_preferences,
        project_brief, product, go_live, volume, must_confirm,
    ])
    has_voice_settings = any([voice_noise, voice_humanize])
    has_toggle_settings = any([
        enable_follows, enable_endorsements, enable_engagements, enable_followups,
        enable_auto_replies, enable_invitations, enable_discovery,
        exclude_connections, connections_only,
        exclude_competitors,
    ])
    has_engagement_settings = bool(engagement_mode)
    has_followup_settings = max_followups > 0 or bool(followup_delay_days)
    # 0 is a real value (no goal this week), so the "unset" sentinel is -1.
    has_target = weekly_meeting_target >= 0
    has_invite_settings = bool(withdraw_stale_invites) or stale_invite_days > 0
    has_inmail_settings = (
        bool(inmail_fallback) or inmail_fallback_days > 0 or bool(inmail_first_touch)
    )
    has_timing_settings = bool(send_in_business_hours) or bool(active_days)
    has_agent_settings = any([
        enable_reply_agent, enable_strategist_replan_agent, enable_hot_lead_closer,
        enable_coordinator_agent,
    ])

    # Normalised once: has_any, the apply block, the early-return push and the
    # sync entry all read this, so the rule cannot drift between them.
    wanted_type = campaign_type.strip().lower()

    has_any = (
        name or mode or booking_link or voice_mode
        or has_context_fields or has_voice_settings or open_to_work_mode
        or has_toggle_settings or has_engagement_settings
        or has_followup_settings or has_invite_settings or has_inmail_settings
        or has_timing_settings or campaign_intent or wanted_type
        or has_agent_settings or competitor_companies or has_target
    )

    if not has_any:
        return (
            "Nothing to change.\n\n"
            "Provide at least one of:\n"
            "  name: New campaign name\n"
            "  mode: \"copilot\" or \"autopilot\"\n"
            "  booking_link: Calendar URL for reply auto-responses\n"
            "  project_brief: Full project paste (required before launch)\n"
            "  product / go_live / volume / must_confirm: optional project facts\n"
            "  campaign_intent: sell, buy, partner, or recruit\n"
            "  campaign_type: outbound or job_search\n"
            "  voice_mode: text_only, voice_only, mixed, or ab_test\n"
            "  voice_noise: office, cafe, street, quiet, none, auto\n"
            "  voice_humanize: on or off\n"
            "\n"
            "  Warm-up sequence:\n"
            "  enable_follows: on or off\n"
            "  enable_endorsements: on or off\n"
            "  enable_engagements: on or off\n"
            "  enable_followups: on or off\n"
            "  enable_auto_replies: on or off\n"
            "  enable_invitations: on or off\n"
            "  exclude_connections: on or off (never message people you were "
            "already connected to before this campaign)\n"
            "  exclude_competitors: on or off (never message people at "
            "competing companies)\n"
            "  competitor_companies: comma-separated company names to skip\n"
            "  enable_discovery: on or off\n"
            "\n"
            "  In-process agents (default act):\n"
            "  enable_reply_agent: on, off, or observe\n"
            "  enable_strategist_replan_agent: on, off, or observe\n"
            "  enable_hot_lead_closer: on, off, or observe\n"
            "  enable_coordinator_agent: on, off, or observe\n"
            "\n"
            "  Engagement:\n"
            "  engagement_mode: auto, comment_only, or react_only\n"
            "\n"
            "  Follow-ups:\n"
            "  max_followups: 1-5\n"
            "  followup_delay_days: e.g. \"1,3,7,14\"\n"
            "\n"
            "  Invites:\n"
            "  withdraw_stale_invites: on or off\n"
            "  stale_invite_days: 7-60\n"
            "\n"
            "  InMail escalation:\n"
            "  inmail_fallback: on or off\n"
            "  inmail_fallback_days: 1-20\n"
            "  inmail_first_touch: on or off\n"
            "\n"
            "  Timing:\n"
            "  send_in_business_hours: on or off\n"
            "  active_days: e.g. \"0,1,2,3,4\" (0=Mon, 6=Sun)"
        )

    # ── Validate mode ──
    if mode and mode not in ("copilot", "autopilot"):
        return (
            f"Invalid mode: '{mode}'\n\n"
            "Must be 'copilot' or 'autopilot'."
        )

    # ── Validate voice_mode ──
    if voice_mode:
        from ..constants import VALID_VOICE_MODES
        if voice_mode not in VALID_VOICE_MODES:
            return (
                f"Invalid voice_mode: '{voice_mode}'\n\n"
                "Must be one of: text_only, voice_only, mixed, ab_test"
            )

    # ── Validate voice_noise ──
    if voice_noise:
        from ..constants import VALID_NOISE_TYPES
        if voice_noise not in VALID_NOISE_TYPES:
            return (
                f"Invalid voice_noise: '{voice_noise}'\n\n"
                "Must be one of: office, cafe, street, quiet, none, auto"
            )

    # ── Validate voice_humanize ──
    if voice_humanize and voice_humanize not in ("on", "off"):
        return (
            f"Invalid voice_humanize: '{voice_humanize}'\n\n"
            "Must be 'on' or 'off'."
        )

    # ── Validate on/off toggles ──
    for label, val in [
        ("enable_profile_views", enable_profile_views),
        ("enable_follows", enable_follows),
        ("enable_endorsements", enable_endorsements),
        ("enable_engagements", enable_engagements),
        ("enable_followups", enable_followups),
        ("enable_auto_replies", enable_auto_replies),
        ("enable_invitations", enable_invitations),
        ("enable_discovery", enable_discovery),
        ("exclude_connections", exclude_connections),
        ("connections_only", connections_only),
        ("exclude_competitors", exclude_competitors),
        ("withdraw_stale_invites", withdraw_stale_invites),
        ("inmail_fallback", inmail_fallback),
        ("inmail_first_touch", inmail_first_touch),
        ("send_in_business_hours", send_in_business_hours),
    ]:
        if val and val not in ("on", "off"):
            return f"Invalid {label}: '{val}'. Must be 'on' or 'off'."

    # Mutually exclusive: one sources the campaign FROM the connections table,
    # the other removes everyone in it (9 Sep 2026).
    if exclude_connections == "on" and connections_only == "on":
        return (
            "❌ exclude_connections and connections_only cannot both be 'on'.\n\n"
            "connections_only targets your existing 1st-degree connections; "
            "exclude_connections leaves every one of them alone."
        )

    for label, val in [
        ("enable_reply_agent", enable_reply_agent),
        ("enable_strategist_replan_agent", enable_strategist_replan_agent),
        ("enable_hot_lead_closer", enable_hot_lead_closer),
        ("enable_coordinator_agent", enable_coordinator_agent),
    ]:
        if val and val not in _VALID_AGENT_FLAGS:
            return (
                f"Invalid {label}: '{val}'. Must be 'on', 'off', or 'observe'."
            )

    # ── Validate engagement_mode ──
    if engagement_mode and engagement_mode not in _VALID_ENGAGEMENT_MODES:
        return (
            f"Invalid engagement_mode: '{engagement_mode}'\n\n"
            "Must be one of: auto, comment_only, react_only"
        )

    # ── Validate max_followups ──
    if max_followups and (max_followups < 1 or max_followups > 5):
        return "Invalid max_followups: must be between 1 and 5."

    # ── Validate weekly_meeting_target ──
    if weekly_meeting_target > _WEEKLY_TARGET_MAX:
        return f"Invalid weekly_meeting_target: must be between 0 and {_WEEKLY_TARGET_MAX}."

    # ── Validate followup_delay_days ──
    parsed_followup_days: list[int] | None = None
    if followup_delay_days:
        try:
            parsed_followup_days = sorted([int(d.strip()) for d in followup_delay_days.split(",")])
            if not parsed_followup_days or any(d < 1 or d > 90 for d in parsed_followup_days):
                return "Invalid followup_delay_days: each value must be between 1 and 90."
        except ValueError:
            return "Invalid followup_delay_days: must be comma-separated numbers (e.g., '1,3,7,14')."

    # ── Validate stale_invite_days ──
    if stale_invite_days and (stale_invite_days < 7 or stale_invite_days > 60):
        return "Invalid stale_invite_days: must be between 7 and 60."

    # ── Validate inmail_fallback_days ──
    # Upper bound is STALE_INVITE_DAYS - 1: the escalation InMail must land
    # while the invitation still exists — withdrawal at day 21 is unchanged.
    from ..constants import STALE_INVITE_DAYS
    if inmail_fallback_days and not (1 <= inmail_fallback_days < STALE_INVITE_DAYS):
        return (
            f"Invalid inmail_fallback_days: must be between 1 and "
            f"{STALE_INVITE_DAYS - 1} (withdrawal fires at day {STALE_INVITE_DAYS})."
        )

    # ── Validate active_days ──
    parsed_active_days: list[int] | None = None
    if active_days:
        try:
            parsed_active_days = sorted(set(int(d.strip()) for d in active_days.split(",")))
            if not parsed_active_days or any(d not in _VALID_ACTIVE_DAYS for d in parsed_active_days):
                return "Invalid active_days: each value must be 0-6 (0=Mon, 6=Sun)."
        except ValueError:
            return "Invalid active_days: must be comma-separated numbers (e.g., '0,1,2,3,4')."

    # ── Resolve campaign ──
    campaign, err = await db.find_active_campaign(campaign_id)
    if not campaign and not campaign_id:
        # Fallback: try any campaign (not just active)
        campaigns = await db.list_campaigns()
        if not campaigns:
            return (
                "No campaigns to edit.\n\n"
                "Create one first: create_campaign(\"your target description\")"
            )
        campaign = campaigns[0]
    elif not campaign:
        return err
    campaign_id = campaign["id"]

    # ── Apply changes ──
    changes: dict[str, str] = {}
    change_descriptions: list[str] = []

    old_name = campaign.get("name", "")
    old_mode = campaign.get("mode", "autopilot")

    if name and name != old_name:
        changes["name"] = name
        change_descriptions.append(f"Name: '{old_name}' -> '{name}'")

    if mode and mode != old_mode and mode == "autopilot":
        changes["mode"] = "autopilot"
        change_descriptions.append(f"Mode: {old_mode} -> Autopilot")

    # Parse config_json once
    config_json = campaign.get("config_json", "{}")
    try:
        config = json.loads(config_json) if config_json else {}
    except (json.JSONDecodeError, TypeError):
        config = {}

    if booking_link:
        old_booking = config.get("booking_link", "")
        if booking_link != old_booking:
            config["booking_link"] = booking_link
            change_descriptions.append(f"Booking link: {booking_link}")

    if voice_mode:
        old_voice = config.get("voice_mode", "text_only")
        if voice_mode != old_voice:
            config["voice_mode"] = voice_mode
            change_descriptions.append(f"Voice mode: {old_voice} -> {voice_mode}")

    if voice_noise:
        old_noise = config.get("voice_noise_type", "auto")
        if voice_noise != old_noise:
            config["voice_noise_type"] = voice_noise
            change_descriptions.append(f"Voice noise: {old_noise} -> {voice_noise}")

    if voice_humanize:
        humanize_bool = voice_humanize == "on"
        old_humanize = config.get("voice_humanize", True)
        if humanize_bool != old_humanize:
            config["voice_humanize"] = humanize_bool
            change_descriptions.append(f"Voice humanize: {'on' if old_humanize else 'off'} -> {voice_humanize}")

    if open_to_work_mode:
        valid_otw = {"off", "on_for_inbound", "off_for_outbound"}
        if open_to_work_mode not in valid_otw:
            return f"Invalid open_to_work_mode. Must be one of: {', '.join(sorted(valid_otw))}"
        old_otw = config.get("open_to_work_mode", "off")
        if open_to_work_mode != old_otw:
            config["open_to_work_mode"] = open_to_work_mode
            change_descriptions.append(f"Open to Work: {old_otw} -> {open_to_work_mode}")

    # ── Warm-up sequence toggles ──
    _TOGGLE_LABELS = {
        "enable_profile_views": "Profile views",
        "enable_follows": "Follows",
        "enable_endorsements": "Endorsements",
        "enable_engagements": "Engagements",
        "enable_followups": "Follow-ups",
        "enable_auto_replies": "Auto-replies",
        "enable_invitations": "Invitations",
        "enable_discovery": "Prospect discovery",
        "exclude_connections": "Exclude existing connections",
        "connections_only": "Connections only",
        "exclude_competitors": "Exclude competitor companies",
        "withdraw_stale_invites": "Withdraw stale invites",
        "inmail_fallback": "InMail fallback",
        "inmail_first_touch": "InMail first touch",
        "send_in_business_hours": "Business hours",
    }
    for key, val in [
        ("enable_profile_views", enable_profile_views),
        ("enable_follows", enable_follows),
        ("enable_endorsements", enable_endorsements),
        ("enable_engagements", enable_engagements),
        ("enable_followups", enable_followups),
        ("enable_auto_replies", enable_auto_replies),
        ("enable_invitations", enable_invitations),
        ("enable_discovery", enable_discovery),
        ("exclude_connections", exclude_connections),
        ("connections_only", connections_only),
        ("exclude_competitors", exclude_competitors),
        ("withdraw_stale_invites", withdraw_stale_invites),
        ("inmail_fallback", inmail_fallback),
        ("inmail_first_touch", inmail_first_touch),
        ("send_in_business_hours", send_in_business_hours),
    ]:
        if val:
            new_bool = val == "on"
            # These two default OFF; every other toggle here defaults ON.
            # Reading them with the shared `True` default reported "off -> on"
            # as no change and never wrote the key.
            default = key not in ("exclude_connections", "connections_only")
            old_bool = config.get(key, default)
            if new_bool != old_bool:
                config[key] = new_bool
                label = _TOGGLE_LABELS[key]
                change_descriptions.append(f"{label}: {'on' if old_bool else 'off'} -> {val}")

    # Turning one of the pair on turns the other off, and says so. Storing both
    # as True would leave the campaign describing an empty audience, and which
    # of the two won would depend on which gate a given code path checked first.
    from ..flags import flag_enabled
    for winner, loser, said in (
        ("exclude_connections", "connections_only", exclude_connections),
        ("connections_only", "exclude_connections", connections_only),
    ):
        if said == "on" and flag_enabled(config, loser, default=False):
            config[loser] = False
            change_descriptions.append(
                f"{_TOGGLE_LABELS[loser]}: on -> off "
                f"(turned off by {winner}, they are mutually exclusive)"
            )

    if competitor_companies.strip():
        old_list = str(config.get("competitor_companies") or "")
        new_list = competitor_companies.strip()
        if new_list != old_list:
            config["competitor_companies"] = new_list
            change_descriptions.append(
                f"Competitor companies: {old_list or '(none)'} -> {new_list}"
            )

    # ── In-process agents (on=act, off, observe=persist mode) ──
    from ..services.coordinator import coordinator_mode
    from ..services.hot_lead_closer import hot_lead_closer_mode
    from ..services.reply_agent import reply_agent_mode
    from ..services.strategist_replan import strategist_replan_mode

    _agent_readers = {
        "enable_reply_agent": reply_agent_mode,
        "enable_strategist_replan_agent": strategist_replan_mode,
        "enable_hot_lead_closer": hot_lead_closer_mode,
        "enable_coordinator_agent": coordinator_mode,
    }
    _agent_values = {
        "enable_reply_agent": enable_reply_agent,
        "enable_strategist_replan_agent": enable_strategist_replan_agent,
        "enable_hot_lead_closer": enable_hot_lead_closer,
        "enable_coordinator_agent": enable_coordinator_agent,
    }
    for key, mode_key, label in _AGENT_FLAG_SPECS:
        val = _agent_values[key]
        if not val:
            continue
        mode_of = _agent_readers[key]
        old = mode_of(config)
        if val == "observe":
            config.pop(key, None)
            config[mode_key] = "observe"
        elif val == "on":
            config[key] = True
            config.pop(mode_key, None)
        else:
            config[key] = False
            config.pop(mode_key, None)
        new = mode_of(config)
        if old != new:
            change_descriptions.append(f"{label}: {old} -> {new}")

    # ── Engagement mode ──
    if engagement_mode:
        old_em = config.get("engagement_mode", "auto")
        if engagement_mode != old_em:
            config["engagement_mode"] = engagement_mode
            change_descriptions.append(f"Engagement mode: {old_em} -> {engagement_mode}")

    # ── Follow-up settings ──
    if max_followups:
        old_mf = config.get("max_followups", 5)
        if max_followups != old_mf:
            config["max_followups"] = max_followups
            change_descriptions.append(f"Max follow-ups: {old_mf} -> {max_followups}")

    # ── Weekly meetings Key Result ──
    if has_target:
        old_target = config.get("weekly_meeting_target")
        if weekly_meeting_target != old_target:
            config["weekly_meeting_target"] = weekly_meeting_target
            was = "none" if old_target is None else old_target
            change_descriptions.append(
                f"Weekly meeting target: {was} -> {weekly_meeting_target}"
            )

    if parsed_followup_days is not None:
        old_fd = config.get("followup_delay_days")
        if old_fd != parsed_followup_days:
            config["followup_delay_days"] = parsed_followup_days
            was = ",".join(map(str, old_fd)) if old_fd else "unset"
            change_descriptions.append(
                f"Follow-up schedule: {was} -> {','.join(map(str, parsed_followup_days))} days"
            )

    # ── Stale invite days ──
    if stale_invite_days:
        old_sid = config.get("stale_invite_days", 21)
        if stale_invite_days != old_sid:
            config["stale_invite_days"] = stale_invite_days
            change_descriptions.append(f"Stale invite days: {old_sid} -> {stale_invite_days}")

    # ── InMail fallback days ──
    if inmail_fallback_days:
        from ..constants import INMAIL_FALLBACK_AFTER_DAYS
        old_ifd = config.get("inmail_fallback_days", INMAIL_FALLBACK_AFTER_DAYS)
        if inmail_fallback_days != old_ifd:
            config["inmail_fallback_days"] = inmail_fallback_days
            change_descriptions.append(
                f"InMail fallback days: {old_ifd} -> {inmail_fallback_days}"
            )

    # ── Active days ──
    if parsed_active_days is not None:
        old_ad = config.get("active_days", [0, 1, 2, 3, 4])
        if parsed_active_days != old_ad:
            config["active_days"] = parsed_active_days
            day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            old_str = ",".join(day_names[d] for d in old_ad)
            new_str = ",".join(day_names[d] for d in parsed_active_days)
            change_descriptions.append(f"Active days: {old_str} -> {new_str}")

    # ── Campaign intent (message stance: sell | buy | partner | recruit) ──
    if campaign_intent:
        from ..ai.intent import VALID_INTENTS
        wanted = campaign_intent.strip().lower()
        if wanted not in VALID_INTENTS:
            return (
                f"❌ Unknown campaign_intent '{campaign_intent}'. "
                f"Valid values: {', '.join(VALID_INTENTS)}."
            )
        old_intent = config.get("campaign_intent", "sell")
        config["campaign_intent"] = wanted
        change_descriptions.append(f"Campaign intent: {old_intent} -> {wanted}")

    # ── Campaign type (prompt family: outbound | job_search) ──
    if wanted_type:
        from ..constants import DEFAULT_CAMPAIGN_TYPE, VALID_CAMPAIGN_TYPES
        if wanted_type not in VALID_CAMPAIGN_TYPES:
            return (
                f"❌ Unknown campaign_type '{campaign_type}'. "
                f"Valid values: {', '.join(VALID_CAMPAIGN_TYPES)}."
            )
        old_type = config.get("campaign_type") or DEFAULT_CAMPAIGN_TYPE
        if wanted_type != old_type:
            config["campaign_type"] = wanted_type
            change_descriptions.append(f"Campaign type: {old_type} -> {wanted_type}")

    # Commit config_json changes if any
    config_updated = json.dumps(config)
    if config_updated != (config_json or "{}"):
        changes["config_json"] = config_updated

    # ── Update context fields (offerings, case_studies, social_proofs, preferences) ──
    if has_context_fields:
        existing_ctx = await db.get_campaign_context(campaign_id)
        ctx_changed = False
        if offerings:
            existing_ctx["offerings"] = offerings
            ctx_changed = True
            change_descriptions.append(f"Offerings: {offerings[:80]}{'...' if len(offerings) > 80 else ''}")
        if case_studies:
            existing_ctx["case_studies"] = case_studies
            ctx_changed = True
            change_descriptions.append(f"Case studies: {case_studies[:80]}{'...' if len(case_studies) > 80 else ''}")
        if social_proofs:
            existing_ctx["social_proofs"] = social_proofs
            ctx_changed = True
            change_descriptions.append(f"Social proofs: {social_proofs[:80]}{'...' if len(social_proofs) > 80 else ''}")
        if campaign_preferences:
            existing_ctx["campaign_preferences"] = campaign_preferences
            ctx_changed = True
            change_descriptions.append(f"Preferences: {campaign_preferences[:80]}{'...' if len(campaign_preferences) > 80 else ''}")
        if project_brief:
            existing_ctx["project_brief"] = project_brief
            ctx_changed = True
            change_descriptions.append(
                f"Project brief: {project_brief[:80]}{'...' if len(project_brief) > 80 else ''}"
            )
        if any([product, go_live, volume, must_confirm]):
            from ..services.project_brief import merge_project_facts
            existing_ctx = merge_project_facts(
                existing_ctx,
                product=product,
                go_live=go_live,
                volume=volume,
                must_confirm=must_confirm,
            )
            ctx_changed = True
            facts = existing_ctx.get("project_facts") or {}
            bits = [f"{k}={facts[k]}" for k in ("product", "go_live", "volume") if facts.get(k)]
            if facts.get("must_confirm"):
                bits.append(f"must_confirm={len(facts['must_confirm'])} items")
            change_descriptions.append("Project facts: " + ", ".join(bits))
        if ctx_changed:
            changes["context_json"] = json.dumps(existing_ctx)

    if not changes:
        # An explicit campaign_type still goes to the cloud. The cloud is what
        # sends, and it learned this key later than the client did: re-issuing
        # the same value is how an operator repairs a campaign whose earlier
        # push was dropped. It must not be a no-op, and it must not report
        # success when the push failed, since that is the case it exists for.
        if wanted_type:
            if not is_backend_mode():
                return (
                    f"Campaign type for '{old_name}' is {wanted_type}. "
                    "No hosted account is connected, so there is nothing to push."
                )
            synced = await sync_campaign_settings(
                campaign_id, {"campaign_type": wanted_type},
            )
            if synced:
                return (
                    f"Campaign type for '{old_name}' is {wanted_type}; "
                    "re-sent to the cloud."
                )
            logger.warning(
                "Could not sync campaign_type for campaign %s to backend",
                campaign_id,
            )
            return (
                f"Campaign type for '{old_name}' is {wanted_type}, "
                "but the cloud push failed. Try again."
            )
        return (
            f"No changes needed for '{old_name}'.\n"
            "The campaign already has those settings."
        )

    await db.update_campaign(campaign_id, **changes)

    # ── Log warmup changes so optimizer respects user decisions ──
    warmup_keys = {"enable_profile_views", "enable_follows", "enable_endorsements", "enable_engagements"}
    warmup_changed = {k for k, v in [
        ("enable_profile_views", enable_profile_views),
        ("enable_follows", enable_follows),
        ("enable_endorsements", enable_endorsements),
        ("enable_engagements", enable_engagements),
    ] if v}
    if warmup_changed:
        try:
            from ..db.signal_queries import save_optimization_history
            from ..db.async_bridge import run_db as _run_db
            for key in warmup_changed:
                val = locals()[key]
                await _run_db(
                    save_optimization_history,
                    optimization_type="user_changed_warmup",
                    target=f"{old_name}::{key}",
                    before_value=str(config.get(key, True)),
                    after_value=val,
                    reason="User manually changed campaign warmup setting",
                )
        except Exception:
            pass  # Don't block campaign edit if logging fails

    # ── Sync settings to backend ──
    # Build a settings dict matching the backend's PATCH /campaigns/{id}/settings schema
    sync_settings: dict[str, str | int] = {}
    for key, val in (
        ("enable_profile_views", enable_profile_views),
        ("enable_follows", enable_follows),
        ("enable_endorsements", enable_endorsements),
        ("enable_engagements", enable_engagements),
        ("enable_followups", enable_followups),
        ("enable_auto_replies", enable_auto_replies),
        ("enable_invitations", enable_invitations),
        ("enable_discovery", enable_discovery),
        # Without this line the client's 15-minute config_json push would
        # overwrite a dashboard-set exclude_connections back to false within
        # the quarter hour. Same trap that ate the other campaign settings.
        ("exclude_connections", exclude_connections),
        ("connections_only", connections_only),
        ("exclude_competitors", exclude_competitors),
        ("withdraw_stale_invites", withdraw_stale_invites),
        ("inmail_fallback", inmail_fallback),
        ("inmail_first_touch", inmail_first_touch),
        ("send_in_business_hours", send_in_business_hours),
    ):
        if val:
            sync_settings[key] = val
    if engagement_mode:
        sync_settings["engagement_mode"] = engagement_mode
    if max_followups:
        sync_settings["max_followups"] = max_followups
    if has_target:
        sync_settings["weekly_meeting_target"] = weekly_meeting_target
    if inmail_fallback_days:
        sync_settings["inmail_fallback_days"] = inmail_fallback_days
    if followup_delay_days:
        sync_settings["followup_delay_days"] = followup_delay_days
    if active_days:
        sync_settings["active_days"] = active_days
    if voice_mode:
        sync_settings["voice_mode"] = voice_mode
    if voice_noise:
        sync_settings["voice_noise"] = voice_noise
    if voice_humanize:
        sync_settings["voice_humanize"] = voice_humanize
    if wanted_type:
        # Already validated and normalised above.
        sync_settings["campaign_type"] = wanted_type
    if booking_link:
        # Same key the dashboard Calendar Link field and reply prompts use.
        # Without this push a hosted campaign never learns the URL.
        sync_settings["booking_link"] = booking_link
    if competitor_companies.strip():
        sync_settings["competitor_companies"] = competitor_companies.strip()

    if sync_settings:
        synced = await sync_campaign_settings(campaign_id, sync_settings)
        if not synced:
            logger.warning("Could not sync settings for campaign %s to backend", campaign_id)

    if "context_json" in changes:
        try:
            from ..services.cloud_sync import sync_to_cloud
            await sync_to_cloud(campaign_id=campaign_id)
        except Exception as e:
            logger.warning("Could not sync context_json for campaign %s: %s", campaign_id, e)

    # ── Format result ──
    output = [
        f"Updated campaign '{changes.get('name', old_name)}':\n",
    ]
    for desc in change_descriptions:
        output.append(f"   {desc}")
    output.append("")

    # Mode-specific hints
    if mode == "autopilot" and old_mode == "copilot":
        output.append(
            "Autopilot is now active. Messages will be sent automatically "
            "after passing validation."
        )
    # Copilot mode removed — all campaigns are autopilot

    from ..services.dashboard_snapshot import status_footer

    output.extend(status_footer("campaign", campaign_id, snapshot=False))
    return "\n".join(output)
