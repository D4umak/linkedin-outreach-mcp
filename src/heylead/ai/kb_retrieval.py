"""Retrieval over the shipped sales-methodology knowledge base.

Two card families ship as package data in `heylead/data/kb/`, byte-identical
to heylead-api's `app/data/kb/` and checksummed in both repos:

* `methodology_cards.jsonl` — own-words techniques distilled chapter by
  chapter from 22 sales, outreach, copywriting and persuasion books. Every
  card cites its book and chapter, carries no statistics and no names, and
  was checked against the paragraphs it cites.
* `persona_cards.jsonl` — 17 buyer personas x 4 stages (connect, discovery,
  objection, close). Each card's messaging angles are written for its stage
  and it links the methodology cards that stage should apply
  (`methodology_ids`).

KB v1 (until Sep 2026) was 80 persona cards (20 personas x 4 stages) exported
from the "AI in Charge" archive: 60 cited one book chapter, 20 objection cards
were empty, and the stage cards were copies of each other with invented
"+15-30%" figures.

No network call, no vector database, no API key: the files are read once per
process.

9 Sep 2026: the README has claimed "RAG-powered buyer personas"
since day one, but the only retrieval that ever ran was over the seller's own
crawled website. `PipelineState.kb_chunks`, `icp_sources.source_type='kb'`
and `evidence.kb_results` were dead hooks waiting for this corpus.

Ranking is filter-first, then lexical:
  1. If the caller names personas (or a job title we can map to one), only
     those persona cards are eligible — a CFO campaign must not be told what
     a CISO cares about — and only methodology cards that fit every persona
     or that one.
  2. A stage, channel or objection type narrows or ranks further.
  3. Remaining cards are scored by IDF-weighted token overlap with the query
     (BM25-lite: no corpus is large enough here for full BM25 to pay off).

The retrieval logic mirrors heylead-api `app/services/rag/kb.py`. The client
routes titles through `ai/persona_cards.py` (`archive_personas_for_title`),
whose alias table is byte-for-byte the backend's.
"""

from __future__ import annotations

import functools
import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

KB_DIR = Path(__file__).resolve().parent.parent / "data" / "kb"
KB_CARDS_PATH = KB_DIR / "persona_cards.jsonl"
KB_METHODOLOGY_PATH = KB_DIR / "methodology_cards.jsonl"
KB_PERSONALIZATION_PATH = KB_DIR / "persona_personalization.md"
KB_CHECKSUM_PATH = KB_DIR / "SHA256"

FAMILY_PERSONA = "persona"
FAMILY_METHODOLOGY = "methodology"

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    """a an and are as at be by for from has have how in is it its of on or
    that the their they this to was were what when which who will with your
    our we you i""".split()
)

# KB v1 persona ids -> the v2 persona that now covers them (None: retired,
# not a buyer HeyLead targets). Honoured for one release so a caller that
# still passes an old id gets the covering cards instead of nothing. Same
# table as heylead-api `app/services/rag/kb.py:LEGACY_PERSONA_IDS`.
LEGACY_PERSONA_IDS: dict[str, str | None] = {
    "PRESIDENT": "FOUNDER_CEO",
    "CINO": "CSO_STRATEGY",
    "CIO_INVESTMENT": "CFO",
    "CDO_DIVERSITY": "CHRO",
    "CHIEF_RESEARCH_OFFICER": None,
    "SDR": None,
}


