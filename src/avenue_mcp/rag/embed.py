"""Local embeddings.

No API key, no network, no coursework leaving the machine. Anthropic has no
embeddings endpoint, so an API key would not help here regardless.

Model identity is recorded with every index. Vectors from different models are
not comparable, and mixing them silently produces garbage rankings -- so a
model change is reported as an error rather than searched across.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

_MODEL_LOCK = threading.Lock()
_MODEL: Any = None
_MODEL_NAME: str | None = None


class Embedder:
    """Lazy wrapper over fastembed.

    Import and model download are deferred until first use, so server startup
    never blocks on a ~130 MB download.
    """

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._dim: int | None = None

    def _model(self) -> Any:
        global _MODEL, _MODEL_NAME
        with _MODEL_LOCK:
            if _MODEL is not None and _MODEL_NAME == self.model_name:
                return _MODEL
            try:
                from fastembed import TextEmbedding  # type: ignore
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "fastembed is not installed. Run: pip install fastembed"
                ) from exc
            log.info("loading embedding model %s (first run downloads weights)", self.model_name)
            _MODEL = TextEmbedding(model_name=self.model_name)
            _MODEL_NAME = self.model_name
            return _MODEL

    @property
    def dim(self) -> int:
        if self._dim is None:
            probe = self.embed_documents(["dimension probe"])
            self._dim = int(probe.shape[1]) if probe.size else 0
        return self._dim

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        vecs = list(self._model().embed(texts))
        mat = np.vstack([np.asarray(v, dtype=np.float32) for v in vecs])
        return _l2_normalize(mat)

    def embed_query(self, text: str) -> np.ndarray:
        """BGE-family models want a query prefix; fastembed's query_embed
        applies it. Fall back to plain embedding if unavailable."""
        model = self._model()
        try:
            vecs = list(model.query_embed(text))
        except (AttributeError, NotImplementedError):
            vecs = list(model.embed([text]))
        vec = np.asarray(next(iter(vecs)), dtype=np.float32).reshape(1, -1)
        return _l2_normalize(vec)[0]


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    """Normalize so cosine similarity is a plain dot product."""
    if mat.size == 0:
        return mat
    norms = np.linalg.norm(mat, axis=-1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype(np.float32)


def cosine_scores(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Both sides are already L2-normalized, so this is a dot product.

    Brute force is genuinely sufficient at this scale -- a few thousand 384-dim
    vectors scan in well under a millisecond, with no extra dependency and
    nothing to corrupt.
    """
    if matrix.size == 0 or query.size == 0:
        return np.zeros((0,), dtype=np.float32)
    if matrix.shape[1] != query.shape[0]:
        return np.zeros((matrix.shape[0],), dtype=np.float32)
    return matrix @ query.astype(np.float32)
