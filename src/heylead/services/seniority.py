"""One seniority vocabulary for the whole product.

Three scorers used to read seniority three different ways and none of them
agreed with what the ICP prompts told the LLM to write. The prompts asked for
``owner/partner, cxo, vice_president, director, experienced_manager,
entry_level_manager, strategic, senior, entry_level, in_training`` while every
consumer keyed on ``SENIORITY_KEYWORDS`` — ``owner, cxo, vp, director,
manager, senior, entry``. An ICP that said ``vice_president`` therefore
matched nothing: the Classic post-filter in ``create_campaign`` dropped every
prospect it saw, and ``_score_seniority_match`` could not place the level on
its ladder and scored 0.2 for a perfect VP.

This module is the single map. Its twin lives at ``app/services/seniority.py``
in heylead-api; the keyword table and the alias table are byte-identical on
both sides and ``tests/fixtures/seniority_cases.json`` (copied verbatim into
both repos) pins them.

Matching is always whole-word through :func:`heylead.textutil.contains_term`.
Substring matching on titles is the bug family that made "cto" match
"dire(cto)r" and "vp" match "vpn".
"""

from __future__ import annotations

from ..constants import SENIORITY_KEYWORDS
from ..textutil import contains_term

__all__ = [
    "SENIORITY_KEYWORDS",
    "SENIORITY_ORDER",
    "DECISION_MAKER_LEVELS",
    "normalize_seniority",
    "normalize_seniority_list",
    "infer_seniority_level",
    "states_seniority",
    "is_decision_maker",
    "constrain_include",
    "apply_seniority_policy",
    "to_unipile_seniority",
]

# Lowest to highest. The ladder distance in the scorers is measured on this.
SENIORITY_ORDER: tuple[str, ...] = (
    "entry", "senior", "manager", "director", "vp", "cxo", "owner",
)

# Levels that hold budget authority. 9 Sep 2026 asked for the
# product to target decision makers; this is what "decision maker" means.
DECISION_MAKER_LEVELS: frozenset[str] = frozenset({
    "owner", "cxo", "vp", "director",
})

# Everything a caller might hand us that is not already a canonical key:
# the ICP prompts' LinkedIn vocabulary, and the free text a human writes.
# Copied verbatim into heylead-api `app/services/seniority.py`.
_SENIORITY_ALIASES: dict[str, str] = {
    # LinkedIn / ICP-prompt vocabulary
    "owner/partner": "owner",
    "partner": "owner",
    "vice president": "vp",
    "experienced manager": "manager",
    "entry level manager": "manager",
    "strategic": "manager",
    "entry level": "entry",
    "in training": "entry",
    # Free text people actually type
    "c level": "cxo",
    "c suite": "cxo",
    "clevel": "cxo",
    "csuite": "cxo",
    "executive": "cxo",
    "exec": "cxo",
    "founder": "owner",
    "co founder": "owner",
    "cofounder": "owner",
    "head": "director",
    "management": "manager",
    "individual contributor": "entry",
    "ic": "entry",
    "junior": "entry",
}


def _canon(value: str) -> str:
    """Lowercase, and treat ``_`` / ``-`` as spaces so one alias covers both."""
    text = str(value or "").strip().lower()
    text = text.replace("_", " ").replace("-", " ")
    return " ".join(text.split())


def normalize_seniority(value: str | None) -> str | None:
    """Map any spelling of a level onto a canonical key, or ``None``.

    Accepts canonical keys, the ICP prompts' LinkedIn vocabulary
    (``vice_president``, ``entry_level``, …) and free text ("Head of", "VP of
    Sales", "C-level"). ``None`` means "this names no level", which every
    caller must treat as *unknown*, never as *low*.
    """
    text = _canon(value)
    if not text:
        return None
    if text in SENIORITY_KEYWORDS:
        return text
    alias = _SENIORITY_ALIASES.get(text)
    if alias:
        return alias
    return infer_seniority_level(text)


def normalize_seniority_list(values: object) -> list[str]:
    """Normalize a list (or comma string) of levels, dropping unreadable ones."""
    if isinstance(values, str):
        raw = [v for v in values.split(",")]
    elif isinstance(values, (list, tuple, set, frozenset)):
        raw = list(values)
    else:
        return []
    out: list[str] = []
    for item in raw:
        key = normalize_seniority(item if isinstance(item, str) else str(item))
        if key and key not in out:
            out.append(key)
    return out


