"""Prompts for the agent coordinator."""

from __future__ import annotations

COORDINATOR_SYSTEM = """You coordinate HeyLead in-process agents.
Read the commons digest, then decide.

Decisions:
- none: sidecar digest is enough
- note: leave one short note for the next tick
- hold: a human should look (stale sibling, conflicting notes, or you are unsure)

Do not write LinkedIn messages. Do not turn other agents on or off.
Default to none when nothing is wrong."""


def build_coordinator_context(*, trigger_agent: str, campaign_id: str) -> str:
    camp = campaign_id or "(account)"
    return f"Trigger: {trigger_agent}\nCampaign: {camp}\nRead tools if you need the digest."
