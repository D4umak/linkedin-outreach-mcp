"""Small text helpers shared across the generators."""

from __future__ import annotations

import re

DEFAULT_GREETING = "there"

# Not whitespace to str.strip() or str.split(), so a name made only of these
# stays truthy and survives every guard shaped like `if name:`. They arrive
# inside real LinkedIn names — see first_name() for where from.
_ZERO_WIDTH = ("\u200b", "\ufeff")


# Emoji, flags and pictographs a LinkedIn display name carries (8 Oct 2026).
_PICTO = ("\U0001F000-\U0001FAFF☀-➿⬀-⯿⌀-⏿"
          "■-◿〰〽㊗㊙")
_WITH = "‍︀-️\U0001F3FB-\U0001F3FF\U000E0020-\U000E007F"
_PICTOGRAPHS = re.compile(rf"(?:[0-9#*]️?⃣|[{_PICTO}](?:[{_WITH}]|[{_PICTO}])*)+")


# A display name's tail after a spaced separator is a headline, not the name:
# "Sam | Founder", "Ana · Head of Growth".
_NAME_TAIL = re.compile(r"\s[|•·—–]\s")


# What a source writes when it has no name (api #5650).
_PLACEHOLDERS = frozenset({"unknown", "linkedin member", "unnamed lead", "someone"})


# A word that marks an account named after a company, not a person (api #5650).
_COMPANY_WORDS = frozenset({
    "ltd", "ltd.", "llc", "inc", "inc.", "gmbh", "corp", "corp.", "plc", "llp", "pvt", "limited",
    "solutions", "technologies", "technology", "consulting", "agency", "labs", "group", "studio", "ventures",
})


# Leading honorifics to strip from a name (api #5650).
_HONORIFICS = frozenset({
    "dr", "mr", "mrs", "ms", "miss", "mx", "prof", "sir", "dame", "lord", "lady",
    "rev", "fr", "herr", "frau", "mme", "mlle",
})


def _clean_name(name: str | None) -> str:
    """Pictographs and zero-width characters taken out, spaces closed up (api #5650)."""
    text = str(name or "")
    for char in _ZERO_WIDTH:
        text = text.replace(char, "")
    return " ".join(_PICTOGRAPHS.sub(" ", text).split())


def first_name(name: str | None, fallback: str = DEFAULT_GREETING) -> str:
    """The greeting's name, or *fallback* when there is none (api #5650).

    Enforces the shared 12-step extraction rule: strips titles, company words,
    pictographs and separators, and refuses acronyms, placeholders, or strings
    shaped like surnames or email addresses.
    """
    cleaned = _clean_name(name)
    if not cleaned or cleaned.lower() in _PLACEHOLDERS:
        return fallback

    said = _NAME_TAIL.split(cleaned, 1)[0].strip(" |•·-–—,;:")
    if not said:
        return fallback

    words = said.split()
    if not words:
        return fallback

    # Step 4: Refuse company names across ALL words.
    if any(w.lower().strip(".,;:()") in _COMPANY_WORDS for w in words):
        return fallback

    # Step 5: Comma-first or credential string ("Doe, Jane", "Jane, PhD").
    if words[0].endswith(","):
        return fallback

    # Step 6: Strip leading honorifics.
    stripped = False
    while words and words[0].lower().rstrip(".") in _HONORIFICS:
        words.pop(0)
        stripped = True

    # Step 7 & 8: Surname-only guard and comma-terminated first tokens.
    if not words or (stripped and len(words) < 2):
        return fallback
    if words[0].endswith(","):
        return fallback

    # Step 9 & 10: Clean trailing punctuation and refuse bad characters.
    first = words[0].rstrip(",.")
    if not first or "@" in first or "_" in first or any(ch.isdigit() for ch in first):
        return fallback

    # Step 11: Refuse if < 2 alpha or all-caps (acronyms or surname-first).
    if sum(ch.isalpha() for ch in first) < 2 or first.isupper():
        return fallback

    return first


def full_name(name: str | None, fallback: str = DEFAULT_GREETING) -> str:
    """The whole name, spacing normalised, or *fallback* when nothing shows.

    Strips pictographs and zero-width characters (api #5650).
    """
    text = _clean_name(name)
    return text if text and text.lower() not in _PLACEHOLDERS else fallback