@dataclass
class KbCard:
    """One retrieved card: a persona card or a methodology card."""

    id: str = ""
    family: str = FAMILY_PERSONA
    personas: list[str] = field(default_factory=list)
    stage: str = ""
    stages: list[str] = field(default_factory=list)
    kpi: str = ""
    pain: str = ""
    triggers: list[str] = field(default_factory=list)
    pain_symptoms: list[str] = field(default_factory=list)
    messaging_angles: list[str] = field(default_factory=list)
    value_drivers: list[str] = field(default_factory=list)
    profile_cues: list[str] = field(default_factory=list)
    company_cues: list[str] = field(default_factory=list)
    objection_types: list[str] = field(default_factory=list)
    methodology_ids: list[str] = field(default_factory=list)
    principle: str = ""
    how_to_apply: list[str] = field(default_factory=list)
    example_pattern: str = ""
    anti_patterns: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    reply_sentiments: list[str] = field(default_factory=list)
    citation: str = ""
    source_title: str = ""
    source_author: str = ""
    score: float = 0.0
    #: This card names the objection the caller asked about. Ranking
    #: treats that as decisive rather than as a score bonus: lexical
    #: scores are IDF sums in the 5-7 range, so any additive nudge
    #: small enough to be safe is also too small to be felt.
    answers_objection: bool = False

    @property
    def source(self) -> str:
        if self.source_title and self.source_author:
            base = f"{self.source_title} — {self.source_author}"
        else:
            base = self.source_title or self.source_author or "HeyLead KB"
        return f"{base}, {self.citation}" if self.citation else base

    def matches_stage(self, stage: str) -> bool:
        return stage in (self.stages or [self.stage])

    def _content_bits(self, for_prompt: bool = False) -> list[str]:
        if self.family == FAMILY_METHODOLOGY:
            if for_prompt:
                # A copy prompt clips each line at 400 characters and must
                # never carry a square-bracket placeholder, so it gets the
                # technique and its first two steps — no message skeleton to
                # copy, no [PROSPECT] token to leak.
                bits = [f"technique: {_debracket(self.principle)}"] if self.principle else []
                if self.how_to_apply:
                    bits.append("apply: " + "; ".join(_debracket(s) for s in self.how_to_apply[:2]))
                return bits
            bits = [f"technique: {self.principle}"] if self.principle else []
            if self.how_to_apply:
                bits.append("apply: " + "; ".join(self.how_to_apply[:4]))
            if self.example_pattern:
                bits.append(f"pattern: {self.example_pattern}")
            if self.anti_patterns:
                bits.append("avoid: " + "; ".join(self.anti_patterns[:2]))
            return bits
        if for_prompt:
            # Under the prompt's 400-character clip, most useful first: what
            # to lead with and what pushback to expect, then the facts.
            bits = []
            if self.messaging_angles:
                bits.append("angles: " + ", ".join(self.messaging_angles[:3]))
            if self.objection_types:
                bits.append("likely objections: " + ", ".join(o.replace("_", " ") for o in self.objection_types[:3]))
            if self.pain:
                bits.append(f"pain: {self.pain}")
            if self.kpi:
                bits.append(f"kpi: {self.kpi}")
            if self.value_drivers:
                bits.append("value: " + ", ".join(self.value_drivers[:2]))
            if self.triggers:
                bits.append("triggers: " + ", ".join(self.triggers[:3]))
            return bits
        bits = []
        if self.pain:
            bits.append(f"pain: {self.pain}")
        if self.kpi:
            bits.append(f"kpi: {self.kpi}")
        if self.triggers:
            bits.append("triggers: " + ", ".join(self.triggers[:4]))
        if self.messaging_angles:
            bits.append("angles: " + ", ".join(self.messaging_angles[:3]))
        if self.objection_types:
            bits.append("likely objections: " + ", ".join(o.replace("_", " ") for o in self.objection_types[:4]))
        if self.profile_cues:
            bits.append("profile cues: " + ", ".join(self.profile_cues[:3]))
        if self.pain_symptoms:
            bits.append("symptoms: " + ", ".join(self.pain_symptoms[:4]))
        if self.value_drivers:
            bits.append("value: " + ", ".join(self.value_drivers[:3]))
        return bits

    def as_prompt_evidence(self) -> str:
        """The card's content only — nothing that identifies where it came from.

        What a copy prompt may see. ``as_evidence_line`` opens with a header
        naming the persona id, the stage and the real book, author and
        chapter. A draft that echoed that header named a book the sender
        never mentioned, in a bracket shape no guardrail regex matched.
        Prompts get this; the tagged line stays for logs, ICP and debugging.
        """
        return " | ".join(self._content_bits(for_prompt=True))

    def as_evidence_line(self) -> str:
        """One prompt line. Persona/stage/source first so the LLM can cite it."""
        if self.family == FAMILY_METHODOLOGY:
            label = "method"
            stage = "/".join(self.stages or [self.stage])
        else:
            label = "/".join(self.personas) or "general"
            stage = self.stage
        return " | ".join([f"[{label} · {stage} · {self.source}]", *self._content_bits()])

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "family": self.family,
            "personas": self.personas,
            "stage": self.stage,
            "kpi": self.kpi,
            "pain": self.pain,
            "triggers": self.triggers,
            "messaging_angles": self.messaging_angles,
            "value_drivers": self.value_drivers,
            "principle": self.principle,
            "source": self.source,
            "score": round(self.score, 4),
        }


_BRACKET_RE = re.compile(r"\[([A-Za-z_ ]{1,20})\]")


