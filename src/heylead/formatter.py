"""Chat-friendly output formatting for HeyLead."""

from __future__ import annotations

from typing import Any

from .textutil import evidence_hit_is_meaningful


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
    """Format a prospect name as a markdown hyperlink if URL is available.

    The link is the profile, /in/<slug>: a stored search-card URL carries a
    ?miniProfileUrn=… tail up to 182 characters long (heylead-api#1898)."""
    if linkedin_url:
        from .linkedin_urls import profile_link_url

        return f"[{name}]({profile_link_url(linkedin_url)})"
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

# Labels for the title families `services.enrolment_why.TITLE_FAMILIES`
# writes, as a sentence says them; the api's person_line and the dashboard's
# outreachLabels say the same (heylead-api#1827).
_TITLE_FAMILY_LABELS: dict[str, str] = {
    "engineering": "engineering",
    "product": "product",
    "sales": "sales",
    "marketing": "marketing",
    "finance": "finance",
    "operations": "operations",
    "people": "people and HR",
    "executive": "executive",
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
    family = str(why.get("title_family") or "").strip().lower()
    family = "" if family == "unknown" else _TITLE_FAMILY_LABELS.get(family, family.replace("_", " "))
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
    if isinstance(hits, list):
        # "about:at" is no evidence (heylead-api#1809).
        hits = [h for h in hits if evidence_hit_is_meaningful(h)]
    if isinstance(hits, list) and hits:
        n = len(hits)
        # A profile term the campaign looks for, in plain words; "evidence
        # hit" was our scoring word (heylead-api#1863, the api's wording).
        reasons.append("1 match in their profile" if n == 1 else f"{n} matches in their profile")
    if why.get("title_only") is True:
        # The one reason named for a miss: the row was held under the floor
        # on this, and "title match" alone would read as a pass
        # (heylead-api#2149).
        reasons.append("held: title matches, no industry or keyword evidence")
    if not reasons:
        segment = str(why.get("segment") or "").strip()
        if segment:
            reasons.append(f"segment {segment}")
    text = ", ".join(reasons)
    score = why.get("fit_score")
    if isinstance(score, (int, float)) and score > 0:
        text = f"fit {stars(float(score))}" + (f", {text}" if text else "")
    return text


# The one name for a person HeyLead has no name for, as in the API's
# person_line (heylead-api #1715): "Unknown" read as a gap.
UNNAMED = "Unnamed prospect"


# What a source writes when it has no name. Stored as a name, it read as one.
PLACEHOLDER_NAMES = frozenset({"unknown", "linkedin member", "unnamed lead", "someone"})


def has_name(name: object) -> bool:
    """True when ``name`` is a person's name, not empty and not a placeholder."""
    text = str(name or "").strip()
    return bool(text) and text.lower() not in PLACEHOLDER_NAMES


def person_name(name: object, unnamed: str = UNNAMED) -> str:
    """The name to show for a person: theirs, or ``unnamed``."""
    return str(name).strip() if has_name(name) else unnamed


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
    line = prospect_link(person_name(name), url or "")
    role = (title or "").strip()
    if (company or "").strip():
        role = f"{role} at {company.strip()}" if role else company.strip()
    if role:
        line += f" — {role}"
    reason = why_line(why)
    if reason:
        line += f" · why: {reason}"
    return line


# ── The voice in the setup_profile and switch_to replies (api #3499) ──
#
# States (designer-1, designer-2 on heylead-api#3499), decided in this order:
#   K / X  the posts fetch or the analysis failed; K keeps the earlier voice
#          of the same LinkedIn account, X has none to keep
#   N / NK no posts the user wrote; NK keeps the same account's earlier voice
#   T      a first read from 1-2 own posts
#   F      3 or more own posts
# Modes: M1 hosted, the cloud sends; M2 hosted, this Mac sends; M3 local mode
# on the user's own LLM key. Only M3 is ever told to check an LLM key.

VOICE_STATES = ("F", "T", "N", "NK", "K", "X")
VOICE_MODES = ("M1", "M2", "M3")


def _posts_phrase(count: int) -> str:
    return "1 post" if count == 1 else f"{count} posts"


def _what_failed(reason: str) -> str:
    return "voice" if reason == "voice" else "LinkedIn posts"


def voice_posts_line(state: str, *, post_count: int = 0, reason: str = "posts") -> str:
    """The 📝 line under the profile header."""
    if state == "F":
        return f"📝 Learned your voice from {post_count} posts you wrote"
    if state == "T":
        return f"📝 Learned your voice from {_posts_phrase(post_count)} you wrote: a first read"
    if state == "N":
        return "📝 No posts you wrote yourself yet, so no voice"
    if state == "NK":
        return (
            "📝 No posts you wrote yourself among your recent ones, "
            "so your earlier voice stays in use"
        )
    if state == "K":
        return (
            f"📝 Couldn't read your {_what_failed(reason)} this time, "
            "so your earlier voice stays in use"
        )
    return f"📝 Couldn't read your {_what_failed(reason)} this time, so there's no voice yet"


def _tree(items: list[tuple[str, Any]]) -> list[str]:
    out = []
    for i, (label, value) in enumerate(items):
        prefix = "└──" if i == len(items) - 1 else "├──"
        out.append(f"{prefix} {label}: {value}")
    return out


def _expertise_lines(expertise: dict | None) -> list[str]:
    expertise = expertise if isinstance(expertise, dict) else {}
    items = [
        (label, expertise[key])
        for key, label in (("core", "Core"), ("credible_topics", "Credible topics"),
                           ("off_limits", "Off-limits"))
        if expertise.get(key)
    ]
    if not items:
        return ["Your Expertise: not read yet"]
    return ["Your Expertise:", *_tree(items)]


def _voice_items(voice: dict) -> list[tuple[str, Any]]:
    return [
        (label, voice[key])
        for key, label in (("tone", "Tone"), ("sentence_length", "Sentence length"),
                           ("signature_pattern", "Signature pattern"), ("no_go", "No-go"))
        if voice.get(key)
    ]


def _recorded_count(voice: dict) -> int:
    try:
        value = voice.get("post_count")
        return 0 if isinstance(value, bool) else int(value or 0)
    except (TypeError, ValueError):
        return 0


def _voice_heading(voice: dict, post_count: int = 0) -> str:
    count = post_count or _recorded_count(voice)
    if count >= 3:
        return f"Your Voice (from {count} posts you wrote):"
    if count > 0:
        return f"Your Voice (a first read from {_posts_phrase(count)} you wrote):"
    return "Your Voice:"  # a voice stored before its record existed


def _in_this_voice(voice: dict, mode: str, post_count: int = 0) -> str:
    line = "HeyLead writes your messages and posts in this voice."
    if 0 < (post_count or _recorded_count(voice)) < 3:
        if mode == "M1":
            return line + " It gets closer once you've written 3 posts."
        return line + " Once you've written 3 posts, run setup_profile() again to sharpen it."
    return line


def _retry_line(mode: str, *, with_key_hint: bool, retry_account_id: str = "") -> str:
    if mode == "M1":
        return "HeyLead tries again on its own."
    if retry_account_id:
        line = (
            f"Run account(action='switch_to', account_id='{retry_account_id}') "
            "again in a few minutes to retry."
        )
    else:
        line = "Run setup_profile() again in a few minutes to retry."
    if with_key_hint and mode == "M3":
        line += (
            " If it fails again, check your LLM key: "
            "setup_profile(llm_api_key='...') replaces it."
        )
    return line


def format_voice_reply(
    state: str,
    voice: dict | None,
    expertise: dict | None,
    *,
    mode: str,
    post_count: int = 0,
    reason: str = "posts",
    retry_account_id: str = "",
) -> str:
    """The voice and expertise section of the setup_profile / switch_to reply.

    ``voice`` is the voice that is now stored (the kept one for K and NK).
    ``post_count`` is this build's own-post count (F, T); 0 reads the count
    the stored voice recorded.
    ``retry_account_id`` makes X's retry line the switch_to call.
    """
    voice = voice if isinstance(voice, dict) else {}
    head = ["✅ Profile analyzed! Here's how I'll represent you:", ""]

    if state in ("F", "T", "NK", "K"):
        count = post_count if state in ("F", "T") else 0
        lines = [*head, _voice_heading(voice, count), *_tree(_voice_items(voice)), "",
                 *_expertise_lines(expertise), ""]
        if state == "K":
            lines.append(_retry_line(mode, with_key_hint=True))
        else:
            lines.append(_in_this_voice(voice, mode, count))
        return "\n".join(lines)

    if state == "N":
        if mode == "M1":
            act = (
                "Write a post or two on LinkedIn. HeyLead reads your posts again "
                "on its own and learns your voice from them."
            )
        else:
            act = (
                "Write a post or two on LinkedIn, then run setup_profile() again "
                "so HeyLead learns your voice from them."
            )
        return "\n".join([
            *head,
            "Your Voice: not learned yet",
            "└── Your LinkedIn account has no posts you wrote yourself. "
            "Reshares and posts HeyLead published for you don't count.",
            "",
            *_expertise_lines(expertise),
            "",
            "Until you post, HeyLead writes your messages in a plain, direct style.",
            act,
        ])

    # X: nothing stored.
    return "\n".join([
        "Your Voice: not learned yet",
        f"└── Couldn't read your {_what_failed(reason)} this time.",
        "",
        *_expertise_lines(expertise),
        "",
        "Until it's learned, HeyLead writes your messages in a plain, direct style.",
        _retry_line(mode, with_key_hint=False, retry_account_id=retry_account_id),
    ])
