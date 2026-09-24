"""Compile targeting requests into recall queries + full-profile evidence.

Country/diaspora ties use gazetteer JSON (school, language, worked-in).
Interests use distinctive about/volunteer/skill terms. Never infer ethnicity
from names. Never AND product stems with identity tokens in one query.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Collection

logger = logging.getLogger(__name__)

COMPILER_VERSION = 3
PROFILE_RETRIEVE_BUDGET = 40
MAX_RECALL_QUERIES = 4
IDENTITY_GEO_MIN_CARDS = 8

# LinkedIn LOCATION codes we see often. Names from the ICP segment still win.
GEO_CODE_ALIASES: dict[str, list[str]] = {
    "103644278": [
        "united states", "usa", "u.s.", "u.s.a.", "united states of america",
        "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
        "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
        "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
        "maine", "maryland", "massachusetts", "michigan", "minnesota",
        "mississippi", "missouri", "montana", "nebraska", "nevada",
        "new hampshire", "new jersey", "new mexico", "new york",
        "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
        "pennsylvania", "rhode island", "south carolina", "south dakota",
        "tennessee", "texas", "utah", "vermont", "virginia", "washington",
        "west virginia", "wisconsin", "wyoming", "district of columbia",
        "ny", "ca", "tx", "sf", "la", "dc", "wa", "il", "ma", "pa", "va",
        "co", "az", "nc", "ga", "fl", "nj", "nv", "tn", "mn", "wi", "mo",
        "md", "ct", "ut", "sc", "al", "ky", "ok", "ia", "ks", "ar", "ms",
        "nm", "ne", "id", "nh", "ri", "mt", "sd", "nd", "ak", "vt", "wy",
        "wv",
        "bay area", "austin", "san francisco", "los angeles", "new york city",
        "seattle", "chicago", "boston", "denver", "miami", "atlanta",
        "dallas", "houston", "san diego", "san jose", "palo alto",
        "silicon valley", "brooklyn", "manhattan",
    ],
    "101165590": [
        "united kingdom", "uk", "u.k.", "great britain", "england",
        "scotland", "wales", "london",
    ],
}

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


def _blob(*parts: Any) -> str:
    bits: list[str] = []
    for part in parts:
        if isinstance(part, list):
            bits.append(" ".join(str(x) for x in part))
        elif part:
            bits.append(str(part))
    return _norm(" ".join(bits))


def _match_gazetteer(text: str) -> dict[str, Any] | None:
    blob = _norm(text)
    if not blob:
        return None
    # Longer aliases first so "ukrainian" wins over a short substring later.
    scored: list[tuple[int, dict[str, Any]]] = []
    for gaz in _load_all_gazetteers().values():
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
    blob = _norm(text)
    terms: list[str] = []
    for key, hints in _INTEREST_HINTS.items():
        if re.search(rf"(?<!\w){re.escape(key)}(?!\w)", blob):
            for h in hints:
                if h not in terms:
                    terms.append(h)
    if not terms:
        # Distinctive leftover words (drop stopwords / generic ICP nouns).
        stop = {
            "people", "person", "lovers", "lover", "who", "with", "that", "the",
            "and", "for", "in", "a", "an", "of", "us", "now", "hiring",
        }
        for tok in re.findall(r"[A-Za-z][A-Za-z0-9\-]+", text or ""):
            if tok.lower() not in stop and tok.lower() not in terms:
                terms.append(tok)
    return terms[:15]


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
        "label": (text or "").strip()[:80],
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
    gaz = _match_gazetteer(text)
    if not gaz and segment:
        existing = segment.get("profile_signals")
        if isinstance(existing, dict) and existing.get("kind") == "country_tie":
            code = str(existing.get("code") or "").lower()
            if code:
                gaz = load_country_gazetteer(code)
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


def compile_from_segment(segment: dict[str, Any] | None) -> dict[str, Any] | None:
    if not segment:
        return None
    text = _blob(
        segment.get("name"),
        segment.get("description"),
        segment.get("keywords"),
    )
    titles = list(segment.get("titles") or [])
    locs = list(segment.get("location_codes") or [])
    names = list(segment.get("locations") or [])
    spec = compile_profile_signals(
        text, titles=titles, location_codes=locs, location_names=names,
        segment=segment,
    )
    if spec and spec.get("kind") == "interest":
        if not _looks_like_interest_request(text):
            return None
    return spec


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
# "VP - Engineering" are one job. Only these may bridge the gap: an arbitrary
# word must not, or "engineering manager" would match "engineering intern and
# marketing manager". Same as the backend's.
_ROLE_GAP = r"[\s,\-\u2013\u2014/|]+(?:(?:of|the|for|and)[\s,\-\u2013\u2014/|]+)?"


def _role_match(hay: str, needle: str) -> bool:
    n = _norm(needle)
    if not n or not hay:
        return False
    if n == "chief":
        return bool(re.search(r"(?<!in-)(?<!\w)chief(?!\w)", hay))
    body = _ROLE_GAP.join(re.escape(part) for part in n.split())
    return bool(re.search(rf"(?<!\w){body}(?!\w)", hay))


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
    for needle in _role_needles(list(titles or []), hiring):
        if any(_role_match(hay, needle) for hay in hays):
            return needle
    return ""


def score_role(
    card_or_profile: dict[str, Any] | None,
    role_facet: dict[str, Any] | None,
) -> dict[str, Any]:
    """Keep hiring titles / stems. Never a bare 'engineering' or stopword."""
    if not role_facet or not role_facet.get("required"):
        return {"keep": True, "hit": "", "explain": []}
    fields = [
        _field_text((card_or_profile or {}).get("title")),
        _field_text((card_or_profile or {}).get("headline")),
    ]
    needle = role_hit(
        fields, list(role_facet.get("titles") or []),
        hiring=bool(role_facet.get("hiring")),
    )
    if needle:
        return {"keep": True, "hit": needle, "explain": [f"role:{needle}"]}
    return {
        "keep": False, "hit": "", "explain": ["role_miss"], "dropped": "role_miss",
    }


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


def _none_plan(
    titles: list[str] | None,
    location_codes: list[str] | None,
    location_names: list[str] | None,
    text: str = "",
) -> dict[str, Any]:
    title_or = _titles_or(titles)
    locs = list(location_codes or [])
    lists: list[dict[str, Any]] = []
    if title_or and locs:
        lists.append({
            "id": "role_geo",
            "layer": "role_geo",
            "keywords": "",
            "title_or": title_or,
            "location_codes": locs,
            "why": "pool",
        })
    return {
        "kind": "none",
        "label": "",
        "code": "",
        "compiler_version": COMPILER_VERSION,
        "recall_queries": lists,
        "lists": lists,
        "evidence": {},
        "keep_rule": "",
        "facets": {
            "identity": {"kind": "none", "code": ""},
            "geo": _geo_facet(location_codes, location_names),
            "role": _role_facet(titles, text),
        },
        "cascade": [{"try": "role_geo", "min_cards": 1}],
        "explain": ["No identity facet. Role + geo only."],
    }


def _identity_overlay_payload(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": spec.get("kind"),
        "code": spec.get("code") or "",
        "label": spec.get("label") or "",
        "evidence": dict(spec.get("evidence") or {}),
        "keep_rule": spec.get("keep_rule") or "",
    }


def campaign_identity_spec(icp_data: dict[str, Any] | None) -> dict[str, Any] | None:
    """First country_tie / interest on the campaign (summary, then each segment)."""
    if not isinstance(icp_data, dict):
        return None
    summary = _blob(
        icp_data.get("summary"),
        icp_data.get("target_description"),
        icp_data.get("name"),
        icp_data.get("description"),
    )
    if summary:
        spec = compile_profile_signals(summary)
        if spec and spec.get("kind") in ("country_tie", "interest"):
            return _identity_overlay_payload(spec)
    for seg in icp_data.get("segments") or []:
        if not isinstance(seg, dict):
            continue
        spec = compile_from_segment(seg)
        if spec and spec.get("kind") in ("country_tie", "interest"):
            return _identity_overlay_payload(spec)
    return None


def _overlay_campaign_identity(
    plan: dict[str, Any], ident: dict[str, Any],
) -> dict[str, Any]:
    """Keep sibling lists; overlay evidence so filter retrieves and identity-keeps."""
    out = dict(plan)
    kind = ident.get("kind")
    out["kind"] = kind
    out["code"] = ident.get("code") or ""
    out["label"] = ident.get("label") or ""
    out["evidence"] = dict(ident.get("evidence") or {})
    out["keep_rule"] = ident.get("keep_rule") or (
        "hard_signal" if kind == "country_tie" else "distinctive_term"
    )
    facets = dict(out.get("facets") or {})
    facets["identity"] = {"kind": kind, "code": ident.get("code") or ""}
    out["facets"] = facets
    out["identity_overlay"] = True
    return out


def compile_search_plan(
    segment: dict[str, Any] | None,
    campaign_identity: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not segment:
        return None
    spec = compile_from_segment(segment)
    if spec:
        return spec
    text = _blob(
        segment.get("name"),
        segment.get("description"),
        segment.get("keywords"),
    )
    plan = _none_plan(
        list(segment.get("titles") or []),
        list(segment.get("location_codes") or []),
        list(segment.get("locations") or []),
        text,
    )
    if campaign_identity and campaign_identity.get("kind") in ("country_tie", "interest"):
        return _overlay_campaign_identity(plan, campaign_identity)
    return plan


def identity_recall_lists(
    campaign_identity: dict[str, Any] | None,
    location_codes: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Identity_geo / identity_wide lists from campaign country/interest. No titles."""
    if not campaign_identity or campaign_identity.get("kind") not in (
        "country_tie", "interest",
    ):
        return []
    locs = list(location_codes or [])
    kind = campaign_identity.get("kind")
    if kind == "country_tie":
        gaz = load_country_gazetteer(str(campaign_identity.get("code") or ""))
        if not gaz:
            return []
        spec = _attach_search_plan(
            _spec_from_gazetteer(gaz, location_codes=locs),
            location_codes=locs,
        )
    else:
        terms = list((campaign_identity.get("evidence") or {}).get("about_terms") or [])
        spec = _attach_search_plan(
            compile_interest_signals(
                str(campaign_identity.get("label") or ""),
                location_codes=locs,
                terms=terms or None,
            ),
            location_codes=locs,
        )
    return [
        q for q in (spec.get("lists") or [])
        if q.get("layer") in {"identity_geo", "identity_wide"}
    ]


