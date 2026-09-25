"""What a job-search first message may say about the reader's hiring.

25 Sep 2026 (outcome D4umak/heylead-api#1416): a job-search preview wrote
"On your search for an autonomous Head of Product: ... Who is the best person
to speak with?" to a Chief Product Officer found by search. Nothing said his
company was hiring; "autonomous" came from a persona description; and the
ask was the referral ask, sent to the man who would make the hire. Here the
ICP target description ("Head of Product roles at ...") was handed to the
model as "What prompted this message", with a structure that said to open on
"their opening".

Twin of heylead-api's ``app.services.job_search_copy``. The rule sentences
and the ask instructions are identical, word for word; both repos pin the
rule sentences in tests/test_job_search_opener_never_presumes.py.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..textutil import contains_term

NO_PRESUMED_OPENING_RULE = (
    "Never state or imply that the recipient's company is hiring, searching "
    "for someone or has an open role unless a hiring signal is supplied; with "
    'no hiring signal, use a conditional frame such as "If you are growing '
    'the team this year".'
)
NO_PERSONA_WORDS_RULE = (
    "Never copy words from an audience, persona or campaign description into "
    "the message; describe the recipient only with the facts listed about them."
)

NO_HIRING_SIGNAL = "None. Nothing shows that this company is hiring for this role."

HIRING_MANAGER_ASK = (
    "The recipient likely makes this hire: close by asking whether a short "
    "call makes sense, and do not ask who the right person is."
)
REFERRAL_ASK = (
    "The recipient may not make this hire: close by asking who leads hiring "
    "for this role."
)

# Titles that make the hire for a product role: the product executive above
# it, and the founder or chief executive of a company small enough to hire it.
HIRING_MANAGER_TERMS = (
    "chief product officer", "cpo",
    "vp product", "vp of product", "vice president product",
    "vice president of product", "svp product", "svp of product",
    "evp product", "evp of product",
    "chief executive officer", "ceo",
    "founder", "cofounder",
)

# Signal types that say a company is hiring (hiring_surge and the compound
# intents built on it in constants.COMPOUND_INTENT_PATTERNS).
HIRING_SIGNAL_TYPES = frozenset({
    "hiring_surge", "job_posting", "funded_and_hiring", "hiring_and_signaling",
    "new_leader_building", "new_leader_building_v2", "promoted_and_building",
    "growth_mode",
})

_BRIEF_HIRING = re.compile(
    r"(?<![a-z])(?:is hiring|are hiring|hiring for|open role|opening for|"
    r"vacancy|job posting|posted a role|posted the role|is recruiting)(?![a-z])",
    re.IGNORECASE,
)

# (pattern, a conditional frame before it makes it acceptable)
_PRESUMPTIVE: tuple[tuple[re.Pattern[str], bool], ...] = tuple(
    (re.compile(p, re.IGNORECASE), conditional_ok) for p, conditional_ok in (
        (r"\bon your search for\b", False),
        (r"\byour search for\b", False),
        (r"\byour (?:hiring|recruiting) (?:for|of)\b", False),
        (r"\byou(?:'re| are) hiring\b", True),
        (r"\byou(?:'re| are) (?:looking|searching) for (?:a|an|someone)\b", True),
        (r"\b(?:your|the) team (?:is|are) hiring\b", True),
        (r"\byour open (?:role|position|req)\b", False),
        (r"\bthe open (?:role|position)\b", False),
        (r"\byour opening\b", False),
        (r"\byour (?:vacancy|job posting|job ad)\b", False),
    )
)
_CONDITIONAL = re.compile(r"\b(?:if|whether|in case|unless|should)\b", re.IGNORECASE)
_CLAUSE_END = re.compile(r"[.!?;:\n]")
# "Executive Assistant to the CEO", "Office of the CPO": the role after these
# words belongs to someone else (heylead-api textmatch.role_holder_text).
_SUPPORT_OF = re.compile(
    r"(?<![0-9a-z])(?:to|office\s+of)\s+(?:the\s+)?(?=[0-9a-z])", re.IGNORECASE,
)

PRESUMED_OPENING_REASON = "presumed_opening"


class PresumedOpening(str):
    """A refusal reason a caller can branch on (api: GuardrailError)."""

    reason: str
    tokens: tuple[str, ...]

    def __new__(cls, text: str, *, reason: str, tokens: tuple[str, ...] = ()):
        obj = super().__new__(cls, text)
        obj.reason = reason
        obj.tokens = tuple(tokens)
        return obj


def _norm_title(title: Any) -> str:
    text = str(title or "")
    match = _SUPPORT_OF.search(text)
    text = (text[:match.start()] if match else text).lower()
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text).split())


def is_hiring_manager_title(title: Any) -> bool:
    """Whether a reader with this title makes the hire for a product role.

    Whole-term matching (``contains_term``), never substring ``in``.
    """
    norm = _norm_title(title)
    return any(contains_term(norm, term) for term in HIRING_MANAGER_TERMS)


def ask_instruction_for(title: Any) -> str:
    """The close for this reader: a short call if they hire, else who does."""
    return HIRING_MANAGER_ASK if is_hiring_manager_title(title) else REFERRAL_ASK


def _signal_text(prospect_analysis: Any) -> str:
    analysis = prospect_analysis if isinstance(prospect_analysis, Mapping) else {}
    sig = analysis.get("signal_context")
    if not isinstance(sig, Mapping):
        return ""
    kind = str(sig.get("signal_type") or "").strip().lower()
    intent = str(sig.get("intent") or "").strip().lower()
    if kind not in HIRING_SIGNAL_TYPES and intent != "hiring":
        return ""
    for key in ("engagement_hook", "signal_summary"):
        value = sig.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return f"A {kind or intent} signal was recorded for this company."


def _brief_names_this_company_hiring(prospect: Any, campaign_context: Any) -> str:
    """The brief, when it says THIS reader's company is hiring."""
    row = prospect if isinstance(prospect, Mapping) else {}
    ctx = campaign_context if isinstance(campaign_context, Mapping) else {}
    company = str(row.get("company") or "").strip()
    brief = str(ctx.get("project_brief") or "").strip()
    if not company or company.lower() == "their company" or not brief:
        return ""
    if contains_term(brief, company) and _BRIEF_HIRING.search(brief):
        return brief[:300]
    return ""


