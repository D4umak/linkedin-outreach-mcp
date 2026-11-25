"""Web-research the competitors of what the client sells, after ICP generation.

For a sale only: Serper for the company the offer names ("X competitors",
"alternatives to X") and a short category phrase, then one LLM extract that
cites a snippet for every name, then a deterministic filter. Failures are
empty, never blocking. Hosted accounts go through the backend Serper proxy.

The subject is the offer (the campaign's company context, else its project
brief), never the sender's company. That value is the tail of the seat's
LinkedIn headline, the employer: on 25 Sep 2026 it had a person selling AI
outreach researched as "Tver State University" and handed "GoIT, Postnauka"
as competitors (D4umak/heylead-api#1428). The sender's company is only ever a
name to drop from the answer. A job search, a hire, a partnership, a vendor
search or research interviews have no competitors, so they are not researched.

What is stored drops everyone who works there, so a wrong name costs buyers
(D4umak/heylead-api#7082, 19 Nov 2026): an offer sold to accelerators stored
"Catalyst Accelerator"; one common word ("Unicorn", "Square", "Forge") matched
unrelated employers through ``companies_match``'s division-prefix rule; "AWS
Textract" made every "AWS" employer a competitor; and 8 of 11 discover runs
came back empty because the 80-character first sentence was searched instead
of the company the offer names. ``filter_names`` is the one gate every
researched or copied name goes through.

The hosted twin is heylead-api ``app/services/competitor_research.py``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .. import goals
from ..textutil import STOPWORDS, contains_term, is_evidence_term
from .competitors import normalize_company, parse_competitor_names

logger = logging.getLogger(__name__)

_EXTRACT_SYSTEM = (
    "You extract competitor company names from web search snippets. "
    "Return strict JSON only."
)

_EXTRACT_PROMPT = """The client sells: {offer}
The client's buyers: {target}

Numbered web search results:
{snippets}

List companies that sell the same kind of product or service to the same
buyers. For each one give its full brand name exactly as a snippet writes it
("Square Appointments", not "Square") and the number of the snippet that
names it. Only companies named in the snippets above.

Do NOT include the client itself, its buyers or customers (the kind of
organisation named under "The client's buyers"), investors, job boards,
schools or universities, products of a larger company written as
"<Company> <Product>" (name the company instead only if it competes as a
whole), or generic industry terms.

Return ONLY JSON:
{{"competitors": [{{"name": "Company A", "snippet": 3}}, {{"name": "Company B", "snippet": 7}}]}}
"""

# The schema the client's LLM is held to: a name with the snippet that names
# it, or a bare name (older prompts, other models).
_EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "competitors": {
            "type": "array",
            "items": {
                "anyOf": [
                    {"type": "string"},
                    {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "snippet": {"type": "integer"},
                        },
                        "required": ["name"],
                    },
                ],
            },
        },
    },
    "required": ["competitors"],
}

_OFFER_QUERY_CHARS = 80
_CATEGORY_WORDS = 8
_RESULTS_PER_QUERY = 10
_MAX_SNIPPETS = 24
_SNIPPET_CHARS = 300
_URL_SCHEME = re.compile(r"^https?://(www\.)?", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")

# The company an offer names: "Rishabh Software is a …", "At Rupyz, we …",
# and the same shape in Spanish, French, German and Portuguese.
_NAME = r"[A-Z0-9][\w&.'\-]*(?:\s+[A-Z0-9][\w&.'\-]*){0,3}"
_SUBJECT_IS = re.compile(
    rf"^(?P<name>{_NAME})\s+(?:"
    r"(?:is|are)\s+(?:a|an|the)"
    r"|(?:es|son)\s+(?:una|un|el|la)"
    r"|(?:est|sont)\s+(?:une|un|le|la)"
    r"|(?:ist|sind)\s+(?:eine|ein|der|die|das)"
    r"|(?:é|são)\s+(?:uma|um|o|a)"
    r")\b"
)
_AT_WE = re.compile(rf"^(?:At|Here\s+at)\s+(?P<name>{_NAME}),?\s+(?:[Ww]e|[Oo]ur)\b")
# Capitalised words that open an offer sentence without naming a company:
# never searched as the subject and never dropped as "self" (an extracted
# "AI" would otherwise wipe every "... AI" competitor).
_NOT_A_COMPANY = frozenset({
    "ai", "api", "b2b", "b2c", "crm", "erp", "it", "linkedin", "saas", "seo", "smb", "smbs",
    "the", "this", "that", "these", "our", "we", "i", "my", "your", "here", "built", "every",
    "most", "many", "today", "now", "with", "for", "from",
})

# Words that name a kind of company, never one buyer: "<X> Software" is not
# dropped for a target of "software companies".
_GENERIC_HEADS = frozenset({
    "agency", "app", "apps", "co", "company", "consultancy", "consultant", "consulting",
    "digital", "firm", "global", "group", "hq", "inc", "lab", "labs", "media", "network",
    "partner", "platform", "service", "solution", "studio", "system", "systems", "tech",
    "technology", "technologies", "software", "venture", "ventures", "works", "ai", "io",
    "business", "businesses", "team", "teams", "leader", "leaders", "owner", "owners",
    "founder", "founders", "manager", "managers", "executive", "executives", "head", "heads",
    "director", "directors", "officer", "officers", "people", "professional", "professionals",
})

_ACRONYM = re.compile(r"^[A-Z0-9]{2,4}$")
_WORD = re.compile(r"[a-z0-9]+")

DROP_RULES = ("self", "not_in_snippets", "parent_product", "common_word", "buyer")


def researches_competitors(goal: Any) -> bool:
    """Only a sale has competitors to keep out of the audience."""
    return goals.normalize_goal(goal) == goals.SELL


def offer_text(company_context: str = "", project_brief: str = "") -> str:
    """What the campaign sells: its company context, else its project brief."""
    for text in (company_context, project_brief):
        flat = " ".join(str(text or "").split())
        if flat:
            return flat
    return ""


def offer_query(offer: str) -> str:
    """The offer's first sentence, cut at a word."""
    text = _URL_SCHEME.sub("", " ".join(str(offer or "").split()))
    first = _SENTENCE_END.split(text, maxsplit=1)[0].rstrip(".!? ")
    if len(first) > _OFFER_QUERY_CHARS:
        first = first[:_OFFER_QUERY_CHARS].rsplit(" ", 1)[0]
    return first.strip(" ,;:-")