def _debracket(text: str) -> str:
    """"[TRIGGER]" -> "the trigger": prompts never carry bracket placeholders."""
    return _BRACKET_RE.sub(lambda m: "the " + m.group(1).lower().replace("_", " "), text or "")


def _strs(values: Any) -> list[str]:
    return [str(v) for v in (values or []) if str(v).strip()]


def _citation_label(raw: dict[str, Any]) -> str:
    cit = raw.get("citation") or {}
    label = str(cit.get("chapter_title") or "")
    if cit.get("page_start"):
        label += f", p. {cit['page_start']}"
    return label


def _card_from_raw(raw: dict[str, Any]) -> KbCard:
    src = raw.get("source") or {}
    if raw.get("family") == FAMILY_METHODOLOGY:
        when = raw.get("when_to_use") or {}
        fit = _strs(raw.get("persona_fit"))
        stages = _strs(when.get("stages"))
        return KbCard(
            id=str(raw.get("id") or ""),
            family=FAMILY_METHODOLOGY,
            personas=[] if fit in ([], ["*"]) else fit,
            stage=stages[0] if stages else "",
            stages=stages,
            principle=str(raw.get("principle") or ""),
            how_to_apply=_strs(raw.get("how_to_apply")),
            example_pattern=str(raw.get("example_pattern") or ""),
            anti_patterns=_strs(raw.get("anti_patterns")),
            channels=_strs(when.get("channels")),
            reply_sentiments=_strs(when.get("reply_sentiments")),
            objection_types=_strs(when.get("objection_types")),
            citation=_citation_label(raw),
            source_title=str(src.get("title") or ""),
            source_author=str(src.get("author") or ""),
        )
    vp = raw.get("value_props") or {}
    pz = raw.get("personalization") or {}
    stage = str(raw.get("stage") or "")
    return KbCard(
        id=str(raw.get("id") or ""),
        family=FAMILY_PERSONA,
        personas=_strs(raw.get("personas")),
        stage=stage,
        stages=[stage] if stage else [],
        kpi=str(raw.get("kpi") or ""),
        pain=str(raw.get("pain") or ""),
        triggers=_strs(raw.get("triggers")),
        pain_symptoms=_strs(raw.get("pain_symptoms")),
        messaging_angles=_strs(vp.get("messaging_angles")),
        value_drivers=_strs(vp.get("value_drivers")),
        profile_cues=_strs(pz.get("profile_cues")),
        company_cues=_strs(pz.get("company_cues")),
        objection_types=_strs(raw.get("objection_types")),
        methodology_ids=_strs(raw.get("methodology_ids")),
        source_title=str(src.get("title") or ""),
        source_author=str(src.get("author") or ""),
    )


def _stem(token: str) -> str:
    """Crude plural strip. "CFOs at fintechs" must reach the CFO cards.

    Whole-word matching is the house rule (`textutil.contains_term`); this is
    the same discipline one step looser — an exact token after removing a
    trailing plural, never a substring.
    """
    if len(token) > 3 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("ses"):
        return token[:-2]
    if len(token) > 2 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokens(text: str) -> list[str]:
    return [
        _stem(t)
        for t in _TOKEN_RE.findall((text or "").lower())
        if t not in _STOPWORDS
    ]


def _searchable_text(raw: dict[str, Any]) -> str:
    def words(values: Any) -> str:
        return " ".join(str(v).replace(":", " ").replace("_", " ") for v in (values or []))

    if raw.get("family") == FAMILY_METHODOLOGY:
        when = raw.get("when_to_use") or {}
        return " ".join(p for p in (
            str(raw.get("principle") or ""),
            words(raw.get("how_to_apply")),
            str(raw.get("example_pattern") or "").replace("[", " ").replace("]", " "),
            words(raw.get("anti_patterns")),
            words(raw.get("tags")),
            words(when.get("stages")),
            words(when.get("objection_types")),
            words(when.get("reply_sentiments")),
            words([p for p in raw.get("persona_fit") or [] if p != "*"]),
        ) if p)
    parts: list[str] = [
        words(raw.get("personas")),
        str(raw.get("stage") or ""),
        str(raw.get("kpi") or ""),
        str(raw.get("pain") or ""),
        " ".join(raw.get("triggers") or []),
        " ".join(raw.get("pain_symptoms") or []),
        words(raw.get("tags")),
        words(raw.get("objection_types")),
    ]
    vp = raw.get("value_props") or {}
    for key in ("pains", "kpis", "messaging_angles", "value_drivers"):
        parts.append(" ".join(str(x) for x in (vp.get(key) or [])))
    pz = raw.get("personalization") or {}
    for key in ("profile_cues", "company_cues"):
        parts.append(" ".join(str(x) for x in (pz.get(key) or [])))
    return " ".join(p for p in parts if p)