def hiring_signal_for(
    prospect: Any, campaign_context: Any, prospect_analysis: Any,
) -> str:
    """Evidence that this reader's company is hiring, or "" when there is none.

    A hiring signal recorded for the person, or a brief that names the
    reader's own company as hiring. A search result is not evidence: the
    target description says whom the sender looked for.
    """
    return _signal_text(prospect_analysis) or _brief_names_this_company_hiring(
        prospect, campaign_context,
    )


def presumptive_hiring_claims(text: str) -> list[str]:
    """The phrases in ``text`` that presume the reader's company is hiring."""
    body = (text or "").replace("’", "'").replace("‘", "'")
    found: list[str] = []
    for pattern, conditional_ok in _PRESUMPTIVE:
        for match in pattern.finditer(body):
            if conditional_ok:
                clause = _CLAUSE_END.split(body[: match.start()])[-1]
                if _CONDITIONAL.search(clause):
                    continue
            phrase = match.group(0).lower()
            if phrase not in found:
                found.append(phrase)
    return found


def job_search_presumption_error(text: str, hiring_signal: str) -> PresumedOpening | None:
    """Refuse a draft that presumes an opening no signal supports."""
    if (hiring_signal or "").strip():
        return None
    found = presumptive_hiring_claims(text)
    if not found:
        return None
    return PresumedOpening(
        "Presumes the reader's company is hiring with no hiring signal: "
        + ", ".join(found),
        reason=PRESUMED_OPENING_REASON,
        tokens=tuple(found),
    )


def presumption_correction(error: Any) -> str:
    """The instruction for the one regeneration after a refused draft."""
    tokens = ", ".join(getattr(error, "tokens", ()) or ()) or "a hiring claim"
    return (
        f'Your previous draft said "{tokens}". Nothing shows that this '
        "company is hiring. Write the message again without claiming or "
        "implying an opening, a search or a hiring process; use a conditional "
        'frame such as "If you are growing the team this year".'
    )


def job_search_trigger(campaign_config: Any, campaign_ctx: Any, analysis: Any) -> str:
    """What prompted a job-search message: a signal hook, then the brief.

    Never the ICP target description: it says whom the sender searched for
    ("Head of Product roles at ..."), and handed over as the trigger it read
    as the reader's own opening. Mirrors heylead-api's _job_search_trigger.
    """
    parts: list[str] = []
    sig = (analysis or {}).get("signal_context") if isinstance(analysis, Mapping) else None
    if isinstance(sig, Mapping) and sig.get("engagement_hook"):
        parts.append(str(sig["engagement_hook"]).strip())
    cfg = campaign_config if isinstance(campaign_config, Mapping) else {}
    ctx = campaign_ctx if isinstance(campaign_ctx, Mapping) else {}
    brief = str(ctx.get("project_brief") or cfg.get("project_brief") or "").strip()
    hook = str(cfg.get("relevance_hook") or "").strip()
    if brief or hook:
        parts.append(brief or hook)
    return "\n\n".join(p for p in parts if p) or (
        "Nothing specific is known about an opening. Do not name a role."
    )