def infer_seniority_level(title: str | None) -> str | None:
    """The level a title *states*, or ``None`` when it states none.

    Unlike ``revenue_estimator.infer_seniority`` this never guesses: a title
    with no level word returns ``None``. Guessing "manager" is what handed
    0.15 of free scoring weight to every unreadable headline.
    """
    text = str(title or "").strip().lower()
    if not text:
        return None
    # "president" is a cxo keyword and it is a whole word inside "vice
    # president", so the highest-first walk classified every VP as a C-level
    # executive — the same shape as "cto" inside "director", one rung up. When
    # "president" is the *only* thing that put a title on the cxo rung and the
    # title also states a VP phrase, it is a VP.
    states_vp = any(contains_term(text, kw) for kw in SENIORITY_KEYWORDS["vp"])
    for level in reversed(SENIORITY_ORDER):
        hits = [kw for kw in SENIORITY_KEYWORDS.get(level, [])
                if contains_term(text, kw)]
        if not hits:
            continue
        if level == "cxo" and states_vp and hits == ["president"]:
            return "vp"
        return level
    return None


def states_seniority(title: str | None) -> bool:
    """True when the title actually names a level rather than being guessed."""
    return infer_seniority_level(title) is not None


def is_decision_maker(title_or_level: str | None) -> bool:
    """True when a title or a level resolves to owner / cxo / vp / director."""
    return normalize_seniority(title_or_level) in DECISION_MAKER_LEVELS


def constrain_include(
    include: object, exclude: object = None, *, decision_makers_only: bool = True,
) -> tuple[list[str], list[str]]:
    """Normalize a persona's seniority and, optionally, force decision makers.

    With ``decision_makers_only`` the include list is intersected with
    :data:`DECISION_MAKER_LEVELS` — an LLM that wrote ``["cxo", "manager"]``
    keeps only ``cxo``, and one that wrote nothing usable gets the full
    decision-maker set rather than an empty filter that would let everyone
    through. Every level outside the include list is added to exclude, so the
    scorers record the drop as `seniority_miss` instead of scoring a ladder.
    """
    inc = normalize_seniority_list(include)
    exc = normalize_seniority_list(exclude)
    if decision_makers_only:
        inc = [lvl for lvl in inc if lvl in DECISION_MAKER_LEVELS]
        if not inc:
            inc = [lvl for lvl in SENIORITY_ORDER if lvl in DECISION_MAKER_LEVELS]
        elif "owner" not in inc:
            # A founder holds the budget whatever rungs the LLM named. The
            # generator listed cxo/vp/director and forgot owner twice on
            # 10 Sep 2026, and the exclude derived below then dropped every
            # technical co-founder from a decision-maker campaign.
            inc.append("owner")
    if inc:
        exc = [lvl for lvl in SENIORITY_ORDER if lvl not in inc]
    return inc, exc


def apply_seniority_policy(result: object, *, decision_makers_only: bool = True) -> None:
    """Rewrite every persona's seniority on an IcpResult, in place.

    The LLM is told to emit canonical keys, but a prompt is a request. This is
    the guarantee: whatever came back, what gets stored is canonical, and with
    ``decision_makers_only`` it names only levels that hold budget authority.
    """
    for icp in getattr(result, "icps", None) or []:
        param = getattr(icp, "seniority", None)
        if param is None:
            continue
        include, exclude = constrain_include(
            getattr(param, "include", None),
            getattr(param, "exclude", None),
            decision_makers_only=decision_makers_only,
        )
        param.include = include
        param.exclude = exclude


# Unipile's Sales Navigator ``seniority.include`` vocabulary, probed live on
# 10 Sep 2026: these ten names answer 200, anything else (``vp``, ``owner``,
# ``CXO``, ``c_suite``, ``partner``) answers 400 invalid_parameters. The ICP
# keeps the canonical keys every scorer reads; only the request is translated.
_UNIPILE_SENIORITY: dict[str, tuple[str, ...]] = {
    "owner": ("owner/partner",),
    "cxo": ("cxo",),
    "vp": ("vice_president",),
    "director": ("director",),
    "manager": ("experienced_manager", "entry_level_manager", "strategic"),
    "senior": ("senior",),
    "entry": ("entry_level", "in_training"),
}
_UNIPILE_SENIORITY_NAMES: frozenset[str] = frozenset(
    name for names in _UNIPILE_SENIORITY.values() for name in names
)


def to_unipile_seniority(values: object) -> list[str]:
    """Canonical seniority keys as the names Unipile's Sales Navigator
    search accepts. Names already in that vocabulary pass through; junk is
    dropped rather than sent; order kept, duplicates removed."""
    if not isinstance(values, (list, tuple, set)):
        values = [values] if values else []
    out: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        if text in _UNIPILE_SENIORITY_NAMES:
            names: tuple[str, ...] = (text,)
        else:
            level = normalize_seniority(text)
            names = _UNIPILE_SENIORITY.get(level or "", ())
        for name in names:
            if name not in out:
                out.append(name)
    return out