def union_recall_lists(*groups: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for group in groups:
        for query in group or []:
            key = (
                str(query.get("layer") or ""),
                _one_token(query.get("keywords") or "").lower(),
                str(query.get("school_name") or query.get("id") or "").lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(query)
    return out


def lists_for_recall(
    segment: dict[str, Any] | None,
    campaign_identity: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Segment lists union campaign identity lists (deduped)."""
    plan = compile_search_plan(segment, campaign_identity=campaign_identity)
    if not plan:
        return [], None
    locs = list((segment or {}).get("location_codes") or [])
    ident = identity_recall_lists(campaign_identity, locs)
    return union_recall_lists(list(plan.get("lists") or []), ident), plan


def gazetteer_school_needles(code: str) -> set[str]:
    gaz = load_country_gazetteer(code)
    if not gaz:
        return set()
    out: set[str] = set()
    for school in gaz.get("schools") or []:
        if school.get("stem"):
            out.add(_norm(str(school["stem"])))
        for alias in school.get("aliases") or []:
            out.add(_norm(str(alias)))
    return {n for n in out if n}


def catalog_query_covered(query: dict[str, Any], needles: set[str]) -> bool:
    blob = _norm(
        f"{query.get('school_name') or ''} {query.get('keywords') or ''}"
    )
    if not blob or not needles:
        return False
    return any(n and n in blob for n in needles)


def lists_to_run(
    plan: dict[str, Any] | None,
    *,
    identity_geo_cards: int | None = None,
) -> list[dict[str, Any]]:
    """First wave: identity_geo + role_geo. Widen if geo cards are below threshold."""
    if not plan:
        return []
    lists = list(plan.get("lists") or [])
    first = [q for q in lists if q.get("layer") in {"identity_geo", "role_geo"}]
    if identity_geo_cards is None:
        return first
    min_cards = IDENTITY_GEO_MIN_CARDS
    for stage in plan.get("cascade") or []:
        if stage.get("try") == "identity_geo":
            try:
                min_cards = int(stage.get("min_cards") or min_cards)
            except (TypeError, ValueError):
                pass
    if identity_geo_cards < min_cards:
        return first + [q for q in lists if q.get("layer") == "identity_wide"]
    return first


def score_geo(
    card_or_profile: dict[str, Any] | None,
    geo: dict[str, Any] | None,
    *,
    stage: str = "profile",
) -> dict[str, Any]:
    """Current location vs requested geo. Worked-in-country is not a geo pass.

    Card stage: blank location defers (geo_unknown). Profile stage: blank is a miss.
    Headline is never used.
    """
    if not geo or not geo.get("required"):
        return {"keep": True, "hit": "", "explain": []}
    needles: list[str] = list(geo.get("location_names") or [])
    for code in geo.get("location_codes") or []:
        needles.extend(GEO_CODE_ALIASES.get(str(code), []))
    hay = _norm(_field_text([(card_or_profile or {}).get("location")]))
    if not hay:
        if stage == "card":
            return {"keep": True, "hit": "", "explain": ["geo_unknown"]}
        return {"keep": False, "hit": "", "explain": ["geo_miss"]}
    for needle in needles:
        n = _norm(needle)
        if not n:
            continue
        if len(n) <= 2:
            if re.search(rf"(?<!\w){re.escape(n)}(?!\w)", hay):
                return {"keep": True, "hit": needle, "explain": [f"geo:{needle}"]}
        elif n in hay:
            return {"keep": True, "hit": needle, "explain": [f"geo:{needle}"]}
    return {"keep": False, "hit": "", "explain": ["geo_miss"]}


def payload_from_list(query: dict[str, Any]) -> dict[str, Any] | None:
    payloads = recall_payloads_from_spec({"recall_queries": [query]})
    return payloads[0] if payloads else None


def _looks_like_interest_request(text: str) -> bool:
    blob = _norm(text)
    for key in _INTEREST_HINTS:
        if re.search(rf"(?<!\w){re.escape(key)}(?!\w)", blob):
            return True
    return False


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
    about_hit = _contains_any(about_hay, list(ev.get("about_terms") or []))
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


def profile_signals_keep(profile: dict[str, Any], spec: dict[str, Any] | None) -> bool:
    return score_profile_evidence(profile, spec)["keep"]


def recall_payloads_from_spec(spec: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Unipile classic payloads for the recall pack (one token each)."""
    if not spec:
        return []
    out: list[dict[str, Any]] = []
    for q in (spec.get("recall_queries") or [])[:MAX_RECALL_QUERIES]:
        payload: dict[str, Any] = {
            "api": "classic",
            "category": "people",
        }
        kw = _one_token(q.get("keywords") or "")
        if kw:
            payload["keywords"] = kw
        if q.get("title_or"):
            payload["advanced_keywords"] = {"title": q["title_or"]}
        if q.get("location_codes"):
            payload["location"] = list(q["location_codes"])
        if q.get("school_id"):
            # Unipile SN people schema rejects a top-level `school` key.
            # Keep the resolved id on the query for later; search the exact
            # LinkedIn school name as a keyword on the Premium/SN seat.
            payload["keywords"] = q.get("school_name") or kw or payload.get("keywords") or ""
            payload["school_id"] = q["school_id"]
        if payload.get("keywords") or q.get("title_or"):
            out.append(payload)
    return out


def _profile_from_unipile_body(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    return {
        "title": body.get("title") or "",
        "headline": body.get("headline") or "",
        "summary": body.get("summary") or body.get("about") or "",
        "about": body.get("about") or body.get("summary") or "",
        "location": body.get("location") or "",
        "education": body.get("education") or [],
        "languages": body.get("languages") or [],
        "experience": (
            body.get("experience")
            or body.get("work_experience")
            or body.get("positions")
            or []
        ),
        "volunteer": body.get("volunteer_experience") or body.get("volunteer") or [],
        "skills": body.get("skills") or [],
        "publications": body.get("publications") or [],
    }


async def filter_prospects_by_profile_signals(
    prospects: list[dict[str, Any]],
    spec: dict[str, Any] | None,
    *,
    retrieve,
    budget: int = PROFILE_RETRIEVE_BUDGET,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Retrieve full profiles and keep those that meet the evidence rule.

    ``retrieve(identifier)`` is async and returns a profile dict or None.
    Prospects that already carry education/languages/experience skip retrieve.
    """
    if not spec or spec.get("kind") in (None, "none"):
        return list(prospects), []
    kept: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    used = 0
    for prospect in prospects:
        profile = prospect.get("profile_json")
        if isinstance(profile, str):
            try:
                profile = json.loads(profile)
            except (json.JSONDecodeError, TypeError):
                profile = None
        has_deep = isinstance(profile, dict) and (
            profile.get("education") or profile.get("languages") or profile.get("experience")
        )
        if not has_deep:
            ident = (
                prospect.get("provider_id")
                or prospect.get("linkedin_id")
                or prospect.get("public_id")
                or ""
            )
            if used >= budget or not ident:
                scores.append({"keep": False, "hits": {}, "explain": ["skipped"]})
                continue
            fetched = await retrieve(str(ident))
            used += 1
            profile = fetched if isinstance(fetched, dict) else {}
        result = score_profile_evidence(profile or {}, spec)
        deep = bool(
            (profile or {}).get("education")
            or (profile or {}).get("languages")
            or (profile or {}).get("experience")
        )
        if not result["keep"] and not deep and not result.get("explain"):
            result["explain"] = ["retrieve_empty"]
        scores.append(result)
        if result["keep"]:
            merged = dict(prospect)
            merged["profile_json"] = profile
            merged["profile_signal_explain"] = result["explain"]
            kept.append(merged)
    return kept, scores


def _prospect_profile(prospect: dict[str, Any]) -> dict[str, Any] | None:
    profile = prospect.get("profile_json")
    if isinstance(profile, str):
        try:
            profile = json.loads(profile)
        except (json.JSONDecodeError, TypeError):
            profile = None
    return profile if isinstance(profile, dict) else None


def _apply_role_keep(
    result: dict[str, Any],
    card_or_profile: dict[str, Any] | None,
    role: dict[str, Any] | None,
) -> dict[str, Any]:
    if not result.get("keep"):
        return result
    scored = score_role(card_or_profile, role)
    if scored["keep"]:
        if scored.get("explain"):
            out = dict(result)
            out["explain"] = list(result.get("explain") or []) + scored["explain"]
            return out
        return result
    out = dict(result)
    out["keep"] = False
    out["dropped"] = scored.get("dropped") or "role_miss"
    out["explain"] = list(result.get("explain") or []) + list(scored.get("explain") or [])
    return out


async def filter_prospects_by_plan(
    prospects: list[dict[str, Any]],
    plan: dict[str, Any] | None,
    *,
    retrieve,
    budget: int = PROFILE_RETRIEVE_BUDGET,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Card geo, retrieve, identity, profile geo, then hard role keep."""
    if not plan:
        return list(prospects), []
    geo = (plan.get("facets") or {}).get("geo") or {}
    role = (plan.get("facets") or {}).get("role") or {}
    identity_kind = (
        ((plan.get("facets") or {}).get("identity") or {}).get("kind")
        or plan.get("kind")
    )
    apply_geo = identity_kind not in (None, "none") and bool(geo.get("required"))
    kept: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    survivors: list[dict[str, Any]] = []
    for prospect in prospects:
        card_unknown = False
        if apply_geo:
            g = score_geo(prospect, geo, stage="card")
            if not g["keep"]:
                scores.append({
                    "keep": False, "hits": {}, "explain": g["explain"],
                    "dropped": "geo_miss",
                })
                continue
            card_unknown = "geo_unknown" in (g.get("explain") or [])
        row = dict(prospect)
        if card_unknown:
            row["_card_geo_unknown"] = True
        survivors.append(row)

    if identity_kind in (None, "none"):
        for prospect in survivors:
            result = _apply_role_keep(
                {"keep": True, "hits": {}, "explain": ["role_geo"]},
                prospect, role,
            )
            scores.append(result)
            if result["keep"]:
                kept.append(prospect)
        return kept, scores

    used = 0
    for prospect in survivors:
        profile = _prospect_profile(prospect)
        has_deep = bool(
            profile and (profile.get("education") or profile.get("languages") or profile.get("experience"))
        )
        if not has_deep:
            ident = (
                prospect.get("provider_id")
                or prospect.get("linkedin_id")
                or prospect.get("public_id")
                or ""
            )
            if used >= budget or not ident:
                scores.append({"keep": False, "hits": {}, "explain": ["skipped"]})
                continue
            fetched = await retrieve(str(ident))
            used += 1
            profile = fetched if isinstance(fetched, dict) else {}
        profile = dict(profile or {})
        if not profile.get("location"):
            profile["location"] = prospect.get("location") or ""
        if not profile.get("title"):
            profile["title"] = prospect.get("title") or ""
        if not profile.get("headline"):
            profile["headline"] = prospect.get("headline") or ""
        result = score_profile_evidence(profile, plan)
        if apply_geo:
            g = score_geo(profile, geo, stage="profile")
            if not g["keep"]:
                result = dict(result)
                result["keep"] = False
                result["explain"] = list(result.get("explain") or []) + g["explain"]
            elif g.get("explain"):
                result = dict(result)
                result["explain"] = list(result.get("explain") or []) + g["explain"]
        if prospect.get("_card_geo_unknown"):
            result = dict(result)
            result["explain"] = ["geo_unknown"] + [
                x for x in (result.get("explain") or []) if x != "geo_unknown"
            ]
        result = _apply_role_keep(result, profile, role)
        scores.append(result)
        if result["keep"]:
            merged = dict(prospect)
            merged.pop("_card_geo_unknown", None)
            merged["profile_json"] = profile
            merged["profile_signal_explain"] = result["explain"]
            kept.append(merged)
    return kept, scores


def identity_fit_skip_tokens(plan: dict[str, Any] | None) -> set[str]:
    """Identity / gazetteer / interest tokens that must not raise fit."""
    if not plan or plan.get("kind") in (None, "none"):
        return set()
    skip: set[str] = set()
    ev = plan.get("evidence") or {}
    for key in (
        "schools", "languages", "experience_places",
        "experience_companies", "about_terms",
    ):
        for token in ev.get(key) or []:
            if token:
                skip.add(_norm(str(token)))
    if plan.get("label"):
        skip.add(_norm(str(plan["label"])))
    code = (
        plan.get("code")
        or ((plan.get("facets") or {}).get("identity") or {}).get("code")
        or ""
    )
    if code:
        gaz = load_country_gazetteer(str(code))
        if gaz:
            skip.add(_norm(str(gaz.get("label") or "")))
            for alias in gaz.get("aliases") or []:
                skip.add(_norm(str(alias)))
            for tok in gaz.get("recall_tokens") or []:
                kw = _one_token(str(tok.get("keywords") or ""))
                if kw:
                    skip.add(_norm(kw))
    for query in list(plan.get("lists") or []) + list(plan.get("recall_queries") or []):
        if query.get("layer") not in {"identity_geo", "identity_wide"}:
            continue
        kw = _one_token(query.get("keywords") or "")
        if kw:
            skip.add(_norm(kw))
    return {t for t in skip if t}


def summarize_evidence(results: list[dict[str, Any]]) -> str:
    kept = sum(1 for r in results if r.get("keep"))
    school = sum(1 for r in results if r.get("hits", {}).get("school"))
    language = sum(1 for r in results if r.get("hits", {}).get("language"))
    experience = sum(1 for r in results if r.get("hits", {}).get("experience"))
    role_drop = sum(1 for r in results if r.get("dropped") == "role_miss")
    extras: list[str] = []
    for r in results:
        extras.extend(str(x) for x in (r.get("explain") or [])[:2])
    line = (
        f"Profile signals: kept {kept}/{len(results)} "
        f"(school={school} language={language} experience={experience})"
    )
    if role_drop:
        line += f" role dropped={role_drop}"
    geo_miss = sum(
        1 for r in results
        if "geo_miss" in (r.get("explain") or []) and not r.get("keep")
    )
    geo_unknown = sum(
        1 for r in results if "geo_unknown" in (r.get("explain") or [])
    )
    if geo_miss:
        line += f" card_geo dropped={geo_miss}"
    if geo_unknown:
        line += f" card_geo unknown={geo_unknown}"
    if extras:
        line += " explain=" + ",".join(extras[:6])
    return line


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
