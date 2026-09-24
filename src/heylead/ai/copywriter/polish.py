"""The pass that makes a draft obey the house rules before anyone reads it.

The client mirror of heylead-api's ``app/services/copywriter/polish.py``
(22 Sep 2026). The rules table reached every prompt on 21 Sep and the model
ignored it anyway: every comment reply for a week opened "Spot on,". This
reads a finished draft back. Three steps, cheapest first:

1. Regexes find the offences. No model call, so a draft that already obeys
   the rules costs nothing, and that is the common case.
2. One rewrite, asked to fix exactly the offences and change nothing else.
   The channel's rules ride along on the system prompt.
3. What the model would not fix is taken out by hand. A formula opener is a
   prefix and a dash is punctuation; deleting either cannot invent a fact,
   which is the one thing a repair must never do.

The regexes and the repair are the api's, kept in step by hand; only the
model call differs (``LLMClient.generate`` here, ``llm.generate_text`` there).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .render import rules_for
from .rules import RULES

logger = logging.getLogger(__name__)

# ── what a broken rule looks like in finished text ──

_FORMULA = (
    r"spot[\s-]*on|agreed|absolutely|exactly this|love this|so true|"
    r"great post|great point|well said|this resonates|nothing beats|"
    r"couldn't agree more|could not agree more|congrats on"
)
# The formula, then optionally the reader (a mention token or a first name),
# then the punctuation that ends the throat-clearing. The case-insensitive
# part is the formula alone: a global IGNORECASE lets [A-Z][a-z]+ eat a real
# word ("whoever") as the reader's name.
_TEMPLATE_OPENER = re.compile(
    rf"^\W*(?i:{_FORMULA})\b"
    r"(?:\s*[,!]?\s*(?:\{\{\d+\}\}|[A-Z][a-z]+))?"
    r"\s*[.!,]\s+"
)
_SIGN_OFF = re.compile(
    r"\n\s*(?:best|cheers|regards|kind regards|warm regards|sincerely|thanks)"
    r"[,!.]?\s*(?:[A-Z][a-z]+)?\s*$|\n\s*[-–—]\s*[A-Z][a-z]+\s*$",
    re.IGNORECASE,
)
_DASH = re.compile(r"[—–―]")
_PLACEHOLDER = re.compile(r"\[[A-Za-z][^\]\n]{0,30}\]")

_CHECKS: dict[str, re.Pattern[str]] = {
    "no-template-openers": _TEMPLATE_OPENER,
    "no-sign-off": _SIGN_OFF,
    "no-dashes": _DASH,
    "no-placeholders": _PLACEHOLDER,
}

_RULE_BY_ID = {rule.id: rule for rule in RULES}


@dataclass(frozen=True)
class Polished:
    """What came back, and what had to be done to it."""

    text: str
    offences: tuple[str, ...] = ()
    repaired: tuple[str, ...] = ()
    refused: str = ""

    @property
    def clean(self) -> bool:
        return not self.refused


def offences(draft: str, channel: str) -> tuple[str, ...]:
    """The ids of the rules this draft breaks, in the order they are checked.

    Only the rules that bind this channel: a sign-off is an offence in a DM
    and the right thing to write in an email.
    """
    text = draft or ""
    found = []
    for rule_id, pattern in _CHECKS.items():
        rule = _RULE_BY_ID.get(rule_id)
        if rule is None or not rule.binds(channel):
            continue
        if pattern.search(text):
            found.append(rule_id)
    return tuple(found)


def normalize_dashes(text: str) -> str:
    """The api's message_guardrails.normalize_dashes: an en dash joining two
    words is a range and keeps a hyphen; every other dash is a clause break
    and becomes a comma."""
    if not text:
        return text
    text = re.sub(r"(?<=\w)–(?=\w)", "-", text)
    text = re.sub(r"\s*[—–―]+\s*", ", ", text)
    text = re.sub(r",(\s*,)+", ",", text)
    text = re.sub(r"^\s*,\s*", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text


def repair(draft: str, channel: str) -> tuple[str, tuple[str, ...]]:
    """Take out by hand what a rewrite would not take out. Deletion only."""
    text = draft or ""
    done: list[str] = []
    current = offences(text, channel)

    if "no-template-openers" in current:
        stripped = _TEMPLATE_OPENER.sub("", text, count=1).lstrip()
        if stripped and stripped != text:
            text = stripped[0].upper() + stripped[1:]
            done.append("no-template-openers")
    if "no-sign-off" in current:
        text = _SIGN_OFF.sub("", text).rstrip()
        done.append("no-sign-off")
    if "no-dashes" in current:
        text = normalize_dashes(text)
        done.append("no-dashes")
    return text, tuple(done)


_POLISH_SYSTEM = (
    "You are the last reader of a draft before the person it is written for "
    "sees it. The draft is already about the right thing: your only job is to "
    "make it obey the house rules below.\n\n"
    "Change exactly what breaks a rule and nothing else. Keep every fact, "
    "every name and the meaning of every sentence. Never add a fact, a name, "
    "a number or a claim that is not already in the draft. If the draft is "
    "thin, make it tighter, never fuller.\n\n"
    "Output ONLY the corrected text. No explanation, no quotes, no preamble."
)

_POLISH_PROMPT = """Correct this {channel} draft.

