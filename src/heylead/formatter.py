"""Chat-friendly output formatting for HeyLead."""

from __future__ import annotations

from typing import Any


def tree(items: list[tuple[str, str]], title: str = "") -> str:
    """Format a list of (label, value) as a tree.

    Example output:
        Title
        ├── Label 1: Value 1
        ├── Label 2: Value 2
        └── Label 3: Value 3
    """
    lines = []
    if title:
        lines.append(title)
    for i, (label, value) in enumerate(items):
        is_last = i == len(items) - 1
        prefix = "└──" if is_last else "├──"
        lines.append(f"{prefix} {label}: {value}")
    return "\n".join(lines)


def tree_nested(data: dict[str, Any], title: str = "", indent: int = 0) -> str:
    """Format nested dict as a tree."""
    lines = []
    if title:
        lines.append(title)
    keys = list(data.keys())
    for i, key in enumerate(keys):
        is_last = i == len(keys) - 1
        prefix = "└──" if is_last else "├──"
        pad = "    " * indent
        value = data[key]
        if isinstance(value, dict):
            lines.append(f"{pad}{prefix} {key}:")
            sub_keys = list(value.keys())
            for j, sk in enumerate(sub_keys):
                sub_last = j == len(sub_keys) - 1
                sub_prefix = "└──" if sub_last else "├──"
                cont = "    " if is_last else "│   "
                lines.append(f"{pad}{cont}{sub_prefix} {sk}: {value[sk]}")
        else:
            lines.append(f"{pad}{prefix} {key}: {value}")
    return "\n".join(lines)


def table(headers: list[str], rows: list[list[str]]) -> str:
    """Format a simple markdown-style table."""
    if not rows:
        return ""
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(str(cell)))

    header_line = " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    sep_line = "-|-".join("-" * col_widths[i] for i in range(len(headers)))
    body_lines = []
    for row in rows:
        body_lines.append(
            " | ".join(str(cell).ljust(col_widths[i]) for i, cell in enumerate(row))
        )

    return "\n".join([header_line, sep_line] + body_lines)


def progress_bar(current: int, total: int, width: int = 20) -> str:
    """Simple text progress bar: [████████░░░░] 60%"""
    if total <= 0:
        return "[" + "░" * width + "] 0%"
    pct = min(current / total, 1.0)
    filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {int(pct * 100)}%"


def stars(score: float, max_score: float = 1.0) -> str:
    """Convert a 0-1 score to star rating ★★★★☆"""
    normalized = min(score / max_score, 1.0) if max_score > 0 else 0
    full = int(normalized * 5)
    return "★" * full + "☆" * (5 - full)


def health_rating(acceptance_rate: float) -> str:
    """Account health based on acceptance rate."""
    if acceptance_rate >= 0.30:
        return "★★★★★ Excellent"
    elif acceptance_rate >= 0.25:
        return "★★★★☆ Good"
    elif acceptance_rate >= 0.15:
        return "★★★☆☆ Fair"
    elif acceptance_rate > 0:
        return "★★☆☆☆ Low"
    else:
        return "☆☆☆☆☆ No data"


def funnel_bar(label: str, count: int, max_count: int, width: int = 20) -> str:
    """Render a single funnel bar: 'Invited  ████████████████████ 50'."""
    if max_count <= 0:
        filled = 0
        pct = 0.0
    else:
        pct = count / max_count
        filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)
    pct_str = f" ({pct:.0%})" if count != max_count else ""
    return f"{label:<12} {bar} {count}{pct_str}"


def sparkline(values: list[int], width: int = 20) -> str:
    """Render a sparkline chart from a list of values."""
    if not values:
        return ""
    blocks = " ▁▂▃▄▅▆▇█"
    max_val = max(values) if max(values) > 0 else 1
    # Downsample if needed
    if len(values) > width:
        step = len(values) / width
        sampled = [values[int(i * step)] for i in range(width)]
    else:
        sampled = values
    return "".join(blocks[min(int(v / max_val * 8), 8)] for v in sampled)


def outcome_icon(status: str) -> str:
    """Status icon for closed outreaches."""
    return {"closed_happy": "\U0001f3c6", "closed_unhappy": "\U0001f4c9", "opted_out": "\U0001f6ab"}.get(status, "\u26aa")


def outcome_label(status: str) -> str:
    """Human-friendly label for outcome status."""
    return {"closed_happy": "Won", "closed_unhappy": "Lost", "opted_out": "Opted Out"}.get(status, status)


def conversion_rate_display(happy: int, unhappy: int) -> str:
    """Format conversion rate: '75% (3W / 1L)'."""
    total = happy + unhappy
    if total == 0:
        return "No outcomes yet"
    rate = happy / total
    return f"{rate:.0%} ({happy}W / {unhappy}L)"


def format_duration(seconds: int | float | None) -> str:
    """Format seconds to human-readable: '2h 15m', '3d 4h', '< 1h', 'N/A'."""
    if seconds is None or seconds < 0:
        return "N/A"
    seconds = int(seconds)
    if seconds < 3600:
        return "< 1h"
    hours = seconds // 3600
    if hours < 24:
        mins = (seconds % 3600) // 60
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    days = hours // 24
    rem_hours = hours % 24
    return f"{days}d {rem_hours}h" if rem_hours else f"{days}d"


