"""Tool handler: delete a recently sent LinkedIn message.

LinkedIn only allows deletion within 60 minutes of sending. On a hosted
account (the only mode supported), message deletion is not available
through the client.
"""

from __future__ import annotations

from ..hosted_writes import message_delete_refused


async def run_delete_message(
    campaign_id: str = "",
    outreach_id: str = "",
    message_id: str = "",
) -> str:
    """Delete a recently sent LinkedIn message.

    Args:
        campaign_id: Campaign context (unused directly, kept for consistency).
        outreach_id: The outreach whose last SDR message should be deleted.
        message_id: A raw Unipile message ID to delete directly.

    Returns:
        Human-readable refusal string.
    """
    return message_delete_refused()