# ── Job-title term matching ──

# Compiled once per term: these run over every prospect of every search page.
_TERM_PATTERNS: dict[str, re.Pattern[str]] = {}


def _term_pattern(term: str) -> re.Pattern[str] | None:
    cached = _TERM_PATTERNS.get(term)
    if cached is not None:
        return cached
    # "sr " and "jr." are written with their own boundary baked in; strip it
    # so the boundary below does that job instead — otherwise "sr " could
    # never match "Sr Engineer", whose next character is a letter.
    cleaned = term.strip().strip(".").strip()
    if not cleaned:
        return None
    pattern = re.compile(
        rf"(?<![0-9a-z]){re.escape(cleaned)}(?![0-9a-z])", re.IGNORECASE,
    )
    _TERM_PATTERNS[term] = pattern
    return pattern


def contains_term(text: str | None, term: str | None) -> bool:
    """True when *term* occurs in *text* as whole words.

    A plain ``term in text`` made "cto" match "successfa(cto)rs" and
    "dire(cto)r", so every Director on LinkedIn scored a perfect CTO title
    match and cleared the campaign fit gate (6 Sep 2026). Same shape put "vp"
    inside "vpn" and "lead" inside "leadership".

    Boundaries are alphanumeric-only, so real titles still match through
    punctuation and inside longer strings: "CTO/Co-Founder", "Sr. Engineer",
    "Global Head of Product, EMEA".
    """
    if not text or not term:
        return False
    pattern = _term_pattern(term)
    return bool(pattern and pattern.search(text))


# Words that say nothing about a person: function words, auxiliaries and the
# filler of an ICP sentence. The same set is in the api's
# app/services/textmatch.py; keep the two alike.
STOPWORDS = frozenset({
    "a", "about", "above", "across", "after", "again", "all", "also", "am", "an", "and",
    "any", "are", "as", "at", "be", "been", "before", "being", "between", "both", "but",
    "by", "can", "could", "did", "do", "does", "doing", "done", "each", "either", "etc",
    "every", "for", "from", "get", "gets", "got", "had", "has", "have", "having", "he",
    "her", "here", "hers", "him", "his", "how", "i", "if", "in", "into", "is", "it", "its",
    "just", "less", "like", "may", "me", "might", "more", "most", "much", "must", "my",
    "need", "needs", "no", "nor", "not", "now", "of", "off", "on", "once", "one", "only",
    "onto", "or", "other", "our", "ours", "out", "over", "own", "per", "same", "she",
    "should", "so", "some", "such", "than", "that", "the", "their", "them", "then", "there",
    "these", "they", "this", "those", "through", "to", "too", "under", "until", "up", "us",
    "use", "very", "via", "want", "wants", "was", "we", "were", "what", "when", "where",
    "which", "while", "who", "whom", "why", "will", "with", "within", "without", "would",
    "you", "your", "yours",
})

# A term needs this many letters or digits to say anything on its own
# ("KPI", "B2B" and "SaaS" do; "at", "UA" and "IT" do not).
MIN_EVIDENCE_CHARS = 3


def is_evidence_term(term: str | None) -> bool:
    """True when *term* can stand as evidence about a person: at least
    MIN_EVIDENCE_CHARS letters or digits, not only digits, and not made only
    of stopwords.

    "about:at" was most of production's evidence: an ICP sentence's "at"
    became an about term, and "at" is in nearly every headline
    (heylead-api#1809, 28 Sep 2026).
    """
    words = re.findall(r"[0-9a-z]+", str(term or "").lower())
    if not words or all(w in STOPWORDS for w in words):
        return False
    body = "".join(words)
    return len(body) >= MIN_EVIDENCE_CHARS and not body.isdigit()


def evidence_hit_is_meaningful(hit: str | None) -> bool:
    """An enrolment evidence hit ("field:value", or a bare term from before
    that form) whose value is evidence."""
    text = str(hit or "")
    _field, sep, value = text.partition(":")
    return is_evidence_term(value if sep else text)
