"""Validate-then-fix gate for drafts that do not already run the send loop.

Invite, follow-up, InMail, comment, and reply already call validate_message
before send. Inbound discovery, counter-pitch, email, and posts do not.
This helper is the shared last check: pass through, one surgical fix, or
return empty so the caller skips the send.
"""

from __future__ import annotations

import logging
from typing import Any

from .message_validator import validate_message

logger = logging.getLogger(__name__)


async def guard_draft(
    text: str,
    voice_signature: dict[str, Any] | None,
    message_type: str,
    max_chars: int,
    *,
    first_touch: bool = False,
) -> str:
    """Return text that passes validate_message, or "" if it cannot be fixed.

    first_touch: whether this draft is the reader's first message from us
    (an invitation or a first DM). Every caller of guard_draft today —
    inbound discovery, counter-pitch, email, posts — is a reply to
    something the reader already sent, or a post nobody solicited, so the
    default is False; a future invitation-shaped caller passes True.
    """
    draft = (text or "").strip()
    if not draft:
        return ""

    result = validate_message(draft, voice_signature, max_chars, message_type, first_touch=first_touch)
    if result.is_valid:
        return draft

    logger.info("guard_draft rejected %s: %s", message_type, result.issues)
    try:
        from .message_fixer import fix_message

        fixed = await fix_message(
            message=draft,
            issues=result.issues,
            voice_signature=voice_signature or {},
            message_type=message_type,
            max_chars=max_chars,
        )
    except Exception as e:
        logger.warning("guard_draft fix failed for %s: %s", message_type, e)
        return ""

    fixed = (fixed or "").strip()
    if not fixed:
        return ""

    again = validate_message(fixed, voice_signature, max_chars, message_type, first_touch=first_touch)
    if again.is_valid:
        return fixed

    logger.info("guard_draft still invalid after fix (%s): %s", message_type, again.issues)
    return ""
