"""An outreach's status in the dashboard's words.

The prospects table on heylead.dev says "Closed: lost" where the store holds
``closed_unhappy``. The exports wrote the codes, so a file disagreed with the
page it came from (D4umak/heylead-api#1926). heylead-api's
``app/services/outreach_labels.py`` is the full twin (status, last action,
next step); ``export_campaign`` reads only the status here, so only the status
is kept (heylead-api#5542).

The words are the dashboard's, ported from heylead-dashboard
``src/lib/outreachLabels.ts`` (status, ``readableCode``). Change them
together. A code neither side has seen still reads as words ("Some new
code"), never blank and never the code.
"""

from __future__ import annotations

import re

STATUS_LABELS: dict[str, str] = {
    "pending": "Pending",
    "invited": "Invited",
    "connected": "Connected",
    "messaged": "Messaged",
    "messaged_without_dm": "Connected",
    "replied": "Replied",
    "replied_without_dm": "Replied",
    "hot_lead": "Hot lead",
    "won": "Won",
    "skipped": "Skipped",
    "opted_out": "Opted out",
    "closed_happy": "Closed: won",
    "closed_unhappy": "Closed: lost",
    "unsubscribed": "Unsubscribed",
    "bounced": "Bounced",
    "expired": "Expired",
    "error": "Error",
    "engaged": "Engaged",
    "qualified": "Qualified",
}


#: Words a code writes in lower case that are written otherwise ("DM", not
#: "Dm"): heylead-api's outreach_labels.WORD_FORMS; change them together.
WORD_FORMS: dict[str, str] = {
    "icp": "ICP", "rag": "RAG", "dm": "DM", "dms": "DMs", "api": "API", "crm": "CRM",
    "llm": "LLM", "ai": "AI", "sn": "SN", "url": "URL", "urls": "URLs", "id": "ID",
    "ids": "IDs", "mcp": "MCP", "ooo": "OOO", "seo": "SEO", "csv": "CSV", "http": "HTTP",
    "json": "JSON", "smtp": "SMTP", "ui": "UI", "b2b": "B2B", "faq": "FAQ",
    "linkedin": "LinkedIn", "inmail": "InMail", "inmails": "InMails", "hubspot": "HubSpot",
    "unipile": "Unipile", "slack": "Slack", "gmail": "Gmail",
}


def readable_code(code: str | None, fallback: str = "Unknown") -> str:
    """``"some_new_code"`` as ``"Some new code"``, ``"dm_waits"`` as ``"DM waits"``; ``fallback`` for nothing."""
    words = re.sub(r"\s+", " ", re.sub(r"[_-]+", " ", str(code or ""))).strip().lower()
    if not words:
        return fallback
    spelled = " ".join(WORD_FORMS.get(w, w) for w in words.split(" "))
    return spelled[0].upper() + spelled[1:]


def status_label(status: str | None) -> str:
    """The Status column's words; an empty status is a pending row."""
    s = str(status or "").strip().lower() or "pending"
    return STATUS_LABELS.get(s) or readable_code(s)

