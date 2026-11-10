"""Compile targeting requests into recall queries + full-profile evidence.

Country/diaspora ties use gazetteer JSON (school, language, worked-in).
Interests use distinctive about/volunteer/skill terms. Never infer ethnicity
from names. Never AND product stems with identity tokens in one query.
"""

from __future__ import annotations

from .campaign_naming import cut_at_word

import json
import logging
import re
from pathlib import Path
from typing import Any, Collection

from ..textutil import is_evidence_term

logger = logging.getLogger(__name__)

COMPILER_VERSION = 3
MAX_RECALL_QUERIES = 4
IDENTITY_GEO_MIN_CARDS = 8

_HIRE_RE = re.compile(r"(?<!\w)hir(?:e|ing)\b", re.I)
_ROLE_STOPWORDS = {"of", "the", "and", "a", "an", "for", "in", "to", "engineering"}
_HIRING_ROLE_NEEDLES = [
    "founder", "co-founder", "cofounder", "ceo", "chief executive",
    "head of", "director", "hiring manager", "engineering manager", "chief",
]
_TITLE_EXPANSIONS: dict[str, list[str]] = {
    "cto": ["cto", "chief technology officer", "chief technology"],
    "chief technology officer": ["cto", "chief technology officer", "chief technology"],
    # The backend's table (heylead-api app/services/profile_signals.py), which
    # this copy had fallen behind: "ceo" was the C-level both missed, and an
    # ICP written "VP Engineering" got no expansion at all.
    "ceo": ["ceo", "chief executive officer"],
    "chief executive officer": ["ceo", "chief executive officer"],
    # The security and information chiefs, which headlines abbreviate too
    # (23 Sep 2026: "CISO | ...", "CIO @ALTEN" filed icp_mismatch against an
    # ICP naming them in full). "CIO" also means Chief Investment Officer on
    # LinkedIn; the fit check reads the profile before anyone is enrolled.
    "ciso": ["ciso", "chief information security officer"],
    "chief information security officer": ["ciso", "chief information security officer"],
    "cio": ["cio", "chief information officer"],
    "chief information officer": ["cio", "chief information officer"],
    "cpo": ["cpo", "chief product officer"],
    "cfo": ["cfo", "chief financial officer"],
    "coo": ["coo", "chief operating officer"],
    "vp of engineering": [
        "vp of engineering", "vice president of engineering", "vp engineering",
    ],
    "vp engineering": [
        "vp engineering", "vp of engineering", "vice president of engineering",
    ],
    "vice president of engineering": [
        "vp of engineering", "vice president of engineering",
    ],
}

_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "country_signals"

_INTEREST_HINTS: dict[str, list[str]] = {
    "car": ["classic car", "Porsche", "automotive enthusiast", "car club"],
    "cars": ["classic car", "Porsche", "automotive enthusiast", "car club"],
    "esoteric": ["esoteric", "hermetic", "occult studies", "theosophy"],
    "climb": ["rock climbing", "alpinism", "climbing gym"],
    "climber": ["rock climbing", "alpinism", "climbing gym"],
    "climbers": ["rock climbing", "alpinism", "climbing gym"],
}

_GAZETTEER_CACHE: dict[str, dict[str, Any]] | None = None


# An interest is named, not inferred (#2371): a hint key counts only as the
# subject of a request, followed by one of these words ("car lovers",
# "esoteric people", "car club"), or when the key is itself the person
# ("climbers"). "Sales directors at car dealerships" and "help brands climb
# the rankings" name no interest; one hint word in a business brief would
# otherwise turn it into Porsche evidence (qa review of #2371).
_INTEREST_REQUEST_NOUNS = (
    "lover", "lovers", "enthusiast", "enthusiasts", "fan", "fans", "people",
    "community", "communities", "collector", "collectors", "hobbyist",
    "hobbyists", "club", "clubs",
)
_INTEREST_PERSON_KEYS = frozenset({"climber", "climbers"})