def category_phrase(offer: str) -> str:
    """A short search phrase for what is sold: the first sentence's content words.

    "I help financial technology, lending, payments, and banking leaders
    accelerate …" is a poor search phrase at 80 characters; its first eight
    content words, in order, are a usable one.
    """
    words = [
        w.strip(",;:()\"'")
        for w in offer_query(offer).split()
    ]
    kept = [
        w for w in words
        if w and w.lower() not in STOPWORDS and w.lower() not in {"help", "helps", "helping"}
    ]
    return " ".join(kept[:_CATEGORY_WORDS])


def _is_generic_name(name: str) -> bool:
    words = _WORD.findall(name.lower())
    return not words or all(w in _NOT_A_COMPANY or w in STOPWORDS for w in words)


def offer_company(offer: str, sender_company: str = "") -> str:
    """The company the offer names as its subject, or "".

    Only "X is a/an/the …" or "At X, we …" in one of the first two sentences,
    or the sender's own company when the offer names it. A generic opener
    ("AI helps …", "LinkedIn outreach that …") is never a company.
    """
    text = _URL_SCHEME.sub("", " ".join(str(offer or "").split()))
    for sentence in _SENTENCE_END.split(text, maxsplit=2)[:2]:
        sentence = sentence.strip()
        for pattern in (_AT_WE, _SUBJECT_IS):
            match = pattern.match(sentence)
            if match:
                name = match.group("name").strip(" ,.;:-")
                if not _is_generic_name(name) and len(normalize_company(name)) >= 2:
                    return name
    sender = str(sender_company or "").strip()
    if sender and not _is_generic_name(sender) and contains_term(text, sender):
        return sender
    return ""


def research_queries(offer: str, sender_company: str = "") -> list[str]:
    """At most three searches: the named company, then the category."""
    company = offer_company(offer, sender_company)
    category = category_phrase(offer)
    queries: list[str] = []
    if company:
        queries += [f"{company} competitors", f"alternatives to {company}"]
        if category:
            queries.append(f"{category} competitors")
    elif category:
        queries += [
            f"{category} competitors",
            f"{category} alternatives",
            f"top {category} companies",
        ]
    return queries


@lru_cache(maxsize=1)
def common_words() -> frozenset[str]:
    """The bundled common-English word list (SCOWL 35 plus a supplement)."""
    path = Path(__file__).resolve().parent.parent / "data" / "competitor_common_words.txt"
    words: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            continue
        words.update(line.split())
    return frozenset(words)


def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return re.sub(r"ies$", "y", word)
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return re.sub(r"s$", "", word)
    return word


