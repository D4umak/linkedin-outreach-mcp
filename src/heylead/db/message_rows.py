"""The one INSERT into messages.

Every row is written here, with the four provenance columns
(ai/copywriter/provenance.py), so a new writer cannot store an outbound
message that forgets where it came from. .semgrep/outbound-message-without-
provenance refuses an INSERT INTO messages anywhere else in src/.
"""

from __future__ import annotations

from typing import Any

from ..ai.copywriter.provenance import EMPTY, Provenance


def insert_message_row(
    db: Any,
    *,
    id: str,
    outreach_id: str,
    role: str,
    text: str,
    sentiment: str = "",
    format: str = "text",
    timestamp: int,
    external_message_id: str | None = None,
    provenance: Provenance | None = None,
) -> None:
    """Insert one message row. The caller commits.

    A prospect's message carries no provenance: nobody's prompt wrote it.
    """
    stamp = (provenance if role == "sdr" and provenance is not None else EMPTY).as_row()
    db.execute(
        """INSERT INTO messages (id, outreach_id, role, text, sentiment, format,
                                 timestamp, external_message_id,
                                 prompt_name, prompt_version, model, variant)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, outreach_id, role, text, sentiment, format, timestamp,
         external_message_id, stamp["prompt_name"], stamp["prompt_version"],
         stamp["model"], stamp["variant"]),
    )
