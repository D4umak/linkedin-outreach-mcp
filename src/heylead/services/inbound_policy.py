"""Decide whether an inbound stranger gets a reply, a counter-pitch, or silence.

Classification stores intent and recommended_action; those used to be decorative
on the DM path. This module is the single send gate for pipeline and backfill.
"""

from __future__ import annotations

from typing import Any, Literal

InboundReplyDecision = Literal["dismiss", "counter_pitch", "reply"]


# The qualifier's verdicts that mean "write to this person". accept_and_monitor
# ("keep in the network, low-confidence match") and ignore mean stay quiet.
# Until 28 Sep 2026 only ignore was read (heylead-api #1815).
DISCOVERY_DM_VERDICTS = frozenset({"engage_immediately", "ask_purpose"})


def verdict_allows_discovery_dm(recommended_action: str | None) -> bool:
    """Does the qualifier's ``recommended_action`` say to write to the person?"""
    return str(recommended_action or "").strip() in DISCOVERY_DM_VERDICTS


def decide_inbound_reply(signal: dict[str, Any] | None) -> InboundReplyDecision:
    """Return what to do with a classified inbound signal's DM.

    Order:
    1. Known campaign thread stays on that campaign's reply path.
    2. Spam / job-seeking are dismissed.
    3. Vendor pitch: counter-pitch only when they match an ICP.
    4. Partnership: reply only when they match an ICP; otherwise stay quiet.
    5. The verdict is honored: only engage_immediately and ask_purpose write;
       accept_and_monitor, ignore and a missing verdict stay quiet.
    6. Everything else gets a contextual reply.
    """
    signal = signal or {}
    if signal.get("_relationship_continue"):
        return "reply"

    intent = str(signal.get("intent") or "unknown").strip()
    action = str(signal.get("recommended_action") or "").strip()
    icp_id = str(signal.get("matched_icp_id") or "").strip()

    if intent in ("spam", "job_seeking"):
        return "dismiss"
    if intent == "vendor_pitch":
        return "counter_pitch" if icp_id else "dismiss"
    if intent == "partnership":
        return "reply" if icp_id else "dismiss"
    if not verdict_allows_discovery_dm(action):
        return "dismiss"
    return "reply"
