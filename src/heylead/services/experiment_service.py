"""Experiment service — internal analysis engine for campaign optimization.

NOT a public MCP tool. Called by:
- campaign_report (include experiment insights summary)
- suggest_next_action (experiment-driven recommendations)
- scheduler (periodic experiment analysis + A/B evaluation)

Pulls all campaign performance data, runs LLM analysis,
generates 1-3 experiment hypotheses in 4 pillars:
Users, Customers, Shareholders, Data.
"""

from __future__ import annotations

import json
import logging
import time as _time
from datetime import datetime, timezone
from typing import Any

from ..db.async_bridge import run_db
from ..db.queries import (
    complete_ab_test,
    get_campaign_outcomes,
    get_campaign_stats,
    get_campaign_velocity,
    get_deal_profiles,
    get_engagement_stats,
    get_monthly_usage,
    get_rate_limit_today,
    get_setting,
    get_stale_outreaches,
    get_headline_variant_stats,
    get_variant_stats,
    get_weekly_invitation_sum,
    list_ab_tests,
    list_campaigns,
    list_experiments,
    save_experiment,
    save_setting,
)
from ..formatter import format_duration

logger = logging.getLogger(__name__)

# On since 2026-08-25: each successful invite writes outreaches.headline_variant
# and evaluate_headline_tests reads that column, not message-test `variant`.
HEADLINE_AB_ENABLED = True

# Minimum invitations across all campaigns before analysis is useful
_MIN_INVITED = 5

# Max campaigns to include in snapshot (token budget)
_MAX_CAMPAIGNS = 5

_PILLAR_ICONS = {
    "users": "\U0001f465",
    "customers": "\U0001f91d",
    "shareholders": "\U0001f4b0",
    "data": "\U0001f4ca",
}

_PRIORITY_LABELS = {
    "high": "[HIGH]",
    "medium": "[MED]",
    "low": "[LOW]",
}

_EXPERIMENT_SYSTEM = (
    "You are a product manager analyzing LinkedIn outreach campaign data.\n"
    "Your job: identify patterns, generate experiment hypotheses, recommend next steps.\n\n"
    "4 pillars:\n"
    "- Users (prospects): Who responds, targeting quality, segment gaps\n"
    "- Customers (converters): Patterns among won deals vs lost deals\n"
    "- Shareholders (business): Efficiency, pipeline velocity, cost\n"
    "- Data (metrics): Anomalies, trends, benchmark comparison\n\n"
    "Benchmarks: acceptance 20-30% good, >35% excellent, <15% needs work.\n"
    "Reply rate: 10-20% of connected is good. Accept time: 2-5d typical.\n"
    "Reply time: 1-3d after connection typical.\n\n"
    "Output structured JSON only. No markdown, no explanation outside JSON."
)

_EXPERIMENT_PROMPT = """Analyze this campaign performance data and generate 1-3 experiment hypotheses.

{snapshot}

Return ONLY valid JSON:
{{
    "health_check": "1-2 sentence overall assessment",
    "hypotheses": [
        {{
            "pillar": "users|customers|shareholders|data",
            "title": "Short hypothesis (< 10 words)",
            "evidence": "Cite specific numbers from the data above",
            "suggested_test": "Concrete action to test this",
            "priority": "high|medium|low"
        }}
    ],
    "do_this_next": "Single most impactful action RIGHT NOW"
}}"""

# Min prospects per variant before we evaluate statistical significance
_MIN_PER_VARIANT = 15


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────


async def run_experiment_analysis(campaign_id: str = "") -> dict[str, Any] | None:
    """Run experiment analysis and return structured result dict.

    Returns the raw analysis dict: {health_check, hypotheses, do_this_next}
    or None if analysis failed or not enough data.
    Also persists to experiments table.
    """
    setup_done = await run_db(get_setting, "setup_complete", False)
    if not setup_done:
        return None

    snapshot, campaigns_data = await run_db(_build_snapshot, campaign_id)
    if not campaigns_data:
        return None

    analysis = await _run_analysis(snapshot)
    if not analysis:
        return None

    camp_ids = ",".join(c["id"] for c in campaigns_data)
    await run_db(save_experiment, snapshot, json.dumps(analysis), camp_ids)

    return analysis


def get_experiment_summary(campaign_id: str = "") -> str:
    """Get a short summary from the most recent experiment for embedding.

    Returns 2-3 lines suitable for embedding in campaign_report or
    suggest_next_action output. Returns "" if no experiments exist.
    """
    experiments = list_experiments(limit=1)
    if not experiments:
        return ""

    exp = experiments[0]
    try:
        result = json.loads(exp.get("result_json", "{}"))
    except (json.JSONDecodeError, TypeError):
        return ""

    health = result.get("health_check", "")
    do_next = result.get("do_this_next", "")
    hypotheses = result.get("hypotheses", [])

    parts: list[str] = []
    if health:
        parts.append(health)
    if hypotheses:
        top = hypotheses[0]
        pillar = top.get("pillar", "data").lower()
        icon = _PILLAR_ICONS.get(pillar, "\U0001f4ca")
        parts.append(f"{icon} Top hypothesis: {top.get('title', '')}")
    if do_next:
        parts.append(f"Next: {do_next}")

    return " | ".join(parts)


