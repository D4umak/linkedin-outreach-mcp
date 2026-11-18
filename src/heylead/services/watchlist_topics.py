"""What a keyword watchlist may hold, and which lists are searched.

One rule, shared with heylead-api (``app/services/watchlist_topics.py``
mirrors this file with the same semantics; change both together):

* ``topic_terms`` — the terms of a persona or segment that name a topic. A
  keyword watchlist is seeded from these and from nothing else. A segment's
  ``keywords`` string starts with its titles and industries
  (``create_campaign._icp_result_to_legacy``), and a post search for a job
  title or an industry finds posts about anything, so those are dropped.
  There is no titles fallback: no topic term, no list.
* ``is_fragment_list`` — the "<persona> — Pain Points" keyword lists the
  client used to save, whose keywords were the first four words of each
  pain-point sentence ("The team has ideas"). They are skipped where posts
  are searched (``searchable``). Their ``is_active`` is never flipped: the
  flip is pushed to production as ``enabled=False`` and overwrites the
  owner's setting (D4umak/heylead-api#6820).
"""

from __future__ import annotations

import re
from typing import Any

from .icp_search import _PLACE_OR_NATIONALITY_TOKENS, _is_hiring_intent_token

FRAGMENT_NAME_SUFFIX = " — Pain Points"
MAX_TOPIC_TERMS = 12

# create_campaign and the api's discovery search fall back to this word when a
# persona has no keyword of its own. It names no topic.
_FALLBACK_TERM = "professional"

_SPLIT_RE = re.compile(r"[,;]")

NOT_SEARCHED_STATUS = "Not searched"
_WHY = "these keywords were cut from longer sentences and found posts off your topic."
_INSTEAD = " HeyLead searches your campaign's topic keywords instead."
_PAUSED = " Your campaign's topic keywords are paused."
_NO_TOPIC = (
    " This campaign has no topic keywords yet. "
    "Add some with manage_watchlist action='add' campaign_id='{campaign_id}'."
)
_STAYS = (
    "It stays unsearched: its keywords were cut from longer sentences and "
    "found posts off your topic. To search other words, add a list with "
    "manage_watchlist action='add'."
)
_ADDED = (
    f'HeyLead does not search a keyword list whose name ends in "{FRAGMENT_NAME_SUFFIX}". '
    "Lists with that name held keywords cut from longer sentences, and they "
    "found posts off your topic.\n"
    "To have these keywords searched, add them again under another name."
)


def _parts(value: Any) -> list[str]:
    """Comma/semicolon-split parts of a string, or of each item of a list.

    Titles, industries and locations are a bare list on a segment and an
    ``{"include": [...]}`` dict on a persona; both are read.
    """
    if isinstance(value, dict):
        value = value.get("include")
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        out.extend(p.strip() for p in _SPLIT_RE.split(item) if p.strip())
    return out


def _whole_and_parts(value: Any) -> set[str]:
    """Lower-cased names to drop: each item whole and each comma-split part."""
    if isinstance(value, dict):
        value = value.get("include")
    if isinstance(value, str):
        value = [value]
    names: set[str] = set()
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str) and item.strip():
                names.add(item.strip().lower())
    names.update(p.lower() for p in _parts(value))
    return names


def topic_terms(segment_or_persona: Any) -> list[str]:
    """The topic terms of one ICP persona or segment, in ICP order, at most 12.

    A topic term is a comma- or semicolon-split part of ``keywords`` (one
    string on a segment, a list on a persona) that is not, case-insensitively:
    the persona's own name, one of its titles (``titles`` / ``job_titles``),
    one of its industries (whole or a comma-split part), one of its locations,
    a place or nationality token, a hiring-intent phrase, or the literal
    fallback "professional". Terms are deduped. No titles fallback: a persona
    with no topic term returns ``[]`` and gets no keyword watchlist.

    Same rule as heylead-api ``app/services/watchlist_topics.topic_terms``.
    """
    if not isinstance(segment_or_persona, dict):
        return []
    seg = segment_or_persona

    dropped: set[str] = {_FALLBACK_TERM}
    name = seg.get("name")
    if isinstance(name, str) and name.strip():
        dropped.add(name.strip().lower())
    for key in ("titles", "job_titles", "industries", "locations"):
        dropped |= _whole_and_parts(seg.get(key))
    dropped |= _PLACE_OR_NATIONALITY_TOKENS

    terms: list[str] = []
    seen: set[str] = set()
    for term in _parts(seg.get("keywords")):
        low = term.lower()
        if low in seen or low in dropped or _is_hiring_intent_token(term):
            continue
        seen.add(low)
        terms.append(term)
        if len(terms) >= MAX_TOPIC_TERMS:
            break
    return terms


def is_fragment_list(watchlist: Any) -> bool:
    """A keyword watchlist the client seeded from pain-point sentences.

    The shape is the kind ``keyword`` plus the name suffix " — Pain Points"
    (the PM's ruling (c) on #6820): the client wrote exactly that.
    """
    if not isinstance(watchlist, dict):
        return False
    kind = str(watchlist.get("watch_type") or watchlist.get("kind") or "keyword")
    name = str(watchlist.get("name") or "").rstrip()
    return kind == "keyword" and name.endswith(FRAGMENT_NAME_SUFFIX)


def searchable(watchlists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The lists whose keywords are searched as text: all but fragment lists.

    Every reader that searches posts (or the web) with a list's keywords, or
    counts what is being searched, calls this straight after
    ``list_watchlists`` and before any cap, cursor or rotation, so a skipped
    list never holds a slot. Readers that show the lists to their owner or
    push them to the cloud read them unfiltered.
    """
    return [w for w in watchlists if not is_fragment_list(w)]


def covers_campaign(watchlist: Any) -> bool:
    """A campaign's own topic list: linked, kind ``keyword``, not a fragment
    list, switched on or paused. A company or competitor list does not cover."""
    if not isinstance(watchlist, dict) or not watchlist.get("campaign_id"):
        return False
    kind = str(watchlist.get("watch_type") or watchlist.get("kind") or "keyword")
    return kind == "keyword" and not is_fragment_list(watchlist)


def not_searched_reason(
    watchlist: dict[str, Any], all_watchlists: list[dict[str, Any]],
) -> str:
    """The one line an owner reads under a fragment list.

    Four states, the same words as heylead-api's ``hosted_watchlists``. The
    second sentence is chosen by ``covers_campaign``, the predicate seeding
    reads, so it never promises a search nothing runs: the campaign has a
    topic list that is switched on; its topic lists are all paused; it has
    none (a company list is not one); the list is linked to no campaign.
    """
    line = f"{NOT_SEARCHED_STATUS}: {_WHY}"
    campaign_id = watchlist.get("campaign_id")
    if not campaign_id:
        return line
    covering = [
        w for w in all_watchlists
        if w.get("campaign_id") == campaign_id and covers_campaign(w)
    ]
    if not covering:
        return line + _NO_TOPIC.format(campaign_id=campaign_id)
    if any(w.get("is_active") for w in covering):
        return line + _INSTEAD
    return line + _PAUSED


def stays_unsearched_line() -> str:
    """Said when an owner resumes a fragment list."""
    return _STAYS


def added_not_searched_lines() -> str:
    """Said when an owner adds a keyword list of the fragment shape."""
    return _ADDED
