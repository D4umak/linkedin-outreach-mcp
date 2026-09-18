"""Text chunking for ICP data ingestion.

Splits text into overlapping chunks with markdown header tracking.
Uses a simple recursive character splitter (no langchain dependency).
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Chunk:
    """A text chunk with metadata."""

    id: str = ""
    text: str = ""
    ctx_text: str = ""          # chunk + surrounding context
    header_path: str = ""       # e.g. "About > Team > Leadership"
    source_uri: str = ""
    source_title: str = ""
    position: int = 0           # chunk index within source

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid.uuid4())


def chunk_text(
    text: str,
    source_uri: str = "",
    source_title: str = "",
    chunk_size: int = 512,
    chunk_overlap: int = 100,
) -> list[Chunk]:
    """Split text into overlapping chunks with header tracking.

    Args:
        text: Raw text or markdown content.
        source_uri: Source URL or identifier.
        source_title: Human-readable source title.
        chunk_size: Target chunk size in characters.
        chunk_overlap: Overlap between consecutive chunks.

    Returns:
        List of Chunk objects.
    """
    if not text or not text.strip():
        return []

    # Split into sections by markdown headers
    sections = _split_by_headers(text)
    chunks: list[Chunk] = []
    position = 0

    for header_path, section_text in sections:
        # Split each section into chunks
        section_chunks = _split_text(
            section_text, chunk_size, chunk_overlap,
        )
        for i, chunk_text_str in enumerate(section_chunks):
            if not chunk_text_str.strip():
                continue

            # Build context: previous + current + next chunk
            prev_text = section_chunks[i - 1][-chunk_overlap:] if i > 0 else ""
            next_text = section_chunks[i + 1][:chunk_overlap] if i < len(section_chunks) - 1 else ""
            ctx = f"{prev_text} {chunk_text_str} {next_text}".strip()

            chunks.append(Chunk(
                text=chunk_text_str.strip(),
                ctx_text=ctx,
                header_path=header_path,
                source_uri=source_uri,
                source_title=source_title,
                position=position,
            ))
            position += 1

    return chunks


def chunk_pages(
    pages: list[dict],
    chunk_size: int = 512,
    chunk_overlap: int = 100,
) -> list[Chunk]:
    """Chunk multiple crawled pages.

    Args:
        pages: List of {"url", "title", "markdown", "content_hash"} dicts.
        chunk_size: Target chunk size.
        chunk_overlap: Overlap between chunks.

    Returns:
        List of Chunk objects from all pages.
    """
    all_chunks: list[Chunk] = []
    for page in pages:
        page_chunks = chunk_text(
            text=page.get("markdown", ""),
            source_uri=page.get("url", ""),
            source_title=page.get("title", ""),
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        all_chunks.extend(page_chunks)
    return all_chunks


# ──────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────

_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def _split_by_headers(text: str) -> list[tuple[str, str]]:
    """Split markdown text by headers, returning (header_path, text) pairs.

    Tracks nested header paths like "About > Team > Leadership".
    """
    sections: list[tuple[str, str]] = []
    header_stack: list[tuple[int, str]] = []  # (level, title)
    current_text_parts: list[str] = []
    current_path = ""

    lines = text.split("\n")
    for line in lines:
        match = _HEADER_RE.match(line.strip())
        if match:
            # Save previous section
            section_text = "\n".join(current_text_parts).strip()
            if section_text:
                sections.append((current_path, section_text))
            current_text_parts = []

            level = len(match.group(1))
            title = match.group(2).strip()

            # Update header stack
            while header_stack and header_stack[-1][0] >= level:
                header_stack.pop()
            header_stack.append((level, title))

            current_path = " > ".join(h[1] for h in header_stack)
        else:
            current_text_parts.append(line)

    # Don't forget the last section
    section_text = "\n".join(current_text_parts).strip()
    if section_text:
        sections.append((current_path, section_text))

    # If no headers found, return entire text as one section
    if not sections:
        sections = [("", text.strip())]

    return sections


def _split_text(
    text: str, chunk_size: int, overlap: int,
) -> list[str]:
    """Split text into overlapping chunks by paragraph boundaries.

    Tries to split at paragraph breaks first, then sentence breaks,
    then word breaks as a last resort.
    """
    if len(text) <= chunk_size:
        return [text]

    # Try splitting by paragraphs first
    paragraphs = re.split(r"\n\n+", text)
    chunks = _merge_splits(paragraphs, chunk_size, overlap, "\n\n")

    if len(chunks) <= 1 and len(text) > chunk_size:
        # Try sentence-level splitting
        sentences = re.split(r"(?<=[.!?])\s+", text)
        chunks = _merge_splits(sentences, chunk_size, overlap, " ")

    if len(chunks) <= 1 and len(text) > chunk_size:
        # Hard split by character
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            chunks.append(text[start:end])
            if end >= len(text):
                break
            # start + 1 floor keeps the loop advancing even if overlap >= chunk_size
            start = max(end - overlap, start + 1)

    return chunks


def _merge_splits(
    splits: list[str], chunk_size: int, overlap: int, separator: str,
) -> list[str]:
    """Merge small splits into chunks of approximately chunk_size."""
    chunks: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for part in splits:
        part_len = len(part)
        if current_len + part_len + len(separator) > chunk_size and current_parts:
            chunks.append(separator.join(current_parts))
            # Keep overlap: retain last part(s) that fit in overlap window
            overlap_parts: list[str] = []
            overlap_len = 0
            for p in reversed(current_parts):
                if overlap_len + len(p) + len(separator) <= overlap:
                    overlap_parts.insert(0, p)
                    overlap_len += len(p) + len(separator)
                else:
                    break
            current_parts = overlap_parts
            current_len = sum(len(p) for p in current_parts) + len(separator) * max(len(current_parts) - 1, 0)

        current_parts.append(part)
        current_len += part_len + (len(separator) if len(current_parts) > 1 else 0)

    if current_parts:
        chunks.append(separator.join(current_parts))

    return chunks
