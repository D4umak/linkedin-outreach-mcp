"""Tool: brand_strategy — Analyze and improve your LinkedIn personal brand.

Audits your profile, generates a 4-week strategy, executes actions
(auto-publish posts, auto-comment/react/follow, profile suggestions),
and tracks progress.

Fully automated execution:
  - Posts: generated with voice matching and published immediately
  - Engagement: finds trending posts in your niche, comments/reacts/follows
  - Profile edits: auto-applied via Unipile PATCH /api/v1/users/me/edit
"""

from __future__ import annotations

import logging
import time as _time
from typing import Any

from ..db.queries import (
    get_campaign_stats,
    get_rate_limit_today,
    get_sending_days_7d,
    get_setting,
    get_weekly_invitation_sum,
    list_campaigns,
    log_action,
    save_engagement,
)
from ..formatter import progress_bar, stars, table, tree
from ..linkedin import get_account_id, get_linkedin_client, UnipileError
from ..services.brand_service import (
    capture_baseline,
    compute_progress,
    count_plan_progress,
    get_next_pending_action,
    load_brand_analysis,
    load_brand_baseline,
    load_brand_plan,
    mark_action_completed,
    save_brand_analysis,
    save_brand_baseline,
    save_brand_plan,
)
from ..services.health_score import coerce_daily_limit, compute_health_score
from ..db.async_bridge import run_db
from ..ai.voice_block import voice_prompt_block

logger = logging.getLogger(__name__)

# LinkedIn truncates text past these, so refuse rather than silently clip.
HEADLINE_MAX_CHARS = 220
SUMMARY_MAX_CHARS = 2600

# The profile fields that can be set to literal text, and how each one talks
# about itself. Everything else about the two writes is identical.
_TEXT_FIELDS = {
    "headline": {
        "label": "Headline",
        "action": "set_headline",
        "hint": "Your exact headline",
        "max_chars": HEADLINE_MAX_CHARS,
        "log_action": "brand_headline_set",
    },
    "summary": {
        "label": "Summary",
        "action": "set_summary",
        "hint": "Your exact About text",
        "max_chars": SUMMARY_MAX_CHARS,
        "log_action": "brand_summary_set",
    },
}


async def run_brand_strategy(
    action: str = "analyze",
    focus: str = "",
    photo: str = "",
) -> str:
    """Main handler for brand_strategy tool."""

    # ── Pre-checks ──
    setup_done = await run_db(get_setting, "setup_complete", False)
    if not setup_done:
        return (
            "Setup required before analyzing your brand.\n\n"
            "Please run setup_profile first."
        )

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected. Run setup_profile first."

    if action == "analyze":
        return await _handle_analyze(account_id, focus)
    elif action == "plan":
        return await _handle_plan(account_id, focus)
    elif action == "execute":
        return await _handle_execute(account_id)
    elif action == "progress":
        return await _handle_progress(account_id)
    elif action == "upload_photo":
        return await _handle_upload_photo(account_id, photo)
    elif action == "upload_cover":
        return await _handle_upload_cover(account_id, photo)
    elif action == "set_link":
        return await _handle_set_link(account_id, focus)
    elif action == "set_headline":
        return await _handle_set_headline(account_id, focus)
    elif action == "set_summary":
        return await _handle_set_summary(account_id, focus)
    elif action == "set_photo_library":
        return await _handle_set_photo_library(focus)
    elif action == "makeover":
        return await _handle_makeover(account_id)
    elif action == "photo_enhance":
        return await _handle_photo_enhance(account_id)
    elif action == "test_headline":
        return await _handle_test_headline(focus)
    elif action == "cancel_headline_test":
        return await _handle_cancel_headline_test()
    else:
        return (
            "Unknown action. Available actions:\n\n"
            '  brand_strategy(action="analyze")       — Full profile audit\n'
            '  brand_strategy(action="plan")          — Generate 4-week strategy\n'
            '  brand_strategy(action="execute")       — Execute next action\n'
            '  brand_strategy(action="progress")      — Track before/after metrics\n'
            '  brand_strategy(action="upload_photo")  — Upload profile photo\n'
            '  brand_strategy(action="upload_cover")  — Upload cover photo\n'
            '  brand_strategy(action="set_link", focus="https://...")  — Set custom CTA link\n'
            '  brand_strategy(action="set_headline", focus="Your exact headline")  — Set the headline verbatim\n'
            '  brand_strategy(action="set_summary", focus="Your exact About text")  — Set the summary verbatim\n'
            '  brand_strategy(action="set_photo_library", focus="~/Pictures/LinkedIn Photos")  — Photos brand posts may attach\n'
            '  brand_strategy(action="makeover")      — One-click full profile optimization\n'
            '  brand_strategy(action="photo_enhance") — Auto-enhance profile photo settings\n'
            '  brand_strategy(action="test_headline", focus="Variant A | Variant B")  — Start headline A/B test\n'
            '  brand_strategy(action="cancel_headline_test")  — Cancel running headline A/B and restore'
        )


# ──────────────────────────────────────────────
# Action: analyze
# ──────────────────────────────────────────────


async def _handle_analyze(account_id: str, focus: str) -> str:
    """Full profile audit with scored areas."""

    from ..ai.brand_strategist import analyze_brand_profile
    from ..services.brand_service import get_active_icp_context

    # ── Gather data ──
    profile = await run_db(get_setting, "profile", {})
    voice = await run_db(get_setting, "voice_signature", {})
    expertise = await run_db(get_setting, "expertise_map", {})
    icp_context = await run_db(get_active_icp_context)

    # Fetch SSI and posts
    ssi_data: dict[str, Any] = {}
    posts: list[dict] = []
    try:
        client = get_linkedin_client()
        try:
            ssi_data = await client.get_ssi_score(account_id)
        except Exception as e:
            logger.warning("SSI fetch failed: %s", e)

        provider_id = profile.get("provider_id", "")
        if provider_id:
            try:
                posts = await client.get_posts(account_id, provider_id)
            except Exception as e:
                logger.warning("Posts fetch failed: %s", e)
        await client.close()
    except Exception as e:
        logger.warning("Client init failed: %s", e)

    # Aggregate campaign stats
    campaign_stats = await _aggregate_campaign_stats()

    # ── Run LLM analysis ──
    analysis = await analyze_brand_profile(
        profile, posts, ssi_data, campaign_stats, voice, expertise,
        icp_context=icp_context,
    )

    # ── Save ──
    await run_db(save_brand_analysis, analysis)

    # ── Format output ──
    return _format_analysis(analysis, focus)


async def _aggregate_campaign_stats() -> dict[str, Any]:
    """Aggregate acceptance and reply rates across all campaigns."""
    campaigns = await run_db(list_campaigns)
    total_invited = 0
    total_connected = 0
    total_replied = 0

    for c in campaigns:
        if c.get("status") in ("draft", "archived"):
            continue
        stats = await run_db(get_campaign_stats, c["id"])
        total_invited += stats.get("invited", 0)
        total_connected += stats.get("connected", 0)
        total_replied += stats.get("replied", 0)

    acceptance_rate = total_connected / total_invited if total_invited > 0 else 0
    reply_rate = total_replied / total_connected if total_connected > 0 else 0

    return {
        "acceptance_rate": acceptance_rate,
        "reply_rate": reply_rate,
        "total_invited": total_invited,
        "total_connected": total_connected,
        "total_replied": total_replied,
    }


def _format_analysis(analysis: dict[str, Any], focus: str) -> str:
    """Format brand analysis for chat display."""
    overall = analysis.get("overall_score", 0)
    areas = analysis.get("areas", {})
    top_actions = analysis.get("top_3_actions", [])

    # Overall score with rating
    if overall >= 80:
        rating = "Excellent"
    elif overall >= 60:
        rating = "Good"
    elif overall >= 40:
        rating = "Needs Work"
    else:
        rating = "Critical"

    lines = [
        f"Brand Profile Audit (Score: {overall}/100 — {rating})",
        "",
    ]

    # ── Each area ──
    area_order = ["headline", "summary", "content", "profile_completeness", "ssi_breakdown", "engagement"]
    area_labels = {
        "headline": "Headline",
        "summary": "Summary/About",
        "content": "Content Strategy",
        "profile_completeness": "Profile Completeness",
        "ssi_breakdown": "SSI Score",
        "engagement": "Engagement",
    }

    for key in area_order:
        area = areas.get(key, {})
        if not area:
            continue

        if focus and focus != "all" and focus != key:
            continue

        label = area_labels.get(key, key)
        score = area.get("score", 0)
        lines.append(f"{label}: {score}/10 {stars(score, 10)}")

        # Area-specific details
        if key == "headline":
            current = area.get("current", "")
            if current:
                lines.append(f"  Current: \"{current}\"")
            suggestion = area.get("suggestion", "")
            if suggestion:
                lines.append(f"  Suggested: \"{suggestion}\"")

        issues = area.get("issues", [])
        if issues:
            for issue in issues[:3]:
                lines.append(f"  - {issue}")

        if key == "profile_completeness":
            missing = area.get("missing", [])
            if missing:
                lines.append(f"  Missing: {', '.join(missing)}")

        if key == "ssi_breakdown":
            improvements = area.get("improvements", [])
            for imp in improvements[:3]:
                lines.append(f"  - {imp}")

        lines.append("")

    # ── Top 3 actions ──
    if top_actions:
        lines.append("Top 3 Actions:")
        for i, action in enumerate(top_actions, 1):
            lines.append(f"  {i}. {action}")
        lines.append("")

    lines.append('Next: Run brand_strategy(action="plan") to generate a 4-week improvement plan.')

    return "\n".join(lines)


