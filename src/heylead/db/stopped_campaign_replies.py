"""Hold logic for stopped campaigns (twin of heylead-api stopped_campaign_replies)."""

from __future__ import annotations


def stopped_reason(status: str) -> str:
    """The hold's words, which the person reads under "Why HeyLead held it"."""
    return (
        f"The campaign is {status or 'stopped'}, so HeyLead doesn't answer for it. "
        "Nothing has been sent; the reply is yours to answer."
    )