def format_wait_age(hours: int | float | None) -> str:
    """Humanize how long someone has been waiting: '6h', '2d', '151d'.

    Bare hours stop being readable somewhere past a day or two — a lead who
    wrote in April renders as '3626h', which nobody parses. Whole days take
    over at 48h, so the number stays monotonic and free of rounding cliffs.
    """
    if hours is None or hours < 0:
        return "0h"
    hours = int(hours)
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def source_badge(source: str, source_detail: str = "") -> str:
    """Format prospect source as a human-readable badge."""
    from .constants import SOURCE_LABELS
    label = SOURCE_LABELS.get(source, source.replace("_", " ").title())
    if source_detail:
        return f"via {label} ({source_detail[:60]})"
    return f"via {label}"


def prospect_link(name: str, linkedin_url: str = "") -> str:
    """Format a prospect name as a markdown hyperlink if URL is available."""
    if linkedin_url:
        return f"[{name}]({linkedin_url})"
    return name


# Labels for the seniority keys `services.seniority.SENIORITY_ORDER` uses.
_SENIORITY_LABELS: dict[str, str] = {
    "owner": "owner or founder",
    "cxo": "C-level",
    "vp": "VP",
    "director": "director",
    "manager": "manager",
    "senior": "senior",
    "entry": "entry level",
}

# A dimension at or above this counts as a reason. compute_icp_match returns
# 0.30 (0.50 for location) for "nothing to compare", so the bar sits well
# above the no-signal values.
_WHY_DIMENSION_MATCH = 0.7


def why_line(why: dict | None) -> str:
    """One short reason a person is in the list, from the stored `why`.

    Reads the enrolment record `services.enrolment_why.enrolment_why` writes
    (and the api's `_enrolment_why`): the fit breakdown per dimension,
    seniority and title family, and the evidence hits. Names what matched
    and leaves out what did not; no bare scores, because "industry 0.30"
    means "nothing to compare", not "poor match".
    """
    if not isinstance(why, dict) or not why:
        return ""
    breakdown = why.get("breakdown") if isinstance(why.get("breakdown"), dict) else {}

    def _hit(key: str) -> bool:
        value = breakdown.get(key)
        return isinstance(value, (int, float)) and value >= _WHY_DIMENSION_MATCH

    reasons: list[str] = []
    family = str(why.get("title_family") or "").strip()
    if _hit("title"):
        reasons.append(f"{family} title match" if family else "title match")
    seniority = str(why.get("seniority") or "").strip()
    if seniority and _hit("seniority"):
        reasons.append(_SENIORITY_LABELS.get(seniority, seniority))
    if _hit("industry"):
        reasons.append("industry fit")
    if _hit("company_size"):
        reasons.append("company size fit")
    if _hit("location"):
        reasons.append("location fit")
    if _hit("keywords"):
        reasons.append("keyword overlap")
    hits = why.get("evidence_hits")
    if isinstance(hits, list) and hits:
        n = len(hits)
        reasons.append(f"{n} evidence hit{'s' if n != 1 else ''}")
    if not reasons:
        segment = str(why.get("segment") or "").strip()
        if segment:
            reasons.append(f"segment {segment}")
    text = ", ".join(reasons)
    score = why.get("fit_score")
    if isinstance(score, (int, float)) and score > 0:
        text = f"fit {stars(float(score))}" + (f", {text}" if text else "")
    return text


def person_line(
    name: str,
    url: str = "",
    title: str | None = None,
    company: str | None = None,
    why: dict | None = None,
) -> str:
    """`[Name](url) — title at company · why: {one line}`.

    The one way a tool result names a found person. The name is a link
    whenever the URL is known, the role follows when there is one, and the
    reason follows when `why` says anything. A list of names with no link
    and no reason is what a new user could not act on (24 Sep 2026).
    """
    line = prospect_link(name or "Unknown", url or "")
    role = (title or "").strip()
    if (company or "").strip():
        role = f"{role} at {company.strip()}" if role else company.strip()
    if role:
        line += f" — {role}"
    reason = why_line(why)
    if reason:
        line += f" · why: {reason}"
    return line


def format_voice_signature(voice: dict, expertise: dict) -> str:
    """Format voice signature + expertise for display (the killer feature)."""
    lines = [
        "✅ Profile analyzed! Here's how I'll represent you:",
        "",
        "Your Voice:",
    ]

    voice_items = []
    if voice.get("tone"):
        voice_items.append(("Tone", voice["tone"]))
    if voice.get("sentence_length"):
        voice_items.append(("Sentence length", voice["sentence_length"]))
    if voice.get("signature_pattern"):
        voice_items.append(("Signature pattern", voice["signature_pattern"]))
    if voice.get("no_go"):
        voice_items.append(("No-go", voice["no_go"]))

    for i, (label, value) in enumerate(voice_items):
        is_last = i == len(voice_items) - 1
        prefix = "└──" if is_last else "├──"
        lines.append(f"{prefix} {label}: {value}")

    lines.append("")
    lines.append("Your Expertise:")

    exp_items = []
    if expertise.get("core"):
        exp_items.append(("Core", expertise["core"]))
    if expertise.get("credible_topics"):
        exp_items.append(("Credible topics", expertise["credible_topics"]))
    if expertise.get("off_limits"):
        exp_items.append(("Off-limits", expertise["off_limits"]))

    for i, (label, value) in enumerate(exp_items):
        is_last = i == len(exp_items) - 1
        prefix = "└──" if is_last else "├──"
        lines.append(f"{prefix} {label}: {value}")

    lines.append("")
    lines.append("This ensures every message sounds like YOU, not a bot.")

    return "\n".join(lines)
