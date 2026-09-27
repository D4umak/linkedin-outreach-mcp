"""Deterministic style rules every outgoing message must satisfy.

These mirror ``prompts/outreach_validate.json`` rule 7 — "Banned punctuation:
contains em dashes, exclamation marks, or emojis -> INVALID" — but run in code
rather than in an LLM validator, so they apply to every send path including
hand-written text that never touched the generation pipeline.

En dashes are deliberately allowed: "£30–£50" is a numeric range, not the
em-dash style tell.
"""

from __future__ import annotations

import os
import re
import unicodedata

EM_DASH = "—"

# Zero-width / format chars that are not Unicode Zs but still eat a "space"
# slot in generated copy and disappear on LinkedIn iOS.
_INVISIBLE = ("\u200b", "\u200c", "\u200d", "\ufeff", "\u2060")

# Split only on a real sentence end followed by a capital — keeps "e.g. this"
# and URLs in one piece more often than a naive split.
_SENTENCE_GAP = re.compile(r"(?<=[.!?])[ \t]+(?=[A-Z])")


def normalize_outbound_spaces(text: str) -> str:
    """Turn every unicode / zero-width space into an ASCII space LinkedIn can draw.

    ChatGPT and some improve-passes emit U+00A0 / thin spaces. LinkedIn's iOS
    bubble drops those glyphs, so the prospect sees one mashed word.
    """
    if not text:
        return text
    out: list[str] = []
    prev_space = False
    for ch in text:
        if ch in "\n\r":
            out.append(ch)
            prev_space = False
            continue
        if ch in _INVISIBLE or ch.isspace() or unicodedata.category(ch) == "Zs":
            if not prev_space:
                out.append(" ")
                prev_space = True
            continue
        out.append(ch)
        prev_space = False
    return "".join(out).strip()


def break_dm_sentences(text: str) -> str:
    """Give LinkedIn iOS a wrap point that is not a space.

    A single paragraph relies on U+0020 to wrap. When that glyph is missing
    or zero-width, the whole DM becomes one token. A blank line still breaks.
    """
    if not text or "\n" in text:
        return text
    return _SENTENCE_GAP.sub("\n\n", text)


# ── AI disclosure on the first message (D4umak/heylead-api#1481) ──
#
# Article 50 of the EU AI Act (in force since 2 Aug 2026): a person must be
# told they are interacting with an AI system. The first thing a person
# receives from a HeyLead sender says so, in this one fixed sentence (decided
# 25 Sep 2026). It is appended after every model pass (improve, fix, the
# validators), so no model can drop or reword it, and it is never shown to a
# validator as part of the copy. No brand name, so no name check rejects it.
# Mirrors heylead-api app/services/message_guardrails.py; keep the two equal.
AI_DISCLOSURE = "Sent with my AI assistant."

# One kill switch, read at send time. No per-workspace opt-out.
AI_DISCLOSURE_ENV = "AI_DISCLOSURE_ENABLED"

# An invitation note stays one block; a DM, an InMail and an email end with the
# sentence as its own paragraph.
_DISCLOSURE_SEPARATOR = {"invite": " ", "dm": "\n\n", "email": "\n\n"}

# What a first touch leaves free for the sentence: the sentence plus the widest
# separator. Taken off the generator's max_chars, so a note written to the cap
# is not cut to make room.
AI_DISCLOSURE_RESERVE = len(AI_DISCLOSURE) + max(len(s) for s in _DISCLOSURE_SEPARATOR.values())


def disclosure_enabled() -> bool:
    """The kill switch. Default on; read on every call, never cached."""
    raw = os.environ.get(AI_DISCLOSURE_ENV, "")
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def first_touch_budget(max_chars: int, *, first_touch: bool = True) -> int:
    """The generator's budget on a message that will carry the sentence."""
    if not first_touch or not disclosure_enabled():
        return int(max_chars)
    return max(int(max_chars) - AI_DISCLOSURE_RESERVE, 10)


def _split_disclosure(text: str) -> tuple[str, bool]:
    stripped = (text or "").rstrip()
    if stripped.endswith(AI_DISCLOSURE):
        return stripped[: -len(AI_DISCLOSURE)].rstrip(), True
    return text or "", False


