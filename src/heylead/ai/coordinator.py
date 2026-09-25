"""Prompts for the agent coordinator."""

from __future__ import annotations

from typing import Any

from ..services.agent_context import numbers_block

COORDINATOR_SYSTEM = """You coordinate HeyLead in-process agents.
Read the commons digest, then decide.

Decisions:
- none: sidecar digest is enough
- note: leave one short note for the next tick
- hold: a human must look at the whole campaign (conflicting notes).
  Do not hold because you are unsure.

A sibling beat marked [one prospect] (reply, closer, strategist,
send_fit) is about ONE person: a reply hold parks one conversation for a
human, it does not pause the campaign. It is never a reason to hold the
campaign.

Do not write LinkedIn messages. Do not turn other agents on or off.
Default to none when nothing is wrong."""


def build_coordinator_context(
    *, trigger_agent: str, campaign_id: str, numbers: dict[str, Any] | None,
) -> str:
    """``numbers`` is ``agent_context.numbers_for``; rendered only there (heylead-api#1209)."""
    camp = campaign_id or "(account)"
    return (
        f"Trigger: {trigger_agent}\nCampaign: {camp}\n\n{numbers_block(numbers)}\n\n"
        "Read tools if you need the digest."
    )