def format_experiment_report(analysis: dict[str, Any]) -> str:
    """Format the LLM analysis into a displayable report."""
    return _format_report(analysis)


def get_voice_format_for_outreach(
    campaign_id: str, outreach_id: str, planned_format: str | None = None,
) -> str:
    """Determine the message format (text/voice) for an outreach.

    The campaign's voice_mode is the user's explicit choice and bounds the
    answer: text_only and voice_only are absolute. Inside "mixed" the daily
    planner's request (``planned_format``) wins when given; otherwise we
    alternate against the format of the last DM that was *actually sent*,
    so a voice attempt that fell back to text is followed by voice, not by
    a second text. "ab_test" uses the existing variant assignment.
    Returns "text" or "voice".
    """
    import json as _json

    from ..db.queries import get_campaign

    campaign = get_campaign(campaign_id)
    if not campaign:
        return "text"

    config = {}
    try:
        config = _json.loads(campaign.get("config_json", "{}") or "{}")
    except (_json.JSONDecodeError, TypeError):
        pass

    voice_mode = config.get("voice_mode", "text_only")

    if voice_mode == "text_only":
        return "text"
    if voice_mode == "voice_only":
        return "voice"
    if voice_mode == "mixed":
        if planned_format in ("text", "voice"):
            return planned_format
        last = _last_sent_dm_format(outreach_id)
        if last is None:
            return "text"  # first DM in the thread is text
        return "text" if last == "voice" else "voice"
    if voice_mode == "ab_test":
        # Use existing A/B variant assignment: A=text, B=voice
        from ..db.queries import get_outreach

        outreach = get_outreach(outreach_id)
        variant = (outreach.get("variant") or "A") if outreach else "A"
        return "voice" if variant == "B" else "text"
    return "text"


def _last_sent_dm_format(outreach_id: str) -> str | None:
    """Format of the most recent SDR message on this outreach, or None."""
    from ..db.schema import get_db

    db = get_db()
    try:
        row = db.execute(
            """SELECT format FROM messages
               WHERE outreach_id = ? AND role = 'sdr'
               ORDER BY timestamp DESC, rowid DESC LIMIT 1""",
            (outreach_id,),
        ).fetchone()
    finally:
        db.close()
    if not row:
        return None
    return "voice" if (row["format"] or "text") == "voice" else "text"


def evaluate_ab_tests() -> list[str]:
    """Auto-evaluate running A/B tests that have enough data.

    Returns list of result messages for tests that were completed.
    """
    results: list[str] = []
    running = list_ab_tests(status="running")

    for t in running:
        # Headline tests are judged on acceptance and force-complete on age;
        # evaluate_headline_tests owns them and is called below.
        if t.get("test_type") == "headline":
            continue
        camp_id = t["campaign_id"]
        v_stats = get_variant_stats(camp_id)
        va = v_stats.get("A", {})
        vb = v_stats.get("B", {})
        a_n = va.get("invited", 0)
        b_n = vb.get("invited", 0)

        if a_n < _MIN_PER_VARIANT or b_n < _MIN_PER_VARIANT:
            continue  # Not enough data

        # Compare on primary metric: reply_rate (most meaningful for SDR)
        a_reply = va.get("reply_rate", 0)
        b_reply = vb.get("reply_rate", 0)
        a_accept = va.get("acceptance_rate", 0)
        b_accept = vb.get("acceptance_rate", 0)

        # Winner is the variant with higher reply rate.
        # If reply rates are within 2pp, use acceptance rate as tiebreaker.
        # If both are within 2pp, declare inconclusive.
        if abs(a_reply - b_reply) > 2:
            winner = "A" if a_reply > b_reply else "B"
            reason = (
                f"Variant {winner} has higher reply rate: "
                f"A={a_reply}% vs B={b_reply}%"
            )
        elif abs(a_accept - b_accept) > 3:
            winner = "A" if a_accept > b_accept else "B"
            reason = (
                f"Reply rates similar (A={a_reply}%, B={b_reply}%), "
                f"but Variant {winner} has better acceptance: "
                f"A={a_accept}% vs B={b_accept}%"
            )
        else:
            winner = "inconclusive"
            reason = (
                f"No significant difference: "
                f"A={a_reply}% reply/{a_accept}% accept vs "
                f"B={b_reply}% reply/{b_accept}% accept"
            )

        result_data = {
            "reason": reason,
            "variant_a_stats": va,
            "variant_b_stats": vb,
        }
        complete_ab_test(t["id"], winner, json.dumps(result_data))
        results.append(
            f"A/B Test '{t['name']}' completed: "
            f"{'Winner is Variant ' + winner if winner != 'inconclusive' else 'Inconclusive'} — {reason}"
        )

    results.extend(evaluate_headline_tests())

    return results


