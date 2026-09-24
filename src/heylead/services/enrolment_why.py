"""The record of why a person was enrolled, written once at enrolment.

The client twin of heylead-api's `_enrolment_why` (scheduler_executor.py):
the same keys, so the dashboard's "Why this prospect" panel reads a
contact the MCP client found the way it reads one the hosted discover job
found. Until 24 Sep 2026 `create_campaign` computed the per-dimension fit
breakdown and kept only the star score; every MCP-created prospect had an
empty panel and the chat gave no reason next to the name.

The record travels as `why` (a JSON object) on each contact of the
`cloud_sync.sync_to_cloud` push, and is stored locally in `contacts.why_json`.
"""

from __future__ import annotations

import time
from typing import Any

from ..textutil import contains_term

# Coarse function buckets, copied from heylead-api
# `app/services/campaign_preferences.TITLE_FAMILIES` so both sides name the
# same family for the same title.
TITLE_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("engineering", ("engineering", "engineer", "developer", "technology",
                     "technical", "cto", "architect", "devops", "platform")),
    ("product", ("product", "cpo", "design", "ux")),
    ("sales", ("sales", "revenue", "cro", "account executive", "business development",
               "partnerships", "commercial")),
    ("marketing", ("marketing", "growth", "cmo", "brand", "demand generation")),
    ("finance", ("finance", "financial", "cfo", "accounting", "controller")),
    ("operations", ("operations", "coo", "supply chain", "logistics")),
    ("people", ("people", "human resources", "hr", "talent", "recruiting",
                "recruitment", "chro")),
    ("executive", ("ceo", "founder", "owner", "president", "managing director",
                   "general manager")),
)

# The keys every `why` carries; the api and the dashboard read these.
WHY_KEYS: tuple[str, ...] = (
    "fit_score", "segment", "seniority", "title_family",
    "evidence_hits", "preference_delta",
)


def title_family(title: str | None) -> str:
    """Coarse function bucket for a title, or "" when nothing matches."""
    text = (title or "").strip()
    if not text:
        return ""
    for family, keywords in TITLE_FAMILIES:
        for kw in keywords:
            if contains_term(text, kw):
                return family
    return ""


def seniority_bucket(title: str | None) -> str:
    """Canonical seniority key for a title, or "" when it states none."""
    from .seniority import infer_seniority_level

    text = (title or "").strip()
    if not text:
        return ""
    return infer_seniority_level(text) or ""


def evidence_hits_for(prospect: dict[str, Any]) -> list[str]:
    """The identity evidence that cleared a filter, as ``field:value``."""
    hits = prospect.get("profile_signal_hits")
    out: list[str] = []
    if isinstance(hits, dict):
        out = [f"{k}:{v}" for k, v in hits.items() if v]
    if not out:
        out = [str(h) for h in (prospect.get("_evidence_hits") or [])]
    return out[:12]


def enrolment_why(
    prospect: dict[str, Any],
    breakdown: dict[str, float] | None,
    segment_name: str = "",
) -> dict[str, Any]:
    """Snapshot why this person is in the list. Plain JSON, no numbers
    beyond the scores themselves; `formatter.why_line` turns it into words.
    """
    title = prospect.get("title") or ""
    score = prospect.get("fit_score", 0.0)
    return {
        "fit_score": float(score) if isinstance(score, (int, float)) else 0.0,
        "segment": (segment_name or "").strip(),
        "seniority": seniority_bucket(title),
        "title_family": title_family(title),
        "evidence_hits": evidence_hits_for(prospect),
        "preference_delta": None,
        "breakdown": {
            k: v for k, v in (breakdown or {}).items()
            if isinstance(v, (int, float))
        },
        "lane": "mcp_create_campaign",
        "recorded_at": int(time.time()),
    }
