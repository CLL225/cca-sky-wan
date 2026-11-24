"""In-memory hybrid index over the YAML repository.

The index combines BM25 keyword scores with vector cosine similarity and a
title-match bonus.  Metadata filters are applied before ranking.  At MVP
scale this is fast and dependency-light; the class boundary is the place to
swap in SQLite FTS5 / Qdrant later.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from cca_kb.bm25 import BM25Index
from cca_kb.embeddings import Embedder
from cca_kb.models import EVIDENCE_LEVEL_RANK, DesignExperience
from cca_kb.repository import KnowledgeRepository
from cca_kb.text import (
    experience_embedding_text,
    experience_search_text,
    title_token_overlap,
    tokenize,
)

ALLOWED_FILTERS = {
    "experience_type",
    "knowledge_status",
    "tags",
    "all_tags",
    "source_id",
    "source_type",
    "min_evidence_level",
    "network_condition",
    "suitable_condition",
    "has_failure_modes",
}


@dataclass
class SearchHit:
    experience: DesignExperience
    score: float
    keyword_score: float
    semantic_score: float
    semantic_cosine: float
    title_boost: float
    matched_terms: list[str]


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]


def _token_overlap_ratio(query: str, text: str) -> float:
    query_tokens = set(tokenize(query))
    if not query_tokens:
        return 0.0
    return len(query_tokens & set(tokenize(text))) / len(query_tokens)


def experience_matches(exp: DesignExperience, filters: dict) -> bool:
    if not filters:
        return True

    if exp.experience_type not in _as_list(filters.get("experience_type")):
        if "experience_type" in filters:
            return False

    if "knowledge_status" in filters:
        if exp.knowledge_status not in _as_list(filters["knowledge_status"]):
            return False

    if "tags" in filters:
        required = set(_as_list(filters["tags"]))
        if required and not (required & set(exp.metadata.tags)):
            return False

    if "all_tags" in filters:
        required = set(_as_list(filters["all_tags"]))
        if required and not required.issubset(set(exp.metadata.tags)):
            return False

    if "source_id" in filters:
        wanted = set(_as_list(filters["source_id"]))
        if wanted and not wanted & {item.source_id for item in exp.evidence}:
            return False

    if "source_type" in filters:
        wanted = set(_as_list(filters["source_type"]))
        if wanted and not wanted & {item.source_type for item in exp.evidence}:
            return False

    if "min_evidence_level" in filters:
        level = str(filters["min_evidence_level"]).upper()
        if level not in EVIDENCE_LEVEL_RANK:
            raise ValueError(
                f"min_evidence_level must be one of A/B/C/D, got '{level}'"
            )
        best = min(EVIDENCE_LEVEL_RANK[item.evidence_level] for item in exp.evidence)
        if best > EVIDENCE_LEVEL_RANK[level]:
            return False

    if "network_condition" in filters:
        condition = str(filters["network_condition"])
        haystack = " ".join(
            exp.problem_context.network_conditions
            + exp.problem_context.observed_symptoms
            + [exp.problem_context.underlying_cause]
        )
        if _token_overlap_ratio(condition, haystack) < 0.5:
            return False

    if "suitable_condition" in filters:
        condition = str(filters["suitable_condition"])
        haystack = " ".join(
            exp.applicability.suitable_conditions + exp.applicability.assumptions
        )
        if _token_overlap_ratio(condition, haystack) < 0.5:
            return False

    if "has_failure_modes" in filters:
        if bool(exp.failure_modes) != bool(filters["has_failure_modes"]):
            return False

    return True


class KnowledgeIndex:
    def __init__(
        self,
        repository: KnowledgeRepository,
        embedder: Embedder,
        *,
        keyword_weight: float = 0.55,
        semantic_weight: float = 0.45,
    ):
        if keyword_weight < 0 or semantic_weight < 0:
            raise ValueError("ranking weights must be non-negative")
        if keyword_weight + semantic_weight == 0:
            raise ValueError("at least one ranking weight must be positive")

        self.repository = repository
        self.embedder = embedder
        self.keyword_weight = keyword_weight
        self.semantic_weight = semantic_weight

        self.experiences = [
            repository.experiences[key] for key in sorted(repository.experiences)
        ]
        self.search_texts = [experience_search_text(exp) for exp in self.experiences]
        self.documents = [tokenize(text) for text in self.search_texts]
        self.bm25 = BM25Index(self.documents)
        self.doc_matrix = embedder.embed_documents(
            [experience_embedding_text(exp) for exp in self.experiences]
        )

    def search(
        self,
        query: str,
        filters: dict | None = None,
        top_k: int = 10,
    ) -> list[SearchHit]:
        if not query or not query.strip():
            raise ValueError("query must not be empty")
        if top_k <= 0:
            raise ValueError("top_k must be positive")

        filters = dict(filters or {})
        unknown = set(filters) - ALLOWED_FILTERS
        if unknown:
            raise ValueError(
                f"unknown filter(s): {', '.join(sorted(unknown))}; "
                f"allowed: {', '.join(sorted(ALLOWED_FILTERS))}"
            )

        query_tokens = tokenize(query)
        keyword_scores = self.bm25.normalized_scores(query_tokens)

        semantic_raw = np.zeros(len(self.experiences), dtype=np.float64)
        if self.doc_matrix.size:
            cosine = self.doc_matrix @ self.embedder.embed_query(query)
            semantic_raw = np.clip(cosine.astype(np.float64), 0.0, 1.0)
        semantic_max = float(semantic_raw.max()) if semantic_raw.size else 0.0
        semantic_scores = (
            semantic_raw / semantic_max if semantic_max > 0 else semantic_raw
        )

        title_scores = np.array(
            [title_token_overlap(query_tokens, exp.title) for exp in self.experiences],
            dtype=np.float64,
        )

        fused = (
            self.keyword_weight * keyword_scores
            + self.semantic_weight * semantic_scores
            + 0.05 * title_scores
        )

        hits: list[SearchHit] = []
        for index, exp in enumerate(self.experiences):
            if not experience_matches(exp, filters):
                continue
            if fused[index] <= 0:
                continue
            token_set = set(self.documents[index])
            matched = sorted(set(query_tokens) & token_set)
            hits.append(
                SearchHit(
                    experience=exp,
                    score=round(float(fused[index]), 6),
                    keyword_score=round(float(keyword_scores[index]), 6),
                    semantic_score=round(float(semantic_scores[index]), 6),
                    semantic_cosine=round(float(semantic_raw[index]), 6),
                    title_boost=round(float(title_scores[index]), 6),
                    matched_terms=matched[:15],
                )
            )

        hits.sort(key=lambda hit: (-hit.score, hit.experience.id))
        return hits[:top_k]