async def swap_headline_for_batch(campaign_id: str) -> str | None:
    """Swap the LinkedIn headline if a headline A/B test is running.

    Returns the variant letter ('A' or 'B') that was applied, or None
    if no headline test is active.

    Headline tests are **time-windowed**: the entire invite batch uses
    one headline. The scheduler alternates between A and B across batches.
    """
    running = await run_db(list_ab_tests, campaign_id=campaign_id, status="running")
    headline_tests = [t for t in running if t.get("test_type") == "headline"]
    if not headline_tests:
        return None

    test = headline_tests[0]
    # The variant cannot come from outreaches.variant — that column is the
    # message-test round robin and is NULL for imported prospects, which pinned
    # every headline test to A. One headline per day, alternating, so both
    # variants are actually live for a comparable stretch.
    created_at = int(test.get("created_at") or 0)
    day_index = (int(_time.time()) - created_at) // 86400 if created_at else 0
    variant = "A" if day_index % 2 == 0 else "B"
    headline = test["variant_a"] if variant == "A" else test["variant_b"]

    # This runs per invite job. Without the guard every invite issues a live
    # LinkedIn profile PATCH and a profile_history row for a headline that is
    # already up.
    applied = await run_db(get_setting, "headline_ab_applied", {})
    if (
        isinstance(applied, dict)
        and applied.get("test_id") == test["id"]
        and applied.get("headline") == headline
    ):
        return variant

    # Apply the headline
    from ..linkedin import get_account_id, get_linkedin_client
    from ..tools.profile_editor import apply_profile_change

    account_id = await run_db(get_account_id)
    if not account_id:
        return None

    profile = await run_db(get_setting, "profile", {})
    provider_id = profile.get("provider_id", "")

    try:
        client = get_linkedin_client()
        result = await apply_profile_change(
            client, account_id, provider_id, "headline", headline,
            source="ab_test",
        )
        await client.close()
        if result.get("success"):
            prev = applied if isinstance(applied, dict) else {}
            original = prev.get("original_headline") or profile.get("headline", "")
            await run_db(
                save_setting, "headline_ab_applied",
                {
                    "test_id": test["id"],
                    "variant": variant,
                    "headline": headline,
                    "original_headline": original,
                },
            )
            await run_db(sync_headline_ab_setting)
            logger.info("Headline swapped to variant %s: %s", variant, headline[:50])
            return variant
    except Exception as e:
        logger.warning("Headline swap failed: %s", e)
    return None


def sync_headline_ab_setting() -> dict[str, Any]:
    """Compact blob the cloud scheduler reads. Message A/B stays local."""
    tests = [t for t in list_ab_tests() if t.get("test_type") == "headline"]
    applied = get_setting("headline_ab_applied", {})
    blob = {
        "tests": [
            {
                "id": t.get("id", ""),
                "campaign_id": t.get("campaign_id", ""),
                "variant_a": t.get("variant_a", ""),
                "variant_b": t.get("variant_b", ""),
                "created_at": t.get("created_at") or 0,
                "status": t.get("status") or "",
                "winner": t.get("winner") or "",
            }
            for t in tests
        ],
        "applied": applied if isinstance(applied, dict) else {},
    }
    save_setting("headline_ab", blob)
    return blob


async def _apply_headline(text: str, *, source: str) -> bool:
    """PATCH the live headline. Does not touch headline_ab_applied."""
    from ..linkedin import get_account_id, get_linkedin_client
    from ..tools.profile_editor import apply_profile_change

    account_id = await run_db(get_account_id)
    profile = await run_db(get_setting, "profile", {})
    provider_id = profile.get("provider_id", "")
    if not account_id or not provider_id or not (text or "").strip():
        return False

    try:
        client = get_linkedin_client()
        result = await apply_profile_change(
            client, account_id, provider_id, "headline", text,
            source=source,
        )
        await client.close()
        return bool(result.get("success"))
    except Exception as e:
        logger.warning("Headline apply (%s) failed: %s", source, e)
        return False


async def _finish_headline_test(outcome: str, test: dict[str, Any] | None = None) -> str:
    """Apply the winner or restore the original. Clears applied only on success.

    ``outcome`` is ``A`` / ``B`` (keep that variant), or anything else
    (cancelled / inconclusive) to restore ``original_headline``.
    """
    applied = await run_db(get_setting, "headline_ab_applied", {})
    if not isinstance(applied, dict):
        applied = {}

    if outcome in ("A", "B") and test:
        target = (test.get("variant_a") if outcome == "A" else test.get("variant_b")) or ""
        source = "ab_test_winner"
    else:
        target = (applied.get("original_headline") or "").strip()
        source = "ab_test_restore"

    if not target:
        if not applied:
            return ""
        # Nothing to PATCH, but the test is over — drop the blob so we do not
        # retry forever with an empty original.
        await run_db(save_setting, "headline_ab_applied", {})
        await run_db(sync_headline_ab_setting)
        return ""

    if not await _apply_headline(target, source=source):
        return ""

    await run_db(save_setting, "headline_ab_applied", {})
    await run_db(sync_headline_ab_setting)
    logger.info("Headline A/B finish (%s): %s", source, target[:50])
    return target


async def finish_headline_tests() -> str:
    """Apply winner or restore once no headline test is still running."""
    running = await run_db(list_ab_tests, status="running")
    if any(t.get("test_type") == "headline" for t in running):
        return ""

    tests = await run_db(list_ab_tests)
    done = [
        t for t in tests
        if t.get("test_type") == "headline" and t.get("status") in ("completed", "cancelled")
    ]
    latest = done[0] if done else None
    winner = (latest or {}).get("winner") or ""
    if latest and latest.get("status") == "completed" and winner in ("A", "B"):
        return await _finish_headline_test(winner, latest)
    return await _finish_headline_test("restore", latest)


