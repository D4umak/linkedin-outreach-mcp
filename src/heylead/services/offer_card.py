"""One Offer card per campaign (D4umak/heylead-api#1993).

The card is what every message lands: what changes for the reader, in the
reader's words. It is distilled once from the operator's paste, stored on
``context_json.offer``, confirmed by the owner, and read by every generator
instead of the ICP's ``relevance_hook`` ("why the sender is credible").

This file is the client twin of heylead-api's app/services/offer_card.py:
the pure functions below are byte-identical there. Change both in one
sitting; tests/test_offer_card.py compares them when the api checkout is
found.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from ..ai.message_validator import PRODUCT_NOUNS
from ..textutil import contains_term

CARD_VERSION = 1
OUTCOME_MAX_WORDS = 18
HOW_MAX_WORDS = 14
ASK_MAX_WORDS = 20
PROOF_MAX_WORDS = 24

# The paste fields the card is distilled from. A change to any of them
# changes the hash, which unconfirms the card.
SOURCE_FIELDS = ("project_brief", "offerings", "case_studies", "social_proofs", "campaign_preferences")

# card_from_context coercion: text fields are always str(); these timestamp
# fields are always int() (or None on a bad value, including a bool).
TEXT_FIELDS = ("for_", "outcome", "how", "proof", "ask", "confirmed_by", "source_hash")
INT_FIELDS = ("confirmed_at", "generated_at", "alert_sent_at")

_PLACEHOLDER_RE = re.compile(r"\[[A-Za-z][^\]\n]{0,30}\]")


@dataclass
class OfferCard:
    for_: str = ""
    outcome: str = ""
    how: str = ""
    proof: str = ""
    ask: str = ""
    source_hash: str = ""
    generated_at: int | None = None
    confirmed_at: int | None = None
    confirmed_by: str = ""
    needs_review: bool = False
    alert_sent_at: int | None = None
    version: int = CARD_VERSION
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def confirmed(self) -> bool:
        return bool(self.confirmed_at)


def _words(text: str) -> int:
    return len([w for w in str(text or "").split() if w.strip()])


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def offer_card_defects(card: OfferCard) -> list[str]:
    """Deterministic checks. Empty means the card may be shown and sent from."""
    defects: list[str] = []
    outcome, how, ask, proof = _clean(card.outcome), _clean(card.how), _clean(card.ask), _clean(card.proof)
    if not outcome:
        defects.append("outcome is empty")
    if _words(outcome) > OUTCOME_MAX_WORDS:
        defects.append(f"outcome is over {OUTCOME_MAX_WORDS} words")
    outcome_norm = outcome.lower().replace("-", " ")
    for noun in PRODUCT_NOUNS:
        noun_norm = noun.replace("-", " ")
        if contains_term(outcome_norm, noun_norm):
            defects.append(f"outcome names a product noun: {noun}")
    if re.match(r"^\s*(I|We)\b", outcome, flags=re.IGNORECASE):
        defects.append("outcome starts with the sender")
    if not ask:
        defects.append("ask is empty")
    elif not ask.rstrip().endswith("?"):
        defects.append("ask is not a question")
    if ask.count("?") > 1:
        defects.append("ask holds more than one question")
    if _words(ask) > ASK_MAX_WORDS:
        defects.append(f"ask is over {ASK_MAX_WORDS} words")
    if _words(how) > HOW_MAX_WORDS:
        defects.append(f"how is over {HOW_MAX_WORDS} words")
    if _words(proof) > PROOF_MAX_WORDS:
        defects.append(f"proof is over {PROOF_MAX_WORDS} words")
    for name, text in (("outcome", outcome), ("how", how), ("ask", ask), ("proof", proof), ("for", _clean(card.for_))):
        if _PLACEHOLDER_RE.search(text):
            defects.append(f"{name} holds a placeholder")
    return defects


def source_hash(context: dict[str, Any] | None) -> str:
    """sha256 of the paste fields, so an edit unconfirms the card."""
    ctx = context or {}
    payload = json.dumps({k: _clean(ctx.get(k)) for k in SOURCE_FIELDS}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def card_to_dict(card: OfferCard) -> dict[str, Any]:
    data = asdict(card)
    data["for"] = data.pop("for_")
    extra = data.pop("extra") or {}
    return {**extra, **data}


def card_from_context(context: dict[str, Any] | None) -> OfferCard | None:
    """The stored card, or None when the campaign has none (or junk)."""
    raw = (context or {}).get("offer")
    if not isinstance(raw, dict):
        return None
    known = {f for f in OfferCard.__dataclass_fields__ if f != "extra"}
    kwargs: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for key, value in raw.items():
        if key == "stale":
            continue
        name = "for_" if key == "for" else key
        if name in known:
            kwargs[name] = value
        else:
            extra[key] = value
    # A hand-edited row must not make `confirmed` truthy with a string (or a
    # bool: bool is an int subclass, so int(True) == 1 would wrongly
    # confirm), and text fields must not be a stray dict/list/number.
    for name in TEXT_FIELDS:
        if name in kwargs and kwargs[name] is not None:
            kwargs[name] = str(kwargs[name])
    for name in INT_FIELDS:
        if name in kwargs and kwargs[name] is not None:
            value = kwargs[name]
            if isinstance(value, bool):
                kwargs[name] = None
            else:
                try:
                    kwargs[name] = int(value)
                except (TypeError, ValueError):
                    kwargs[name] = None
    try:
        card = OfferCard(**kwargs, extra=extra)
    except TypeError:
        return None
    if not _clean(card.outcome) and not _clean(card.ask):
        return None
    return card


def confirm_card(card: OfferCard, *, by: str, now: int) -> OfferCard:
    return replace(card, confirmed_at=int(now), confirmed_by=str(by or ""), needs_review=False)


def card_is_stale(card: OfferCard | None, context: dict[str, Any] | None) -> bool:
    """True when the paste changed since the card was written."""
    if card is None:
        return False
    return bool(card.source_hash) and card.source_hash != source_hash(context)


def render_offer_block(card: OfferCard, *, touch: str) -> str:
    """The prompt section every generator reads.

    ``touch`` is "first_touch" (note, first DM, InMail, email, any
    follow-up) or "reply". A first touch sees the outcome and the ask only;
    a reply may add how and proof. Never carries a bracketed token.
    """
    lines = [
        "THE OFFER (what this message lands; the style rules below govern only how it sounds)",
        f"For: {_clean(card.for_)}" if _clean(card.for_) else "",
        f"What changes for them, in their words: {_clean(card.outcome)}",
        f"The one question to ask: {_clean(card.ask)}",
    ]
    if touch == "reply":
        if _clean(card.how):
            lines.append(f"What the sender does (say it once, only now that they replied): {_clean(card.how)}")
        if _clean(card.proof):
            lines.append(f"Proof (at most once per thread): {_clean(card.proof)}")
    else:
        lines.append("Do not say what the sender builds, runs or leads. Do not name a product, platform or programme. The message is about them.")
    return _PLACEHOLDER_RE.sub("", "\n".join(line for line in lines if line))


# ── Distilling (one model call; the api holds the same prompt text) ──

DISTIL_SYSTEM = (
    "You turn a seller's own description of what they offer into five short lines "
    "a buyer would say. Plain words. No product nouns (" + ", ".join(PRODUCT_NOUNS) + "). "
    "Never invent a fact, a number or a name. Return ONLY JSON."
)

DISTIL_PROMPT = """From the seller's paste below, write the Offer card.

