"""MCP tool: show what the cloud's strategy loop has learned."""

from __future__ import annotations


async def run_show_strategy(campaign_id: str = "") -> str:
    """Build the strategy dashboard output.

    The cloud runs the strategy loop (heylead-api#1211), so read it there. A
    machine with no sign-in gets the sign-in line: the local strategy engine
    that read this machine's tables could not run since v0.10.506 and was
    deleted (heylead-api#6252).
    """
    from .. import config
    if config.is_backend_mode():
        from ..services import cloud_learning
        data = await cloud_learning.fetch_learning()
        return cloud_learning.format_strategy(data) if data else cloud_learning.UNREACHABLE
    from ..hosted_writes import not_signed_in_reply
    return not_signed_in_reply("there is no strategy to show")