async def restore_headline_after_ab_test() -> str:
    """Put the pre-test headline back, or apply the winner if one was chosen."""
    return await finish_headline_tests()


async def persist_invite_headline_variant(
    campaign_id: str,
    outreach_id: str,
    variant: str | None = None,
) -> str | None:
    """Record which headline a successful invite went out under.

    Does not PATCH. Call ``swap_headline_for_batch`` before the send.
    """
    if not HEADLINE_AB_ENABLED or not outreach_id:
        return None
    if not variant:
        applied = await run_db(get_setting, "headline_ab_applied", {})
        if isinstance(applied, dict):
            variant = applied.get("variant")
    if not variant:
        return None
    from ..db.queries import update_outreach

    running = await run_db(list_ab_tests, campaign_id, "running")
    test_id = next(
        (t["id"] for t in running if t.get("test_type") == "headline"),
        None,
    )
    await run_db(
        update_outreach,
        outreach_id,
        headline_variant=variant,
        headline_test_id=test_id,
    )
    return variant


def evaluate_headline_tests() -> list[str]:
    """Auto-evaluate running headline A/B tests.

    Uses the same metrics as message A/B tests (acceptance_rate, reply_rate)
    but also auto-completes after HEADLINE_TEST_DURATION_DAYS.
    """
    from ..constants import HEADLINE_MIN_SAMPLE, HEADLINE_TEST_DURATION_DAYS

    results: list[str] = []
    running = list_ab_tests(status="running")
    headline_tests = [t for t in running if t.get("test_type") == "headline"]
    now = int(_time.time())

    for t in headline_tests:
        camp_id = t["campaign_id"]
        v_stats = get_headline_variant_stats(camp_id)
        va = v_stats.get("A", {})
        vb = v_stats.get("B", {})
        a_n = va.get("invited", 0)
        b_n = vb.get("invited", 0)

        # Auto-complete after duration even if not enough data
        age_days = (now - t["created_at"]) / 86400
        force_complete = age_days >= HEADLINE_TEST_DURATION_DAYS

        if not force_complete and (a_n < HEADLINE_MIN_SAMPLE or b_n < HEADLINE_MIN_SAMPLE):
            continue

        a_accept = va.get("acceptance_rate", 0)
        b_accept = vb.get("acceptance_rate", 0)
        a_reply = va.get("reply_rate", 0)
        b_reply = vb.get("reply_rate", 0)

        if abs(a_accept - b_accept) > 3:
            winner = "A" if a_accept > b_accept else "B"
            reason = (
                f"Variant {winner} has higher acceptance rate: "
                f"A={a_accept}% vs B={b_accept}%"
            )
        elif abs(a_reply - b_reply) > 2:
            winner = "A" if a_reply > b_reply else "B"
            reason = (
                f"Acceptance rates similar (A={a_accept}%, B={b_accept}%), "
                f"but Variant {winner} has higher reply rate: "
                f"A={a_reply}% vs B={b_reply}%"
            )
        else:
            winner = "inconclusive"
            reason = (
                f"No significant difference after {age_days:.0f} days: "
                f"A={a_accept}% accept/{a_reply}% reply vs "
                f"B={b_accept}% accept/{b_reply}% reply"
            )

        result_data = {
            "reason": reason,
            "variant_a_headline": t["variant_a"],
            "variant_b_headline": t["variant_b"],
            "variant_a_stats": va,
            "variant_b_stats": vb,
            "duration_days": round(age_days, 1),
        }
        complete_ab_test(t["id"], winner, json.dumps(result_data))
        sync_headline_ab_setting()

        winner_headline = t["variant_a"] if winner == "A" else t["variant_b"] if winner == "B" else "neither"
        if winner != "inconclusive":
            verdict = f'Winner: {winner} — "{winner_headline[:60]}"'
        else:
            verdict = "Inconclusive"
        results.append(
            f"Headline A/B Test '{t['name']}' completed: {verdict} — {reason}"
        )

    return results


