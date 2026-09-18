"""Message brief — the deterministic WHAT behind every generated message.

Increment 2 of the crafting redesign (increment 1: ai/intent.py, the
stance layer). Templates and voice decide HOW a message sounds; this
module decides WHAT it must say, assembled from data the system already
holds: campaign intent and goal, the matched ICP persona's psychology
(pain points/fears/barriers — restored to icp_json by
_icp_result_to_legacy after being dropped since the feature shipped),
the prospect's analysis, and live signals.

Everything here is a pure function of its inputs: no DB, no LLM, fully
unit-testable. Precedence of layers, lowest to highest: intent template
(stance) < message brief (content) < campaign_rules_section (explicit
user override).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_PLACEHOLDER_RE = re.compile(r"\[[A-Za-z][^\]\n]{0,30}\]")


@dataclass
class MessageBrief:
    intent: str = "sell"
    role_frame: str = ""
    touch_goal: str = ""
    must_say: str = ""
    must_not_say: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)


def _match_persona(prospect: dict[str, Any], icp_data: dict[str, Any]) -> dict[str, Any]:
    """Pick the ICP segment whose titles best match the prospect's title."""
    segments = (icp_data or {}).get("segments") or []
    if not segments:
        return {}
    title = str((prospect or {}).get("title") or "").casefold()
    if title:
        for seg in segments:
            for t in seg.get("titles") or []:
                if str(t).casefold() in title or title in str(t).casefold():
                    return seg
    return segments[0]


def _clean(text: Any) -> str:
    s = str(text or "").strip()
    return "" if s.lower() in ("not specified", "none", "n/a") else s


_ONE_POINT_MAX = 180
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

_TOUCH_GOALS = {
    "invite": "get the connection accepted",
    "inmail": "get a short reply",
    "followup": "continue the thread without repeating",
    "reply": "answer and advance",
}


_DATE_TOKEN_RE = re.compile(
    r"\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\b",
    re.I,
)
_VOLUME_TOKEN_RE = re.compile(r"~\s*\d[\d,]*")


def _fact_needles(facts: dict[str, Any] | None) -> list[str]:
    """Tokens from project_facts that identify the landable sentence."""
    if not isinstance(facts, dict):
        return []
    needles: list[str] = []
    for key in ("go_live", "volume", "product"):
        value = _clean(facts.get(key))
        if value:
            needles.append(value)
    for item in facts.get("must_confirm") or []:
        value = _clean(item)
        if value:
            needles.append(value)
    extra: list[str] = []
    for needle in needles:
        extra.extend(_DATE_TOKEN_RE.findall(needle))
        extra.extend(_VOLUME_TOKEN_RE.findall(needle))
    seen: list[str] = []
    for needle in needles + extra:
        token = needle.strip()
        if token and token.casefold() not in {s.casefold() for s in seen}:
            seen.append(token)
    return seen


def _sentence_has_fact(sentence: str, needles: list[str]) -> bool:
    low = sentence.casefold()
    return any(needle.casefold() in low for needle in needles if needle)


def _one_point(text: Any, project_facts: dict[str, Any] | None = None) -> str:
    """Compress a paste to one landable point. No LLM, no invention."""
    s = _clean(text)
    if not s:
        return ""
    sentences = [part.strip() for part in _SENTENCE_RE.split(s) if part.strip()]
    needles = _fact_needles(project_facts)
    if needles:
        for sentence in sentences:
            if sentence and len(sentence) <= _ONE_POINT_MAX and _sentence_has_fact(sentence, needles):
                return sentence
    if len(s) <= _ONE_POINT_MAX and (not sentences or sentences[0] == s):
        return s
    first = sentences[0] if sentences else s
    if first and len(first) <= _ONE_POINT_MAX:
        return first
    cut = s[:_ONE_POINT_MAX]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.strip()


