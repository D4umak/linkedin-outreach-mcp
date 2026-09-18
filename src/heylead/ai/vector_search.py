"""Vector search over stored chunks for ICP RAG.

Loads chunk embeddings from SQLite, computes cosine similarity
with the query embedding, and returns ranked results.

Requires: pip install heylead[icp]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..db.async_bridge import run_db

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """A search result from vector search."""

    chunk_id: str = ""
    text: str = ""
    ctx_text: str = ""
    header_path: str = ""
    source_uri: str = ""
    source_title: str = ""
    similarity_score: float = 0.0
    position: int = 0


async def search_chunks(
    query: str,
    icp_id: str | None = None,
    source_id: str | None = None,
    top_k: int = 20,
) -> list[SearchResult]:
    """Retrieve top-k chunks by cosine similarity to query embedding.

    Args:
        query: Search query text.
        icp_id: Search within all sources of a specific ICP.
        source_id: Search within a specific source.
        top_k: Number of results to return.

    Returns:
        List of SearchResult, sorted by similarity (highest first).
    """
    from .embeddings import _check_icp_deps, bytes_to_embedding, get_embedder

    _check_icp_deps()
    import numpy as np

    # Get chunks from DB
    chunks = await run_db(_load_chunks_with_embeddings, icp_id, source_id)
    if not chunks:
        logger.info("No chunks with embeddings found for search")
        return []

    # Build embedding matrix
    embedder = get_embedder()
    dim = embedder.dimension
    valid_chunks = []
    embedding_list = []

    for chunk in chunks:
        emb_bytes = chunk.get("embedding")
        if emb_bytes is None:
            continue
        try:
            vec = bytes_to_embedding(emb_bytes, dim)
            embedding_list.append(vec)
            valid_chunks.append(chunk)
        except Exception as e:
            logger.warning(f"Failed to decode embedding for chunk {chunk['id']}: {e}")

    if not valid_chunks:
        logger.info("No valid embeddings found")
        return []

    corpus_vecs = np.stack(embedding_list)

    # Compute query embedding
    query_vec = embedder.embed_query(query)

    # Compute similarities
    similarities = embedder.cosine_similarity(query_vec, corpus_vecs)

    # Sort and take top-k
    top_indices = similarities.argsort()[::-1][:top_k]

    results = []
    for idx in top_indices:
        chunk = valid_chunks[idx]
        score = float(similarities[idx])
        if score <= 0:
            continue
        results.append(SearchResult(
            chunk_id=chunk["id"],
            text=chunk.get("text", ""),
            ctx_text=chunk.get("ctx_text", ""),
            header_path=chunk.get("header_path", ""),
            source_uri=chunk.get("source_uri", ""),
            source_title=chunk.get("source_title", ""),
            similarity_score=score,
            position=chunk.get("position", 0),
        ))

    return results


def _load_chunks_with_embeddings(
    icp_id: str | None, source_id: str | None,
) -> list[dict[str, Any]]:
    """Load chunks with their embeddings and source metadata."""
    from ..db.schema import get_db

    db = get_db()

    if source_id:
        rows = db.execute(
            """SELECT c.*, s.uri as source_uri, s.title as source_title
               FROM icp_chunks c
               JOIN icp_sources s ON c.source_id = s.id
               WHERE c.source_id = ? AND c.embedding IS NOT NULL
               ORDER BY c.position""",
            (source_id,),
        ).fetchall()
    elif icp_id:
        rows = db.execute(
            """SELECT c.*, s.uri as source_uri, s.title as source_title
               FROM icp_chunks c
               JOIN icp_sources s ON c.source_id = s.id
               WHERE s.icp_id = ? AND c.embedding IS NOT NULL
               ORDER BY c.source_id, c.position""",
            (icp_id,),
        ).fetchall()
    else:
        # Search all chunks (not recommended for large datasets)
        rows = db.execute(
            """SELECT c.*, s.uri as source_uri, s.title as source_title
               FROM icp_chunks c
               JOIN icp_sources s ON c.source_id = s.id
               WHERE c.embedding IS NOT NULL
               ORDER BY c.source_id, c.position""",
        ).fetchall()

    db.close()
    return [dict(r) for r in rows]
