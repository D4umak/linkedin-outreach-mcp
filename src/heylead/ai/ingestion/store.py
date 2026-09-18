"""Chunk storage for ICP data ingestion.

Stores chunks in SQLite (icp_chunks table) with optional embedding blobs.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

from .chunker import Chunk

logger = logging.getLogger(__name__)


def save_source(
    icp_id: str,
    source_type: str,
    uri: str = "",
    title: str = "",
    content_hash: str = "",
    chunk_count: int = 0,
) -> str:
    """Create an ICP source record and return its ID."""
    from ...db.schema import get_db

    source_id = str(uuid.uuid4())
    db = get_db()
    db.execute(
        """INSERT INTO icp_sources
           (id, icp_id, source_type, uri, title, content_hash, chunk_count, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (source_id, icp_id, source_type, uri, title, content_hash, chunk_count,
         int(time.time())),
    )
    db.commit()
    db.close()
    return source_id


def save_chunks(source_id: str, chunks: list[Chunk]) -> int:
    """Save a list of chunks to the DB.

    Returns the number of chunks saved.
    """
    from ...db.schema import get_db

    if not chunks:
        return 0

    db = get_db()
    now = int(time.time())
    for chunk in chunks:
        db.execute(
            """INSERT INTO icp_chunks
               (id, source_id, text, ctx_text, header_path, position, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (chunk.id, source_id, chunk.text, chunk.ctx_text,
             chunk.header_path, chunk.position, now),
        )
    db.commit()

    # Update chunk count on source
    db.execute(
        "UPDATE icp_sources SET chunk_count = ? WHERE id = ?",
        (len(chunks), source_id),
    )
    db.commit()
    db.close()

    return len(chunks)


def save_chunk_embeddings(
    source_id: str,
    chunk_ids: list[str],
    embeddings_bytes: list[bytes],
) -> None:
    """Save embedding blobs for chunks.

    Args:
        source_id: Source ID (for logging).
        chunk_ids: List of chunk IDs.
        embeddings_bytes: List of serialized numpy arrays (one per chunk).
    """
    from ...db.schema import get_db

    if len(chunk_ids) != len(embeddings_bytes):
        raise ValueError("chunk_ids and embeddings_bytes must have the same length")

    db = get_db()
    for chunk_id, emb_bytes in zip(chunk_ids, embeddings_bytes):
        db.execute(
            "UPDATE icp_chunks SET embedding = ? WHERE id = ?",
            (emb_bytes, chunk_id),
        )
    db.commit()
    db.close()
    logger.info(f"Saved {len(chunk_ids)} embeddings for source {source_id[:8]}")


def get_chunks_for_source(source_id: str) -> list[dict[str, Any]]:
    """Get all chunks for a source."""
    from ...db.schema import get_db

    db = get_db()
    rows = db.execute(
        "SELECT * FROM icp_chunks WHERE source_id = ? ORDER BY position",
        (source_id,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_chunks_for_icp(icp_id: str) -> list[dict[str, Any]]:
    """Get all chunks across all sources for an ICP."""
    from ...db.schema import get_db

    db = get_db()
    rows = db.execute(
        """SELECT c.* FROM icp_chunks c
           JOIN icp_sources s ON c.source_id = s.id
           WHERE s.icp_id = ?
           ORDER BY c.source_id, c.position""",
        (icp_id,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_sources_for_icp(icp_id: str) -> list[dict[str, Any]]:
    """Get all sources for an ICP."""
    from ...db.schema import get_db

    db = get_db()
    rows = db.execute(
        "SELECT * FROM icp_sources WHERE icp_id = ? ORDER BY created_at",
        (icp_id,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]