def buyer_terms(target_description: str) -> frozenset[str]:
    """The kinds of organisation the target names, singular: "accelerators" → "accelerator"."""
    terms = {
        _singular(w)
        for w in _WORD.findall(str(target_description or "").lower())
        if is_evidence_term(w)
    }
    return frozenset(t for t in terms if t not in _GENERIC_HEADS and not t.isdigit())


def _flat(text: str) -> str:
    return re.sub(r"[\s\-_.]+", "", text.casefold())


def _in_snippets(name: str, snippets: str) -> bool:
    """Whole-term, casefolded; "T-Systems" also matches "T Systems"."""
    if contains_term(snippets, name):
        return True
    key = normalize_company(name)
    if key and contains_term(snippets, key):
        return True
    flat = _flat(name)
    return len(flat) >= 4 and flat in _flat(snippets)


def _qualified_form(name: str, snippets: str) -> str:
    """The longer brand the snippets write for a bare word: "Square" → "Square Appointments"."""
    # Only the word itself is matched in any case; the words after it must be
    # capitalised, as a brand is written ("Square Appointments", not "square is").
    pattern = re.compile(
        rf"(?<!\w)(?P<head>(?i:{re.escape(name)}))\s+(?P<tail>[A-Z][\w&\-]*(?:\s+[A-Z][\w&\-]*)?)",
    )
    for match in pattern.finditer(snippets):
        candidate = f"{match.group('head')} {match.group('tail')}"
        key = normalize_company(candidate).split()
        if len(key) >= 2 and not all(w in STOPWORDS for w in key[1:]):
            return candidate
    return ""


def _is_self(name: str, self_keys: list[str]) -> bool:
    key = normalize_company(name)
    return any(
        contains_term(key, own) or contains_term(own, key)
        for own in self_keys
    )


def _self_keys(self_names: list[str]) -> list[str]:
    return [
        k for k in (normalize_company(n) for n in self_names if n)
        if len(k) >= 2 and not _is_generic_name(k)
    ]


def filter_names(
    names: list[str],
    *,
    snippets: str | None,
    target_description: str = "",
    self_names: list[str] | None = None,
    dropped: dict[str, int] | None = None,
) -> list[str]:
    """Keep only names that are a competitor's full brand. Pure, deterministic.

    Drops, in order: the client itself (``self``); a name no snippet writes
    (``not_in_snippets``, only when ``snippets`` is given); a larger
    company's product, "<ACRONYM> <Product>" (``parent_product``: "AWS
    Textract" would match every "AWS" employer); one common word or a short
    acronym that the snippets never write in a longer brand form
    (``common_word``: "Square" is kept as "Square Appointments" when a
    snippet says so); a name whose last word is a buyer the target names
    (``buyer``: "Catalyst Accelerator" for an offer sold to accelerators).
    """
    counts = dropped if dropped is not None else {}
    for rule in DROP_RULES:
        counts.setdefault(rule, 0)
    own = _self_keys(list(self_names or []))
    buyers = buyer_terms(target_description)
    words = common_words()
    kept: list[str] = []
    seen: set[str] = set()
    for raw in parse_competitor_names(names):
        name = " ".join(raw.split())
        if own and _is_self(name, own):
            counts["self"] += 1
            continue
        if snippets is not None and not _in_snippets(name, snippets):
            counts["not_in_snippets"] += 1
            continue
        tokens = name.split()
        if len(tokens) >= 2 and _ACRONYM.match(tokens[0]) and not tokens[0].isdigit():
            counts["parent_product"] += 1
            continue
        key = normalize_company(name)
        key_words = key.split()
        if len(key_words) == 1 and (
            key_words[0] in words or (_ACRONYM.match(name) and name.isupper())
        ):
            longer = _qualified_form(key_words[0], snippets) if snippets else ""
            if not longer:
                counts["common_word"] += 1
                continue
            name, key_words = longer, normalize_company(longer).split()
        if key_words and buyers and _singular(key_words[-1]) in buyers:
            counts["buyer"] += 1
            continue
        nkey = normalize_company(name)
        if nkey in seen:
            continue
        seen.add(nkey)
        kept.append(name)
    return kept


def _extracted_names(raw: Any) -> list[str]:
    if isinstance(raw, dict):
        raw = raw.get("competitors") or raw.get("companies") or []
    if not isinstance(raw, list):
        return parse_competitor_names(raw)
    names: list[Any] = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("name") or item.get("company") or ""
        names.append(item)
    return parse_competitor_names(names)


