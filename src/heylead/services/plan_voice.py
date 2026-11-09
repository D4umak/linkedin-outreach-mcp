"""What the local campaign plan may say about the owner's voice (heylead-api#5429).

The plan's Invite and Open lines said "in your voice" whatever the voice
state. The api's plan now asks its own reader (app/services/plan_voice.py,
five states in the first-send gate's order). The client's local plan has no
verdict and no wait, so it has three:

- USABLE   a self-hosted install whose ``voice_signature`` is a usable voice.
- BUILDING a self-hosted install with none: messages go plain until it exists.
- UNKNOWN  a hosted account whose api plan call failed: the voice lives on the
           api, so the line makes no claim either way (designer round 2).

"in your voice" lives only here; a semgrep rule keeps it out of
campaign_plan.py. The words are the four-role review's (designer, #5429).
"""

from __future__ import annotations

from typing import Any

USABLE = "usable"
BUILDING = "building"
UNKNOWN = "unknown"
STATES = (USABLE, BUILDING, UNKNOWN)

IN_YOUR_VOICE = " in your voice"
BUILDING_SENTENCE = "Messages use a plain style until HeyLead has built your voice from your LinkedIn posts."


def state_for(voice: Any, *, hosted: bool) -> str:
    """The plan's voice state from the stored voice signature and the install kind."""
    if hosted:
        return UNKNOWN
    from ..ai.voice_block import is_usable_voice

    return USABLE if is_usable_voice(voice) else BUILDING


def local_voice_state() -> str:
    """This install's voice state: UNKNOWN when hosted, else from its own voice_signature."""
    from .. import config

    hosted = config.is_backend_mode()
    if hosted:
        return UNKNOWN
    from ..db.queries import get_setting

    return state_for(get_setting("voice_signature", {}), hosted=False)


def voiced(state: str) -> str:
    """" in your voice" when the voice exists, else nothing."""
    return IN_YOUR_VOICE if state == USABLE else ""


def voice_sentence(state: str) -> str:
    """The sentence added when no usable voice is known to be missing ("" otherwise)."""
    return BUILDING_SENTENCE if state == BUILDING else ""