# ──────────────────────────────────────────────
# Action: plan
# ──────────────────────────────────────────────


async def _handle_plan(account_id: str, focus: str) -> str:
    """Generate a 4-week brand strategy plan."""

    from ..ai.brand_strategist import generate_brand_plan
    from ..services.brand_service import get_active_icp_context

    # Require existing analysis
    analysis = await run_db(load_brand_analysis)
    if not analysis:
        return (
            "No brand analysis found.\n\n"
            'Run brand_strategy(action="analyze") first to audit your profile.'
        )

    profile = await run_db(get_setting, "profile", {})
    voice = await run_db(get_setting, "voice_signature", {})
    expertise = await run_db(get_setting, "expertise_map", {})
    icp_context = await run_db(get_active_icp_context)

    # ── Generate plan via LLM ──
    plan = await generate_brand_plan(
        analysis, profile, voice, expertise, focus or "all",
        icp_context=icp_context,
    )

    if not plan.get("weeks"):
        return "Failed to generate brand plan. Please try again."

    plan["focus"] = focus or "all"

    # ── Capture baseline ──
    ssi_data: dict[str, Any] = {}
    try:
        client = get_linkedin_client()
        ssi_data = await client.get_ssi_score(account_id)
        await client.close()
    except Exception:
        pass

    campaign_stats = await _aggregate_campaign_stats()
    hs = await _compute_current_health(ssi_data, campaign_stats)

    baseline = capture_baseline(profile, ssi_data, hs.total, campaign_stats.get("acceptance_rate", 0))
    await run_db(save_brand_baseline, baseline)

    # ── Save plan ──
    await run_db(save_brand_plan, plan)

    # ── Format output ──
    return _format_plan(plan)


def _format_plan(plan: dict[str, Any]) -> str:
    """Format brand strategy plan for chat display."""
    lines = [
        "4-Week Brand Strategy",
        "",
    ]

    for week in plan.get("weeks", []):
        week_num = week.get("week", "?")
        theme = week.get("theme", "")
        lines.append(f"Week {week_num}: {theme}")

        actions = week.get("actions", [])
        for i, action in enumerate(actions):
            is_last = i == len(actions) - 1
            prefix = "└──" if is_last else "├──"
            desc = action.get("description", "")
            action_type = action.get("type", "")
            icon = {"profile_optimize": "✏️", "post": "📝", "engagement": "💬", "photo_enhance": "📸"}.get(action_type, "📌")
            lines.append(f"  {prefix} {icon} {desc}")

        lines.append("")

    # Content calendar
    calendar = plan.get("content_calendar", [])
    if calendar:
        lines.append("Content Calendar:")
        for entry in calendar:
            day = entry.get("day", "")
            ctype = entry.get("type", "")
            topic = entry.get("example_topic", "")
            lines.append(f"  {day}: {ctype} — {topic}")
        lines.append("")

    # Engagement targets
    targets = plan.get("engagement_targets", {})
    if targets:
        lines.append("Weekly Targets:")
        lines.append(f"  Posts: {targets.get('weekly_posts', 2)}/week")
        lines.append(f"  Comments: {targets.get('daily_comments', 5)}/day")
        lines.append(f"  Reactions: {targets.get('daily_reactions', 10)}/day")
        lines.append("")

    completed, total = count_plan_progress(plan)
    lines.append(f"Progress: {completed}/{total} actions")
    lines.append(f"  {progress_bar(completed, total, 20)}")
    lines.append("")
    lines.append('Next: Run brand_strategy(action="execute") to start working on your plan.')

    return "\n".join(lines)


# ──────────────────────────────────────────────
# Action claims
# ──────────────────────────────────────────────

# The plan is one JSON blob in settings, so an action row cannot be CAS'd the
# way try_claim_outreach() CAS's outreaches.status. The claim therefore lives in
# its own settings row per action, where the PRIMARY KEY on `key` gives the same
# guarantee: one conditional write, rowcount decides the winner. Needed because
# the scheduler daemon and an MCP brand_strategy(action="execute") run in
# separate processes — the leader lock only serialises schedulers — and both
# select the next pending action before either marks it done.
BRAND_ACTION_CLAIM_PREFIX = "brand_action_claim:"

# A claim outlives the run that took it: mark_action_completed rewrites the
# whole plan, so an interleaved write can drop a completion and re-offer an
# action that was already published to LinkedIn.
_CLAIM_RUNNING = "running"
_CLAIM_DONE = "done"

# Long enough to cover generation plus publish; short enough that a crashed run
# does not strand the action forever.
BRAND_ACTION_LEASE_SECONDS = 900

ALREADY_CLAIMED = "Another run is already executing this brand plan action."


def _claim_key(action_id: str) -> str:
    """Namespace the claim to the plan that owns the action.

    Action ids come from the plan LLM as "w1a1", "w1a2"… so every regenerated
    plan reuses them. Without the plan's created_at in the key, the first plan's
    finished claims would block the identically-numbered actions of the next.
    """
    plan = load_brand_plan() or {}
    return f"{BRAND_ACTION_CLAIM_PREFIX}{plan.get('created_at', 0)}:{action_id}"


def _claim_brand_action(action_id: str) -> bool:
    """Atomically claim a plan action. False means another run holds it.

    Takes the claim over only when the previous holder's lease has expired and
    it never reached _CLAIM_DONE — a finished action stays claimed for good.
    """
    import json as _json

    from ..db.schema import get_db

    if not action_id:
        return True  # Nothing to key a claim on; unclaimed types are unchanged.

    now = int(_time.time())
    db = get_db()
    count = db.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at "
        "WHERE settings.value = ? AND settings.updated_at <= ?",
        (
            _claim_key(action_id),
            _json.dumps(_CLAIM_RUNNING),
            now,
            _json.dumps(_CLAIM_RUNNING),
            now - BRAND_ACTION_LEASE_SECONDS,
        ),
    ).rowcount
    db.commit()
    db.close()
    return count > 0


def _finish_brand_action(action_id: str) -> None:
    """Close the claim permanently, so no later run can repeat the action."""
    import json as _json

    from ..db.schema import get_db

    if not action_id:
        return
    db = get_db()
    db.execute(
        "UPDATE settings SET value = ?, updated_at = ? WHERE key = ?",
        (_json.dumps(_CLAIM_DONE), int(_time.time()), _claim_key(action_id)),
    )
    db.commit()
    db.close()


async def _complete_brand_action(action_id: str, result: str) -> None:
    """Permanently close the claim, then tick the plan.

    Finish first: mark_action_completed rewrites the whole plan blob and can
    lose to a concurrent writer. The claim is what stops a retry.
    """
    await run_db(_finish_brand_action, action_id)
    await run_db(mark_action_completed, action_id, result)


# ──────────────────────────────────────────────
# Action: execute
# ──────────────────────────────────────────────


async def _handle_execute(account_id: str) -> str:
    """Execute the next pending action from the plan."""

    plan = await run_db(load_brand_plan)
    if not plan:
        return (
            "No brand strategy plan found.\n\n"
            'Run brand_strategy(action="plan") first to generate a plan.'
        )

    action = get_next_pending_action(plan)
    if not action:
        return (
            "All brand strategy actions completed!\n\n"
            'Run brand_strategy(action="progress") to see your results.'
        )

    action_type = action.get("type", "")
    action_id = action.get("id", "")

    if not await run_db(_claim_brand_action, action_id):
        return ALREADY_CLAIMED

    if action_type == "post":
        return await _execute_post_action(action, action_id, account_id)
    elif action_type == "profile_optimize":
        return await _execute_profile_action(action, action_id)
    elif action_type == "photo_enhance":
        return await _handle_photo_enhance(account_id, action_id=action_id)
    elif action_type == "engagement":
        return await _execute_engagement_action(action, action_id, account_id)
    else:
        await _complete_brand_action(action_id, "Skipped — unknown type")
        return f"Skipped unknown action type: {action_type}. Moving to next."


