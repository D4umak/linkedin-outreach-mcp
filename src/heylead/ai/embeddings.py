"""Local embedding engine for ICP RAG.

Uses sentence-transformers for high-quality local embeddings.
Model is lazy-loaded on first use and cached in ~/.heylead/embeddings/.

Requires: pip install heylead[icp]
  (sentence-transformers>=2.2.0, numpy>=1.24.0)
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from ..constants import DEFAULT_EMBEDDING_MODEL

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)

_EMBEDDER_INSTANCE: LocalEmbedder | None = None


def _check_icp_deps() -> None:
    """Check that ICP RAG dependencies are installed."""
    try:
        import numpy
        import sentence_transformers
    except ImportError:
        raise RuntimeError(
            "ICP RAG features require additional dependencies.\n"
            "Install them with: pip install heylead[icp]\n"
            "Or: pip install sentence-transformers numpy"
        )


class LocalEmbedder:
    """Local sentence-transformer embedding engine.

    Lazy-loads the model on first use to avoid startup cost.
    Model files are cached in ~/.heylead/embeddings/<model_name>/.
    """

    def __init__(self, model_name: str = DEFAULT_EMBEDDING_MODEL) -> None:
        _check_icp_deps()
        self._model_name = model_name
        self._model = None
        self._dim: int | None = None

    def _ensure_model(self) -> None:
        """Load the model if not already loaded."""
        if self._model is not None:
            return

        from sentence_transformers import SentenceTransformer

        from ..config import embeddings_path

        cache_dir = str(embeddings_path())
        os.makedirs(cache_dir, exist_ok=True)

        logger.info(f"Loading embedding model: {self._model_name}")
        self._model = SentenceTransformer(
            self._model_name, cache_folder=cache_dir,
        )
        self._dim = self._model.get_sentence_embedding_dimension()
        logger.info(
            f"Embedding model loaded: {self._model_name} (dim={self._dim})"
        )

    @property
    def dimension(self) -> int:
        """Return embedding dimension."""
        self._ensure_model()
        return self._dim  # type: ignore[return-value]

    def embed(self, texts: list[str]) -> "np.ndarray":
        """Embed a batch of texts.

        Args:
            texts: List of strings to embed.

        Returns:
            numpy array of shape (N, dim), dtype float32.
        """
        self._ensure_model()
        import numpy as np

        if not texts:
            return np.empty((0, self._dim), dtype=np.float32)

        embeddings = self._model.encode(
            texts,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,  # L2-normalize for cosine
        )
        return embeddings.astype(np.float32)

    def embed_query(self, query: str) -> "np.ndarray":
        """Embed a single query.

        Returns:
            numpy array of shape (dim,), dtype float32.
        """
        result = self.embed([query])
        return result[0]

    @staticmethod
    def cosine_similarity(
        query_vec: "np.ndarray", corpus_vecs: "np.ndarray",
    ) -> "np.ndarray":
        """Compute cosine similarities between query and corpus.

        Both inputs should be L2-normalized. If they are, dot product = cosine.

        Args:
            query_vec: shape (dim,)
            corpus_vecs: shape (N, dim)

        Returns:
            numpy array of shape (N,) with similarity scores.
        """
        import numpy as np

        if corpus_vecs.shape[0] == 0:
            return np.array([], dtype=np.float32)

        # Dot product on normalized vectors = cosine similarity
        return corpus_vecs @ query_vec


def get_embedder(model_name: str = DEFAULT_EMBEDDING_MODEL) -> LocalEmbedder:
    """Get or create the singleton embedder instance."""
    global _EMBEDDER_INSTANCE
    if _EMBEDDER_INSTANCE is None or _EMBEDDER_INSTANCE._model_name != model_name:
        _EMBEDDER_INSTANCE = LocalEmbedder(model_name)
    return _EMBEDDER_INSTANCE


def embeddings_to_bytes(embeddings: "np.ndarray") -> list[bytes]:
    """Convert numpy embeddings to a list of bytes for SQLite storage.

    Each embedding is stored as raw float32 bytes.
    """
    return [row.tobytes() for row in embeddings]


def bytes_to_embedding(data: bytes, dim: int) -> "np.ndarray":
    """Convert bytes back to a numpy embedding vector."""
    import numpy as np
    return np.frombuffer(data, dtype=np.float32).copy()
