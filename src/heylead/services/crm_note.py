"""The HubSpot conversation note, built once (api #2825).

The note is HTML. The prospect's name and what the prospect wrote are a
stranger's text, so every value is escaped and only our own tags remain; line
breaks inside a message are kept as <br>. crm_sync and close_outreach both
build their note here.
"""

from __future__ import annotations

import html
from typing import Any

MAX_MESSAGES = 10


def _text(value: Any) -> str:
    return "<br>".join(html.escape(line) for line in str(value or "").splitlines())


def conversation_note_html(*, campaign_name: str, prospect_name: str, messages: list[dict[str, Any]]) -> str:
    lines = []
    for msg in messages[:MAX_MESSAGES]:
        role = "You" if msg.get("role") == "sdr" else str(prospect_name or "")
        lines.append(f"{html.escape(role)}: {_text(msg.get('text'))}")
    return f"<b>HeyLead Conversation ({html.escape(str(campaign_name or ''))})</b><br><br>" + "<br>".join(lines)
