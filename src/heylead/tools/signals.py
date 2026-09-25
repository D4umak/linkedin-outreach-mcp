"""Tool: signals — View buying signals, signal reports, strategy insights, and feedback.

Thin dispatcher that routes to existing run_* functions based on the action parameter.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def run_signals(
    action: str = "show",
    campaign_id: str = "",
    signal_type: str = "",
    status: str = "",
    limit: int = 20,
    days: int = 30,
    signal_id: str = "",
    feedback: str = "",
) -> str:
    """View and analyze buying signals from LinkedIn.

    Actions:
      show     — Display detected buying signals (keyword mentions, job changes, etc.)
      report   — Signal analytics report with trends and ROI
      strategy — Show strategy engine insights, patterns, and autonomous actions
      feedback — Mark a signal as 'useful' or 'not_useful' (improves future classification)

    Args:
        action: What to do: 'show', 'report', 'strategy', 'feedback'.
        campaign_id: Filter by campaign. Shows all if empty.
        signal_type: Filter by signal type, e.g. 'keyword_mention', 'job_change' (for 'show').
        status: Filter by status: 'new', 'classified', 'actioned' (for 'show').
        limit: Max signals to show (for 'show'). Default 20.
        days: Lookback window in days (for 'report'). Default 30.
        signal_id: Signal ID (for 'feedback' action).
        feedback: 'useful' or 'not_useful' (for 'feedback' action).
    """
    action = action.lower().strip()

    if action == "show":
        from .show_signals import run_show_signals
        return await run_show_signals(
            signal_type=signal_type,
            campaign_id=campaign_id,
            status=status,
            limit=limit,
        )

    if action == "report":
        from .signal_report import run_signal_report
        return await run_signal_report(days=days, campaign_id=campaign_id)

    if action == "strategy":
        from .show_strategy import run_show_strategy
        return await run_show_strategy(campaign_id=campaign_id)

    if action == "feedback":
        from ..db.async_bridge import run_db
        return await run_db(_handle_feedback, signal_id=signal_id, feedback=feedback)

    if action in ("website_setup", "website"):
        return await _handle_website_setup()

    if action == "website_stats":
        return await _handle_website_stats(days=days)

    # ── Signal Self-Optimization actions ──

    if action == "optimize":
        return await _handle_optimize()

    if action in ("optimize_history", "history"):
        return await _handle_optimize_history(signal_type=signal_type, limit=limit)

    if action in ("optimize_rollback", "rollback"):
        return await _handle_optimize_rollback(entry_id=signal_id)

    if action in ("optimize_weights", "weights"):
        return await _handle_optimize_weights()

    return (
        f"Unknown action: '{action}'. "
        "Use 'show', 'report', 'strategy', 'feedback', 'website_setup', 'website_stats', "
        "'optimize', 'optimize_history', 'optimize_rollback', or 'optimize_weights'."
    )


def _handle_feedback(signal_id: str, feedback: str) -> str:
    """Record user feedback on a signal's usefulness."""
    import time as _time
    from ..db.signal_queries import get_signal, update_signal

    if not signal_id:
        return "signal_id is required for feedback action."

    feedback = feedback.lower().strip()
    if feedback not in ("useful", "not_useful"):
        return "feedback must be 'useful' or 'not_useful'."

    signal = get_signal(signal_id)
    if not signal:
        return f"Signal '{signal_id}' not found."

    update_signal(
        signal_id,
        user_feedback=feedback,
        feedback_at=int(_time.time()),
    )

    return f"Feedback recorded: signal {signal_id[:8]} marked as '{feedback}'."


_BACKEND_REQUIRED = (
    "Website tracking requires backend mode. "
    "Connect your account via setup_profile() first."
)


def _backend_client():
    """The shared backend client, or None when this install is self-hosted.

    Requests must go through it: `BackendClient._headers()` is the only place
    `X-Org-Id` (the workspace the user has selected) and `X-Correlation-ID` are
    attached, and `/t/*` is keyed on the workspace. A hand-rolled
    `Authorization` header lands every call in the account's default workspace.
    """
    from ..linkedin import get_linkedin_client

    client = get_linkedin_client()
    if hasattr(client, "base_url") and hasattr(client, "jwt_token"):
        return client
    return None


def _paths(values: list[str]) -> str:
    return ", ".join(values) if values else "(none)"


