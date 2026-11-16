"""Likely to accept: the second signal behind the invite order (#4410).

Mirror of heylead-api/app/services/accept_signal.py for the client SQLite DB.
The weights, the prior and the steps are the api's, and the SQL below is built
from them, so a re-fit that edits them (heylead-api #6006, applied only by a
reviewed PR quoting a monthly reading with ``adopt: True``) moves the Python
score and the SQL order together. tests/test_invite_rank_constants_6006.py
holds both to the api's values and to each other.
"""

from __future__ import annotations

import json
import math
from typing import Any

FIT_WEIGHT = 0.4
ACCEPT_WEIGHT = 0.6
ACCEPT_PRIOR = 0.43
ACCEPT_INPUTS = ("none", "profile", "research", "both")
#: The accept score's steps and their weights: heylead-api's ``accept_signal.STEPS``.
SIGNAL_KEYS = ("posts_crawled", "shared_5", "shared_20", "followers_2000", "open_profile", "third_degree")
STEPS = {
    "posts_crawled": 0.30, "shared_5": 0.25, "shared_20": 0.10,
    "followers_2000": 0.25, "open_profile": 0.15, "third_degree": -0.15,
}

_BASE = 0.35
_THIRD_DEGREE = frozenset({"THIRD_DEGREE", "DISTANCE_3", "OUT_OF_NETWORK"})
_FIRST_DEGREE = frozenset({"FIRST_DEGREE", "DISTANCE_1", "1ST", "1", "SELF"})
_PROFILE_KEYS = ("shared_connections_count", "follower_count", "is_open_profile", "network_distance")

def _as_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}

def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None

def is_first_degree_profile(profile: Any) -> bool:
    p = _as_dict(profile)
    dist = str(p.get("network_distance") or p.get("connection_degree") or "").strip().upper()
    if dist in _FIRST_DEGREE:
        return True
    if p.get("is_connection") is True or p.get("is_first_degree") is True:
        return True
    return False

def likely_to_accept(profile: Any, analysis: Any = None, research_summary: Any = None) -> float | None:
    p = _as_dict(profile)
    a = _as_dict(analysis)
    r = _as_dict(research_summary)

    has_profile = any(p.get(k) is not None for k in _PROFILE_KEYS)
    has_research = a.get("posts_crawled") is not None or r.get("total_posts_analyzed") is not None

    if not has_profile and not has_research:
        return None

    posts_val = _num(a.get("posts_crawled"))
    posts = (_num(r.get("total_posts_analyzed")) if posts_val is None else posts_val) or 0.0
    shared = _num(p.get("shared_connections_count")) or 0
    met = {
        "posts_crawled": posts >= 1,
        "shared_5": shared >= 5,
        "shared_20": shared >= 20,
        "followers_2000": (_num(p.get("follower_count")) or 0) >= 2000,
        "open_profile": p.get("is_open_profile") is True,
        "third_degree": str(p.get("network_distance") or "").upper() in _THIRD_DEGREE,
    }
    score = _BASE + sum(STEPS[k] for k in SIGNAL_KEYS if met[k])
    return round(max(0.0, min(1.0, score)), 4)

def rank(fit_score: Any, accept_score: Any) -> float:
    f = _num(fit_score) or 0.0
    if f <= 0:
        return 0.0
    a = _num(accept_score)
    return round(FIT_WEIGHT * f + ACCEPT_WEIGHT * (ACCEPT_PRIOR if a is None else a), 4)

_SAFE_PROF = "CASE WHEN json_valid(c.profile_json) THEN c.profile_json ELSE '{}' END"
_SAFE_ANAL = "CASE WHEN json_valid(c.analysis_json) THEN c.analysis_json ELSE '{}' END"
_SAFE_RES = "CASE WHEN json_valid(c.research_summary) THEN c.research_summary ELSE '{}' END"

_POSTS_SQL = f"COALESCE(json_extract({_SAFE_ANAL}, '$.posts_crawled'), json_extract({_SAFE_RES}, '$.total_posts_analyzed'))"
_SHARED_SQL = f"json_extract({_SAFE_PROF}, '$.shared_connections_count')"
_FOLLOWERS_SQL = f"json_extract({_SAFE_PROF}, '$.follower_count')"
_OPEN_SQL = f"(json_type({_SAFE_PROF}, '$.is_open_profile') = 'true')"
_DIST_SQL = f"json_extract({_SAFE_PROF}, '$.network_distance')"

_ANY_INPUT_SQL = (
    f"(json_extract({_SAFE_PROF}, '$.shared_connections_count') IS NOT NULL OR "
    f"json_extract({_SAFE_PROF}, '$.follower_count') IS NOT NULL OR "
    f"json_extract({_SAFE_PROF}, '$.is_open_profile') IS NOT NULL OR "
    f"json_extract({_SAFE_PROF}, '$.network_distance') IS NOT NULL OR "
    f"json_extract({_SAFE_ANAL}, '$.posts_crawled') IS NOT NULL OR "
    f"json_extract({_SAFE_RES}, '$.total_posts_analyzed') IS NOT NULL)"
)

_STEP_SQL = {
    "posts_crawled": f"COALESCE({_POSTS_SQL}, 0) >= 1",
    "shared_5": f"COALESCE({_SHARED_SQL}, 0) >= 5",
    "shared_20": f"COALESCE({_SHARED_SQL}, 0) >= 20",
    "followers_2000": f"COALESCE({_FOLLOWERS_SQL}, 0) >= 2000",
    "open_profile": _OPEN_SQL,
    "third_degree": f"UPPER(COALESCE({_DIST_SQL}, '')) IN ({', '.join(repr(d) for d in sorted(_THIRD_DEGREE))})",
}

ACCEPT_SCORE_SQL = (
    f"CASE WHEN NOT {_ANY_INPUT_SQL} THEN NULL ELSE "
    f"ROUND(MIN(1.0, MAX(0.0, {_BASE!r} + "
    + " + ".join(f"CASE WHEN {_STEP_SQL[k]} THEN {STEPS[k]!r} ELSE 0.0 END" for k in SIGNAL_KEYS)
    + ")), 4) END"
)

RANK_SQL = (
    f"(CASE WHEN COALESCE(c.fit_score, 0.0) <= 0.0 THEN 0.0 "
    f"ELSE ROUND({FIT_WEIGHT!r} * MIN(1.0, COALESCE(c.fit_score, 0.0)) + "
    f"{ACCEPT_WEIGHT!r} * COALESCE({ACCEPT_SCORE_SQL}, {ACCEPT_PRIOR!r}), 4) END)"
)

INVITE_ORDER_SQL = f"{RANK_SQL} DESC, o.created_at ASC, o.rowid ASC, o.id ASC"
DM_ORDER_SQL = "c.fit_score DESC, o.created_at ASC, o.rowid ASC, o.id ASC"

def pending_order_sql(*, by_rank: bool = True) -> str:
    return INVITE_ORDER_SQL if by_rank else DM_ORDER_SQL