@dataclass
class _Corpus:
    cards: list[KbCard]
    token_sets: list[set[str]]
    idf: dict[str, float]
    by_id: dict[str, int] = field(default_factory=dict)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        logger.warning("KB file missing at %s — its cards are not retrievable", path)
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("Skipping malformed KB card line in %s", path.name)
    return rows


@functools.lru_cache(maxsize=1)
def _load_corpus() -> _Corpus:
    cards: list[KbCard] = []
    token_sets: list[set[str]] = []
    for raw in _read_jsonl(KB_CARDS_PATH) + _read_jsonl(KB_METHODOLOGY_PATH):
        cards.append(_card_from_raw(raw))
        token_sets.append(set(_tokens(_searchable_text(raw))))

    n = len(cards) or 1
    df: dict[str, int] = {}
    for ts in token_sets:
        for tok in ts:
            df[tok] = df.get(tok, 0) + 1
    idf = {tok: math.log(1.0 + n / (1 + count)) for tok, count in df.items()}
    return _Corpus(cards, token_sets, idf, {c.id: i for i, c in enumerate(cards)})


def load_kb_cards(family: str | None = None) -> list[KbCard]:
    """Every shipped card of a family (all when None), unranked."""
    return [c for c in _load_corpus().cards if family is None or c.family == family]


def kb_card_count(family: str | None = None) -> int:
    return len(load_kb_cards(family))


def personas_for_titles(titles: list[str]) -> list[str]:
    """Persona keys covering a list of ICP job titles."""
    from .persona_cards import archive_personas_for_title

    out: list[str] = []
    for title in titles:
        for key in archive_personas_for_title(title):
            if key not in out:
                out.append(key)
    return out


def _canonical_personas(personas: list[str] | None) -> set[str]:
    """Upper-cased persona ids, with KB v1 ids mapped to their v2 cover."""
    out: set[str] = set()
    for p in personas or []:
        key = str(p).upper()
        if key in LEGACY_PERSONA_IDS:
            mapped = LEGACY_PERSONA_IDS[key]
            if mapped:
                out.add(mapped)
        else:
            out.add(key)
    return out


_PHRASE_RE = re.compile(r"[^a-zA-Z0-9]+")


def _persona_phrases(query: str) -> list[str]:
    """Candidate title phrases inside a free-text query.

    `archive_personas_for_title` expects a title, not a sentence, so feed it
    the query's word n-grams (1-3 words) rather than the whole string.
    """
    words = [w for w in _PHRASE_RE.split(query or "") if w]
    out: list[str] = []
    for n in (3, 2, 1):
        for i in range(len(words) - n + 1):
            out.append(" ".join(words[i : i + n]))
    return out


def retrieve_kb(
    query: str,
    *,
    personas: list[str] | None = None,
    stage: str | None = None,
    top_k: int = 8,
    family: str | None = FAMILY_PERSONA,
    channel: str | None = None,
    objection_type: str | None = None,
) -> list[KbCard]:
    """Top-k knowledge-base cards for a query.

    Args:
        query: free text — a target description, a campaign goal, an ICP name.
        personas: persona keys (`CFO`, `VP_SALES`, …). When given, only those
            personas' persona cards are eligible, and only methodology cards
            that fit every persona or one of these. KB v1 ids are mapped to
            the v2 persona that covers them; asking only for retired
            personas returns [].
        stage: one of connect / discovery / objection / close.
        top_k: how many cards to return.
        family: "persona" (the default — what the ICP and goal-match callers
            read), "methodology", or None for both. Copy drafting uses
            :func:`retrieve_for_copy`.
        channel: excludes methodology cards written for other channels.
        objection_type: ranks up cards that answer this objection.

    Returns:
        Cards sorted by descending score. Empty when nothing matches — the
        caller must be able to proceed with no evidence rather than invent it.
    """
    corpus = _load_corpus()
    if not corpus.cards or top_k <= 0:
        return []

    explicit = personas is not None and len(personas) > 0
    wanted = _canonical_personas(personas)
    if explicit and not wanted:
        return []  # only retired personas were asked for
    if not wanted:
        # No caller-supplied personas: try to read one out of the query
        # itself ("CFOs at fintech startups" -> CFO). A hit narrows the
        # candidate set the way an explicit persona would; a miss leaves
        # every card eligible and lexical ranking decides.
        wanted = set(personas_for_titles(_persona_phrases(query)))

    q_set = set(_tokens(query))
    if not q_set and not wanted and not objection_type:
        return []

    scored: list[KbCard] = []
    for card, tokens in zip(corpus.cards, corpus.token_sets):
        if family and card.family != family:
            continue
        card_personas = {p.upper() for p in card.personas}
        if wanted:
            if card.family == FAMILY_PERSONA and not (card_personas & wanted):
                continue
            if card.family == FAMILY_METHODOLOGY and card_personas and not (card_personas & wanted):
                continue
        if stage and not card.matches_stage(stage):
            continue
        if channel and card.family == FAMILY_METHODOLOGY and card.channels and channel not in card.channels:
            continue
        score = sum(corpus.idf.get(tok, 0.0) for tok in (q_set & tokens))
        if wanted and card_personas & wanted:
            # A persona-filtered card is relevant by construction; the lexical
            # score only orders the stages within that persona.
            score += 1.0
        answers = bool(objection_type) and objection_type in card.objection_types
        if score <= 0 and not answers:
            continue
        hit = KbCard()
        hit.__dict__.update(card.__dict__)
        hit.score = score
        hit.answers_objection = answers
        scored.append(hit)

    scored.sort(key=lambda c: (not c.answers_objection, -c.score, c.id))
    return scored[:top_k]