def _today_weekday() -> str:
    import datetime

    return datetime.date.today().strftime("%A")


def _image_was_rejected(result: dict[str, Any]) -> bool:
    """True when create_post definitely refused the request (a 4xx other than
    auth or rate limiting), so nothing was published and text-only is safe.

    Unipile reports "Post creation failed (400): ..."; the backend proxy's
    httpx error reads "Client error '400 Bad Request' ...". A timeout, a 5xx,
    a block or a disconnected account is not a refusal of the image.
    """
    if result.get("blocked") or result.get("auth_error"):
        return False
    import re

    return bool(re.search(r"\b4(?!01|03|29)\d\d\b", str(result.get("error", ""))))


async def _execute_post_action(action: dict[str, Any], action_id: str, account_id: str) -> str:
    """Generate and auto-publish a LinkedIn post."""
    topic = action.get("topic", action.get("description", ""))
    tone = action.get("tone", "thought-leader")
    if not topic:
        from ..services.brand_service import calendar_entry_for

        plan = await run_db(load_brand_plan) or {}
        entry = calendar_entry_for(plan, _today_weekday())
        if entry:
            topic = entry.get("example_topic") or ""
            if not action.get("tone") and entry.get("type"):
                tone = str(entry["type"])

    profile = await run_db(get_setting, "profile", {})
    voice = await run_db(get_setting, "voice_signature", {})

    name = profile.get("name", "")
    title = profile.get("title", "")
    company = profile.get("company", "")
    industry = profile.get("industry", "")
    from ..ai.copywriter.author_claims import known_facts_rule
    from ..ai.voice_analyzer import voice_prompt_block
    from ..services.voice_examples import example_posts_block, select_example_posts

    voice_block = voice_prompt_block(voice)
    examples = await run_db(select_example_posts, profile, 3)
    examples_block = example_posts_block(examples)

    prompt = f"""Write a LinkedIn post for {name} ({title} at {company}).
Industry: {industry}

HOW {name} WRITES
{voice_block or "No voice signature on file — write plainly and concretely."}

{examples_block}

Tone requested: {tone}
Topic: {topic}

Requirements:
- Write in first person, using {name}'s voice and style
- Keep it between 150-1200 characters (optimal LinkedIn engagement)
- Include a hook in the first line to stop scrolling
- Use short paragraphs (1-3 sentences each)
- End with a question or call-to-action to drive engagement
- Do NOT use hashtags unless the user's style includes them
- Do NOT use emojis unless the user's style includes them
- Be authentic and conversational, not corporate
- Share a genuine insight or perspective. A story about {name} only if this
  prompt tells it.
{known_facts_rule(name)}

Return ONLY the post text, nothing else."""

    try:
        from ..config import has_local_llm_key, is_backend_mode

        if is_backend_mode() and not has_local_llm_key():
            # Route through backend — use generate_message endpoint with post context
            client = get_linkedin_client()
            resp = await client.generate_message(
                {"name": name, "title": title, "company": company, "industry": industry},
                {"name": "audience", "title": "LinkedIn audience"},
                voice,
                {
                    "target": f"LinkedIn post about: {topic}",
                    "type": "post",
                    "max_chars": "1200",
                    "instructions": prompt,
                },
            )
            post_text = resp.get("message", "")
            await client.close()
        else:
            from ..ai.llm import LLMClient

            llm = LLMClient()
            post_text = await llm.generate(prompt, max_tokens=800)
    except Exception as e:
        logger.error("Brand post generation failed: %s", e)
        return f"Failed to generate post: {e}"

    if not post_text or len(post_text) < 30:
        return "Failed to generate post content. Try again."

    # This path auto-publishes on the brand calendar with nobody watching, so
    # it needs the gate more than the manual tool does — and had none until
    # 9 Sep 2026.
    from ..ai.draft_guard import guard_draft
    from ..services.voice_examples import borrows_from_examples

    if borrows_from_examples(post_text, examples):
        return "Draft copied from a past post and was not published."

    # 25 Sep 2026: "Having spoken with over 600 CTOs", for an author who
    # never said it. Nobody reads this post before it goes out, so a figure
    # the author did not give is taken out here or the post does not go.
    from ..ai.copywriter.polish import keep_to_sources

    post_text = await keep_to_sources(
        post_text, sources=(profile, topic), channel="post", max_chars=1200,
    )
    # The floor the draft already had to clear: what is left after the claims
    # go must still be a post, not a stub the gate would pad out.
    if len(post_text) < 30:
        return "Draft was made of claims you never gave and was not published."

    post_text = await guard_draft(post_text, voice or {}, "post", 1200)
    if not post_text:
        return "Draft failed quality checks and was not published."

    # Only once the text has passed the gate, so a rejected draft costs no
    # selection call. The choice is an index into the user's own library;
    # the post text is not touched. Any miss publishes text only.
    from ..services import post_photo

    photo = await post_photo.choose_brand_post_image(topic, post_text)

    # ── Auto-publish ──
    try:
        client = get_linkedin_client()
        # Only pass image when there is one: a text post keeps the two-argument
        # call every implementation of this method already accepts.
        if photo:
            result = await client.create_post(account_id, post_text, image=photo.image)
            if not result.get("success") and _image_was_rejected(result):
                # Before photos this post went out; the photo must not be why
                # it doesn't. Only on a definite refusal: a timeout or 5xx may
                # mean the image post is live, and a retry would post twice.
                logger.warning("Brand post photo rejected, posting text only: %s",
                               result.get("error", ""))
                photo = None
                result = await client.create_post(account_id, post_text)
        else:
            result = await client.create_post(account_id, post_text)
        await client.close()

        if result.get("success"):
            if photo:
                await post_photo.mark_photo_used(photo.path)
            await run_db(log_action, "brand_post_published", details={
                "topic": topic, "tone": tone, "chars": len(post_text),
                "post_id": result.get("post_id", ""),
                post_photo.IMAGE_PATH_DETAIL: photo.path if photo else "",
            })
            # Before mark_action_completed, which is a whole-plan rewrite and
            # can lose to a concurrent one: the post is already public.
            await _complete_brand_action(action_id, f"Published: {topic[:50]}")

            plan = await run_db(load_brand_plan)
            completed, total = count_plan_progress(plan) if plan else (0, 0)

            remaining = await post_photo.library_remaining()
            folder = await run_db(get_setting, post_photo.PHOTO_LIBRARY_SETTING, "")
            refill = ""
            if folder and remaining == 0:
                refill = (
                    "\n\nNo unused photos left. Add more to the photo folder "
                    "(or brand_strategy(action=\"set_photo_library\")) so the "
                    "next posts keep a picture — posts with a photo get more engagement."
                )
            elif folder and remaining <= post_photo.LOW_WATERMARK:
                refill = (
                    f"\n\n{remaining} unused photo"
                    f"{'' if remaining == 1 else 's'} left. Add more so upcoming "
                    "posts keep a picture."
                )
            return (
                f"Brand Strategy: Post Published!\n\n"
                f'   "{post_text[:300]}{"..." if len(post_text) > 300 else ""}"\n'
                f"   ({len(post_text)} chars)\n\n"
                f"Tip: Reply to comments in the first 2 hours to boost reach.\n\n"
                f"Progress: {completed}/{total} actions completed"
                f"{refill}"
            )
        else:
            error = result.get("error", "Unknown error")
            return f"Post generated but publish failed: {error}\n\nDraft:\n{post_text}"
    except Exception as e:
        return f"Post generated but publish failed: {e}\n\nDraft:\n{post_text}"


