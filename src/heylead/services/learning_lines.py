"""What HeyLead is learning in a workspace, said by ``suggest_next_action`` (api #6252).

The cloud reads the workspace's own message-arm shares (Phase 2, api #2403)
and its workspace-scope result lessons (Phase 3, api #3398), each only when
that loop is in ``act``, and returns them as ``what_works`` on
``GET /api/v1/analytics/learning`` (:func:`cloud_learning.fetch_learning`).
This module turns that into 0 to 2 lines.

The strings and :func:`next_action_lines` are copied byte for byte from
heylead-api ``app/services/learning_summary.py``, so the client tool and the
hosted one say the same sentences; ``tests/test_next_action_learning_lines.py``
pins every string to the api's literal value. Change them in both repos at
once, or not at all.

The read gets its own bound (:data:`FETCH_TIMEOUT_S`): a slow cloud costs the
answer its learning lines, never its steps.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

RATE_FLOOR = 30
#: At most this many learning lines in one answer (``learning_offered`` and
#: ``learning_lines`` on ``tool.called`` are 0 to this).
MAX_LINES = 2
#: An arm leads when its share is above this; an even split leads nothing.
LEADING_SHARE = 0.5
SELL = "sell"
#: suggest_next_action waits this long for the cloud's learning read, then
#: answers without the block (Decision on #6252, PR 4 rule 2).
FETCH_TIMEOUT_S = 3.0

# ── The pinned strings (Decision on #6252), copied from heylead-api. ──

CLIENT_HEADING = "📈 **What HeyLead is learning** (across your workspace)"
#: Every learning line starts with this, under its heading.
LINE_MARK = "- "
#: Only when Phase 2's pick applies to Sell campaigns alone (its ARM_GOALS).
SELL_PREFIX = "In your Sell campaigns, "
REPLY_ARM_LINE = (
    "{prefix}HeyLead now writes {share}% of new opening messages {label}, and keeps testing the other ways. "
    "It follows replies so far across HeyLead, yours included. Yours written this way: {mature} sent, {worked} replied."
)
NOTE_ARM_LINE = (
    "{prefix}where a note is allowed, HeyLead now {label} {share}% of new invitations, and keeps testing the other way. "
    "It follows acceptances so far across HeyLead, yours included. Yours this way: {mature} sent, {worked} accepted."
)
LESSON_LINE = "From your results, given to HeyLead's writers: {body}"
#: Appended to a lesson line only when its body carries no number.
LESSON_SENDS = " ({sends} sends)"
#: The words for an arm. A factor or arm missing here gives no line, so no
#: internal key (pain_point, blank, ...) is ever printed.
LABELS: dict[tuple[str, str], str] = {
    ("angle", "pain_point"): "around a problem in their work",
    ("angle", "peer_expertise"): "around the field their title names",
    ("angle", "direct_value"): "that say plainly what you do",
    ("note", "note"): "adds one to",
    ("note", "blank"): "leaves it off",
}
ARM_LINES: dict[str, str] = {"angle": REPLY_ARM_LINE, "note": NOTE_ARM_LINE}

_DIGIT = re.compile(r"\d")


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _arm_line(arm: Mapping[str, Any], prefix: str) -> str:
    factor, key = str(arm.get("factor") or ""), str(arm.get("arm") or "")
    template, label = ARM_LINES.get(factor), LABELS.get((factor, key))
    share, mature, worked = _float(arm.get("share")), _int(arm.get("mature")), _int(arm.get("worked"))
    if not template or not label or share is None or mature is None or worked is None:
        return ""
    if arm.get("stopped") or share <= LEADING_SHARE or mature < RATE_FLOOR:
        return ""
    line = template.format(prefix=prefix, share=round(share * 100), label=label, mature=mature, worked=worked)
    return line[:1].upper() + line[1:]


def _lesson_line(lesson: Mapping[str, Any]) -> str:
    body = str(lesson.get("text") or "").strip()
    if not body:
        return ""
    line = LESSON_LINE.format(body=body)
    sends = _int(lesson.get("sends"))
    if not _DIGIT.search(body) and sends is not None:
        line += LESSON_SENDS.format(sends=sends)
    return line


def _dicts(value: Any) -> list[Mapping[str, Any]]:
    """The cloud's list, its non-dict items left out (the api's copy reads its own rows)."""
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def next_action_lines(what_works: Mapping[str, Any] | None) -> list[str]:
    """0 to 2 learning lines, without their ``- `` mark: leading arms first, then a lesson.

    An arm leads when it is not stopped, its share is above one half and it
    has at least ``RATE_FLOOR`` mature sends; an arm with no label gives no
    line. When a lesson exists it keeps the second place (designer-2-2: the
    arm line covers the workspace, the lesson names its group).
    """
    if not isinstance(what_works, Mapping) or not what_works:
        return []
    goals_raw = what_works.get("arm_goals")
    goals = [str(g) for g in goals_raw] if isinstance(goals_raw, list) else []
    prefix = SELL_PREFIX if goals == [SELL] else ""
    arms = [line for line in (_arm_line(a, prefix) for a in _dicts(what_works.get("arms"))) if line]
    lessons = [line for line in (_lesson_line(x) for x in _dicts(what_works.get("lessons"))) if line]
    return (arms[:MAX_LINES - min(1, len(lessons))] + lessons)[:MAX_LINES]


def block(lines: list[str]) -> list[str]:
    """The block as output lines: the heading and one ``- `` line each; nothing for no line."""
    if not lines:
        return []
    return [CLIENT_HEADING, *(f"{LINE_MARK}{line}" for line in lines)]


def count_learning_lines(text: str, heading: str = CLIENT_HEADING) -> int:
    """How many learning lines ``text`` shows: the ``- `` lines right under ``heading``."""
    lines = (text or "").split("\n")
    if heading not in lines:
        return 0
    n = 0
    for line in lines[lines.index(heading) + 1:]:
        if not line.startswith(LINE_MARK):
            break
        n += 1
    return n


async def fetch_what_works(timeout: float = FETCH_TIMEOUT_S) -> Mapping[str, Any] | None:
    """The cloud's ``what_works``, or None when signed out, failing or slower than ``timeout``."""
    from . import cloud_learning

    try:
        data = await asyncio.wait_for(cloud_learning.fetch_learning(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.info("learning read took longer than %.0f s; answering without it", timeout)
        return None
    except Exception as exc:  # the steps still answer without the lines
        logger.info("learning read failed: %s", type(exc).__name__)
        return None
    works = (data or {}).get("what_works") if isinstance(data, dict) else None
    return works if isinstance(works, Mapping) else None


async def learning_for_next_action() -> list[str]:
    """The lines suggest_next_action offers now: the bounded read, then :func:`next_action_lines`."""
    try:
        return next_action_lines(await fetch_what_works())
    except Exception:  # a malformed answer costs the lines, never the steps
        logger.debug("learning lines skipped", exc_info=True)
        return []