def _named_interests(text: str) -> list[str]:
    """The _INTEREST_HINTS keys *text* names as an interest request, in table order."""
    blob = _norm(text)
    nouns = "|".join(re.escape(n) for n in _INTEREST_REQUEST_NOUNS)
    named: list[str] = []
    for key in _INTEREST_HINTS:
        k = re.escape(key)
        if key in _INTEREST_PERSON_KEYS:
            pattern = rf"(?<!\w){k}(?!\w)"
        else:
            pattern = rf"(?<!\w){k}\s+(?:{nouns})(?!\w)"
        if re.search(pattern, blob):
            named.append(key)
    return named


def _load_all_gazetteers() -> dict[str, dict[str, Any]]:
    global _GAZETTEER_CACHE
    if _GAZETTEER_CACHE is not None:
        return _GAZETTEER_CACHE
    out: dict[str, dict[str, Any]] = {}
    if _DATA_DIR.is_dir():
        for path in sorted(_DATA_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("profile_signals: skip %s: %s", path.name, e)
                continue
            code = str(data.get("code") or path.stem).lower()
            data["code"] = code
            out[code] = data
    _GAZETTEER_CACHE = out
    return out


def list_country_gazetteers() -> list[str]:
    return sorted(_load_all_gazetteers())


def load_country_gazetteer(code: str) -> dict[str, Any] | None:
    return _load_all_gazetteers().get(code.lower())


def _norm(text: str) -> str:
    return (text or "").strip().lower()


def _says(blob: str, phrase: str) -> bool:
    """*phrase* appears in *blob* as whole words (both already normalised)."""
    return bool(phrase) and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", blob) is not None


def is_place_in_a_list(
    gaz: dict[str, Any], text: str, location_names: list[str] | None,
) -> bool:
    """True when *text* names this country as one of the places searched.

    The gazetteer reads a country as who the people are (a country tie: a
    school, a language, an employer there). A brief that lists where it looks,
    "Regions: Pakistan, India, Malaysia, UK, Nigeria, Kenya", says nothing
    about who anyone is, and picking the longest country name out of that list
    planned campaign 6b9f51d6 as a Nigerian-identity campaign (#2729).

    So: matched only by its own name (or a name the campaign's locations use),
    never by a word for its people ("Nigerian", "Ukrainians"); one of the
    campaign's target locations; and named next to another target location.
    A single country, or a country the campaign does not search in, is read as
    before.
    """
    blob = _norm(text)
    locations = [_norm(str(n)) for n in (location_names or []) if n and _norm(str(n))]
    if not blob or not locations:
        return False
    label = _norm(gaz.get("label", ""))
    names = {_norm(a) for a in [gaz.get("label", "")] + list(gaz.get("aliases") or []) if a}
    place_words = {label} | {n for n in names if n in locations}
    said = [n for n in names if _says(blob, n)]
    if not said or any(n not in place_words for n in said):
        return False

    def _is_this_country(location: str) -> bool:
        return any(_says(location, n) for n in names)

    if not any(_is_this_country(loc) for loc in locations):
        return False
    return any(_says(blob, loc) for loc in locations if not _is_this_country(loc))


def _names(gaz: dict[str, Any]) -> list[str]:
    return [_norm(a) for a in [gaz.get("label", "")] + list(gaz.get("aliases") or []) if a]


def language_is_spoken_everywhere(
    gaz: dict[str, Any], location_names: list[str] | None,
) -> bool:
    """Every place searched is one where this country's language is spoken.

    The gazetteer's ``spoken_in`` lists them (German: Germany, Austria,
    Switzerland, Liechtenstein, Luxembourg); without one, only the country
    itself. A brief that also searches elsewhere ("German-speaking engineers
    in the UK and Germany") still needs the tie for that half (api #5290 review).
    """
    places = [_norm(p) for p in (gaz.get("spoken_in") or [gaz.get("label", "")]) if p]
    names = set(_names(gaz)) | set(places)
    locations = [_norm(str(loc)) for loc in (location_names or []) if loc and _norm(str(loc))]
    return bool(locations) and all(any(_says(loc, n) for n in names) for loc in locations)


def is_searched_country(
    gaz: dict[str, Any], text: str, location_names: list[str] | None,
) -> bool:
    """True when *text* asks for this country's language where it is spoken.

    A country tie is for people away from the country ("Ukrainians in
    Germany", "Nigerian founders in the UK and Nigeria"). A brief that asks
    for a language ("German-speaking", "French speaking") and searches only
    places where it is spoken names people the place already describes:
    a brief for "German-speaking … in Germany, Austria and
    Switzerland" was planned as a Germany tie, so a Head of Marketing in
    Zurich or Bavaria whose profile named no German school, language or city
    was turned down (api #5290).
    """
    blob = _norm(text)
    if not any(
        re.search(rf"(?<![\w-]){re.escape(n)}[\s-]+speaking(?!\w)", blob)
        for n in _names(gaz)
    ):
        return False
    return language_is_spoken_everywhere(gaz, location_names)


def _match_gazetteer(
    text: str, location_names: list[str] | None = None,
) -> dict[str, Any] | None:
    blob = _norm(text)
    if not blob:
        return None
    # Longer aliases first so "ukrainian" wins over a short substring later.
    scored: list[tuple[int, dict[str, Any]]] = []
    for gaz in _load_all_gazetteers().values():
        if is_place_in_a_list(gaz, blob, location_names):
            continue
        if is_searched_country(gaz, blob, location_names):
            continue
        aliases = [gaz.get("label", "")] + list(gaz.get("aliases") or [])
        for alias in aliases:
            a = _norm(alias)
            if a and re.search(rf"(?<!\w){re.escape(a)}(?!\w)", blob):
                scored.append((len(a), gaz))
                break
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def _titles_or(titles: list[str] | None) -> str:
    return " OR ".join(t for t in (titles or [])[:3] if t)


def _one_token(keywords: str) -> str:
    return (keywords or "").split(",")[0].strip()


def _spec_from_gazetteer(
    gaz: dict[str, Any],
    *,
    titles: list[str] | None = None,
    location_codes: list[str] | None = None,
) -> dict[str, Any]:
    title_or = _titles_or(titles)
    locs = list(location_codes or [])
    queries: list[dict[str, Any]] = []
    for token in gaz.get("recall_tokens") or []:
        kw = _one_token(str(token.get("keywords") or ""))
        if not kw:
            continue
        why = token.get("why") or "signal"
        queries.append({
            "keywords": kw,
            "title_or": "",
            "location_codes": locs,
            "why": why,
        })
        if len(queries) >= MAX_RECALL_QUERIES:
            break
    schools = [s.get("stem") for s in (gaz.get("schools") or []) if s.get("stem")]
    school_aliases: list[str] = []
    for s in gaz.get("schools") or []:
        school_aliases.append(s.get("stem") or "")
        school_aliases.extend(s.get("aliases") or [])
    return {
        "kind": "country_tie",
        "label": gaz.get("label") or gaz.get("code") or "",
        "code": gaz.get("code") or "",
        "recall_queries": queries,
        "evidence": {
            "schools": [a for a in school_aliases if a],
            "languages": list(gaz.get("languages") or []),
            "experience_places": list(gaz.get("experience_places") or []),
            "experience_companies": list(gaz.get("experience_companies") or []),
            "about_terms": list(gaz.get("weak_about") or []),
        },
        "keep_rule": "hard_signal",
        "explain": [
            f"Country tie: {gaz.get('label')}. Keep if school OR language OR worked-in-country.",
            "Headline-only identity words are supporting, not enough to keep.",
        ],
    }


def _interest_terms_from_text(text: str) -> list[str]:
    """The curated evidence terms of each interest *text* names (_INTEREST_HINTS),
    or [] when it names none.

    One gate (_named_interests) for every path: compile_profile_signals
    and attach_signals_to_icp_result. A sentence is never
    split into evidence (#2371, 7 Oct 2026). Until then,
    a text that named no interest fell back to its first 15 words that were
    not stopwords, and every caller that compiles a campaign's own brief
    (campaign_identity_spec) skipped the interest guard, so nearly every
    sell brief became an "interest" campaign: campaign 6caa0f6a's hotel brief
    yielded independent, owners, capital, asset, industry ..., and one of
    those words anywhere in a profile kept the row, added 2.5 of 3.0 to its
    fit, waived the title-only cap and ran one-word LinkedIn searches. 858
    of 3,288 contacts in 30 days rested on one such word. An interest is
    named (a hint) or given (``terms=``); it is never read out of prose.
    """
    terms: list[str] = []
    for key in _named_interests(text):
        for h in _INTEREST_HINTS[key]:
            if h not in terms:
                terms.append(h)
    return terms


def compile_interest_signals(
    text: str,
    *,
    titles: list[str] | None = None,
    location_codes: list[str] | None = None,
    terms: list[str] | None = None,
) -> dict[str, Any]:
    about_terms = list(terms or _interest_terms_from_text(text))
    locs = list(location_codes or [])
    queries: list[dict[str, Any]] = []
    for term in about_terms:
        kw = _one_token(term)
        if not kw:
            continue
        queries.append({
            "keywords": kw,
            "title_or": "",
            "location_codes": locs,
            "why": "interest",
        })
        if len(queries) >= 3:
            break
    return {
        "kind": "interest",
        "label": cut_at_word(text or "", 80),
        "code": "",
        "recall_queries": queries,
        "evidence": {
            "schools": [],
            "languages": [],
            "experience_places": [],
            "experience_companies": [],
            "about_terms": about_terms,
        },
        "keep_rule": "distinctive_term",
        "explain": [
            "Interest: keep if a distinctive term appears in about, volunteer, "
            "skills, publications, or headline.",
        ],
    }


def compile_profile_signals(
    text: str,
    *,
    titles: list[str] | None = None,
    location_codes: list[str] | None = None,
    location_names: list[str] | None = None,
    segment: dict[str, Any] | None = None,
    interest_terms: list[str] | None = None,
) -> dict[str, Any] | None:
    """Compile a ProfileSignalSpec. Country gazetteer wins over interest.

    Always rebuilds lists (compiler_version). Persisted recall_queries are ignored.
    """
    gaz = _match_gazetteer(text, location_names)
    if not gaz and segment:
        existing = segment.get("profile_signals")
        if isinstance(existing, dict) and existing.get("kind") == "country_tie":
            code = str(existing.get("code") or "").lower()
            if code:
                gaz = load_country_gazetteer(code)
                # A tie stored without its brief: the places alone decide (api #5290 review).
                if gaz and language_is_spoken_everywhere(gaz, location_names):
                    gaz = None
    if gaz:
        spec = _spec_from_gazetteer(
            gaz, titles=titles, location_codes=location_codes,
        )
        return _attach_search_plan(
            spec, titles=titles, location_codes=location_codes,
            location_names=location_names, text=text,
        )
    blob = _norm(text)
    if not blob:
        return None
    if interest_terms or _interest_terms_from_text(text):
        spec = compile_interest_signals(
            text, titles=titles, location_codes=location_codes, terms=interest_terms,
        )
        return _attach_search_plan(
            spec, titles=titles, location_codes=location_codes,
            location_names=location_names, text=text,
        )
    return None


def _geo_facet(
    location_codes: list[str] | None,
    location_names: list[str] | None,
) -> dict[str, Any]:
    codes = [str(c) for c in (location_codes or []) if c]
    names = [str(n) for n in (location_names or []) if n]
    return {
        "location_codes": codes,
        "location_names": names,
        "required": bool(codes or names),
    }


def _role_facet(titles: list[str] | None, text: str = "") -> dict[str, Any]:
    hiring = bool(_HIRE_RE.search(text or ""))
    return {
        "titles": list(titles or []),
        "required": bool(titles) or hiring,
        "hiring": hiring,
    }


def _role_needles(titles: list[str] | None, hiring: bool) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def add(needle: str) -> None:
        n = _norm(needle)
        if n and n not in seen and n not in _ROLE_STOPWORDS:
            seen.add(n)
            out.append(n)

    for title in titles or []:
        add(title)
        for exp in _TITLE_EXPANSIONS.get(_norm(title), []):
            add(exp)
    if hiring:
        for extra in _HIRING_ROLE_NEEDLES:
            add(extra)
    return out


# Between the words of a role needle, people write a space, a comma, a dash, or
# a connective — "VP Engineering", "VP of Engineering", "VP, Engineering",
# "VP - Engineering" are one job. This is the phrase pass, which also crosses
# a bar ("VP | Engineering"). The word pass below (#2737) lets other words
# stand between a needle's words, but never a word that is itself a job, so
# "engineering manager" still does not match "engineering intern and
# marketing manager".
_ROLE_GAP = r"[\s,\-\u2013\u2014/|]+(?:(?:of|the|for|and)[\s,\-\u2013\u2014/|]+)?"


def _role_phrase_match(hay: str, n: str) -> bool:
    """The needle's words side by side, bridged only by _ROLE_GAP."""
    body = _ROLE_GAP.join(re.escape(part) for part in n.split())
    return bool(re.search(rf"(?<!\w){body}(?!\w)", hay))


# A title's words in order, other words between and around them (#2737):
# "ACCA SBR Expert Tutor" holds "ACCA Tutor", "Senior ACCA & AAT Lecturer"
# holds "ACCA Lecturer". Until 10 Oct 2026 a needle had to stand as one
# phrase, and on 6b9f51d6 16 of the 40 people the owner picked by hand failed
# his own campaign's title check.
#
# Words are runs of letters and digits ("R&D" is r, d; "Co-Founder" is co,
# founder; "&" is no word). The connectives below are optional between a
# needle's words; a needle that ends in one ("head of", a hiring needle) keeps
# it, right after the word before it, or "head of" would be any Head Chef.
_ROLE_CONNECTIVES = frozenset({"of", "the", "for", "and", "a", "an", "in", "to", "at"})
# One headline holds several roles: "ACCA member | Maths Tutor", "Founder @
# X", "Trainee at Tutor Academy", and sentences ("Head of PR in defence.
# Global PR for AI"). A needle's words are looked for inside one of them,
# never across.
_ROLE_CLAUSE_SPLIT = re.compile(
    r"\s*(?:[|\u2022\u00b7;()\[\]\n]|@|\s(?:at|@)\s|\s/\s|[.!?:](?:\s|$))\s*",
)
# A word that names a job ends a role, so it never stands between a needle's
# words: "Engineering intern and Marketing Manager" is two roles, not an
# Engineering Manager; "Head Chef, Growth Kitchen" is not a Head of Growth.
# A word the needle itself names is no bridge either way (it is matched).
# Words that are as often a title's modifier are left out ("Lead", "Head",
# "Chief", "Executive", "Graduate", "Partner": "ACCA Lead Tutor" is a tutor).
_ROLE_NOUNS = frozenset({
    "intern", "interns", "trainee", "student", "apprentice",
    "manager", "director", "leader", "officer",
    "president", "vp", "svp", "evp", "ceo", "cto", "cfo", "coo", "cio",
    "ciso", "cpo", "cmo", "founder", "cofounder", "owner",
    "engineer", "developer", "architect", "analyst", "consultant", "advisor",
    "adviser", "specialist", "associate", "assistant", "coordinator",
    "administrator", "representative", "rep", "agent",
    "accountant", "auditor", "lawyer", "attorney", "designer", "scientist",
    "researcher", "teacher", "tutor", "lecturer", "professor", "instructor",
    "trainer", "coach", "mentor", "chef", "nurse", "doctor", "recruiter",
    "member", "volunteer", "freelancer", "contractor", "investor", "chair",
    "chairman", "chairwoman", "secretary", "treasurer", "clerk", "operator",
    "technician", "supervisor", "editor", "writer", "author",
})
_WORD = re.compile(r"[^\W_]+")


def _role_words_in_order(hay: str, n: str) -> bool:
    raw = _WORD.findall(n)
    if not raw:
        return False
    # (word, must sit right after the previous needle word)
    need: list[tuple[str, bool]] = []
    content_seen = False
    for i, w in enumerate(raw):
        if w in _ROLE_CONNECTIVES:
            trailing = all(x in _ROLE_CONNECTIVES for x in raw[i:])
            if trailing and content_seen:
                need.append((w, True))
            continue
        need.append((w, False))
        content_seen = True
    if not content_seen:
        return False
    needed = {w for w, _ in need}
    for clause in _ROLE_CLAUSE_SPLIT.split(hay):
        words = _WORD.findall(clause)
        if _words_in_order(words, need, needed):
            return True
    return False


def _words_in_order(
    words: list[str], need: list[tuple[str, bool]], needed: set[str],
) -> bool:
    # Every start of the first word is tried: "Head Chef, Head of Growth"
    # fails from the first "head" and holds from the second.
    first = need[0][0]
    for start, w in enumerate(words):
        if w != first:
            continue
        pos, ok = start, True
        for word, adjacent in need[1:]:
            nxt = pos + 1
            if adjacent:
                if nxt < len(words) and words[nxt] == word:
                    pos = nxt
                    continue
                ok = False
                break
            while nxt < len(words) and words[nxt] != word:
                if words[nxt] in _ROLE_NOUNS and words[nxt] not in needed:
                    break
                nxt += 1
            if nxt < len(words) and words[nxt] == word:
                pos = nxt
                continue
            ok = False
            break
        if ok:
            return True
    return False


def _role_match(hay: str, needle: str, *, in_order: bool = True) -> bool:
    n = _norm(needle)
    if not n or not hay:
        return False
    if n == "chief":
        return bool(re.search(r"(?<!in-)(?<!\w)chief(?!\w)", hay))
    # The phrase first: everything that matched before 10 Oct 2026 still
    # does ("VP | Engineering" crosses a bar the clause split would not).
    # Needle words joined by a tolerant gap rather than matched verbatim. A
    # needle written the short way ("vp engineering") used to match only
    # itself, so an ICP naming "VP Engineering" dropped every "VP of
    # Engineering" on LinkedIn — on the segment "Ukrainian CTOs & VPs in US"
    # that gate reported role dropped=25 out of a pool of 44.
    if _role_phrase_match(hay, n):
        return True
    return in_order and _role_words_in_order(hay, n)


def role_hit(
    text: str | Collection[str] | None,
    titles: Collection[str] | None,
    *,
    hiring: bool = False,
) -> str:
    """The ICP title (or the alias of one) that *text* holds as a role, or "".

    The one answer to "is this person's title one the ICP names?", shared with
    the backend (heylead-api profile_signals.role_hit). Until 23 Sep 2026 only
    the role gate below knew that "CTO" is the Chief Technology Officer;
    icp_match_scorer, the comment-mining collector and the profile-view
    collector matched the ICP's words as one exact phrase, so an ICP that
    spelled its titles out turned every CTO away.
    tests/test_one_title_matcher.py holds them to one answer.

    *text* is one field or several (title, headline), matched one at a time:
    the gap between a title's words tolerates a space, so a title ending
    "... VP" and a headline starting "Engineering intern" must not be joined.
    """
    fields = [text] if isinstance(text, str) or text is None else list(text)
    hays = [h for h in (_norm(str(f or "")) for f in fields) if h]
    if not hays:
        return ""
    # The ICP's own titles (and their aliases) match word by word. The
    # hiring stems stay phrases: "hiring manager" word by word is any
    # "HIRING: UA Manager" (measured on a live campaign's pool, #2737).
    own = set(_role_needles(list(titles or []), False))
    # An owner needle ("Owner", "Business Owner") never meets the "owner" of a
    # role: a Product Owner owns a backlog, not a business (heylead-api #5470).
    # A needle that is itself a role owner ("Product Owner") reads the title
    # as written.
    from .seniority import mentions_owner, names_role_owner, without_role_owners

    plain = [_norm(without_role_owners(h)) for h in hays]
    for needle in _role_needles(list(titles or []), hiring):
        loose = needle in own
        read = plain if mentions_owner(needle) and not names_role_owner(needle) else hays
        if any(_role_match(hay, needle, in_order=loose) for hay in read):
            return needle
    return ""


def _attach_search_plan(
    spec: dict[str, Any],
    *,
    titles: list[str] | None = None,
    location_codes: list[str] | None = None,
    location_names: list[str] | None = None,
    text: str = "",
) -> dict[str, Any]:
    """Rebuild orthogonal lists. Never AND identity + title + geo."""
    out = dict(spec)
    title_or = _titles_or(titles)
    locs = list(location_codes or [])
    names = list(location_names or [])
    tokens: list[dict[str, Any]] = []
    seen: set[str] = set()
    for q in out.get("recall_queries") or []:
        if (q.get("why") or "") == "pool":
            continue
        kw = _one_token(q.get("keywords") or "")
        if not kw or kw.lower() in seen:
            continue
        seen.add(kw.lower())
        tokens.append({"keywords": kw, "why": q.get("why") or "signal"})
        if len(tokens) >= MAX_RECALL_QUERIES:
            break
    lists: list[dict[str, Any]] = []
    for tok in tokens:
        lists.append({
            "id": f"identity_geo:{tok['keywords']}",
            "layer": "identity_geo",
            "keywords": tok["keywords"],
            "title_or": "",
            "location_codes": locs,
            "why": tok["why"],
        })
    for tok in tokens:
        lists.append({
            "id": f"identity_wide:{tok['keywords']}",
            "layer": "identity_wide",
            "keywords": tok["keywords"],
            "title_or": "",
            "location_codes": [],
            "why": tok["why"],
        })
    if title_or and locs:
        lists.append({
            "id": "role_geo",
            "layer": "role_geo",
            "keywords": "",
            "title_or": title_or,
            "location_codes": locs,
            "why": "pool",
        })
    out["compiler_version"] = COMPILER_VERSION
    out["facets"] = {
        "identity": {"kind": out.get("kind"), "code": out.get("code") or ""},
        "geo": _geo_facet(locs, names),
        "role": _role_facet(titles, text),
    }
    out["lists"] = lists
    out["recall_queries"] = [
        q for q in lists if q["layer"] in {"identity_geo", "role_geo"}
    ]
    out["cascade"] = [
        {"try": "identity_geo", "min_cards": IDENTITY_GEO_MIN_CARDS},
        {"try": "identity_wide", "min_cards": IDENTITY_GEO_MIN_CARDS},
        {"try": "role_geo", "min_cards": 1},
    ]
    return out


def _looks_like_interest_request(text: str) -> bool:
    return bool(_named_interests(text))


def _field_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_field_text(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_field_text(v) for v in value)
    return str(value)


def _contains_any(haystack: str, needles: list[str]) -> str | None:
    hay = _norm(haystack)
    if not hay:
        return None
    for needle in needles:
        n = _norm(needle)
        if n and n in hay:
            return needle
    return None


def score_profile_evidence(
    profile: dict[str, Any], spec: dict[str, Any] | None,
) -> dict[str, Any]:
    """Score a full LinkedIn profile against a compiled spec."""
    empty = {
        "keep": False, "hits": {}, "supporting": [], "explain": [],
    }
    if not spec or spec.get("kind") in (None, "none"):
        return empty
    ev = spec.get("evidence") or {}
    hits: dict[str, str] = {}
    supporting: list[str] = []
    explain: list[str] = []

    edu_text = _field_text(profile.get("education"))
    school_hit = _contains_any(edu_text, list(ev.get("schools") or []))
    if school_hit:
        hits["school"] = school_hit
        explain.append(f"school:{school_hit}")

    lang_text = _field_text(profile.get("languages"))
    lang_hit = _contains_any(lang_text, list(ev.get("languages") or []))
    if lang_hit:
        hits["language"] = lang_hit
        explain.append(f"language:{lang_hit}")

    exp_text = _field_text(profile.get("experience"))
    place_hit = _contains_any(
        exp_text,
        list(ev.get("experience_places") or []) + list(ev.get("experience_companies") or []),
    )
    if place_hit:
        hits["experience"] = place_hit
        explain.append(f"experience:{place_hit}")

    about_hay = _field_text([
        profile.get("headline"),
        profile.get("summary"),
        profile.get("about"),
        profile.get("volunteer"),
        profile.get("skills"),
        profile.get("publications"),
    ])
    # A spec compiled before heylead-api#1809 may still carry "at"; only a
    # term that is evidence is looked for.
    about_hit = _contains_any(about_hay, [t for t in (ev.get("about_terms") or []) if is_evidence_term(t)])
    if about_hit:
        supporting.append("about")
        explain.append(f"about:{about_hit}")

    kind = spec.get("kind")
    keep = False
    if kind == "country_tie":
        keep = bool(hits.get("school") or hits.get("language") or hits.get("experience"))
    elif kind == "interest":
        keep = bool(about_hit)
        if keep:
            hits["about"] = about_hit  # type: ignore[assignment]

    return {
        "keep": keep,
        "hits": hits,
        "supporting": supporting,
        "explain": explain,
    }


def attach_signals_to_icp_result(result: Any, target_description: str = "") -> None:
    """Stamp each persona with a compiled ProfileSignalSpec when one applies."""
    for icp in getattr(result, "icps", []) or []:
        if getattr(icp, "profile_signals", None):
            continue
        titles = []
        jt = getattr(icp, "job_titles", None)
        if jt is not None:
            titles = list(getattr(jt, "include", None) or [])
        locs: list[str] = []
        names: list[str] = []
        enriched = getattr(icp, "linkedin_enriched_params", None)
        loc_field = getattr(enriched, "locations", None) if enriched else None
        if loc_field is not None:
            for item in getattr(loc_field, "include", None) or []:
                code = getattr(item, "code", None) or (item.get("code") if isinstance(item, dict) else "")
                title = getattr(item, "title", None) or getattr(item, "name", None)
                if isinstance(item, dict):
                    title = title or item.get("title") or item.get("name")
                if code:
                    locs.append(str(code))
                if title:
                    names.append(str(title))
        text_blob = " ".join([
            getattr(icp, "name", "") or "",
            getattr(icp, "description", "") or "",
            " ".join(getattr(icp, "keywords", None) or []),
            target_description or "",
        ])
        spec = compile_profile_signals(
            text_blob, titles=titles, location_codes=locs, location_names=names or None,
        )
        if spec and spec.get("kind") in ("country_tie", "interest"):
            if spec["kind"] == "interest" and not _looks_like_interest_request(text_blob):
                continue
            icp.profile_signals = spec