def disclose(text: str, *, kind: str = "dm") -> str:
    """``text`` ending with the sentence, exactly once.

    Idempotent: text that already carries it is returned unchanged. An empty
    invitation note stays empty; the first DM then carries the sentence.
    """
    body = (text or "").strip()
    if not body or not disclosure_enabled() or AI_DISCLOSURE in body:
        return text or ""
    return body + _DISCLOSURE_SEPARATOR.get(kind, "\n\n") + AI_DISCLOSURE


def prepare_outbound_text(text: str, *, kind: str = "dm", first_touch: bool = False) -> str:
    """Normalise copy the moment before it goes on the wire.

    Invitation notes stay one block — they already sit on a 200-char budget.
    DMs get a blank line between sentences so mobile can scan them.

    ``first_touch`` appends AI_DISCLOSURE (see ``disclose``). A sentence
    already at the end is kept as its own paragraph and never doubled.
    """
    body, had = _split_disclosure(normalize_outbound_spaces(text or ""))
    prepared = normalize_outbound_spaces(body)
    if kind == "dm":
        prepared = break_dm_sentences(prepared)
    if had:
        sep = _DISCLOSURE_SEPARATOR.get(kind, "\n\n")
        return prepared + sep + AI_DISCLOSURE if prepared else AI_DISCLOSURE
    if first_touch:
        return disclose(prepared, kind=kind)
    return prepared


# Scripts that do not separate words with spaces. Counting their characters as
# letters made every note in Chinese, Japanese or Thai read as one mashed word
# and refused it before sending. Hangul is absent on purpose: Korean does put
# spaces between words, so it stays under the rule.
_SPACELESS_SCRIPT_RANGES = (
    (0x0E00, 0x0EFF),    # Thai, Lao
    (0x0F00, 0x0FFF),    # Tibetan
    (0x1000, 0x109F),    # Myanmar
    (0x1780, 0x17FF),    # Khmer
    (0x3040, 0x30FF),    # Hiragana, Katakana
    (0x31F0, 0x31FF),    # Katakana phonetic extensions
    (0x3400, 0x4DBF),    # CJK extension A
    (0x4E00, 0x9FFF),    # CJK unified ideographs
    (0xF900, 0xFAFF),    # CJK compatibility ideographs
    (0xFF66, 0xFF9D),    # halfwidth Katakana
    (0x20000, 0x2FA1F),  # CJK extensions B onward, compatibility supplement
)


def _in_spaceless_script(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in _SPACELESS_SCRIPT_RANGES)


def _words_run_together(text: str) -> bool:
    """A wall of letters with too few spaces to wrap on a phone.

    Only letters from scripts that separate words with spaces are counted, so
    a Japanese note is not a mashed word, while a run-together English phrase
    is still caught even inside one.
    """
    compact = normalize_outbound_spaces(text)
    letters = sum(1 for ch in compact if ch.isalpha() and not _in_spaceless_script(ch))
    spaces = compact.count(" ")
    return letters >= 40 and spaces < max(3, letters // 12)


class GuardrailViolation(Exception):
    """Raised when a message breaks HeyLead's outgoing style rules."""


def _has_emoji(text: str) -> bool:
    for ch in text:
        if unicodedata.category(ch) == "So":
            return True
        # Regional indicators (flags) and emoji modifiers fall outside "So".
        if 0x1F1E6 <= ord(ch) <= 0x1F1FF or 0x1F3FB <= ord(ch) <= 0x1F3FF:
            return True
    return False


def check_message(text: str) -> list[str]:
    """Return a list of rule violations; empty means the message may send."""
    violations: list[str] = []
    if EM_DASH in text:
        violations.append("contains an em dash")
    if "!" in text:
        violations.append("contains an exclamation mark")
    if _has_emoji(text):
        violations.append("contains an emoji")
    if _words_run_together(text):
        violations.append("has words running together without spaces")
    return violations


def assert_sendable(text: str) -> None:
    """Raise GuardrailViolation if the message breaks any style rule."""
    violations = check_message(text)
    if violations:
        raise GuardrailViolation("; ".join(violations))
