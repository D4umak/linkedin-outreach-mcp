"""Normalize company names so attach can survive legal suffixes.

Used when a company-level signal (news, ATS, G2, Reddit/HN) looks for
pending campaign people. Exact string match misses ``Citi`` vs ``Citi Inc.``;
prefix match would glue ``Meta`` to ``Metaswitch``. Token subset is the
middle: whole words only, no alias table.
"""

from __future__ import annotations

import re

# Trailing legal/form tokens only. Do not add product aliases here
# (Citi ↔ Citigroup is a different token and stays a non-match).
_LEGAL_SUFFIXES = frozenset({
    "inc",
    "llc",
    "ltd",
    "plc",
    "corp",
    "gmbh",
    "ag",
    "sa",
    "nv",
    "bv",
    "pty",
    "limited",
    "incorporated",
    "corporation",
})

_NON_WORD = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def normalize_company_name(name: str) -> str:
    """Lowercase, strip punctuation, drop trailing legal suffixes."""
    tokens = _WS.split(_NON_WORD.sub(" ", (name or "").lower()).strip())
    tokens = [t for t in tokens if t]
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def companies_match(a: str, b: str) -> bool:
    """True when normalized names are equal or one is a whole-token subset.

    ``citi`` matches ``citi inc`` and ``citi bank``. ``citi`` does not match
    ``citigroup``. ``meta`` does not match ``metaswitch``.
    """
    left = normalize_company_name(a)
    right = normalize_company_name(b)
    if not left or not right:
        return False
    if left == right:
        return True
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    return left_tokens <= right_tokens or right_tokens <= left_tokens