## GOAL
{goal_line}

## THE SELLER'S PASTE
{paste}

## WRITE
- "for": who it is for, one phrase (from the paste; empty if the paste does not say).
- "outcome": what changes for that person, in their words, at most {outcome_max} words. Not what the seller builds. Not a sentence starting with I or We.
- "how": what the seller does, at most {how_max} words. May name the craft. Never who it is for.
- "proof": one fact from the paste, at most {proof_max} words, or empty. Never invented.
- "ask": one question, at most {ask_max} words, ending in ?, that this person can answer about their own week with yes, no or a number.
{defects_section}
Return ONLY a JSON object with keys for, outcome, how, proof, ask."""

_GOAL_LINES = {
    "sell": "The seller wants this person to buy or champion what they offer.",
    "partner": "The seller proposes a partnership; the outcome is what the partner gains.",
    "hire": "The seller is hiring; the outcome is what the candidate gains.",
    "buy": "The seller is the buyer; the outcome is what the vendor gains by answering.",
    "research": "The seller wants an interview; the outcome is what the participant gains.",
}


def paste_text(context: dict[str, Any] | None) -> str:
    ctx = context or {}
    parts = [f"{k}: {_clean(ctx.get(k))}" for k in SOURCE_FIELDS if _clean(ctx.get(k))]
    return "\n".join(parts)


def _first_sentence(text: str) -> str:
    text = _clean(text)
    if not text:
        return ""
    return re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0][:160]


def card_from_model(data: dict[str, Any], context: dict[str, Any], *, now: int) -> OfferCard:
    return OfferCard(
        for_=_clean(data.get("for")),
        outcome=_clean(data.get("outcome")),
        how=_clean(data.get("how")),
        proof=_clean(data.get("proof")),
        ask=_clean(data.get("ask")),
        source_hash=source_hash(context),
        generated_at=int(now),
    )


async def distil_offer(context: dict[str, Any] | None, *, goal: str = "sell", now: int | None = None) -> OfferCard | None:
    """One model call, one retry with the defects listed, then needs_review.

    None when there is no paste at all: a card is never invented from nothing.
    """
    import time

    from ..ai.llm import loads_json_object
    from ..ai.llm_router import call_llm

    ctx = context or {}
    paste = paste_text(ctx)
    if not paste:
        return None
    stamp = int(now if now is not None else time.time())
    goal_line = _GOAL_LINES.get(goal or "sell", _GOAL_LINES["sell"])
    defects: list[str] = []
    card: OfferCard | None = None
    for _attempt in range(2):
        section = ""
        if defects:
            section = "\n## THE LAST ATTEMPT FAILED THESE CHECKS; FIX THEM\n" + "\n".join(f"- {d}" for d in defects) + "\n"
        prompt = DISTIL_PROMPT.format(
            goal_line=goal_line, paste=paste, outcome_max=OUTCOME_MAX_WORDS, how_max=HOW_MAX_WORDS,
            proof_max=PROOF_MAX_WORDS, ask_max=ASK_MAX_WORDS, defects_section=section,
        )
        raw = await call_llm(prompt, system=DISTIL_SYSTEM, temperature=0.2, max_tokens=600, json_mode=True)
        card = card_from_model(loads_json_object(raw, {}), ctx, now=stamp)
        defects = offer_card_defects(card)
        if not defects:
            return card
    assert card is not None
    # The spec: on the second failure the outcome is the raw first sentence
    # of the first non-empty SOURCE_FIELDS value (never the "field: " label
    # paste_text prefixes it with), never the model's still-defective
    # outcome, and never empty (paste_text is non-empty here, since we
    # returned early above).
    fallback_outcome = ""
    for key in SOURCE_FIELDS:
        cleaned = _clean(ctx.get(key))
        if cleaned:
            fallback_outcome = _first_sentence(cleaned)
            break
    return replace(card, outcome=fallback_outcome, needs_review=True)


# ── The hold (read at send time, never remembered) ──

HOLD_CONFIRM = "Confirm what you offer before the first message goes out: the campaign's Offer card is waiting for your yes."
HOLD_CHANGED = "Your offer text changed, so the Offer card was rewritten; confirm it before the next first message goes out."


def first_touch_hold_reason(campaign: dict[str, Any] | None) -> str:
    """Why a first touch (note, first DM, InMail, email) may not go out now.

    Empty when it may. A campaign with no card at all is not held: the card
    is written by the refresh job and the hold starts then. Job-search
    campaigns have no card.
    """
    from .project_brief import parse_campaign_context

    if not campaign:
        return ""
    try:
        config = json.loads(campaign.get("config_json") or "{}") if isinstance(campaign.get("config_json"), str) else (campaign.get("config_json") or {})
    except (json.JSONDecodeError, TypeError):
        config = {}
    ctx = parse_campaign_context(campaign)
    if str((ctx.get("campaign_type") or config.get("campaign_type") or "")).strip().lower() == "job_search":
        return ""
    card = card_from_context(ctx)
    if card is None:
        return ""
    if card_is_stale(card, ctx):
        return HOLD_CHANGED
    if not card.confirmed:
        return HOLD_CONFIRM
    return ""


def hold_message(campaign_id: str, reason: str) -> str:
    """The ⏸️ line generate_send and send_inmail return when a first touch holds."""
    return (
        f"⏸️ {reason} Confirm with edit_campaign(campaign_id='{campaign_id}', "
        "offer_confirm='on') after reading it in campaign_status(action='plan')."
    )
