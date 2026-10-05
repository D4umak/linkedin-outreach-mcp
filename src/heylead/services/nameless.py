"""A person whose name LinkedIn withholds is never enrolled or written to.

D4umak/heylead-api#2164. Unipile returns the placeholder "LinkedIn Member"
for a private or out-of-network profile; the search card still carries a
title ("Partner @ BoxGroup"), so until 5 Oct 2026 both search parsers kept
the row (they skipped only an EMPTY name), ``enroll_prospect`` wrote it, the
sync push landed it in the cloud, and the hosted executor invited two such
people with a 20-minute-call ask. A first touch to someone we cannot name
cannot address them and reads as mass mail.

One predicate for every door on this side: the two search parsers,
``refuse_junk_at_enrol`` (the enrol choke point), ``create_campaign`` and
``campaign_refill_service`` before they enrol. The cloud twin is
``app.services.nameless``. The placeholders live in ONE place,
``formatter.PLACEHOLDER_NAMES``; the comparison is an exact, case-folded,
stripped equality, never a substring test (textutil rule).
"""

from __future__ import annotations

from typing import Any

from ..formatter import PLACEHOLDER_NAMES

# The enrol refusal reason, the same word the cloud's gate and repair use.
NAMELESS_REASON = "nameless_profile"

# The keys a person's name is stored under, by row shape: a prospect dict or
# contact row (``name``), an outreach joined with its contact
# (``contact_name``), a signal (``prospect_name``). The first key present
# decides, so an outreach row's campaign ``name`` is never read.
_NAME_KEYS = ("contact_name", "prospect_name", "name")


def profile_name(row: Any) -> str:
    """The stored name of the person in *row*, stripped; "" when none."""
    if not isinstance(row, dict):
        return ""
    for key in _NAME_KEYS:
        if key in row:
            return str(row.get(key) or "").strip()
    return ""


def is_nameless_profile(row: Any) -> bool:
    """True when the person in *row* has no usable name.

    Blank, missing, or exactly one of the placeholders a source writes when
    it has no name (``LinkedIn Member`` above all), compared case-folded and
    stripped. "LinkedIn Members Club Ltd" is a name; "linkedin member" is not.
    """
    name = profile_name(row)
    return not name or name.casefold() in PLACEHOLDER_NAMES
