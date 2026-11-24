"""What a follow-up may offer: the offer card's ask and nothing else (client mirror of heylead-api#7086).

Until 20 Nov 2026 the follow-up prompts had a ``resource_offer`` CTA and a GOOD
example ending "Happy to share whenever it's useful.", and no rule on what the
sender may offer. The hosted writer offered a deck, "creative ideas" or specs
nobody had on file, and its claim judge repaired 50 of them in 14 days. The
list is the card's ask only (case-pm ruling R2' on #7086): campaign text that
mentions a document does not make it offerable. The text matches heylead-api's
``app/services/offer_commitments.py``.

The client's polish step (``message_improver.improve_message``) has no
campaign context, so its rule only forbids adding an offer the draft does not
already make.
"""

from __future__ import annotations

from typing import Any

from ..services.offer_card import ASK_MAX_WORDS, card_from_context
from . import untrusted

RULE_HEADING = "## WHAT THE SENDER CAN SEND OR SHARE"

# Invitation notes, comments and replies had 0 offer flags in the 14 days
# heylead-api's review read; only DMs and follow-ups carry the rule.
RULED_MESSAGE_TYPES = frozenset({"dm", "followup"})

__all__ = ["ASK_MAX_WORDS", "RULE_HEADING", "allowed_offers", "offer_rule", "polish_rule"]


def allowed_offers(campaign_ctx: dict[str, Any] | None) -> list[str]:
    """The card's ask on one line, capped at ASK_MAX_WORDS words; [] when no card or an empty ask."""
    card = card_from_context(campaign_ctx)
    if card is None:
        return []
    ask = untrusted.neutralise(card.ask, multiline=False)
    ask = " ".join(ask.split()[:ASK_MAX_WORDS])
    return [ask] if ask else []


def offer_rule(campaign_ctx: dict[str, Any] | None) -> str:
    """The rule appended to a follow-up system prompt, before the language rule."""
    offers = allowed_offers(campaign_ctx)
    if offers:
        body = (
            f'- Only what the campaign\'s ask offers, if it offers anything: "{offers[0]}"\n'
            "- Never offer to send, share or show anything else (a deck, a document, specs, ideas, "
            "a case study, a demo, a link), even if the campaign text mentions one, and never write "
            "as if one is ready to send."
        )
    else:
        body = (
            "- Nothing may be offered to send, share or show. Never offer a deck, a document, specs, "
            "ideas, a case study, a demo or a link, even if the campaign text mentions one, and never "
            "write as if one is ready to send."
        )
    return f"\n\n{RULE_HEADING}\n{body}"


def polish_rule(message_type: str) -> str:
    """For a polish of a DM or follow-up: never add an offer; "" for every other message type."""
    if str(message_type or "").strip().lower() not in RULED_MESSAGE_TYPES:
        return ""
    return (
        f"\n\n{RULE_HEADING}\n- Never add an offer to send, share or show something the draft "
        "does not already offer (a deck, a document, specs, ideas, a case study, a demo, a link)."
    )