async def _execute_profile_action(action: dict[str, Any], action_id: str) -> str:
    """Generate profile optimization and auto-apply via Unipile PATCH endpoint."""
    from ..ai.brand_strategist import generate_brand_action
    from ..services.brand_service import get_active_icp_context

    profile = await run_db(get_setting, "profile", {})
    voice = await run_db(get_setting, "voice_signature", {})
    expertise = await run_db(get_setting, "expertise_map", {})
    analysis = await run_db(load_brand_analysis) or {}
    subtype = action.get("subtype", "headline")
    icp_context = await run_db(get_active_icp_context)

    # For education/open_to_work — use action description directly (no LLM generation needed)
    if subtype in ("education", "open_to_work"):
        return await _execute_direct_profile_action(action, action_id, subtype)

    result = await generate_brand_action(
        action, profile, voice, analysis, icp_context=icp_context,
        expertise=expertise,
    )

    if result.get("error"):
        return f"Failed to generate suggestion: {result['error']}"

    # ── Auto-apply via profile editor (with change tracking) ──
    from .profile_editor import apply_profile_change

    provider_id = profile.get("provider_id", "")
    account_id = await run_db(get_account_id)
    auto_applied = False

    if provider_id and account_id:
        try:
            client = get_linkedin_client()
            if subtype == "headline" and result.get("options"):
                best = result["options"][0]["text"]
                apply_result = await apply_profile_change(
                    client, account_id, provider_id, "headline", best,
                    source="brand_strategy",
                )
                if apply_result.get("success"):
                    auto_applied = True
                    logger.info("Auto-applied headline: %s", best[:50])
                else:
                    logger.warning(
                        "Auto-apply headline failed: %s",
                        apply_result.get("error", "unknown error"),
                    )
            elif subtype == "summary" and result.get("summary"):
                apply_result = await apply_profile_change(
                    client, account_id, provider_id, "summary", result["summary"],
                    source="brand_strategy",
                )
                if apply_result.get("success"):
                    auto_applied = True
                    logger.info("Auto-applied summary update")
                else:
                    logger.warning(
                        "Auto-apply summary failed: %s",
                        apply_result.get("error", "unknown error"),
                    )
            await client.close()
        except Exception as e:
            logger.warning("Auto-apply %s failed (falling back to suggestions): %s", subtype, e)

    if auto_applied:
        await _complete_brand_action(action_id, f"Auto-applied {subtype}")
    elif not provider_id or not account_id:
        await _complete_brand_action(action_id, f"Generated {subtype} suggestions")
    else:
        # Apply was attempted and failed — leave pending so the next run retries.
        return (
            f"Brand Strategy: Optimize {subtype.title()}\n"
            "(Auto-apply failed — action left pending for retry)\n"
        )

    plan = await run_db(load_brand_plan)
    completed, total = count_plan_progress(plan) if plan else (0, 0)

    if auto_applied:
        # Profile was updated automatically
        if subtype == "headline":
            best = result["options"][0]["text"]
            lines = [
                f"Brand Strategy: Headline Updated Automatically!",
                "",
                f'  New headline: "{best}"',
                f"  Rationale: {result['options'][0].get('rationale', '')}",
                "",
            ]
        else:
            lines = [
                f"Brand Strategy: Summary Updated Automatically!",
                "",
                f"  {result['summary'][:300]}{'...' if len(result.get('summary', '')) > 300 else ''}",
                "",
            ]
        lines.append(f"Progress: {completed}/{total} actions completed")
        return "\n".join(lines)

    # Fallback: show suggestions for manual application
    lines = [
        f"Brand Strategy: Optimize {subtype.title()}",
        "(Auto-apply was not possible — please apply manually)",
        "",
    ]

    if subtype == "headline":
        options = result.get("options", [])
        if options:
            for i, opt in enumerate(options, 1):
                text = opt.get("text", "")
                rationale = opt.get("rationale", "")
                lines.append(f"Option {i}: \"{text}\"")
                if rationale:
                    lines.append(f"  Why: {rationale}")
                lines.append("")
            lines.append("Copy your preferred headline and update it on LinkedIn.")
        else:
            lines.append("Could not generate headline options.")

    elif subtype == "summary":
        summary = result.get("summary", "")
        hook = result.get("hook", "")
        if summary:
            lines.append("Suggested About Section:")
            lines.append("")
            lines.append(f"  {summary}")
            lines.append("")
            if hook:
                lines.append(f"Hook (first 2 lines): \"{hook}\"")
                lines.append("")
            lines.append("Copy this to your LinkedIn About section and customize as needed.")
        else:
            lines.append("Could not generate summary.")

    lines.append("")
    lines.append(f"Progress: {completed}/{total} actions completed")

    return "\n".join(lines)


async def _execute_engagement_action(
    action: dict[str, Any], action_id: str, account_id: str,
) -> str:
    """Auto-execute engagement: find trending posts, comment/react/follow."""
    desc = action.get("description", "")
    target = action.get("target_count", 5)
    engagement_type = action.get("engagement_type", "comment")  # comment, react, follow

    profile = await run_db(get_setting, "profile", {})
    voice = await run_db(get_setting, "voice_signature", {})
    expertise = await run_db(get_setting, "expertise_map", {})

    # Derive search keywords from user's expertise and industry
    keywords = expertise.get("credible_topics", expertise.get("core", ""))
    if not keywords:
        keywords = profile.get("industry", profile.get("title", ""))

    results: list[str] = []
    errors: list[str] = []
    count = 0

    try:
        client = get_linkedin_client()

        # ── Step 1: Discover trending posts in the user's niche ──
        posts: list[dict] = []
        try:
            search_results, _ = await client.search_posts(
                account_id, keywords, limit=min(target * 2, 20),
            )
            for sr in search_results:
                post_id = sr.get("post_id", sr.get("id", ""))
                text = sr.get("text", "")
                author = sr.get("author_name", "")
                if post_id and text and len(text) > 30:
                    author_lid = sr.get("author_provider_id", sr.get("provider_id", ""))
                    posts.append({
                        "post_id": post_id,
                        "text": text,
                        "author": author,
                        "provider_id": author_lid,
                    })
                    # Persist post + author
                    from ..db.post_queries import upsert_post
                    await run_db(upsert_post, post_id,
                        author_linkedin_id=author_lid,
                        author_name=author,
                        text=text[:2000],
                        source="brand_strategy",)
        except Exception as e:
            logger.warning("Post search failed: %s", e)
            errors.append(f"Post search failed: {e}")

        # ── Step 2: Execute engagements ──
        from ..linkedin.rate_limiter import check_engagement_budget

        for post in posts[:target]:
            post_id = post["post_id"]

            if engagement_type == "follow" and post.get("provider_id"):
                # Follow the post author
                try:
                    ok, current, cap = await check_engagement_budget("follow")
                    if not ok:
                        errors.append(
                            f"Follow cap reached ({current}/{cap}) — stopping"
                        )
                        break
                    follow_result = await client.follow_profile(account_id, post["provider_id"])
                    if follow_result.get("success"):
                        await run_db(save_engagement, outreach_id=None,
                            action_type="follow",
                            post_id=post_id,
                            post_text="",
                            text=f"Followed {post['author']}",
                            status="sent",
                            reasoning="brand_strategy",)
                        results.append(f"Followed {post['author']}")
                        count += 1
                    else:
                        err = follow_result.get("error", "unknown error")
                        logger.warning("Follow failed for %s: %s", post["author"], err)
                        errors.append(f"Follow failed ({post['author']}): {err}")
                except Exception as e:
                    errors.append(f"Follow failed ({post['author']}): {e}")
                continue

            if engagement_type == "react" or engagement_type == "like":
                # React to the post
                try:
                    ok, current, cap = await check_engagement_budget("react")
                    if not ok:
                        errors.append(
                            f"React cap reached ({current}/{cap}) — stopping"
                        )
                        break
                    react_result = await client.send_post_reaction(account_id, post_id, "LIKE")
                    if react_result.get("success"):
                        await run_db(save_engagement, outreach_id=None,
                            action_type="react",
                            post_id=post_id,
                            post_text=post["text"][:500],
                            reaction_type="LIKE",
                            status="sent",
                            reasoning="brand_strategy",)
                        results.append(f"Liked post by {post['author']}")
                        count += 1
                    else:
                        err = react_result.get("error", "unknown error")
                        logger.warning("React failed for %s: %s", post["author"], err)
                        errors.append(f"React failed ({post['author']}): {err}")
                except Exception as e:
                    errors.append(f"React failed ({post['author']}): {e}")
                continue

            # Default: comment
            try:
                ok, current, cap = await check_engagement_budget(
                    "comment", reserve=False,
                )
                if not ok:
                    errors.append(
                        f"Comment cap reached ({current}/{cap}) — stopping"
                    )
                    break

                voice_desc = voice_prompt_block(voice) or "Plain and direct."
                comment_prompt = f"""Write a brief, authentic LinkedIn comment on this post.

Post by {post['author']}:
"{post['text'][:500]}"

You are {profile.get('name', '')} ({profile.get('title', '')}).
Your voice:
{voice_desc}

Rules:
- 50-200 characters max
- Be genuine, add value (insight, question, agreement with nuance)
- Match the user's voice tone
- No generic "Great post!" or "Thanks for sharing!"
- Do NOT use emojis unless the user's style includes them

Return ONLY the comment text."""

                from ..config import has_local_llm_key, is_backend_mode

                if is_backend_mode() and not has_local_llm_key():
                    resp = await client.generate_comment(
                        {"name": profile.get("name", ""), "title": profile.get("title", "")},
                        {"name": post["author"]},
                        voice,
                        {"text": post["text"][:500], "post_id": post["post_id"]},
                    )
                    comment_text = resp.get("comment", "")
                else:
                    from ..ai.llm import LLMClient

                    llm = LLMClient()
                    comment_text = await llm.generate(comment_prompt, max_tokens=200)
                if comment_text and len(comment_text) >= 10:
                    ok, current, cap = await check_engagement_budget("comment")
                    if not ok:
                        errors.append(
                            f"Comment cap reached ({current}/{cap}) — stopping"
                        )
                        break
                    comment_result = await client.send_post_comment(account_id, post_id, comment_text)
                    if comment_result.get("success"):
                        await run_db(save_engagement, outreach_id=None,
                            action_type="comment",
                            post_id=post_id,
                            post_text=post["text"][:500],
                            text=comment_text,
                            status="sent",
                            reasoning="brand_strategy",)
                        results.append(
                            f"Commented on {post['author']}'s post: "
                            f'"{comment_text[:60]}{"..." if len(comment_text) > 60 else ""}"'
                        )
                        count += 1
                    else:
                        err = comment_result.get("error", "unknown error")
                        logger.warning("Comment failed for %s: %s", post["author"], err)
                        errors.append(f"Comment failed ({post['author']}): {err}")
                else:
                    # Fallback to reaction if comment generation fails
                    ok, current, cap = await check_engagement_budget("react")
                    if not ok:
                        errors.append(
                            f"React cap reached ({current}/{cap})"
                        )
                        continue
                    fallback_result = await client.send_post_reaction(account_id, post_id, "LIKE")
                    if fallback_result.get("success"):
                        await run_db(save_engagement, outreach_id=None,
                            action_type="react",
                            post_id=post_id,
                            post_text=post["text"][:500],
                            reaction_type="LIKE",
                            status="sent",
                            reasoning="brand_strategy",)
                        results.append(f"Liked post by {post['author']} (comment gen failed)")
                        count += 1
                    else:
                        err = fallback_result.get("error", "unknown error")
                        errors.append(f"React fallback failed ({post['author']}): {err}")
            except Exception as e:
                # Fallback to reaction on any error
                try:
                    ok, current, cap = await check_engagement_budget("react")
                    if not ok:
                        errors.append(
                            f"Engage failed ({post['author']}): {e}; "
                            f"react cap reached ({current}/{cap})"
                        )
                        continue
                    fallback_result = await client.send_post_reaction(account_id, post_id, "LIKE")
                    if fallback_result.get("success"):
                        await run_db(save_engagement, outreach_id=None,
                            action_type="react",
                            post_id=post_id,
                            post_text=post["text"][:500],
                            reaction_type="LIKE",
                            status="sent",
                            reasoning="brand_strategy",)
                        results.append(f"Liked post by {post['author']} (comment failed)")
                        count += 1
                    else:
                        err = fallback_result.get("error", "unknown error")
                        errors.append(f"Engage failed ({post['author']}): {err}")
                except Exception as fallback_err:
                    errors.append(f"Engage failed ({post['author']}): {e}; fallback also failed: {fallback_err}")

        await client.close()
    except Exception as e:
        logger.error("Engagement execution failed: %s", e)
        errors.append(f"Client error: {e}")

    await run_db(log_action, "brand_engagement", details={
        "type": engagement_type, "target": target,
        "completed": count, "keywords": keywords[:100],
    })

    if count > 0:
        await _complete_brand_action(action_id, f"{engagement_type}: {count}/{target}")

    plan = await run_db(load_brand_plan)
    completed, total = count_plan_progress(plan) if plan else (0, 0)

    # ── Format output ──
    lines = [
        f"Brand Strategy: Engagement ({engagement_type.title()})",
        f"Target: {desc}",
        f"Completed: {count}/{target}",
        "",
    ]

    if results:
        lines.append("Actions taken:")
        for r in results:
            lines.append(f"  ✅ {r}")
        lines.append("")

    if errors:
        lines.append("Issues:")
        for e in errors[:3]:
            lines.append(f"  ⚠️ {e}")
        lines.append("")

    lines.append(f"Progress: {completed}/{total} actions completed")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# Action: progress
