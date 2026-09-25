"""Claims about the author that the author never gave.

25 Sep 2026: a post drafted for Denys opened "Having spoken with over 600
CTOs". He never said it. The house rule against invention was in the system
prompt and the model wrote the count anyway, because a prompt rule is advice
and nothing read the finished post for claims. This file is the reading.

A claim here is a figure (600, 10 years, $2M, 40%, twenty, hundreds) in a
sentence about the author, whose number appears nowhere in what the author
gave: the profile fields, what they told us (the knowledge block) and the
topic. A sentence is about the author when it says I, me, my, we, us or our,
or opens the way a claim about oneself does ("After 10 years in sales,",
"600 calls later,", "Helped 40 teams"). In a profile section (headline,
About) every line is about the author, pronoun or not.

What is not a claim: an opinion, an observation or a question with no
figure in it; a figure the sources hold; a count of the post's own parts
("3 lessons"); a list number; this year or a later one (a forecast); the
number one. Tokens that merely contain a digit (B2B, GPT-4, 24/7, 1:1, Q3,
#1, 9am) are words, not figures.

What it does not see, on purpose: a spelled number below ten ("two exits"),
"a decade", and a claim with no figure at all ("I've coached CTOs for
years"). The prompt rule is what covers those.

Pure data and the standard library, like rules.py: the client carries this
file byte for byte and a digest test on each side proves the two have not
drifted. The model call that removes a claim lives with each repo's LLM
client, not here.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

# ── the prompt rule ──

KNOWN_FACTS_ONLY = (
    "- Claim nothing about {name} that this prompt does not state: no "
    "experience, counts, clients, years, results or events of your own "
    "making. An opinion, an observation or a question needs no source; a "
    "fact about {name} does."
)


def known_facts_rule(name: str) -> str:
    """The rule line for a prompt that writes as *name*."""
    return KNOWN_FACTS_ONLY.format(name=(name or "").strip() or "the author")


# ── the rewrite that removes a claim ──

REWRITE_SYSTEM = (
    "You are the last reader of a draft before it is published under its "
    "author's name. The draft is already about the right thing: your only "
    "job is to take out the claims about the author that nobody gave you.\n\n"
    "Change exactly those claims and nothing else. Keep every other sentence, "
    "the opinion, the question and the voice as they are. Never add a fact, "
    "a name, a number, an experience or a story. If a sentence has nothing "
    "left to say without its claim, delete the sentence.\n\n"
    "Output ONLY the corrected text. No explanation, no quotes, no preamble."
)

REWRITE_PROMPT = """Correct this {channel} draft.

DRAFT
{draft}

CLAIMS NOBODY GAVE YOU
{claims}

