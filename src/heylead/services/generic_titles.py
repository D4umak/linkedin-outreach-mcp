"""A generic title names a level, not a role (heylead-api #5751).

"Founder", "Owner", "CEO", "Managing Director", "Principal" and "Partner" say
how senior someone is, not what they do. A coach's ICP that asked for
"Founder, Managing Director, Principal Coach" (campaign fa1b6ff0, 10 Nov 2026)
found every founder in three countries, and the discovery scorer gave a
"Founder - … Business Advisory" the full title points for the word "Founder"
and the owner-seniority point for the same word: 0.754, above every Executive
Coach (0.727). The first accepted lead was an advisor, not a coach.

The rule, in one place for the scorer (generic_only_title_hit, re-exported by
fit_rules), the signal matcher (icp_match), the search plan
(profile_signals._role_geo_lists) and the invariant
generic_title_kept_without_role:

* A segment *names a role* when one of its titles is not generic and says
  more than rank, scope, role or function words. Its *role terms* are what
  is left of those titles: "coach" of "Principal Coach", "hotel" of "Hotel
  Owner"; "VP Sales", "CTO" and "Board Member" leave nothing.
* In such a segment a prospect whose title meets only the generic titles, and
  whose profile carries none of the role terms, matched a level, not the role:
  the row is held as title-only (fit_rules.TITLE_ONLY_FIT_CAP, the #5069 send
  hold), and a search for that generic title is paired with the first role
  term instead of being sent alone.
* In an investor segment (icp_vocabulary.is_investor_segment) "Partner",
  "Managing Partner", "General Partner" and "Principal" are the role, not a
  level, and the investor words count as role terms.
* A segment whose titles are all generic (fintech founders: "Founder,
  Co-Founder, CEO") means founders and is left exactly as it was.

Mirrored word for word in the client's heylead/services/generic_titles.py;
both repos read tests/fixtures/generic_titles.json.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .profile_signals import role_hit

# One phrase per entry, lower case, hyphens and spaces equal. A title is
# generic when every part of it (split on "&", "and", "/", "," and "+") is one
# of these. Longer phrases first, so "managing partner" is removed before
# "partner" when a role term is cut out of a title.
GENERIC_TITLES: tuple[str, ...] = (
    "chief executive officer",
    "managing director",
    "managing partner",
    "managing principal",
    "co founder",
    "cofounder",
    "co owner",
    "founder",
    "owner",
    "ceo",
    "md",
    "principal",
    "partner",
    "president",
)

# Investor titles that are generic elsewhere; never generic in an investor
# segment (a VC "Partner at X Ventures" holds the role, #5751 review qa-1-1).
INVESTOR_ROLE_TITLES: frozenset[str] = frozenset({
    "partner", "managing partner", "general partner", "principal", "managing principal",
})
# Role terms an investor segment accepts besides its own titles' terms.
INVESTOR_ROLE_TERMS: tuple[str, ...] = ("investor", "angel", "fund", "venture", "capital")

# Words that say the rank or scope of a role, not the role: cut from a title
# with the generic phrases and the scorer's role and function words.
RANK_WORDS: frozenset[str] = frozenset({
    "vp", "svp", "evp", "avp", "vice", "head", "chief", "senior", "sr", "lead",
    "director", "manager", "general", "of", "the", "and", "for", "a", "an",
    # Scope, not role: "National Sales Manager" names sales, not a "national"
    # (39597a77, an FMCG leadership segment that wants its founders, #5751).
    "national", "international", "regional", "global", "area", "zonal", "zone",
    "territory", "district", "country", "field", "local", "deputy", "acting",
})

def _plain_words() -> frozenset[str]:
    """The scorer's role and function words (#2415): fit_rules in
    heylead-api, icp_match_scorer in the client. One lookup, so this module
    stays the same file in both repos."""
    import importlib

    for name in ("fit_rules", "icp_match_scorer"):  # heylead-api, the client
        try:
            mod = importlib.import_module(f".{name}", __package__)
        except ImportError:
            continue
        if hasattr(mod, "_ROLE_WORDS"):
            return frozenset(mod._ROLE_WORDS) | frozenset(mod._FUNCTION_WORDS)
    raise ImportError("generic_titles: no module carries _ROLE_WORDS")


_SPLIT = re.compile(r"\s*(?:&|\+|/|,|\band\b)\s*")


def _norm(text: str | None) -> str:
    out = re.sub(r"[^0-9a-z&+/,]+", " ", str(text or "").lower().replace("-", " "))
    return re.sub(r"\s+", " ", out).strip()


def is_generic_title(title: str | None, *, investor: bool = False) -> bool:
    """True when *title* is made only of generic seniority titles
    ("Founder", "Founder & CEO", "Co-Founder, Managing Partner"). In an
    investor segment the INVESTOR_ROLE_TITLES are the role, not generic."""
    parts = [p.strip() for p in _SPLIT.split(_norm(title)) if p.strip()]
    if not parts or not all(p in GENERIC_TITLES for p in parts):
        return False
    return not (investor and any(p in INVESTOR_ROLE_TITLES for p in parts))


def role_term(title: str | None) -> str:
    """What a title says once its generic phrases, rank and scope words, and
    the scorer's role and function words are gone: "coach" of "Principal
    Coach" and of "Executive Coach", "hotel" of "Hotel Owner", "seo" of
    "Junior SEO Specialist", "" of "VP Sales", "CTO" and "Board Member"."""
    text = f" {_norm(title)} "
    for phrase in GENERIC_TITLES:
        text = text.replace(f" {phrase} ", " ")
    drop = RANK_WORDS | _plain_words()
    words = [w for w in re.findall(r"[0-9a-z]+", text) if w not in drop]
    return " ".join(words)


def segment_role_terms(
    titles: Iterable[str] | None, *, investor: bool = False,
) -> list[str]:
    """The role terms of a segment, in title order, without duplicates; []
    when the segment names no role: every title generic, or nothing left of
    its other titles but rank, role and function words. An investor segment
    always names one, through INVESTOR_ROLE_TERMS."""
    # role_term drops role and function words, so a title made only of
    # them names a function, not a role a founder could hold beside it:
    # "CTO", "VP Sales", "Board Member". A ["CEO", "CTO", "VP Engineering"]
    # segment wants its CEOs and names no role here (#5751 review qa-2-1).
    out: list[str] = []
    for title in titles or []:
        if not str(title or "").strip() or is_generic_title(title, investor=investor):
            continue
        term = role_term(title)
        if term and term not in out:
            out.append(term)
    if investor:
        out.extend(t for t in INVESTOR_ROLE_TERMS if t not in out)
    return out


def mentions_role_term(text: str | None, terms: Iterable[str]) -> bool:
    """True when *text* carries one of *terms* as whole words, the last word
    in any of its plain forms: "coach" meets "coaches" and "coaching"."""
    hay = _norm(text)
    if not hay:
        return False
    for term in terms:
        words = [re.escape(w) for w in str(term or "").split() if w]
        if not words:
            continue
        pattern = r"(?<![0-9a-z])" + r"\s+".join(words) + r"(?:s|es|ing)?(?![0-9a-z])"
        if re.search(pattern, hay):
            return True
    return False


def generic_only_title_hit(
    fields: object, titles: list[str], *, evidence: str = "", investor: bool = False,
) -> bool:
    """True when a prospect matched a segment that names a role only on one of
    its generic titles, and nothing about it names that role. The callers
    hold such a row as title-only.

    *fields* is the prospect's title, or its title and headline, as role_hit
    takes them; *evidence* is the rest of what it says about itself
    (heylead-api fit_rules.prospect_evidence_text). False when the segment
    names no role (its titles are all generic: it means founders), when the
    prospect holds one of the role titles (asked of every role title, never
    of the needle role_hit happened to return first: "Founder & Principal
    Executive Career Coach" is a Principal Coach), and when its title,
    headline or evidence carries a role term ("Founder, X Coaching" against
    "Principal Coach"). *investor* is icp_vocabulary.is_investor_segment for
    the segment.
    """
    terms = segment_role_terms(titles, investor=investor)
    if not terms:
        return False
    listed = [t for t in titles if t and str(t).strip()]
    generic = [t for t in listed if is_generic_title(t, investor=investor)]
    roles = [t for t in listed if not is_generic_title(t, investor=investor)]
    if not generic or not role_hit(fields, generic) or role_hit(fields, roles):
        return False
    texts = [fields] if isinstance(fields, str) or fields is None else list(fields)  # type: ignore[call-overload]
    return not any(mentions_role_term(str(t or ""), terms) for t in [*texts, evidence])