def retrieve_for_copy(
    query: str,
    *,
    personas: list[str] | None = None,
    stage: str | None = None,
    channel: str | None = None,
    objection_type: str | None = None,
    max_cards: int = 3,
) -> list[KbCard]:
    """The cards a drafting prompt gets: one persona card, then methodology.

    The persona card for this buyer and stage comes first. Its linked
    methodology cards follow (those written for this channel, the ones
    answering this objection first), and the lexical best matches fill any
    room left. Without a recognisable persona it is methodology only.
    Render them with :meth:`KbCard.as_prompt_evidence`, never the tagged
    evidence line, so a draft cannot echo a book title.
    """
    if max_cards <= 0:
        return []
    corpus = _load_corpus()
    out: list[KbCard] = []
    wanted = _canonical_personas(personas) or set(personas_for_titles(_persona_phrases(query)))
    persona_hits = (
        retrieve_kb(query, personas=sorted(wanted), stage=stage, top_k=1, family=FAMILY_PERSONA)
        if wanted else []
    )
    if persona_hits:
        out.append(persona_hits[0])

    # Methodology: lexical ranking for this stage/channel/objection, with the
    # persona card's linked cards ranked up rather than placed first — a
    # link says "fits this buyer", the query says "fits this message".
    linked_ids = set(persona_hits[0].methodology_ids) if persona_hits else set()
    ranked = retrieve_kb(
        query, personas=sorted(wanted) or None, stage=stage, top_k=max_cards * 6,
        family=FAMILY_METHODOLOGY, channel=channel, objection_type=objection_type,
    )
    seen_ranked = {c.id for c in ranked}
    for mid in linked_ids - seen_ranked:
        idx = corpus.by_id.get(mid)
        if idx is None:
            continue
        card = corpus.cards[idx]
        if stage and not card.matches_stage(stage):
            continue
        if channel and card.channels and channel not in card.channels:
            continue
        hit = KbCard()
        hit.__dict__.update(card.__dict__)
        hit.score = 0.0
        hit.answers_objection = bool(objection_type) and objection_type in card.objection_types
        ranked.append(hit)
    for card in ranked:
        if card.id in linked_ids:
            card.score += 1.0
    ranked.sort(key=lambda c: (not c.answers_objection, -c.score, c.id))

    seen = {c.id for c in out}
    books: set[str] = set()
    for card in ranked:
        if len(out) >= max_cards:
            break
        if card.id in seen or card.source_title in books:
            continue  # two techniques from one book read as one voice
        seen.add(card.id)
        books.add(card.source_title)
        out.append(card)
    return out


def format_kb_evidence(cards: list[KbCard], max_cards: int = 8) -> str:
    """Render retrieved cards as the prompt's evidence block.

    Mirrors the original ICP agent's `_format_kb_results_for_synthesis`:
    numbered, persona/stage/source labelled, so the model can cite or abstain.
    """
    if not cards:
        return ""
    return "\n".join(
        f"{i}. {card.as_evidence_line()}" for i, card in enumerate(cards[:max_cards], 1)
    )