The author did not say any of these. They are not in the author's profile,
in what the author has told us, or in the topic. Take each one out. Keep the
point of its sentence when the point stands without it ("Having spoken with
over 600 CTOs, I've learned" becomes "I've learned"), and never swap in a
vaguer claim ("hundreds of CTOs", "countless calls", "years of experience").
{limit}
Return only the corrected draft."""


@dataclass(frozen=True)
class Claim:
    """One figure the sources do not hold, and the sentence carrying it."""

    figure: str
    sentence: str


def rewrite_prompt(draft: str, claims: tuple[Claim, ...], channel: str, max_chars: int = 0) -> str:
    lines = [f'- "{c.figure}" in: {c.sentence}' for c in claims]
    return REWRITE_PROMPT.format(
        channel=channel.replace("_", " "),
        draft=draft,
        claims="\n".join(lines),
        limit=f"\nStay under {max_chars} characters.\n" if max_chars else "",
    )


# ── what the sources say ──

# A profile key whose value is not something the author said about
# themselves. Their past posts are skipped on purpose: the examples block
# tells the model to reuse none of their numbers, because last quarter's
# count is not this morning's fact.
_SKIP_KEYS = frozenset({
    "posts", "recent_posts", "id", "urn", "url", "image", "picture", "photo",
    "avatar", "timestamp", "provider_id", "public_id", "member_urn",
})
_SKIP_SUFFIXES = ("_id", "_url", "_urn", "_at", "_ts")
_ISO_DATE = re.compile(r"\b(\d{4})-\d{1,2}(?:-\d{1,2})?(?:[T ][\d:.]+Z?)?\b")
_URL = re.compile(r"https?://\S+|\bwww\.\S+")


def known_text(*sources: Any) -> str:
    """Every string (and number) in *sources*, as one text to read figures from.

    A source is a string (the topic, the knowledge block), a dict (the
    profile) or a list. Keys that name an id, a link, a timestamp or the
    author's past posts are skipped. A date keeps its year only: "2019-03"
    is a claim about 2019, not about 3.
    """
    parts: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                name = str(key).lower()
                if name in _SKIP_KEYS or name.endswith(_SKIP_SUFFIXES):
                    continue
                walk(inner)
        elif isinstance(value, (list, tuple, set)):
            for inner in value:
                walk(inner)
        elif isinstance(value, bool) or value is None:
            return
        elif isinstance(value, (int, float)):
            if abs(value) < 1e9:  # a count, not an epoch
                parts.append(str(value))
        else:
            parts.append(_ISO_DATE.sub(r"\1", str(value)))

    for source in sources:
        walk(source)
    return "\n".join(p for p in parts if p.strip())


# ── figures ──

_FIGURE = re.compile(
    r"""
    (?<![\w.:/#@{])(?<![A-Za-z]-)        # not part of a word: B2B, GPT-4, 24/7, #1
    [$€£]?
    (?P<int>\d{1,3}(?:,\d{3})+(?!\d)|\d+)
    (?P<frac>\.\d+)?
    (?![/:]\d)                           # 24/7, 1:1, 9:30
    (?:[ \t]?(?P<scale>[kK]|[mM]{1,2}|[bB]n?|hundred|thousand|million|billion)(?![A-Za-z]))?
    (?P<tail>\+|%|[xX](?![A-Za-z]))?
    (?![A-Za-z0-9])                      # 9am, 30s, 2nd, Q3 stay words
    """,
    re.VERBOSE,
)
_SCALE = {
    "hundred": 1e2, "k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6,
    "b": 1e9, "bn": 1e9, "billion": 1e9,
}

_ONES = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9,
}
_TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_WORD_SCALE = {"hundred": 100, "thousand": 1000, "million": 1e6, "billion": 1e9, "dozen": 12}
# A vague plural is a claim of a size: "hundreds of CTOs" is 200 to 999 of them.
_VAGUE = {
    "dozens": (24, 143), "hundreds": (200, 999), "thousands": (2000, 999_999),
    "millions": (2e6, 999e6), "billions": (2e9, 999e9),
}

_ONES_RE = "|".join(k for k in _ONES if k not in ("a", "an"))
_TEENS_RE = "|".join(_TEENS)
_TENS_RE = "|".join(_TENS)
_TENS_COMPOUND = rf"(?:{_TENS_RE})(?:[-\s](?:{_ONES_RE}))?"
_WORDS = re.compile(
    rf"""
    \b(?:
        (?P<lead>a|an|{_ONES_RE}|{_TEENS_RE}|{_TENS_COMPOUND})\s+
        (?P<scale>hundred|thousand|million|billion|dozen)\b(?!s)
      | (?P<vague>{"|".join(_VAGUE)})\b
      | (?P<bare>{_TENS_COMPOUND}|{_TEENS_RE}|hundred|thousand|dozen)\b
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)


@dataclass(frozen=True)
class _Figure:
    start: int
    end: int
    value: float | None  # None for a vague plural
    vague: str = ""
    year: bool = False


def _word_value(phrase: str) -> float:
    words = [w for w in re.split(r"[-\s]+", phrase.lower()) if w]
    total = 0.0
    for w in words:
        total += _ONES.get(w, 0) + _TEENS.get(w, 0) + _TENS.get(w, 0)
    return total


def _figures(text: str) -> list[_Figure]:
    found: list[_Figure] = []
    for m in _FIGURE.finditer(text):
        whole = m.group("int").replace(",", "")
        value = float(whole + (m.group("frac") or ""))
        scale = (m.group("scale") or "").lower()
        value *= _SCALE.get(scale, 1)
        year = (
            len(whole) == 4 and not m.group("frac") and not scale
            and not m.group("tail") and 1900 <= value <= 2100
        )
        found.append(_Figure(m.start(), m.end(), round(value, 6), year=year))
    digits = [(f.start, f.end) for f in found]
    for m in _WORDS.finditer(text):
        # "2 thousand" is one figure, read above; its word is not a second.
        if any(m.start() < end and start < m.end() for start, end in digits):
            continue
        if m.group("vague"):
            found.append(_Figure(m.start(), m.end(), None, vague=m.group("vague").lower()))
        elif m.group("scale"):
            lead = m.group("lead").lower()
            base = 1 if lead in ("a", "an") else _word_value(lead)
            found.append(_Figure(m.start(), m.end(), base * _WORD_SCALE[m.group("scale").lower()]))
        else:
            bare = m.group("bare").lower()
            found.append(_Figure(m.start(), m.end(), _WORD_SCALE.get(bare) or _word_value(bare)))
    return sorted(found, key=lambda f: f.start)


@dataclass(frozen=True)
class Known:
    """The figures the sources hold."""

    values: frozenset[float]
    vague: frozenset[str]

    def holds(self, figure: _Figure) -> bool:
        if figure.value is not None:
            return figure.value in self.values
        if figure.vague in self.vague:
            return True
        low, high = _VAGUE[figure.vague]
        return any(low <= v <= high for v in self.values)


def known(*sources: Any) -> Known:
    """What the author gave, as figures. See known_text for what a source is."""
    text = _URL.sub(" ", known_text(*sources))
    figures = _figures(text)
    return Known(
        values=frozenset(f.value for f in figures if f.value is not None),
        vague=frozenset(f.vague for f in figures if f.vague),
    )


# ── sentences about the author ──

_FIRST_PERSON = re.compile(
    r"(?<![\w'’])(?:I|I['’](?:m|ve|d|ll)|[Mm]e|[Mm]y|[Mm]ine|[Mm]yself|"
    r"[Ww]e|[Ww]e['’](?:re|ve|d|ll)|[Uu]s|[Oo]ur|[Oo]urs|[Oo]urselves)(?![\w'’])"
)
# How a sentence about oneself opens when it drops the pronoun: a phrase
# that counts the author's own experience, or a verb in the past tense with
# no subject in front of it ("Helped 40 teams", "Spent 10 years").
_OPENER = re.compile(r"^\W*(?:after|having|across|over|through|since)\b", re.IGNORECASE)
_LATER = re.compile(r"\b(?:in|later)\W*$", re.IGNORECASE)
_FIRST_WORD = re.compile(r"^\W*([A-Za-z]+)\b")
_PAST = frozenset({
    "spent", "built", "led", "ran", "grew", "sold", "made", "won", "lost",
    "met", "spoke", "wrote", "took", "taught", "drove", "cut", "sent", "got",
    "did", "saw", "went", "brought", "bought", "kept", "paid", "held", "told",
    "found", "gave", "hit", "set", "shipped", "hired",
})
_NOT_PAST = frozenset({
    "based", "indeed", "need", "needed", "speed", "seed", "feed", "exceed",
    "proceed", "succeed", "embed", "shed", "tired", "bored", "interested",
    "excited", "scared", "worried", "compared", "related", "used", "red",
})
# A count of the post's own parts, not of anything the author did.
_STRUCTURE = frozenset({
    "thing", "things", "lesson", "lessons", "way", "ways", "reason", "reasons",
    "tip", "tips", "mistake", "mistakes", "question", "questions", "step",
    "steps", "sign", "signs", "rule", "rules", "takeaway", "takeaways", "idea",
    "ideas", "truth", "truths", "habit", "habits", "principle", "principles",
    "pattern", "patterns", "myth", "myths", "trap", "traps", "word", "words",
    "part", "parts", "point", "points", "stage", "stages", "phase", "phases",
    "type", "types", "kind", "kinds", "option", "options", "secret", "secrets",
})
_LIST_MARK = re.compile(r"^\s*(?:[-*•·▪►]|\d{1,2}[.)/])\s+")
# A sentence ends at . ! or ? (with any closing quote) before a space; a
# headline's parts end at a spaced | or ·.
_BREAK = re.compile(r"((?:(?<=[.!?])|(?<=[.!?][\"'”’)]))\s+|\s+[|·•]\s+)")
_NEXT_WORDS = re.compile(r"[\s-]*([A-Za-z'’]+)(?:\s+([A-Za-z'’]+))?")
# Words that say nothing about what a figure counts ("$2M last year").
_FILLER = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "per",
    "last", "this", "more", "less", "than", "into", "from", "with",
})


def _this_year(year: int | None) -> int:
    return year if year is not None else time.gmtime().tm_year


def _about_the_author(sentence: str, figure: _Figure) -> bool:
    if _FIRST_PERSON.search(sentence):
        return True
    lead = re.split(r"[,:;](?:\s|$)", sentence, maxsplit=1)[0]
    if figure.start >= len(lead):
        return False
    if _OPENER.match(lead) or _LATER.search(lead):
        return True
    first = _FIRST_WORD.match(lead)
    word = first.group(1).lower() if first else ""
    return word not in _NOT_PAST and (word in _PAST or (len(word) > 4 and word.endswith("ed")))


def _is_structure(sentence: str, figure: _Figure) -> bool:
    m = _NEXT_WORDS.match(sentence, figure.end)
    if not m:
        return False
    return any((w or "").lower() in _STRUCTURE for w in m.groups())


def _claims_in(sentence: str, facts: Known, about_author: bool, year: int) -> list[Claim]:
    body = _URL.sub(lambda m: " " * len(m.group(0)), sentence)
    mark = _LIST_MARK.match(body)
    if mark:
        body = " " * mark.end() + body[mark.end():]
    out: list[Claim] = []
    for fig in _figures(body):
        if fig.value is not None and fig.value in (0, 1):
            continue
        if fig.year and fig.value is not None and fig.value >= year:
            continue
        if facts.holds(fig) or _is_structure(body, fig):
            continue
        if not about_author and not _about_the_author(body, fig):
            continue
        tail = _NEXT_WORDS.match(body, fig.end)
        noun = (
            f" {tail.group(1)}"
            if tail and fig.value is not None and not fig.year
            and tail.group(1).lower() not in _FILLER else ""
        )
        figure = (body[fig.start:fig.end] + noun).strip()
        out.append(Claim(figure=figure, sentence=sentence.strip()))
    return out


def _split(line: str) -> tuple[list[str], list[str]]:
    parts = _BREAK.split(line)
    return parts[0::2], parts[1::2]


def find(
    draft: str,
    facts: Known,
    *,
    about_author: bool = False,
    year: int | None = None,
) -> tuple[Claim, ...]:
    """Every claim in *draft* whose figure *facts* does not hold, in order.

    ``about_author`` reads every sentence as one about the author, for a
    profile section. ``year`` is this year (a later year is a forecast).
    """
    this_year = _this_year(year)
    out: list[Claim] = []
    for line in (draft or "").split("\n"):
        sentences, _ = _split(line)
        for sentence in sentences:
            out.extend(_claims_in(sentence, facts, about_author, this_year))
    return tuple(out)


def drop(
    draft: str,
    facts: Known,
    *,
    about_author: bool = False,
    year: int | None = None,
) -> str:
    """*draft* without the sentences that carry a claim. Deletion only.

    This runs after a model has declined to remove the claims once, so it
    must not be able to make the text say anything new: whole sentences go,
    nothing is reworded, and a line left with nothing on it goes too.
    """
    this_year = _this_year(year)
    kept_lines: list[str] = []
    for line in (draft or "").split("\n"):
        sentences, breaks = _split(line)
        keep = [not _claims_in(s, facts, about_author, this_year) for s in sentences]
        if all(keep):
            kept_lines.append(line)
            continue
        rebuilt = ""
        for i, sentence in enumerate(sentences):
            if not keep[i]:
                continue
            if rebuilt:
                rebuilt += breaks[i - 1] if i - 1 < len(breaks) else " "
            rebuilt += sentence
        mark = _LIST_MARK.match(line)
        if mark and not keep[0] and rebuilt.strip():
            rebuilt = mark.group(0) + rebuilt
        if rebuilt.strip() and not _LIST_MARK.fullmatch(rebuilt + " "):
            kept_lines.append(rebuilt.rstrip())
    text = "\n".join(kept_lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
