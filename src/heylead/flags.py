"""Reading campaign feature flags safely.

Campaign config is written from several places: the MCP `edit_campaign` tool
documents its parameters as the strings "on"/"off", the wizard and API write
real booleans, and older records carry 0/1. Reading any of those with
`config.get(flag, True)` is a trap — the string "off" is truthy, so a gate the
user explicitly closed reads as open. That is exactly how engagements, profile
views, follows and invites ran on a locked-down campaign on 8 Aug 2026.

Always gate on `flag_enabled(config, "enable_x")`, never on `config.get(...)`.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_TRUE = {"true", "on", "yes", "y", "1", "enabled", "enable"}
_FALSE = {"false", "off", "no", "n", "0", "disabled", "disable"}


def flag_enabled(config: dict[str, Any], key: str, default: bool = True) -> bool:
    """Return whether a campaign feature flag is on.

    Absent, None or empty-string values mean "not set" and yield ``default``.
    An explicit value that cannot be interpreted yields ``False``: a user who
    wrote something deliberate should never have it silently read as "on".
    """
    if key not in config:
        return default
    value = config[key]

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return default
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        logger.warning(
            "Unrecognised value %r for flag %s — treating as OFF", value, key
        )
        return False

    logger.warning(
        "Unsupported type %s for flag %s — treating as OFF", type(value).__name__, key
    )
    return False