def _snippet_lines(data: dict, *, read_only: bool = False) -> list[str]:
    """Render a snippet config. /t/setup and /t/config return the same shape."""
    high_intent = data.get("high_intent_paths") or []
    exclude = data.get("exclude_paths") or []

    title = (
        "## Website Visitor Tracking — Current Snippet"
        if read_only
        else "## Website Visitor Tracking — Setup Complete"
    )
    lead = (
        "This workspace's snippet (you have view-only access, so nothing was "
        "changed). Add this to your website's `<head>` tag:"
        if read_only
        else "Your tracking snippet is ready! Add this to your website's `<head>` tag:"
    )

    lines = [
        title,
        "",
        lead,
        "",
        "```html",
        data.get("embed_code", ""),
        "```",
        "",
        f"**High-intent pages** (generate stronger signals): {_paths(high_intent)}",
        f"**Excluded pages** (never tracked): {_paths(exclude)}",
        "",
        "**How it works:**",
        "- Identifies visiting companies via IP-to-company resolution",
        "- No cookies, no fingerprinting — GDPR/CCPA compliant",
        "- Company-level identification only (no individual tracking)",
        "- Generates `website_visit` and `website_high_intent` signals",
        "- Signals feed into your existing scoring and outreach pipeline",
        "",
        "**Customize paths:** call `POST /t/config` on the backend with",
        "`high_intent_paths` and/or `exclude_paths`. A list you leave out keeps",
        "its current value, so you can change one without resending the other.",
        "",
        "This applies to the workspace you have selected. Switch with",
        "`organization(action='switch', org_id='...')` before running it for a",
        "different client.",
    ]
    return lines


async def _read_snippet_config(client) -> str:
    """GET /t/config — readable by any member, and it creates nothing.

    The fallback for a viewer, who `POST /t/setup` now refuses.
    """
    url = f"{client.base_url.rstrip('/')}/t/config"
    try:
        resp = await client._client.get(url, headers=client._headers())
    except Exception as e:
        return f"Failed to connect to backend for website config: {e}"

    if resp.status_code != 200:
        return (
            f"Failed to read website tracking config (HTTP {resp.status_code}): "
            f"{resp.text}"
        )

    data = resp.json()
    if not data.get("configured"):
        return (
            "No tracking snippet exists in this workspace yet, and setting one "
            "up needs an editor role. Ask the workspace owner to run "
            "`signals(action='website_setup')`, or switch workspace with "
            "`organization(action='switch', org_id='...')`."
        )
    return "\n".join(_snippet_lines(data, read_only=True))


async def _handle_website_setup() -> str:
    """Set up website visitor tracking — generate snippet token and embed code."""
    client = _backend_client()
    if client is None:
        return _BACKEND_REQUIRED

    url = f"{client.base_url.rstrip('/')}/t/setup"
    try:
        resp = await client._client.post(url, headers=client._headers(), json={})
    except Exception as e:
        return f"Failed to connect to backend for website setup: {e}"

    # /t/setup is editor-gated. A viewer can still read the workspace's snippet.
    if resp.status_code == 403:
        return await _read_snippet_config(client)

    if resp.status_code != 200:
        return f"Failed to set up website tracking (HTTP {resp.status_code}): {resp.text}"

    return "\n".join(_snippet_lines(resp.json()))


async def _handle_website_stats(days: int = 30) -> str:
    """Fetch website tracking stats from the backend."""
    client = _backend_client()
    if client is None:
        return "Website tracking requires backend mode."

    url = f"{client.base_url.rstrip('/')}/t/stats"
    try:
        resp = await client._client.get(
            url, headers=client._headers(), params={"days": days},
        )
    except Exception as e:
        return f"Failed to connect to backend for website stats: {e}"

    if resp.status_code != 200:
        return f"Failed to fetch website stats (HTTP {resp.status_code}): {resp.text}"

    data = resp.json()
    lines = [
        f"## Website Tracking Stats (last {days} days)",
        "",
        f"- **Total visits:** {data.get('total_visits', 0)}",
        f"- **Unique companies:** {data.get('unique_companies', 0)}",
        f"- **High-intent visits:** {data.get('high_intent_visits', 0)}",
        f"- **Signals created:** {data.get('signals_created', 0)}",
    ]

    top = data.get("top_companies", [])
    if top:
        lines.append("")
        lines.append("### Top Companies")
        for c in top[:10]:
            hi = f" ({c['high_intent']} high-intent)" if c.get("high_intent") else ""
            lines.append(f"- **{c['company']}** — {c['visits']} visits{hi}")

    recent = data.get("recent_visits", [])
    if recent:
        lines.append("")
        lines.append("### Recent Visits")
        for v in recent[:10]:
            intent = " [HIGH INTENT]" if v.get("high_intent") else ""
            company = f" ({v['company']})" if v.get("company") else ""
            lines.append(f"- {v.get('url', 'unknown')}{company}{intent}")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# Signal Self-Optimization handlers
# ──────────────────────────────────────────────


# Hosted: the optimiser runs once a day in the cloud (heylead-api#1211) and
# the local optimiser's tables are not the account's.
_HOSTED_OPTIMISER = (
    "Your signals are optimised in the cloud once a day. "
    "See what changed with signals(action='optimize_history')."
)


async def _handle_optimize() -> str:
    """Manually trigger the signal optimization loop."""
    from .. import config
    if config.is_backend_mode():
        return _HOSTED_OPTIMISER
    from ..services.signal_optimizer import run_signal_optimization

    result = await run_signal_optimization()
    parts = []
    if result["weights_adjusted"]:
        parts.append(f"**{result['weights_adjusted']} signal weights** adjusted")
    if result["keywords_added"]:
        parts.append(f"**{result['keywords_added']} keywords** discovered and added")
    if result["warmup_changes"]:
        parts.append(f"**{result['warmup_changes']} campaigns** had warm-up disabled")
    if result["threshold_changes"]:
        parts.append(f"**{result['threshold_changes']} thresholds** tuned")
    if result.get("errors"):
        parts.append(f"Errors: {', '.join(result['errors'])}")

    if not parts:
        return "Signal optimization ran — no changes needed. All weights, thresholds, keywords, and warm-up settings are performing well."

    return "## Signal Optimization Results\n\n" + "\n".join(f"- {p}" for p in parts) + "\n\nUse `signals(action='optimize_history')` to see all changes."