def build_message_brief(
    intent: str,
    campaign_config: dict[str, Any] | None,
    campaign_ctx: dict[str, Any] | None,
    prospect: dict[str, Any] | None,
    icp_data: dict[str, Any] | None = None,
    analysis: dict[str, Any] | None = None,
    touch: str = "invite",
) -> MessageBrief:
    """Assemble the single content point this message must land.

    must_say priority (first non-empty wins):
      project_brief → one point from the operator paste (all intents)
      buy intent → the concrete need (offerings, else campaign_preferences)
      signal hook → the live reason we're reaching out now
      persona ∩ analysis pains → psychology confirmed by this prospect
      analysis pains → prospect-specific, unconfirmed by persona
      research summary → what we know about them
      target_description → campaign-level, last resort
    """
    cfg = campaign_config or {}
    ctx = campaign_ctx or {}
    ana = analysis or {}
    signal = ana.get("signal_context") or {}

    role_frame = ""
    kind = touch if touch in _TOUCH_GOALS else "invite"
    touch_goal = _TOUCH_GOALS[kind]
    must_not: list[str] = []
    constraints = ["one question maximum", "never invent volumes, dates, or names"]

    if intent == "buy":
        role_frame = (
            "You are the buyer: you want to evaluate and pay for what this "
            "vendor sells."
        )
        if kind == "invite":
            touch_goal = "open a supplier conversation as an identified buyer"
        must_not.append("do not pitch or sell anything of ours")
    elif intent == "partner":
        role_frame = "You are proposing a mutual partnership, not selling."
    elif intent == "recruit":
        role_frame = "You are recruiting the prospect for a role."

    # ── must_say ──
    facts = ctx.get("project_facts") if isinstance(ctx.get("project_facts"), dict) else {}
    must_say = _one_point(ctx.get("project_brief"), facts)
    if not must_say and intent == "buy":
        must_say = _clean(ctx.get("offerings")) or _clean(ctx.get("campaign_preferences"))
    confirms = [
        _clean(item) for item in (facts.get("must_confirm") or []) if _clean(item)
    ]
    if confirms:
        constraints.append(
            "do not invent answers; ask these if relevant: " + "; ".join(confirms)
        )

    if not must_say:
        must_say = _clean(signal.get("engagement_hook"))
        for ban in signal.get("do_not") or []:
            b = _clean(ban)
            if b:
                must_not.append(b)

    if not must_say:
        persona = _match_persona(prospect or {}, icp_data or {})
        persona_pains = [_clean(p) for p in persona.get("pain_points") or [] if _clean(p)]
        analysis_pains = [_clean(p) for p in ana.get("pain_points") or [] if _clean(p)]
        shared = [p for p in analysis_pains if any(
            p.casefold() in pp.casefold() or pp.casefold() in p.casefold()
            for pp in persona_pains
        )]
        if shared:
            must_say = shared[0]
        elif analysis_pains:
            must_say = analysis_pains[0]
        elif persona_pains:
            must_say = persona_pains[0]

    if not must_say:
        must_say = _clean(ana.get("summary")) or _clean(cfg.get("target_description"))

    # Extract recent post intelligence if available
    posts = ana.get("recent_posts") or (prospect or {}).get("recent_posts") or []
    if isinstance(posts, list) and posts:
        first_post = posts[0]
        if isinstance(first_post, dict):
            post_topic = first_post.get("topic") or first_post.get("text", "")[:120]
            if post_topic:
                constraints.append(f"natural context if relevant: prospect recently posted about {_clean(post_topic)}")
    elif ana.get("post_topic"):
        constraints.append(f"natural context if relevant: prospect recently posted about {_clean(ana.get('post_topic'))}")

    return MessageBrief(
        intent=intent or "sell",
        role_frame=role_frame,
        touch_goal=touch_goal,
        must_say=must_say,
        must_not_say=must_not,
        constraints=constraints,
    )


def render_brief_block(brief: MessageBrief) -> str:
    """Render the brief for prompt injection.

    Guaranteed free of bracketed tokens — the deterministic validator
    hard-fails any message containing one, so the brief must never teach
    the model that shape.
    """
    if not brief or not (brief.must_say or brief.role_frame):
        return ""
    lines = ["THE BRIEF — this defines WHAT the message says; style rules below govern only HOW. If they conflict, the brief wins."]
    if brief.role_frame:
        lines.append(f"Role: {brief.role_frame}")
    if brief.touch_goal:
        lines.append(f"Goal of this touch: {brief.touch_goal}")
    if brief.must_say:
        lines.append(f"The one point to land: {brief.must_say}")
    for ban in brief.must_not_say:
        lines.append(f"Do not: {ban}")
    for c in brief.constraints:
        lines.append(f"Constraint: {c}")
    return _PLACEHOLDER_RE.sub("", "\n".join(lines))
