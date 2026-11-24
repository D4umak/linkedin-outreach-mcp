"""Prompts for the campaign-reply exception operator."""

from __future__ import annotations

from heylead.services.booking_links import offered_link

from typing import Any

REPLY_AGENT_SYSTEM = """You are a safety operator for LinkedIn auto-replies.
Read tools if you need context, then decide.

Decisions:
- hold: a human must see this (ambiguous intent, legal/compliance, possible wrong person, sensitive, or you are unsure)
- skip: no answer to this message is needed; wait for their next message (do not close the outreach)
- reply: a clear, safe conversational reply is appropriate
- book: they clearly want a meeting, ask for a call, propose a day or time, or hand off a phone number or calendar link for scheduling our meeting (whether or not a booking link is in context). Do NOT choose book if they are pitching their own product/service, or if they step back from a meeting ask to request written questions instead
- none: you cannot decide

Default to hold when unsure. Never invent facts. Do not write the reply yourself."""


def build_reply_agent_context(
    *,
    prospect_name: str,
    title: str,
    company: str,
    sentiment: str,
    reply_text: str,
    has_booking_target: bool,
) -> str:
    booking = "yes" if has_booking_target else "no"
    return (
        f"Prospect: {prospect_name} — {title} at {company}\n"
        f"Last sentiment: {sentiment}\n"
        f"Calendar or booking link available: {booking}\n"
        f"Last prospect message:\n{(reply_text or '')[:1500]}"
    )


def booking_target_available(prospect_calendar_url: str, campaign_config: dict[str, Any]) -> bool:
    if (prospect_calendar_url or "").strip():
        return True
    link = offered_link(campaign_config)
    return bool(str(link).strip())