async def _handle_optimize_history(signal_type: str = "", limit: int = 30) -> str:
    """Show optimization history log."""
    from .. import config
    if config.is_backend_mode():
        from ..services import cloud_learning
        data = await cloud_learning.fetch_learning(limit)
        return cloud_learning.format_history(data) if data else cloud_learning.UNREACHABLE

    from ..db.async_bridge import run_db
    from ..db.signal_queries import list_optimization_history

    entries = await run_db(list_optimization_history, optimization_type=signal_type, limit=limit)
    if not entries:
        return "No optimization history yet. Run `signals(action='optimize')` to trigger optimization."

    lines = ["## Optimization History\n"]
    for e in entries:
        ts = e.get("applied_at", 0)
        import time as _time
        age = int(_time.time()) - ts
        if age < 3600:
            age_str = f"{age // 60}m ago"
        elif age < 86400:
            age_str = f"{age // 3600}h ago"
        else:
            age_str = f"{age // 86400}d ago"

        status = e.get("status", "applied")
        icon = "✅" if status == "applied" else "⏪" if status == "rolled_back" else "⚪"
        opt_type = e.get("optimization_type", "?")
        target = e.get("target", "?")
        before = e.get("before_value", "")
        after = e.get("after_value", "")
        reason = e.get("reason", "")
        entry_id = e.get("id", "")

        lines.append(f"{icon} **{opt_type}** `{target}` — {before} → {after} ({age_str})")
        if reason:
            lines.append(f"   {reason}")
        lines.append(f"   ID: `{entry_id}` | Data: {e.get('data_points', 0)} samples")
        lines.append("")

    lines.append("Rollback: `signals(action='optimize_rollback', signal_id='<entry_id>')`")
    return "\n".join(lines)


async def _handle_optimize_rollback(entry_id: str) -> str:
    """Rollback a specific optimization change."""
    from .. import config
    if config.is_backend_mode():
        return _HOSTED_OPTIMISER
    if not entry_id:
        return "Provide the entry ID via signal_id parameter. Use `signals(action='optimize_history')` to see IDs."

    from ..db.async_bridge import run_db
    from ..db.signal_queries import (
        rollback_optimization, delete_weight_override, delete_threshold_override,
    )

    entry = await run_db(rollback_optimization, entry_id)
    if not entry:
        return f"Entry `{entry_id}` not found."

    opt_type = entry.get("optimization_type", "")
    target = entry.get("target", "")

    # Actually revert the change
    if opt_type == "weight":
        await run_db(delete_weight_override, target)
        from ..services.signal_scorer import invalidate_weight_cache
        invalidate_weight_cache()
    elif opt_type == "threshold":
        await run_db(delete_threshold_override, target)
        from ..services.signal_activator import invalidate_threshold_cache
        invalidate_threshold_cache()

    return (
        f"Rolled back: **{opt_type}** on `{target}`\n"
        f"- Reverted: {entry.get('after_value', '?')} → {entry.get('before_value', '?')}\n"
        f"- The system will use the default value from constants."
    )


async def _handle_optimize_weights() -> str:
    """Show all signal types with default vs effective weights."""
    from .. import config
    if config.is_backend_mode():
        return _HOSTED_OPTIMISER
    from ..db.async_bridge import run_db
    from ..db.signal_queries import get_weight_overrides, get_threshold_overrides
    from ..services.signal_scorer import SIGNAL_WEIGHTS

    overrides = await run_db(get_weight_overrides)
    threshold_overrides = await run_db(get_threshold_overrides)

    lines = ["## Signal Weights (Effective)\n"]
    lines.append("| Signal Type | Default | Effective | Source |")
    lines.append("|---|---|---|---|")

    for sig_type, default_weight in sorted(SIGNAL_WEIGHTS.items(), key=lambda x: -x[1]):
        effective = overrides.get(sig_type, default_weight)
        source = "override" if sig_type in overrides else "default"
        marker = " ✏️" if sig_type in overrides else ""
        lines.append(f"| {sig_type} | {default_weight:.3f} | {effective:.3f} | {source}{marker} |")

    if threshold_overrides:
        lines.append("\n## Threshold Overrides\n")
        lines.append("| Threshold | Value |")
        lines.append("|---|---|")
        for name, val in threshold_overrides.items():
            lines.append(f"| {name} | {val:.3f} |")

    lines.append(f"\n**{len(overrides)} weight overrides** active, **{len(threshold_overrides)} threshold overrides** active.")
    lines.append("\nRuns daily via scheduler. Manual: `signals(action='optimize')`")
    return "\n".join(lines)