def names_from_extract(
    raw: Any,
    *,
    self_names: list[str],
    snippets: str | None = None,
    target_description: str = "",
    dropped: dict[str, int] | None = None,
) -> list[str]:
    """Accept a list, {competitors: [...]} or [{name, snippet}], then filter.

    Own is a whole-term match on the normalised names, either way round:
    "HeyLead AI" is HeyLead, and "Northwind" is "Northwind Group Ltd". Exact
    equality let the sender's employer through under any other spelling.
    """
    return filter_names(
        _extracted_names(raw),
        snippets=snippets,
        target_description=target_description,
        self_names=self_names,
        dropped=dropped,
    )


def _snippet_lines(items: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for item in items:
        title = " ".join(str(item.get("title") or "").split())
        snippet = " ".join(str(item.get("snippet") or "").split())
        if not (title or snippet):
            continue
        line = f"{title}: {snippet}"[:_SNIPPET_CHARS]
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
        if len(lines) >= _MAX_SNIPPETS:
            break
    return lines


async def research_competitors(
    offer: str,
    *,
    goal: str,
    sender_company: str = "",
    target_description: str = "",
    outcome: dict[str, Any] | None = None,
) -> list[str]:
    """Search the web for what competes with *offer*. Empty on failure.

    ``sender_company`` and the company the offer names are dropped from the
    answer. ``outcome`` receives ``status``, ``empty_reason`` and the drop
    counts per rule.
    """
    report = outcome if outcome is not None else {}
    report["status"] = "failed"
    if not researches_competitors(goal):
        return []
    queries = research_queries(offer, sender_company)
    if not queries:
        return []
    company = offer_company(offer, sender_company)

    try:
        from ..ai.news_service import _fetch_serper_search
    except Exception:
        logger.info("Competitor search unavailable", exc_info=True)
        return []

    async def one(query: str) -> list[dict[str, Any]] | None:
        try:
            return await _fetch_serper_search(query, num_results=_RESULTS_PER_QUERY)
        except Exception:
            logger.info("Competitor search failed for %r", query, exc_info=True)
            return None

    results = await asyncio.gather(*(one(q) for q in queries))
    items = [item for batch in results if batch for item in batch]
    lines = _snippet_lines(items)
    report["snippets"] = len(lines)
    if not lines:
        if any(batch is not None for batch in results):
            report.update(status="empty", empty_reason="no_snippets")
        return []

    numbered = "\n".join(f"[{i}] {line}" for i, line in enumerate(lines, 1))
    try:
        from ..ai.llm import LLMClient
        from ..constants import LLM_TIER_FAST
        client = LLMClient()
        parsed = await client.generate_json(
            _EXTRACT_PROMPT.format(
                offer=offer_query(offer) or offer[:200],
                target=str(target_description or "(not given)")[:600],
                snippets=numbered,
            ),
            schema=_EXTRACT_SCHEMA,
            system=_EXTRACT_SYSTEM,
            temperature=0.1,
            max_tokens=2048,
            tier=LLM_TIER_FAST,
        )
    except Exception:
        logger.info("Competitor extract failed for %r", queries[0], exc_info=True)
        return []
    dropped: dict[str, int] = {}
    names = names_from_extract(
        parsed,
        self_names=[sender_company, company],
        snippets="\n".join(lines),
        target_description=target_description,
        dropped=dropped,
    )
    raw_names = _extracted_names(parsed)
    report.update(extracted=len(raw_names), dropped=dropped, kept=len(names))
    if names:
        report["status"] = "ok"
    else:
        report.update(
            status="empty",
            empty_reason="all_filtered" if raw_names else "none_extracted",
        )
    logger.info(
        "competitor_research queries=%d snippets=%d extracted=%d kept=%d dropped=%s status=%s",
        len(queries), len(lines), len(raw_names), len(names), dropped, report["status"],
    )
    return names


async def attach_competitors_to_icp(
    result: Any,
    *,
    goal: str,
    offer: str,
    sender_company: str = "",
    target_description: str = "",
) -> list[str]:
    """Research and set ``result.competitors``. Returns the names. Never raises.

    A list the result already carries goes through the name-only rules
    (no snippets to check against), so a saved ICP's old research cannot
    bring "Unicorn" or a buyer back.
    """
    existing = parse_competitor_names(getattr(result, "competitors", None))
    if existing:
        cleaned = filter_names(
            existing, snippets=None, target_description=target_description,
            self_names=[sender_company],
        )
        if hasattr(result, "competitors") and cleaned != existing:
            result.competitors = cleaned
        if cleaned:
            return cleaned
    if not researches_competitors(goal):
        return []
    try:
        names = await research_competitors(
            offer,
            goal=goal,
            sender_company=sender_company,
            target_description=target_description,
        )
    except Exception:
        logger.info("Competitor research skipped", exc_info=True)
        return []
    if names and hasattr(result, "competitors"):
        result.competitors = names
    return names