# ──────────────────────────────────────────────


async def _handle_progress(account_id: str) -> str:
    """Show before/after metrics and action progress."""

    baseline = await run_db(load_brand_baseline)
    plan = await run_db(load_brand_plan)

    if not baseline or not plan:
        return (
            "No brand strategy in progress.\n\n"
            'Run brand_strategy(action="analyze") first, then brand_strategy(action="plan").'
        )

    # Fetch current metrics
    ssi_data: dict[str, Any] = {}
    ssi_available = False
    try:
        client = get_linkedin_client()
        ssi_data = await client.get_ssi_score(account_id)
        await client.close()
        ssi_available = True
    except Exception:
        pass

    campaign_stats = await _aggregate_campaign_stats()
    hs = await _compute_current_health(ssi_data, campaign_stats)

    progress = await run_db(
        compute_progress,
        baseline,
        ssi_data,
        campaign_stats.get("acceptance_rate", 0),
        hs.total,
        plan,
        ssi_available=ssi_available,
    )

    return await run_db(_format_progress, progress, plan)


def _format_progress(progress: dict[str, Any], plan: dict[str, Any]) -> str:
    """Format progress report."""

    def _change_str(change: float | int, is_pct: bool = False) -> str:
        if change > 0:
            return f"+{change:.0%}" if is_pct else f"+{change}"
        elif change < 0:
            return f"{change:.0%}" if is_pct else f"{change}"
        return "—"

    lines = [
        f"Brand Strategy Progress (Day {progress['days_since_start']})",
        "",
        "Brand work since plan start:",
    ]
    lines.append(table(
        ["Work", "Count"],
        [
            ["Posts published", str(progress.get("posts_published", 0))],
            ["Brand engagements", str(progress.get("brand_engagements", 0))],
            ["Profile edits", str(progress.get("profile_changes", 0))],
        ],
    ))
    lines.append("")

    # Account metrics — SSI only when the fetch succeeded (a miss is not 0).
    headers = ["Metric", "Before", "Now", "Change"]
    rows: list[list[str]] = []
    if progress.get("ssi_available"):
        rows.append([
            "SSI Score",
            str(progress["ssi_before"]),
            str(progress["ssi_now"]),
            _change_str(progress["ssi_change"]),
        ])
    rows.extend([
        [
            "Acceptance Rate",
            f"{progress['acceptance_before']:.0%}",
            f"{progress['acceptance_now']:.0%}",
            _change_str(progress["acceptance_change"], is_pct=True),
        ],
        [
            "Health Score",
            str(progress["health_before"]),
            str(progress["health_now"]),
            _change_str(progress["health_change"]),
        ],
    ])

    lines.append("Account:")
    lines.append(table(headers, rows))
    lines.append("")

    # Action progress
    completed = progress["actions_completed"]
    total = progress["actions_total"]
    lines.append(f"Actions: {completed}/{total} completed")
    lines.append(f"  {progress_bar(completed, total, 20)}")
    lines.append("")

    # Per-week breakdown
    for week in plan.get("weeks", []):
        week_num = week.get("week", "?")
        theme = week.get("theme", "")
        actions = week.get("actions", [])
        week_done = sum(1 for a in actions if a.get("status") == "completed")
        week_total = len(actions)
        status_icon = "✅" if week_done == week_total else "⏳"
        lines.append(f"  {status_icon} Week {week_num}: {theme} ({week_done}/{week_total})")

    lines.append("")

    from ..db.queries import get_headline_variant_stats, list_ab_tests

    running_headline = [
        t for t in list_ab_tests(status="running") if t.get("test_type") == "headline"
    ]
    if running_headline:
        test = running_headline[0]
        stats = get_headline_variant_stats(test.get("campaign_id", ""))
        age_days = 0
        if test.get("created_at"):
            import time as _t
            age_days = max(0, (int(_t.time()) - int(test["created_at"])) // 86400)
        a_n = (stats.get("A") or {}).get("invited", 0)
        b_n = (stats.get("B") or {}).get("invited", 0)
        lines.append("Headline A/B test running:")
        lines.append(f'  A: "{test.get("variant_a", "")}" — {a_n} invited')
        lines.append(f'  B: "{test.get("variant_b", "")}" — {b_n} invited')
        lines.append(f"  Day {age_days + 1}")
        lines.append('  Cancel: brand_strategy(action="cancel_headline_test")')
        lines.append("")
    else:
        done = [
            t for t in list_ab_tests(status="completed") if t.get("test_type") == "headline"
        ]
        if done:
            latest = done[0]
            winner = latest.get("winner") or ""
            if winner in ("A", "B"):
                winning = latest.get("variant_a") if winner == "A" else latest.get("variant_b")
                lines.append(f'Headline A/B winner {winner} is live: "{winning}"')
            else:
                lines.append("Headline A/B ended inconclusive — pre-test headline restored.")
            lines.append("")

    if completed < total:
        lines.append(f'{progress["actions_remaining"]} actions remaining.')
        lines.append('Run brand_strategy(action="execute") to continue.')
    else:
        lines.append("All actions completed! Great work on your personal brand.")
        lines.append('Run brand_strategy(action="analyze") for a fresh audit to measure improvement.')

    return "\n".join(lines)


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


async def _handle_upload_photo(account_id: str, photo_input: str) -> str:
    """Handle profile photo upload via file path or base64 data."""
    if not photo_input:
        return (
            "Please provide your photo:\n\n"
            '  brand_strategy(action="upload_photo", photo="/path/to/your/photo.jpg")\n\n'
            "Or provide base64-encoded image data:\n"
            '  brand_strategy(action="upload_photo", photo="data:image/jpeg;base64,...")\n\n'
            "Supported formats: JPEG, PNG (recommended: 400x400px, under 8MB)"
        )

    import base64
    from pathlib import Path

    image_bytes: bytes | None = None
    content_type = "image/jpeg"

    # Option 1: Base64 data URI
    if photo_input.startswith("data:"):
        try:
            header, data = photo_input.split(",", 1)
            content_type = header.split(";")[0].replace("data:", "")
            image_bytes = base64.b64decode(data)
        except Exception as e:
            return f"Invalid base64 data URI: {e}"

    # Option 2: Raw base64 string (long string, not a file path)
    elif len(photo_input) > 500 and not photo_input.startswith("/"):
        try:
            image_bytes = base64.b64decode(photo_input)
        except Exception as e:
            return f"Invalid base64 data: {e}"

    # Option 3: File path
    else:
        path = Path(photo_input).expanduser()
        if not path.exists():
            return f"File not found: {photo_input}"
        if path.stat().st_size > 8 * 1024 * 1024:
            return "Photo must be under 8MB."
        image_bytes = path.read_bytes()
        suffix = path.suffix.lower().lstrip(".")
        content_type = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        }.get(suffix, "image/jpeg")

    if not image_bytes:
        return "Could not read the photo. Please provide a valid file path or base64 data."

    # Attempt upload (with change tracking)
    from .profile_editor import apply_profile_change

    profile = await run_db(get_setting, "profile", {})
    provider_id = profile.get("provider_id", "")

    try:
        client = get_linkedin_client()
        result = await apply_profile_change(
            client, account_id, provider_id, "photo", "<photo uploaded>",
            source="brand_strategy",
            image_bytes=image_bytes, content_type=content_type,
        )
        await client.close()

        if result.get("success"):
            await run_db(log_action, "brand_photo_uploaded", details={"size": len(image_bytes)})
            return (
                "Profile photo uploaded successfully!\n\n"
                "LinkedIn may take a few minutes to process and display your new photo.\n"
                "A professional headshot significantly improves connection acceptance rates."
            )
        else:
            error = result.get("error", "Unknown error")
            logger.warning("Photo upload via API failed: %s", error)
            return (
                f"Photo upload via API failed: {error}\n\n"
                "LinkedIn's API may not support direct photo uploads at this time.\n"
                "Please upload your photo manually:\n"
                "  1. Go to linkedin.com/in/me\n"
                "  2. Click the camera icon on your profile photo\n"
                "  3. Upload your photo\n\n"
                "A professional headshot increases profile views by 14x."
            )
    except Exception as e:
        return f"Photo upload failed: {e}\n\nPlease upload manually at linkedin.com/in/me"