DRAFT
{draft}

WHAT IT BREAKS
{offences}
{limit}
Return only the corrected draft."""


def _offence_lines(ids: tuple[str, ...], channel: str) -> str:
    lines = []
    for rule_id in ids:
        rule = _RULE_BY_ID.get(rule_id)
        if rule is not None:
            lines.append(f"- {rule.wording(channel)}")
    return "\n".join(lines) or "- It reads as though a machine wrote it."


async def polish(draft: str, *, channel: str, max_chars: int = 0) -> Polished:
    """The draft, obeying the rules that bind *channel*.

    A draft with no offences is returned untouched and costs no model call.
    A polish that cannot run never loses the draft.
    """
    text = (draft or "").strip()
    if not text:
        return Polished(text="", refused="empty draft")

    found = offences(text, channel)
    if not found:
        return Polished(text=text)

    from ..llm import LLMClient

    limit = f"\nStay under {max_chars} characters.\n" if max_chars else ""
    try:
        rewritten = await LLMClient().generate(
            _POLISH_PROMPT.format(
                channel=channel.replace("_", " "),
                draft=text,
                offences=_offence_lines(found, channel),
                limit=limit,
            ),
            system=_POLISH_SYSTEM + "\n\n" + rules_for(channel),
            temperature=0.3,
            max_tokens=1024,
        )
    except Exception as e:  # a polish that cannot run must not lose the draft
        logger.info("polish: rewrite failed on %s (%s); repairing by hand", channel, e)
        rewritten = ""

    candidate = (rewritten or "").strip()
    repaired: tuple[str, ...] = ()
    if candidate and not offences(candidate, channel):
        return Polished(text=candidate, offences=found, repaired=("model",))

    base = candidate if candidate else text
    fixed, repaired = repair(base, channel)
    left = offences(fixed, channel)
    if left:
        logger.info("polish: %s still breaks %s after repair", channel, ", ".join(left))
        return Polished(
            text=fixed, offences=found,
            repaired=("model", *repaired) if candidate else repaired,
            refused=", ".join(left),
        )
    return Polished(
        text=fixed, offences=found,
        repaired=("model", *repaired) if candidate else repaired,
    )


async def read_back(text: str, *, channel: str, max_chars: int = 0) -> str:
    """``polish`` for a generator: the text, with what it broke in the log."""
    polished = await polish(text, channel=channel, max_chars=max_chars)
    if polished.offences:
        logger.info(
            "%s draft broke %s; repaired by %s", channel,
            ", ".join(polished.offences), ", ".join(polished.repaired) or "nothing",
        )
    return polished.text