def evaluate_signal_vs_cold() -> list[str]:
    """Auto-evaluate signal-triggered outreach vs cold outreach performance.

    Compares outreaches with signal_id (signal-triggered) against those
    without (cold). Runs per campaign and overall. Returns list of result
    messages for campaigns that have enough data.

    Requires at least _MIN_PER_VARIANT signal-triggered AND cold outreaches
    (invited or beyond) to produce a meaningful comparison.
    """
    from ..db.signal_queries import get_signal_performance

    results: list[str] = []

    # Overall comparison
    try:
        perf = get_signal_performance()
        sig = perf["signal"]
        cold = perf["cold"]

        sig_invited = sig.get("invited", 0)
        cold_invited = cold.get("invited", 0)

        if sig_invited >= _MIN_PER_VARIANT and cold_invited >= _MIN_PER_VARIANT:
            sig_acc = sig.get("acceptance_rate", 0)
            cold_acc = cold.get("acceptance_rate", 0)
            sig_reply = sig.get("reply_rate", 0)
            cold_reply = cold.get("reply_rate", 0)

            # Calculate lift
            lift = perf.get("signal_lift", {})
            acc_lift = lift.get("acceptance")
            reply_lift = lift.get("reply")

            # Determine winner
            winner_msg = _determine_signal_winner(
                sig_acc, cold_acc, sig_reply, cold_reply, acc_lift, reply_lift,
            )

            results.append(
                f"Signal vs Cold (overall): {winner_msg} "
                f"| Signal: {sig_invited} invited, {sig_acc:.0%} accept, {sig_reply:.0%} reply, {sig.get('won', 0)} won "
                f"| Cold: {cold_invited} invited, {cold_acc:.0%} accept, {cold_reply:.0%} reply, {cold.get('won', 0)} won"
            )

            # Per-signal-type breakdown for insights
            by_type = perf.get("by_signal_type", {})
            if by_type:
                best_type = None
                best_rate = 0.0
                for sig_type, stats in by_type.items():
                    total = stats.get("total", 0)
                    replied = stats.get("replied", 0)
                    if total >= 5:
                        rate = replied / total
                        if rate > best_rate:
                            best_rate = rate
                            best_type = sig_type
                if best_type:
                    label = best_type.replace("_", " ").title()
                    results.append(
                        f"  Best signal type: {label} ({best_rate:.0%} reply rate)"
                    )
    except Exception as e:
        logger.debug("Signal vs cold evaluation failed: %s", e)

    return results


def _determine_signal_winner(
    sig_acc: float,
    cold_acc: float,
    sig_reply: float,
    cold_reply: float,
    acc_lift: float | None,
    reply_lift: float | None,
) -> str:
    """Determine whether signal-triggered or cold outreach is winning."""
    # Check reply rate first (most meaningful)
    reply_diff = sig_reply - cold_reply
    acc_diff = sig_acc - cold_acc

    if reply_diff > 0.05:  # Signal reply rate 5pp+ higher
        lift_str = f"(+{reply_lift:.0%} lift)" if reply_lift else ""
        return f"Signal outreach winning on reply rate {lift_str}"
    elif reply_diff < -0.05:  # Cold reply rate 5pp+ higher
        return "Cold outreach winning on reply rate — signal targeting may need tuning"
    elif acc_diff > 0.05:  # Similar reply but better acceptance
        lift_str = f"(+{acc_lift:.0%} lift)" if acc_lift else ""
        return f"Signal outreach winning on acceptance rate {lift_str}"
    elif acc_diff < -0.05:
        return "Cold outreach has better acceptance — review signal quality"
    else:
        return "No significant difference yet — collecting more data"


def get_signal_conversion_rates() -> dict[str, Any]:
    """Analyze which signal types correlate with conversions.

    For each signal_type:
    - How many signals → outreach created?
    - How many outreaches → accepted?
    - How many accepted → replied?
    - How many replied → won?

    Returns:
        {signal_type: {volume, outreach_rate, acceptance_rate, reply_rate, won_rate}, ...}
    """
    from ..db.signal_queries import get_signal_performance

    try:
        perf = get_signal_performance()
        by_type = perf.get("by_signal_type", {})

        rates: dict[str, dict[str, Any]] = {}
        for sig_type, stats in by_type.items():
            total = stats.get("total", 0)
            connected = stats.get("connected", 0)
            replied = stats.get("replied", 0)
            won = stats.get("won", 0)

            rates[sig_type] = {
                "volume": total,
                "acceptance_rate": connected / total if total > 0 else 0,
                "reply_rate": replied / connected if connected > 0 else 0,
                "won_rate": won / total if total > 0 else 0,
                "won": won,
            }

        return rates
    except Exception as e:
        logger.debug("Signal conversion rates failed: %s", e)
        return {}


# ──────────────────────────────────────────────
# Data Gathering
# ──────────────────────────────────────────────