async def _handle_upload_cover(account_id: str, photo_input: str) -> str:
    """Handle cover photo upload via file path or base64 data."""
    if not photo_input:
        return (
            "Please provide your cover photo:\n\n"
            '  brand_strategy(action="upload_cover", photo="/path/to/cover.jpg")\n\n'
            "Or provide base64-encoded image data:\n"
            '  brand_strategy(action="upload_cover", photo="data:image/jpeg;base64,...")\n\n'
            "Recommended: 1584x396px, under 8MB (JPEG or PNG)"
        )

    import base64
    from pathlib import Path

    image_bytes: bytes | None = None
    content_type = "image/jpeg"

    if photo_input.startswith("data:"):
        try:
            header, data = photo_input.split(",", 1)
            content_type = header.split(";")[0].replace("data:", "")
            image_bytes = base64.b64decode(data)
        except Exception as e:
            return f"Invalid base64 data URI: {e}"
    elif len(photo_input) > 500 and not photo_input.startswith("/"):
        try:
            image_bytes = base64.b64decode(photo_input)
        except Exception as e:
            return f"Invalid base64 data: {e}"
    else:
        path = Path(photo_input).expanduser()
        if not path.exists():
            return f"File not found: {photo_input}"
        if path.stat().st_size > 8 * 1024 * 1024:
            return "Cover photo must be under 8MB."
        image_bytes = path.read_bytes()
        suffix = path.suffix.lower().lstrip(".")
        content_type = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        }.get(suffix, "image/jpeg")

    if not image_bytes:
        return "Could not read the cover photo. Please provide a valid file path or base64 data."

    from .profile_editor import apply_profile_change

    profile = await run_db(get_setting, "profile", {})
    provider_id = profile.get("provider_id", "")

    try:
        client = get_linkedin_client()
        result = await apply_profile_change(
            client, account_id, provider_id, "cover_photo", "<cover photo uploaded>",
            source="brand_strategy",
            image_bytes=image_bytes, content_type=content_type,
        )
        await client.close()

        if result.get("success"):
            await run_db(log_action, "brand_cover_uploaded", details={"size": len(image_bytes)})
            return (
                "Cover photo uploaded successfully!\n\n"
                "LinkedIn may take a few minutes to process and display your new cover photo.\n"
                "A branded cover image reinforces your value proposition at a glance."
            )
        else:
            error = result.get("error", "Unknown error")
            return (
                f"Cover photo upload failed: {error}\n\n"
                "Please upload manually: linkedin.com/in/me → Edit intro → pencil on cover image."
            )
    except Exception as e:
        return f"Cover photo upload failed: {e}\n\nPlease upload manually at linkedin.com/in/me"


async def _handle_set_link(account_id: str, url: str) -> str:
    """Set the custom CTA link on the profile (e.g. Calendly, website)."""
    if not url:
        return (
            "Please provide the URL for your custom link:\n\n"
            '  brand_strategy(action="set_link", focus="https://cal.com/your-link")\n\n'
            "This sets the clickable CTA button on your LinkedIn profile.\n"
            "Common choices: Calendly, website, portfolio, lead magnet."
        )

    import json

    category = "WEBSITE"
    link_data = json.dumps({"category": category, "url": url})

    from .profile_editor import apply_profile_change

    profile = await run_db(get_setting, "profile", {})
    provider_id = profile.get("provider_id", "")

    try:
        client = get_linkedin_client()
        result = await apply_profile_change(
            client, account_id, provider_id, "custom_link", link_data,
            source="brand_strategy",
        )
        await client.close()

        if result.get("success"):
            await run_db(log_action, "brand_link_set", details={"url": url, "category": category})
            return (
                f"Custom link set successfully!\n\n"
                f"  Category: {category}\n"
                f"  URL: {url}\n\n"
                "Visitors to your profile will now see a clickable link button.\n"
                "This is one of the highest-conversion profile elements for outreach."
            )
        else:
            error = result.get("error", "Unknown error")
            return f"Failed to set custom link: {error}"
    except Exception as e:
        return f"Failed to set custom link: {e}"


