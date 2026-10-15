"""What a LinkedIn message carries besides its words: one rule for the client.

A message whose only content is a file arrives from Unipile with ``text`` ""
and the file under ``attachments``. Every normaliser built the message from
its text alone, so a PDF a prospect sent read as an empty line in ``inbox``,
and ``check_replies`` skipped the turn as if nobody had written
(heylead-api#3077: a CTO's research paper, 13 Oct 2026). The self-hosted
normaliser appended the attachment's URL instead, which for a file is an
``att://`` handle that means nothing to a reader or a model.

Every normaliser in ``linkedin/`` builds a message's text with
``message_text`` and keeps ``attachment_summary`` under ``attachments``.
``.semgrep/a-message-read-from-its-text-alone.yaml`` fails a normaliser that
reads ``text`` without going through here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_TYPE_LABELS = {
    "application/pdf": "PDF",
    "application/msword": "Word",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word",
    "application/vnd.ms-excel": "Excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel",
    "application/vnd.ms-powerpoint": "PowerPoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PowerPoint",
    "text/plain": "text",
    "text/csv": "CSV",
    "application/zip": "ZIP",
}

# Unipile's attachment types that have no file name of their own.
_KIND_NAMES = {
    "img": "an image",
    "image": "an image",
    "video": "a video",
    "audio": "a voice message",
    "file": "a file",
    "linkedin_post": "a LinkedIn post",
    "video_meeting": "a video meeting",
}


def _readable_url(url: str) -> bool:
    """A link a reader can open. ``att://`` is Unipile's handle for a file."""
    return url.startswith(("http://", "https://"))


def _size(n: Any) -> int | None:
    if isinstance(n, bool):
        return None
    try:
        size = int(n)
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


def attachment_summary(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each attachment of a raw Unipile message as name, type and size.

    Never the ``att://`` handle: it opens nothing. ``id`` is kept so a
    caller can ask the api for the file.
    """
    out: list[dict[str, Any]] = []
    for att in raw.get("attachments") or []:
        if not isinstance(att, Mapping):
            continue
        url = str(att.get("url") or att.get("link") or att.get("href") or "")
        item: dict[str, Any] = {
            "id": str(att.get("id") or ""),
            "type": str(att.get("type") or ""),
            "name": str(att.get("file_name") or att.get("name") or att.get("title") or ""),
            "mimetype": str(att.get("mimetype") or att.get("mime_type") or ""),
            "size": _size(att.get("file_size") or att.get("size")),
            "unavailable": bool(att.get("unavailable")),
        }
        if _readable_url(url):
            item["url"] = url
        out.append(item)
    return out


def _type_label(att: Mapping[str, Any]) -> str:
    mimetype = str(att.get("mimetype") or "").lower()
    if mimetype in _TYPE_LABELS:
        return _TYPE_LABELS[mimetype]
    if mimetype.startswith("image/"):
        return "image"
    if mimetype.startswith("video/"):
        return "video"
    if mimetype.startswith("audio/"):
        return "audio"
    name = str(att.get("name") or "")
    if "." in name:
        return name.rsplit(".", 1)[1].upper()
    return ""


def _size_label(size: int | None) -> str:
    if not size:
        return ""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{round(size / 1024)} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def attachment_line(att: Mapping[str, Any]) -> str:
    """One attachment as a reader sees it: ``📎 paper.pdf (PDF, 872 KB)``."""
    url = str(att.get("url") or "")
    if not att.get("name") and not att.get("type") and url:
        return f"📎 {url}"  # a bare link preview
    name = str(att.get("name") or "") or _KIND_NAMES.get(str(att.get("type") or ""), "an attachment")
    details = ", ".join(p for p in (_type_label(att), _size_label(att.get("size"))) if p)
    line = f"📎 {name}"
    if details:
        line += f" ({details})"
    if att.get("unavailable"):
        line += ", no longer available"
    if url and url not in name:
        line += f" {url}"
    return line


def message_text(raw: Mapping[str, Any]) -> str:
    """A raw message's words, then one line per attachment.

    A message that is only a file reads as its file line, never as "".
    A link preview's URL is already in the words, or on its line.
    """
    words = str(raw.get("text") or raw.get("body") or raw.get("content") or "").strip()
    lines = [
        attachment_line(att) for att in attachment_summary(raw)
        if not (att.get("url") and att["url"] in words)
    ]
    return "\n".join([words, *lines]).strip()


def is_file_only(text: str) -> bool:
    """Whether a message's text, built by ``message_text``, is files and no words.

    Nobody can answer a file unread: the reply paths hold such a turn for the
    owner instead of writing to it (#3077).
    """
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    return bool(lines) and all(line.startswith("📎 ") for line in lines)