def _build_snapshot(campaign_id: str = "") -> tuple[str, list[dict]]:
    """Build a compact text snapshot of all campaign data for LLM analysis.

    Returns (snapshot_text, campaigns_data_list).
    If not enough data, returns (error_message, []).
    """
    if campaign_id:
        from ..db.queries import get_campaign
        camp = get_campaign(campaign_id)
        if not camp:
            return (f"Campaign not found: {campaign_id}", [])
        campaigns = [camp]
    else:
        campaigns = list_campaigns("active") + list_campaigns("paused")

    if not campaigns:
        return ("No active or paused campaigns found.", [])

    # Sort by invitation count (most active first), cap at _MAX_CAMPAIGNS
    campaigns_with_stats = []
    total_invited = 0

    for camp in campaigns:
        camp_id = camp["id"]
        stats = get_campaign_stats(camp_id)
        total_invited += stats.get("invited", 0)
        campaigns_with_stats.append({
            "id": camp_id,
            "campaign": camp,
            "stats": stats,
        })

    # Check minimum threshold
    if total_invited < _MIN_INVITED:
        return (
            f"Not enough data yet — only {total_invited} invitations sent "
            f"(need at least {_MIN_INVITED}).",
            [],
        )

    # Sort by invited count desc, cap
    campaigns_with_stats.sort(
        key=lambda x: x["stats"].get("invited", 0), reverse=True,
    )
    top = campaigns_with_stats[:_MAX_CAMPAIGNS]
    overflow = len(campaigns_with_stats) - _MAX_CAMPAIGNS

    # Build snapshot text
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"## CAMPAIGN DATA (as of {today})\n"]

    for cws in top:
        camp = cws["campaign"]
        stats = cws["stats"]
        camp_id = camp["id"]

        config = {}
        try:
            config = json.loads(camp.get("config_json", "{}") or "{}")
        except (json.JSONDecodeError, TypeError):
            pass

        status = camp.get("status", "active")
        mode = camp.get("mode", "copilot")
        created = camp.get("created_at", 0)
        age_days = max(1, (_time.time() - created) // 86400) if created else 0
        target = config.get("target_description", "N/A")

        lines.append(
            f'### Campaign: "{camp.get("name", "Untitled")}" '
            f"({status}, Day {int(age_days)}, {mode})"
        )
        lines.append(f"Target: {target}")

        # Funnel
        total = stats.get("total_prospects", 0)
        invited = stats.get("invited", 0)
        connected = stats.get("connected", 0)
        replied = stats.get("replied", 0)
        hot = stats.get("hot_leads", 0)
        acc_rate = stats.get("acceptance_rate", 0)
        reply_rate = stats.get("reply_rate", 0)
        lines.append(
            f"Funnel: {total} prospects \u2192 {invited} invited \u2192 "
            f"{connected} connected ({acc_rate:.0%}) \u2192 "
            f"{replied} replied ({reply_rate:.0%}) \u2192 {hot} hot"
        )

        # Velocity
        velocity = get_campaign_velocity(camp_id)
        avg_tta = velocity.get("avg_time_to_accept")
        avg_ttr = velocity.get("avg_time_to_reply")
        if avg_tta is not None or avg_ttr is not None:
            parts = []
            if avg_tta is not None:
                parts.append(f"accept {format_duration(avg_tta)} avg")
            if avg_ttr is not None:
                parts.append(f"reply {format_duration(avg_ttr)} avg")
            lines.append(f"Velocity: {' | '.join(parts)}")

        # Outcomes
        outcomes = get_campaign_outcomes(camp_id)
        won = outcomes.get("closed_happy", 0)
        lost = outcomes.get("closed_unhappy", 0)
        opted = outcomes.get("opted_out", 0)
        total_closed = won + lost
        conv_pct = f"{won / total_closed * 100:.0f}%" if total_closed else "N/A"
        lines.append(
            f"Outcomes: {won} won, {lost} lost, {opted} opted_out ({conv_pct} conv)"
        )

        # Engagement
        eng = get_engagement_stats(camp_id)
        comments = eng.get("comments", 0)
        reactions = eng.get("reactions", 0)
        lines.append(f"Engagement: {comments} comments, {reactions} reactions")

        # A/B test variant stats
        v_stats = get_variant_stats(camp_id)
        if "A" in v_stats and "B" in v_stats:
            va = v_stats["A"]
            vb = v_stats["B"]
            lines.append(
                f"A/B Split: Variant A ({va['total']} prospects, "
                f"{va['acceptance_rate']}% accept, {va['reply_rate']}% reply) "
                f"vs B ({vb['total']} prospects, "
                f"{vb['acceptance_rate']}% accept, {vb['reply_rate']}% reply)"
            )
            # Show running tests
            running_tests = list_ab_tests(camp_id, status="running")
            for t in running_tests[:2]:
                lines.append(f"  Running test: {t['name']}")

        # Stale leads
        stale = get_stale_outreaches(camp_id, stale_days=14)
        if stale:
            stale_names = ", ".join(
                f"{s['name']} {s['days_stale']}d" for s in stale[:3]
            )
            lines.append(f"Stale (14d+): {len(stale)} leads ({stale_names})")

        # Won/Lost profiles
        won_profiles = get_deal_profiles(camp_id, "closed_happy", 3)
        for p in won_profiles:
            lines.append(
                f"Won: {p['name']} ({p['title']}, {p['company']}, fit={p['fit_score']:.1f})"
            )
        lost_profiles = get_deal_profiles(camp_id, "closed_unhappy", 3)
        for p in lost_profiles:
            lines.append(
                f"Lost: {p['name']} ({p['title']}, {p['company']}, fit={p['fit_score']:.1f})"
            )

        lines.append("")  # blank line between campaigns

    if overflow > 0:
        lines.append(f"(+ {overflow} more campaign(s) not shown)\n")

    # Account health
    rate = get_rate_limit_today()
    weekly = get_weekly_invitation_sum()
    usage = get_monthly_usage()
    # The ceilings the sender enforces, not this row's column default: the
    # model reasons about headroom from these two numbers.
    from ..linkedin.rate_limiter import invite_limits_for_display_sync

    weekly_cap, daily_cap = invite_limits_for_display_sync(rate)
    lines.append("### Account Health")
    lines.append(
        f"Today: {rate.get('sent', 0)}/{daily_cap} invitations "
        f"| Weekly: {weekly}/{weekly_cap} "
        f"| Monthly: {usage.get('invitations_sent', 0)} sent, "
        f"{usage.get('messages_sent', 0)} msgs"
    )

    # Signal intelligence section
    try:
        from ..db.signal_queries import get_signal_performance

        perf = get_signal_performance()
        sig = perf["signal"]
        cold = perf["cold"]

        if sig["total"] > 0:
            lines.append("")
            lines.append("### Signal vs Cold Outreach")
            lines.append(
                f"Signal-triggered: {sig['total']} total, "
                f"{sig['invited']} invited, "
                f"{sig['acceptance_rate']:.0%} accept, "
                f"{sig['reply_rate']:.0%} reply, "
                f"{sig['won']} won"
            )
            lines.append(
                f"Cold outreach: {cold['total']} total, "
                f"{cold['invited']} invited, "
                f"{cold['acceptance_rate']:.0%} accept, "
                f"{cold['reply_rate']:.0%} reply, "
                f"{cold['won']} won"
            )

            # Signal lift
            lift = perf.get("signal_lift", {})
            acc_lift = lift.get("acceptance")
            reply_lift = lift.get("reply")
            lift_parts = []
            if acc_lift is not None:
                lift_parts.append(f"acceptance {'+'if acc_lift>0 else ''}{acc_lift:.0%}")
            if reply_lift is not None:
                lift_parts.append(f"reply {'+'if reply_lift>0 else ''}{reply_lift:.0%}")
            if lift_parts:
                lines.append(f"Signal lift: {', '.join(lift_parts)}")

            # Per-signal-type performance
            by_type = perf.get("by_signal_type", {})
            if by_type:
                type_parts = []
                for stype, stats in sorted(by_type.items(), key=lambda x: -x[1]["total"]):
                    label = stype.replace("_", " ")
                    type_parts.append(
                        f"{label}: {stats['total']}→{stats['connected']} connected, "
                        f"{stats['replied']} replied, {stats['won']} won"
                    )
                lines.append("By signal type: " + " | ".join(type_parts[:5]))
    except Exception:
        pass  # Signal data not available yet

    snapshot = "\n".join(lines)
    campaigns_data = [cws["campaign"] for cws in top]
    return snapshot, campaigns_data


# ──────────────────────────────────────────────
# LLM Analysis
# ──────────────────────────────────────────────


async def _run_analysis(snapshot: str) -> dict[str, Any] | None:
    """Send snapshot to LLM and get experiment hypotheses."""
    prompt = _EXPERIMENT_PROMPT.format(snapshot=snapshot)

    from ..config import has_local_llm_key, is_backend_mode

    try:
        if is_backend_mode() and not has_local_llm_key():
            from ..linkedin import get_linkedin_client
            client = get_linkedin_client()
            try:
                result = await client.analyze_experiments(snapshot)
            finally:
                await client.close()
            return result
        else:
            from ..ai.llm import LLMClient
            from ..ai.llm import loads_json_object as parse_json

            llm_client = LLMClient()
            raw = await llm_client.generate(
                prompt,
                system=_EXPERIMENT_SYSTEM,
                temperature=0.4,
                max_tokens=2500,
            )
            return parse_json(raw, fallback={
                "health_check": "Analysis could not be parsed.",
                "hypotheses": [],
                "do_this_next": "Run campaign_report() for detailed metrics.",
            })
    except Exception as e:
        logger.error(f"Experiment analysis failed: {e}", exc_info=True)
        return None


# ──────────────────────────────────────────────
# Output Formatting
# ──────────────────────────────────────────────


def _format_report(analysis: dict[str, Any]) -> str:
    """Format the LLM analysis into a displayable report."""
    today = datetime.now(timezone.utc).strftime("%b %d")
    out = [f"\U0001f52c **Experiment Analysis** ({today})\n"]

    # Health check
    health = analysis.get("health_check", "")
    if health:
        out.append(f"**Health:** {health}\n")

    # Hypotheses
    hypotheses = analysis.get("hypotheses", [])
    if hypotheses:
        out.append("**Hypotheses:**\n")
        for i, hyp in enumerate(hypotheses, 1):
            pillar = hyp.get("pillar", "data").lower()
            icon = _PILLAR_ICONS.get(pillar, "\U0001f4ca")
            priority = hyp.get("priority", "medium").lower()
            plabel = _PRIORITY_LABELS.get(priority, "[MED]")
            title = hyp.get("title", "")
            evidence = hyp.get("evidence", "")
            test = hyp.get("suggested_test", "")

            out.append(f"{i}. {plabel} {icon} **{pillar.title()}:** {title}")
            if evidence:
                out.append(f"   Evidence: {evidence}")
            if test:
                out.append(f"   Test: {test}")
            out.append("")
    else:
        out.append("No hypotheses generated — try again with more data.\n")

    # Do this next
    do_next = analysis.get("do_this_next", "")
    if do_next:
        out.append(f"\u2192 **Do this next:** {do_next}\n")

    # A/B test section
    ab_section = _format_ab_tests()
    if ab_section:
        out.append(ab_section)

    # Signal vs Cold section
    signal_section = _format_signal_vs_cold()
    if signal_section:
        out.append(signal_section)

    out.append("\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
    out.append("Experiment insights appear in campaign_report.")

    return "\n".join(out)


def _format_ab_tests() -> str:
    """Format running and recent completed A/B tests."""
    running = list_ab_tests(status="running")
    completed = list_ab_tests(status="completed")
    if not running and not completed:
        return ""

    lines = ["\n**A/B Tests:**\n"]

    for t in running:
        camp_id = t["campaign_id"]
        v_stats = get_variant_stats(camp_id)
        va = v_stats.get("A", {})
        vb = v_stats.get("B", {})
        a_n = va.get("invited", 0)
        b_n = vb.get("invited", 0)
        lines.append(f"\U0001f9ea {t['name']} (running)")
        lines.append(f"   A: {t['variant_a']} \u2014 {a_n} invited, {va.get('acceptance_rate', 0)}% accept, {va.get('reply_rate', 0)}% reply")
        lines.append(f"   B: {t['variant_b']} \u2014 {b_n} invited, {vb.get('acceptance_rate', 0)}% accept, {vb.get('reply_rate', 0)}% reply")
        if a_n >= _MIN_PER_VARIANT and b_n >= _MIN_PER_VARIANT:
            lines.append("   \u2705 Enough data to evaluate \u2014 A/B tests are auto-evaluated by the scheduler.")
        else:
            needed = max(0, _MIN_PER_VARIANT - min(a_n, b_n))
            lines.append(f"   \u23f3 Need ~{needed} more prospects per variant for significance")
        lines.append("")

    for t in completed[:3]:
        winner = t.get("winner", "?")
        lines.append(f"\u2705 {t['name']} \u2014 Winner: Variant {winner}")
        if t.get("result_json"):
            try:
                res = json.loads(t["result_json"])
                if res.get("reason"):
                    lines.append(f"   {res['reason']}")
            except (json.JSONDecodeError, TypeError):
                pass
        lines.append("")

    return "\n".join(lines)


def _format_signal_vs_cold() -> str:
    """Format signal vs cold comparison for experiment reports."""
    try:
        from ..db.signal_queries import get_signal_performance

        perf = get_signal_performance()
        sig = perf["signal"]
        cold = perf["cold"]

        if sig["total"] == 0:
            return ""

        lines = ["\n**Signal vs Cold Outreach:**\n"]

        sig_invited = sig.get("invited", 0)
        cold_invited = cold.get("invited", 0)
        sig_acc = sig.get("acceptance_rate", 0)
        cold_acc = cold.get("acceptance_rate", 0)
        sig_reply = sig.get("reply_rate", 0)
        cold_reply = cold.get("reply_rate", 0)

        lines.append(
            f"\U0001f4e1 Signal-triggered: {sig['total']} outreaches, "
            f"{sig_invited} invited, "
            f"{sig_acc:.0%} accept, "
            f"{sig_reply:.0%} reply, "
            f"{sig.get('won', 0)} won"
        )
        lines.append(
            f"\u2744\ufe0f Cold outreach: {cold['total']} outreaches, "
            f"{cold_invited} invited, "
            f"{cold_acc:.0%} accept, "
            f"{cold_reply:.0%} reply, "
            f"{cold.get('won', 0)} won"
        )

        # Lift calculation
        lift = perf.get("signal_lift", {})
        acc_lift = lift.get("acceptance")
        reply_lift = lift.get("reply")
        lift_parts = []
        if acc_lift is not None and acc_lift != 0:
            direction = "\u2191" if acc_lift > 0 else "\u2193"
            lift_parts.append(f"acceptance {direction}{abs(acc_lift):.0%}")
        if reply_lift is not None and reply_lift != 0:
            direction = "\u2191" if reply_lift > 0 else "\u2193"
            lift_parts.append(f"reply {direction}{abs(reply_lift):.0%}")
        if lift_parts:
            lines.append(f"Signal lift: {', '.join(lift_parts)}")

        # Enough data verdict
        if sig_invited >= _MIN_PER_VARIANT and cold_invited >= _MIN_PER_VARIANT:
            winner = _determine_signal_winner(
                sig_acc, cold_acc, sig_reply, cold_reply, acc_lift, reply_lift,
            )
            lines.append(f"\u2705 {winner}")
        else:
            sig_needed = max(0, _MIN_PER_VARIANT - sig_invited)
            cold_needed = max(0, _MIN_PER_VARIANT - cold_invited)
            lines.append(
                f"\u23f3 Need more data: signal needs ~{sig_needed} more, "
                f"cold needs ~{cold_needed} more invited"
            )

        # Best signal type
        by_type = perf.get("by_signal_type", {})
        if by_type:
            best_type = None
            best_rate = 0.0
            for sig_type, stats in by_type.items():
                total = stats.get("total", 0)
                replied = stats.get("replied", 0)
                if total >= 3:
                    rate = replied / total
                    if rate > best_rate:
                        best_rate = rate
                        best_type = sig_type
            if best_type:
                label = best_type.replace("_", " ").title()
                lines.append(f"\U0001f947 Best converting signal: {label} ({best_rate:.0%} reply rate)")

        lines.append("")
        return "\n".join(lines)
    except Exception:
        return ""