async def _handle_set_photo_library(folder: str) -> str:
    """Point brand-calendar posts at a folder of the user's own photos."""
    import os

    from ..db.queries import save_setting
    from ..services import cloud_sync, post_photo

    setting = post_photo.PHOTO_LIBRARY_SETTING
    folder = (folder or "").strip()

    if not folder:
        current = await run_db(get_setting, setting, "")
        return (
            "Please provide the folder of photos brand posts may use:\n\n"
            '  brand_strategy(action="set_photo_library", focus="~/Pictures/LinkedIn Photos")\n'
            '  brand_strategy(action="set_photo_library", focus="off")  — Post text only\n\n'
            "Name files after what they show, e.g. \"036 - headshot, plain wall.jpeg\".\n"
            "Photos in personal, family or screenshot folders are never used.\n\n"
            f"Current: {current or 'not set (brand posts are text only)'}"
        )

    if folder.lower() in ("off", "none", "clear"):
        await run_db(save_setting, setting, "")
        await run_db(log_action, "brand_photo_library_cleared")
        return "Photo library cleared. Brand-calendar posts go out text only."

    root = os.path.abspath(os.path.expanduser(folder))
    if not os.path.isdir(root):
        return f"No folder at {folder}. Nothing changed."

    import asyncio

    # Off the loop: a folder under iCloud Drive can stall a directory walk.
    try:
        photos = await asyncio.wait_for(
            asyncio.to_thread(post_photo.list_library_photos, root),
            timeout=post_photo.LIBRARY_IO_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return f"Reading {folder} took too long (is it still syncing?). Nothing changed."
    if not photos:
        return (
            f"{folder} has no photos a post can carry (png, jpg, gif or webp, outside "
            "personal, family or screenshot folders). Nothing changed."
        )

    await run_db(save_setting, setting, root)
    await run_db(log_action, "brand_photo_library_set", details={
        "folder": root, "photos": len(photos),
    })

    lines = [
        f"Photo library set: {root}",
        "",
        f"  {len(photos)} photos brand posts can use",
    ]
    private = post_photo.private_subfolders(root)
    if private:
        lines.append(f"  Never used: {', '.join(private)}")
    lines += [
        "",
        (
            "Each unused photo is attached to one draft and never reused. "
            "When they run out, HeyLead will ask for more so posts keep a picture."
        ),
    ]
    ingested = 0
    try:
        ingested = await post_photo.ingest_library_to_cloud(photos)
    except Exception as e:
        logger.warning("Photo library ingest failed: %s", e)
    if ingested:
        lines += [
            "",
            f"  {ingested} photos copied to the hosted library so drafts already show them.",
        ]
    elif await run_db(cloud_sync.cloud_owns_account_sending):
        lines += [
            "",
            (
                "Could not copy this folder to the host yet. Add the same photos in "
                "Content when you first turn on autoposting, so drafts are not text only."
            ),
        ]
    return "\n".join(lines)


def _preview(value: str, width: int = 70) -> str:
    """One-line, length-capped rendering — a summary is many lines long."""
    flat = " ".join(value.split())
    if len(flat) <= width:
        return flat
    return flat[: width - 1] + "…"


async def _handle_set_profile_text(account_id: str, field: str, text: str) -> str:
    """Set *field* to exactly *text*.

    ``execute`` and ``makeover`` write model-generated copy. This writes the
    words the user chose, unchanged, so wording decided outside HeyLead does
    not have to be pasted into LinkedIn by hand.
    """
    spec = _TEXT_FIELDS[field]
    label, max_chars = spec["label"], spec["max_chars"]

    profile = await run_db(get_setting, "profile", {})
    stored = profile.get(field) or ""
    current = stored or "(not set)"

    value = (text or "").strip()
    if not value:
        return (
            f"Set your LinkedIn {label.lower()} to exact text:\n\n"
            f'  brand_strategy(action="{spec["action"]}", focus="{spec["hint"]}")\n\n'
            f"  Current: {_preview(current)}\n\n"
            f"The text is written verbatim, up to {max_chars} characters.\n"
            f'For generated copy instead, use brand_strategy(action="makeover").'
        )

    if len(value) > max_chars:
        return (
            f"{label} is {len(value)} characters, over the LinkedIn limit "
            f"of {max_chars}.\n\n"
            f"Trim {len(value) - max_chars} characters and try again. "
            "Nothing was changed."
        )

    if value == stored:
        return f"{label} is already:\n\n  {_preview(value)}\n\nNothing to change."

    from .profile_editor import apply_profile_change

    provider_id = profile.get("provider_id", "")

    try:
        client = get_linkedin_client()
        result = await apply_profile_change(
            client, account_id, provider_id, field, value,
            source="brand_strategy",
        )
        await client.close()
    except Exception as e:
        return f"Failed to set {label.lower()}: {e}"

    if not result.get("success"):
        error = result.get("error", "Unknown error")
        return f"Failed to set {label.lower()}: {error}"

    await run_db(
        log_action,
        spec["log_action"],
        details={field: value, "previous": current},
    )
    return (
        f"{label} updated.\n\n"
        f"  Before: {_preview(current)}\n"
        f"  After:  {_preview(value)}\n\n"
        f"  {len(value)}/{max_chars} characters\n\n"
        f'Roll back with profile(action="history", field="{field}") '
        'then profile(action="restore", change_id="...").'
    )


async def _handle_set_headline(account_id: str, text: str) -> str:
    """Set the headline to exactly *text*."""
    return await _handle_set_profile_text(account_id, "headline", text)


async def _handle_set_summary(account_id: str, text: str) -> str:
    """Set the About section to exactly *text*."""
    return await _handle_set_profile_text(account_id, "summary", text)


async def _compute_current_health(ssi_data: dict[str, Any], campaign_stats: dict[str, Any]):
    """Compute current health score from available data."""
    daily_record = await run_db(get_rate_limit_today)
    weekly = await run_db(get_weekly_invitation_sum)
    sending_days = await run_db(get_sending_days_7d)
    from ..linkedin.rate_limiter import invite_limits_for_display

    weekly_limit, daily_limit = await invite_limits_for_display(
        daily_record if isinstance(daily_record, dict) else None,
    )

    return compute_health_score(
        ssi_score=ssi_data.get("score", 0),
        acceptance_rate=campaign_stats.get("acceptance_rate", 0),
        total_sent=campaign_stats.get("total_invited", 0),
        daily_sent=daily_record.get("sent", 0) if isinstance(daily_record, dict) else daily_record,
        daily_limit=daily_limit,
        weekly_sent=weekly,
        weekly_limit=weekly_limit,
        sending_days_7d=sending_days,
    )


# ──────────────────────────────────────────────
# Action: photo_enhance
# ──────────────────────────────────────────────


async def _handle_photo_enhance(account_id: str, *, action_id: str = "") -> str:
    """Auto-enhance profile photo with Unipile's STUDIO filter."""
    import json

    from .profile_editor import apply_profile_change

    profile = await run_db(get_setting, "profile", {})
    provider_id = profile.get("provider_id", "")

    settings = {"filter": "STUDIO"}

    try:
        client = get_linkedin_client()
        result = await apply_profile_change(
            client, account_id, provider_id, "picture_settings",
            json.dumps(settings), source="brand_strategy",
        )
        await client.close()

        if result.get("success"):
            if action_id:
                await _complete_brand_action(action_id, "Applied STUDIO filter")
            await run_db(log_action, "brand_photo_enhanced", details=settings)
            return (
                "Profile photo enhanced!\n\n"
                "  Applied: STUDIO filter (professional look)\n\n"
                "LinkedIn's STUDIO filter optimizes contrast and lighting\n"
                "for a professional headshot appearance."
            )
        else:
            error = result.get("error", "Unknown error")
            return f"Photo enhancement failed: {error}"
    except Exception as e:
        return f"Photo enhancement failed: {e}"


# ──────────────────────────────────────────────
# Action: makeover (one-click full profile optimization)
# ──────────────────────────────────────────────


async def _handle_makeover(account_id: str) -> str:
    """One-click full profile optimization using all available fields."""
    import json

    from ..ai.brand_strategist import generate_brand_action
    from ..services.brand_service import get_active_icp_context

    from .profile_editor import apply_profile_change

    profile = await run_db(get_setting, "profile", {})
    voice = await run_db(get_setting, "voice_signature", {})
    expertise = await run_db(get_setting, "expertise_map", {})
    provider_id = profile.get("provider_id", "")
    analysis = await run_db(load_brand_analysis)
    icp_context = await run_db(get_active_icp_context)

    if not analysis:
        return (
            "No brand analysis found.\n\n"
            'Run brand_strategy(action="analyze") first to audit your profile.'
        )

    applied: list[str] = []
    failed: list[str] = []
    change_ids: list[str] = []

    client = get_linkedin_client()

    try:
        # ── 1. Headline ──
        headline_result = await generate_brand_action(
            {"subtype": "headline"}, profile, voice, analysis, icp_context=icp_context,
            expertise=expertise,
        )
        if headline_result.get("options"):
            best = headline_result["options"][0]["text"]
            r = await apply_profile_change(
                client, account_id, provider_id, "headline", best,
                source="makeover",
            )
            if r.get("success"):
                applied.append(f'Headline: "{best}"')
                if r.get("change_id"):
                    change_ids.append(r["change_id"])
            else:
                failed.append(f"Headline: {r.get('error', 'unknown')}")

        # ── 2. Summary ──
        summary_result = await generate_brand_action(
            {"subtype": "summary"}, profile, voice, analysis, icp_context=icp_context,
            expertise=expertise,
        )
        if summary_result.get("summary"):
            r = await apply_profile_change(
                client, account_id, provider_id, "summary", summary_result["summary"],
                source="makeover",
            )
            if r.get("success"):
                applied.append(f"Summary: updated ({len(summary_result['summary'])} chars)")
                if r.get("change_id"):
                    change_ids.append(r["change_id"])
            else:
                failed.append(f"Summary: {r.get('error', 'unknown')}")

        # ── 3. Photo enhancement ──
        r = await apply_profile_change(
            client, account_id, provider_id, "picture_settings",
            json.dumps({"filter": "STUDIO"}), source="makeover",
        )
        if r.get("success"):
            applied.append("Photo: STUDIO filter applied")
            if r.get("change_id"):
                change_ids.append(r["change_id"])
        else:
            failed.append(f"Photo settings: {r.get('error', 'unknown')}")

        # ── 4. Skills from brand audit ──
        areas = analysis.get("areas", {})
        missing = areas.get("profile_completeness", {}).get("missing", [])

        # Suggest skills based on expertise
        expertise = await run_db(get_setting, "expertise_map", {})
        core_skills = expertise.get("core", [])
        if isinstance(core_skills, str):
            core_skills = [s.strip() for s in core_skills.split(",") if s.strip()]
        current_skills = profile.get("skills", [])
        new_skills = [s for s in core_skills if s not in current_skills][:5]
        if new_skills:
            r = await apply_profile_change(
                client, account_id, provider_id, "skills",
                json.dumps(new_skills), source="makeover",
            )
            if r.get("success"):
                applied.append(f"Skills: added {', '.join(new_skills)}")
                if r.get("change_id"):
                    change_ids.append(r["change_id"])
            else:
                failed.append(f"Skills: {r.get('error', 'unknown')}")

        await client.close()
    except Exception as e:
        logger.error("Makeover failed: %s", e)
        failed.append(f"Error: {e}")
        try:
            await client.close()
        except Exception:
            pass

    await run_db(log_action, "brand_makeover", details={
        "applied": len(applied), "failed": len(failed),
    })

    lines = ["Profile Makeover Complete!", ""]

    if applied:
        lines.append("Applied:")
        for item in applied:
            lines.append(f"  {item}")
        lines.append("")

    if failed:
        lines.append("Could not apply:")
        for item in failed:
            lines.append(f"  {item}")
        lines.append("")

    if change_ids:
        lines.append(f"Change IDs (for rollback): {', '.join(change_ids)}")
        lines.append('Use profile_history(action="restore", change_id="...") to undo any change.')
        lines.append("")

    lines.append(f"Total: {len(applied)} applied, {len(failed)} skipped")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# Direct profile actions (education, open_to_work)
# ──────────────────────────────────────────────


async def _execute_direct_profile_action(
    action: dict[str, Any], action_id: str, subtype: str,
) -> str:
    """Execute education/open_to_work profile actions from the brand plan."""
    import json

    from .profile_editor import apply_profile_change

    profile = await run_db(get_setting, "profile", {})
    provider_id = profile.get("provider_id", "")
    account_id = await run_db(get_account_id)
    desc = action.get("description", "")

    if not account_id or not provider_id:
        await _complete_brand_action(action_id, "Skipped — no account")
        return f"Cannot auto-apply {subtype}: no LinkedIn account connected."

    # Extract structured data from action if available, otherwise skip
    data = action.get("data")
    if not data:
        await _complete_brand_action(action_id, f"Suggestion: {desc}")
        plan = await run_db(load_brand_plan)
        completed, total = count_plan_progress(plan) if plan else (0, 0)
        return (
            f"Brand Strategy: {subtype.replace('_', ' ').title()} Suggestion\n\n"
            f"  {desc}\n\n"
            f"This action requires manual data input. Use the profile editor:\n"
            f'  Apply via: profile_editor(field="{subtype}", value=\'{{...}}\')\n\n'
            f"Progress: {completed}/{total} actions completed"
        )

    try:
        client = get_linkedin_client()
        result = await apply_profile_change(
            client, account_id, provider_id, subtype,
            json.dumps(data) if isinstance(data, dict) else data,
            source="brand_strategy",
        )
        await client.close()

        if result.get("success"):
            await _complete_brand_action(action_id, f"Auto-applied {subtype}")
            plan = await run_db(load_brand_plan)
            completed, total = count_plan_progress(plan) if plan else (0, 0)
            return (
                f"Brand Strategy: {subtype.replace('_', ' ').title()} Updated!\n\n"
                f"  {desc}\n\n"
                f"Progress: {completed}/{total} actions completed"
            )
        else:
            error = result.get("error", "unknown")
            return f"Failed to apply {subtype}: {error}"
    except Exception as e:
        return f"Failed to apply {subtype}: {e}"


# ──────────────────────────────────────────────
# Action: test_headline (headline A/B testing)
# ──────────────────────────────────────────────


async def _handle_test_headline(focus: str) -> str:
    """Start a headline A/B test.

    ``focus`` should be "Variant A headline | Variant B headline".
    Uses the first active campaign for tracking.
    """
    from ..services.experiment_service import HEADLINE_AB_ENABLED

    # Off-path: do not start a test that nothing will rotate or attribute.
    if not HEADLINE_AB_ENABLED:
        return (
            "Headline A/B testing is turned off.\n\n"
            "The test could not attribute an invitation to the headline it went "
            "out under, so it changed your real LinkedIn headline without ever "
            "being able to compare the two variants.\n\n"
            "Your headline is untouched. Set it directly with "
            'brand_strategy(action="set_headline", focus="...") for exact text, '
            'or brand_strategy(action="makeover") for a generated one.'
        )

    if not focus or "|" not in focus:
        return (
            "Start a headline A/B test:\n\n"
            '  brand_strategy(action="test_headline",\n'
            '    focus="AI-Powered Sales Leader | Building the Future of Outbound")\n\n'
            "Separate the two headline variants with a pipe (|).\n"
            "The test will alternate headlines across invite batches\n"
            "and measure acceptance/reply rate per variant.\n\n"
            "Auto-evaluates after 14 days or when 15+ prospects per variant."
        )

    parts = [p.strip() for p in focus.split("|", 1)]
    variant_a = parts[0]
    variant_b = parts[1]

    if len(variant_a) > 220 or len(variant_b) > 220:
        return "Headlines must be 220 characters or less."

    # Use the first active campaign
    campaigns = await run_db(list_campaigns, status="active")
    if not campaigns:
        campaigns = await run_db(list_campaigns)
    if not campaigns:
        return "No campaigns found. Create a campaign first to run a headline test."

    campaign_id = campaigns[0]["id"]
    campaign_name = campaigns[0].get("name", campaign_id[:8])

    from ..db.queries import create_ab_test, list_ab_tests

    # Check for existing running headline tests
    running = await run_db(list_ab_tests, campaign_id=campaign_id, status="running")
    headline_running = [t for t in running if t.get("test_type") == "headline"]
    if headline_running:
        existing = headline_running[0]
        return (
            f"A headline test is already running for campaign '{campaign_name}':\n\n"
            f'  A: "{existing["variant_a"]}"\n'
            f'  B: "{existing["variant_b"]}"\n\n'
            f"Wait for it to complete or cancel it first."
        )

    test_id = await run_db(
        create_ab_test,
        campaign_id=campaign_id,
        name=f"Headline: {variant_a[:30]}... vs {variant_b[:30]}...",
        variant_a=variant_a,
        variant_b=variant_b,
        hypothesis="Which headline drives higher acceptance and reply rates?",
        test_type="headline",
    )
    from ..services.experiment_service import sync_headline_ab_setting
    await run_db(sync_headline_ab_setting)

    return (
        f"Headline A/B Test Started!\n\n"
        f"  Campaign: {campaign_name}\n"
        f'  Variant A: "{variant_a}"\n'
        f'  Variant B: "{variant_b}"\n'
        f"  Test ID: {test_id[:8]}...\n\n"
        f"How it works:\n"
        f"  - Headlines alternate across invite batches\n"
        f"  - Each prospect tracks which headline was active\n"
        f"  - Acceptance rate and reply rate compared per variant\n"
        f"  - Auto-evaluates after 14 days or 15+ prospects per variant\n\n"
        f'Check progress: brand_strategy(action="progress")'
    )


async def _handle_cancel_headline_test() -> str:
    """Cancel a running headline A/B test and restore the pre-test headline."""
    from ..db.queries import cancel_ab_test, list_ab_tests
    from ..services.experiment_service import finish_headline_tests, sync_headline_ab_setting

    running = await run_db(list_ab_tests, status="running")
    headline_tests = [t for t in running if t.get("test_type") == "headline"]
    if not headline_tests:
        return "No running headline A/B test to cancel."

    for test in headline_tests:
        await run_db(cancel_ab_test, test["id"])
    await run_db(sync_headline_ab_setting)

    restored = await finish_headline_tests()
    if restored:
        return (
            f"Headline A/B test cancelled.\n\n"
            f'  Restored headline: "{restored}"'
        )
    return "Headline A/B test cancelled. No pre-test headline was stored to restore."
