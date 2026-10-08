"""A model call that gates a message to a person failed: nothing is sent (#2390).

The self-hosted twin of heylead-api ``app/services/model_failure.py``. Every
check that reads a message before it reaches a person used to read its own
failure as a pass here: the reply agent's loop ``none`` became ``continue``,
the LLM validator's exception became ``pass_stage``, an unreadable inbound
qualification became ``ask_purpose`` (a verdict that writes a DM), a failed
sentiment read became ``neutral``, a failed targeting recheck became a fit.

The client has no approval queue, so a failure resolves to skip: the message
is not sent, the item stays where a later run picks it up again, and one
``actions_log`` row (``model_failed``, ``outcome='model_failed'``) says why.
It never answers send. ``.semgrep/model-failure-falls-open.yaml`` fails the
suite on the old shapes.

The self-hosted engine is slated for removal (heylead-api#2320); this stays
small on purpose.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

MODEL_FAILED = "model_failed"
EXCEPTION = "exception"
TIMEOUT = "timeout"
UNPARSEABLE = "unparseable"
BUDGET = "budget"
FAILURES = frozenset({EXCEPTION, TIMEOUT, UNPARSEABLE, BUDGET})
MODEL_UNAVAILABLE = "model_unavailable"
# The sentiment a reply gets when the model could not give one. Not neutral:
# only one of "they acknowledged" and "we could not read this" is safe to
# answer unattended (heylead-api llm.SENTIMENT_UNKNOWN).
SENTIMENT_UNKNOWN = "unknown"

# What the failure was, in the words of the skip line (designer review, #2390).
_IN_BRACKETS = {
    TIMEOUT: "no answer from the model in time",
    EXCEPTION: "the model returned an error",
    UNPARSEABLE: "the model's answer could not be read",
    BUDGET: "the check ran out of steps",
}


def failure_of(exc: BaseException | None) -> str:
    """The failure kind of an exception raised by a model call."""
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "Timeout" in type(exc).__name__:
        return TIMEOUT
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return UNPARSEABLE
    return EXCEPTION


def skip_line(failure: str) -> str:
    """The self-hosted skip line: what happened and that nothing went out."""
    why = _IN_BRACKETS.get(failure, _IN_BRACKETS[EXCEPTION])
    return f"Not sent: the check before sending didn't finish ({why}). Nothing went out."


async def record_skip(
    *, actor: str, failure: str, outreach_id: str = "", campaign_id: str = "", detail: str = "",
) -> str:
    """Journal one failed check as a skip and return the skip line. Never raises."""
    kind = failure if failure in FAILURES else EXCEPTION
    try:
        from ..db.queries import log_action
        from ..db.async_bridge import run_db

        await run_db(
            log_action, MODEL_FAILED,
            outreach_id=outreach_id,
            campaign_id=campaign_id,
            result="skipped",
            details={
                "actor": actor, "outcome": MODEL_FAILED, "failure": kind,
                "reason": MODEL_UNAVAILABLE, "detail": (detail or "")[:120],
            },
        )
    except Exception as e:  # noqa: BLE001 - the journal never decides the outcome
        logger.warning("model-failure journal skipped for %s: %s", actor, e)
    logger.warning("%s check did not finish (%s) for %s: not sent", actor, kind, (outreach_id or "-")[:8])
    return skip_line(kind)
