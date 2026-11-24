"""Small, dependency-free BM25 index.

The knowledge base is intentionally small at MVP scale, so a pure-Python
BM25 is preferable to pulling in a search server.  The public shape mirrors
what a future SQLite FTS5 or Elasticsearch adapter would expose.
"""

from __future__ import annotations

import math
from collections import Counter

import numpy as np


class BM25Index:
    def __init__(self, documents: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.documents = documents
        self.k1 = k1
        self.b = b
        self.n_docs = len(documents)
        self.doc_freqs = [Counter(doc) for doc in documents]
        self.doc_lengths = np.array([len(doc) for doc in documents], dtype=np.float64)
        self.avgdl = float(self.doc_lengths.mean()) if self.n_docs else 0.0
        self._df: Counter[str] = Counter()
        for doc in documents:
            self._df.update(set(doc))
        self._idf = {
            term: math.log(1.0 + (self.n_docs - df + 0.5) / (df + 0.5))
            for term, df in self._df.items()
        }

    def scores(self, query_tokens: list[str]) -> np.ndarray:
        scores = np.zeros(self.n_docs, dtype=np.float64)
        if self.n_docs == 0 or self.avgdl <= 0:
            return scores

        unique_query = list(dict.fromkeys(query_tokens))
        for doc_idx, freqs in enumerate(self.doc_freqs):
            dl = self.doc_lengths[doc_idx]
            total = 0.0
            for term in unique_query:
                tf = freqs.get(term, 0)
                if tf == 0:
                    continue
                idf = self._idf.get(term, 0.0)
                denom = tf + self.k1 * (1.0 - self.b + self.b * dl / self.avgdl)
                total += idf * (tf * (self.k1 + 1.0)) / denom
            scores[doc_idx] = total
        return scores

    def normalized_scores(self, query_tokens: list[str]) -> np.ndarray:
        scores = self.scores(query_tokens)
        maximum = float(scores.max()) if scores.size else 0.0
        if maximum <= 0:
            return np.zeros_like(scores)
        return scores / maximum
