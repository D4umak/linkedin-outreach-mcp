"""Did a tool answer with an error it handled? (api #3206)

A tool that catches a failure and answers the person in words ("❌ Campaign not
found", "Failed to send …") raised nothing, so tool.called recorded ok=true:
`ok = error is None` in both telemetry writers. In the #3077 week, 61 of 61
calls in Denys's workspaces read ok=true. This reads the answer's first line:
a listed marker there is a handled error.

Only (ok, error_class) leaves this function. The answer's text never does: it
can hold a campaign name or a person's name, and telemetry carries none.

The same list lives in heylead-api (app/mcp/handled_error.py), and
MARKERS_DIGEST pins the two: each repo's test fails when its list changes alone.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

#: What a handled error's first line starts with, after markdown emphasis is dropped. The
#: client's tools write exactly these ("Error: 'name' is required", "Failed to send reply: …",
#: "Could not load …"); "Errors: 0" and "Failed sends: 2" are not errors. "⚠️" is not one:
#: suggest_next_action starts a normal answer with it.
MARKERS: tuple[str, ...] = ("❌", "Error:", "Failed to", "Could not")
MARKERS_DIGEST = "fa607c8b8980d741e675ccd81fffcfe93ceab2c87fd3ab5923308c50810c1bde"
HANDLED = "HandledError"

_LEAD = re.compile(r"^[\s#>]+")
_EMPHASIS = re.compile(r"[*_]+")
_WORD = re.compile(r"^(?:" + "|".join(re.escape(m) for m in MARKERS[1:]) + r")(?![A-Za-z])")


def digest() -> str:
    return hashlib.sha256("\n".join(MARKERS).encode()).hexdigest()


def is_handled_error(result: Any) -> bool:
    """True when ``result`` is a string whose first non-blank line starts with a marker."""
    if not isinstance(result, str):
        return False
    for line in result.splitlines():
        line = _LEAD.sub("", _EMPHASIS.sub("", line))
        if not line.strip():
            continue
        return line.startswith(MARKERS[0]) or bool(_WORD.match(line))
    return False


def classify(result: Any, error: BaseException | None) -> tuple[bool, str]:
    """(ok, error_class) of one call. A raised exception keeps its own class and wins."""
    if error is not None:
        return False, type(error).__name__
    if is_handled_error(result):
        return False, HANDLED
    return True, ""
