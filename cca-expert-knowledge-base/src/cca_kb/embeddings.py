"""Pluggable embedding backends.

Two backends are provided:

* ``sentence_transformers`` - a real multilingual dense encoder.  The Docker
  image bakes in a small model so retrieval works without network access.
* ``hashing`` - a deterministic, dependency-free lexical vectorizer used as a
  fallback and in fast tests.  It is *not* a semantic model; callers that care
  must surface that limitation in results (the retrieval layer does).

The interface is intentionally tiny so a future adapter (Qdrant, Chroma,
OpenAI embeddings, ...) can replace either backend without touching the
knowledge records.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Protocol, Sequence

import numpy as np

from cca_kb.text import LATIN_RE, tokenize

logger = logging.getLogger(__name__)

_MODEL_CACHE: dict[str, "SentenceTransformerEmbedder"] = {}


class Embedder(Protocol):
    name: str
    is_semantic: bool
    dimension: int

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


def _normalize_rows(matrix: np.ndarray) -> np.ndarray:
    if matrix.size == 0:
        return matrix
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


class HashingEmbedder:
    """Signed feature hashing over word, bigram and character n-gram features."""

    is_semantic = False

    def __init__(self, dimension: int = 2048):
        self.dimension = dimension
        self.name = f"hashing-ngram-{dimension}"

    @staticmethod
    def _digest(feature: str) -> int:
        return int.from_bytes(
            hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest(),
            "little",
        )

    @staticmethod
    def _features(text: str) -> list[str]:
        lower = text.lower()
        tokens = tokenize(text)
        features = list(tokens)

        latin_tokens = [token for token in tokens if token.isascii() and token.isalpha()]
        features.extend(
            f"{left}_{right}"
            for left, right in zip(latin_tokens, latin_tokens[1:])
        )

        for word in LATIN_RE.findall(lower):
            if len(word) >= 3:
                features.extend(
                    word[i : i + 3] for i in range(len(word) - 2)
                )
        return features

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        matrix = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for row, text in enumerate(texts):
            for feature in self._features(text):
                digest = self._digest(feature)
                column = digest % self.dimension
                sign = 1.0 if (digest >> 63) & 1 else -1.0
                matrix[row, column] += sign
        return _normalize_rows(matrix)

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_documents([text])[0]


class SentenceTransformerEmbedder:
    """Dense multilingual embeddings via sentence-transformers."""

    is_semantic = True

    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer

        self.name = model_name
        self._model = SentenceTransformer(model_name)
        self.dimension = int(self._model.get_sentence_embedding_dimension())
        # E5-family models require asymmetric query/passage prefixes.
        self._needs_prefix = "e5" in model_name.lower()

    def _encode(self, texts: Sequence[str], prefix: str) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dimension), dtype=np.float32)
        prepared = (
            [f"{prefix}: {text}" for text in texts]
            if self._needs_prefix
            else list(texts)
        )
        vectors = self._model.encode(
            prepared,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return np.asarray(vectors, dtype=np.float32)

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        return self._encode(texts, "passage")

    def embed_query(self, text: str) -> np.ndarray:
        return self._encode([text], "query")[0]


def build_embedder(kind: str, model_name: str) -> Embedder:
    """Build an embedder by name.

    ``auto`` tries the dense model and falls back to hashing with a warning.
    ``sentence_transformers`` raises instead of silently degrading.
    """

    normalized = (kind or "auto").lower()
    if normalized in {"hashing", "lexical", "none"}:
        return HashingEmbedder()

    if normalized in {"auto", "sentence_transformers", "sentence-transformers", "st"}:
        cached = _MODEL_CACHE.get(model_name)
        if cached is not None:
            return cached
        try:
            embedder = SentenceTransformerEmbedder(model_name)
            _MODEL_CACHE[model_name] = embedder
            return embedder
        except Exception as exc:  # noqa: BLE001 - optional dependency/model
            if normalized == "auto":
                logger.warning(
                    "dense embedder unavailable (%s); falling back to hashing", exc
                )
                return HashingEmbedder()
            raise

    raise ValueError(
        f"unknown embedder '{kind}' (expected auto, sentence_transformers or hashing)"
    )
