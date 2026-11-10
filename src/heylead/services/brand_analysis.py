"""Is this a brand analysis, or the leftover of a failed AI call? (api #5838)

Until 10 Nov 2026 ``brand_strategist.analyze_brand_profile`` answered a reply
it could not parse with a data-shaped default: score 0, no areas, and one
"action" telling the user to check their LLM configuration. The tool stored
whatever came back over the last good analysis, printed it as an audit
scoring 0/100, and every sync push sent it to the cloud.

Twin of heylead-api's ``app/services/brand_analysis.py`` (api #5784), clause
for clause. One predicate, ``is_failed``, decides it everywhere here:
``analyze_brand_profile`` raises ``BrandAnalysisFailed`` on it instead of
returning a default, and ``brand_service`` neither stores, reads back nor
pushes what it names. The prefix is matched, not the sentence: the server's
default and this client's differ after it.

A real score of 0 and an analysis with ``areas: {}`` are analyses.
"""

from __future__ import annotations

from typing import Any

from ..ai.voice_block import SENTINEL_PREFIX

# Reason codes on ``BrandAnalysisFailed``. Codes only: the prompt carries the
# person's name, summary and posts, so no reply text ever goes with them.
UNPARSED = "unparsed"
NOT_AN_ANALYSIS = "not_an_analysis"
PROXY = "proxy"


class BrandAnalysisFailed(Exception):
    """The analysis call produced nothing to store. ``reason`` is a code."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"brand analysis failed ({reason})")
        self.reason = reason


def is_failed(analysis: Any) -> bool:
    """True when ``analysis`` is not something to store, show or plan from."""
    if not isinstance(analysis, dict):
        return True
    actions = analysis.get("top_3_actions")
    first = actions[0] if isinstance(actions, list) and actions else ""
    if isinstance(first, str) and first.strip().startswith(SENTINEL_PREFIX):
        return True
    # A reply of "{}" or {"error": "..."} parses, and is no analysis either.
    return "overall_score" not in analysis and not analysis.get("areas")


def usable(analysis: Any) -> bool:
    """A stored analysis an audit may show and a plan may be built from."""
    return bool(analysis) and not is_failed(analysis)
