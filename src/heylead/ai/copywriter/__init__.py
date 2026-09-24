"""The copywriter skill: one set of house rules for every word this product writes.

``rules.py`` is byte-identical to heylead-api's
``app/services/copywriter/rules.py``; a test in each repo hashes it and
compares the digest, so a rule changed on one side fails the other's suite
until it is mirrored. Design:
heylead-api docs/superpowers/specs/2026-09-21-copywriter-skill-design.md
"""

from .render import HEADING, rules_for
from .rules import ALL, AUTHORED, CHANNELS, CONVERSATIONAL, OUTREACH, RULES, Rule

__all__ = [
    "ALL", "AUTHORED", "CHANNELS", "CONVERSATIONAL", "HEADING", "OUTREACH",
    "RULES", "Rule", "rules_for",
]


# The client's generators name what they are writing as a "message_type";
# the table is keyed by channel. The two names meet here rather than at six
# call sites. An unknown type falls back to "dm", where every conversational
# rule binds: an unmapped caller is over-ruled, never unruled.
_CHANNEL_BY_MESSAGE_TYPE = {
    "invitation": "invite", "invite": "invite", "dm": "dm", "message": "dm",
    "followup": "followup", "follow_up": "followup", "inmail": "inmail",
    "email": "email", "reply": "reply", "check_in": "check_in",
    "comment": "comment", "comment_reply": "comment_reply",
    "discovery": "discovery_dm", "counter_pitch": "counter_pitch",
    "post": "post", "x_post": "x_post", "headline": "headline", "about": "about",
}


def channel_for_message_type(message_type: str) -> str:
    return _CHANNEL_BY_MESSAGE_TYPE.get(str(message_type or "").strip().lower(), "dm")


__all__.append("channel_for_message_type")
