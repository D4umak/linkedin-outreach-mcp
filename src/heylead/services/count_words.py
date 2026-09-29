"""A count and its noun, for anything a person reads.

The twin of heylead-api's app/services/count_words.py. A campaign with one
prospect read '1 prospects' (heylead-api#1894, found while fixing #1864).
No imports: every module that writes a count can use it without a cycle.
.semgrep/a-count-written-with-a-fixed-plural.yaml fails on '{n} prospects'.
"""

from __future__ import annotations


def count_noun(n: int, noun: str, plural_form: str = "") -> str:
    """'1 prospect', '3 prospects': never '1 prospects'."""
    return f"{n} {noun_for(n, noun, plural_form)}"


def noun_for(n: int, noun: str, plural_form: str = "") -> str:
    """The noun alone, for a count written some other way ('1 queued + 2 sent prospects')."""
    return noun if n == 1 else (plural_form or noun + "s")
