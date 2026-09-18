"""Small text helpers shared across the generators."""

from __future__ import annotations

import re

DEFAULT_GREETING = "there"

# Not whitespace to str.strip() or str.split(), so a name made only of these
# stays truthy and survives every guard shaped like `if name:`. They arrive
# inside real LinkedIn names — see first_name() for where from.
_ZERO_WIDTH = ("\u200b", "﻿")


def _visible_words(name: str | None) -> list[str]:
    """The words of *name* that would actually be seen if we printed it."""
    if not name:
        return []
    cleaned = str(name)
    for char in _ZERO_WIDTH:
        cleaned = cleaned.replace(char, " ")
    return cleaned.split()


def first_name(name: str | None, fallback: str = DEFAULT_GREETING) -> str:
    """The first word of a name, or *fallback* when there isn't one.

    Eight call sites carried this idiom::

        prospect.get("name", "").split()[0] if prospect.get("name") else "there"

    which raises IndexError on a name that is only whitespace: the string is
    truthy so the guard passes, then ``"   ".split()`` is ``[]`` and ``[0]``
    blows up — from inside message, follow-up and reply generation, prospect
    analysis and the inbound qualifier.

    Such names do arrive. connection_sync builds one by joining first and last,
    so a profile carrying only a separator character yields a non-empty string
    with no words; the same shape reached global_contacts as HTML-titled rows
    earlier this month. Zero-width characters (U+200B and friends) are not
    whitespace to ``str.split``, so they are stripped explicitly — otherwise
    the "first name" is an invisible character and the greeting reads as
    "Hi ,".
    """
    words = _visible_words(name)
    return words[0] if words else fallback


def full_name(name: str | None, fallback: str = DEFAULT_GREETING) -> str:
    """The whole name, spacing normalised, or *fallback* when nothing shows.

    The sibling of first_name(), for the copy that prints a person's full name
    rather than greeting them by their first. A caller that guards with
    ``.strip()`` is safe from the whitespace-only case but not from the
    zero-width one, which strips to something truthy and prints as nothing —
    so "<invisible> asked me to reach you" goes out to a real person.
    """
    words = _visible_words(name)
    return " ".join(words) if words else fallback


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
