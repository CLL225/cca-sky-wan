"""Stable, framework-agnostic access functions.

These are the functions an LLM agent, script or external service should
import.  They intentionally mirror the interface suggested by the project
specification and do not expose internal index classes.

    search_experiences(query, filters=None, top_k=10)
    get_experience(experience_id)
    get_source(source_id)
    get_related_experiences(experience_id)

Optional keyword-only ``root`` and ``embedder`` arguments allow tests and
multi-tenant callers to use independent repositories.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cca_kb.config import Settings
from cca_kb.embeddings import build_embedder
from cca_kb.repository import KnowledgeRepository
from cca_kb.retrieval import RetrievalService

_CACHE: dict[tuple[str, str, str], RetrievalService] = {}


class KnowledgeBase:
    """Explicit handle for callers that do not want process-wide caching."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        embedder: str | None = None,
    ):
        self.settings = Settings.from_env(root)
        self.repository = KnowledgeRepository.load(self.settings.root)
        self.service = RetrievalService(
            self.repository,
            settings=(
                Settings(
                    root=self.settings.root,
                    embedder=embedder or self.settings.embedder,
                    model_name=self.settings.model_name,
                    keyword_weight=self.settings.keyword_weight,
                    semantic_weight=self.settings.semantic_weight,
                )
                if embedder
                else self.settings
            ),
        )

    def search_experiences(
        self, query: str, filters: dict | None = None, top_k: int = 10
    ) -> dict[str, Any]:
        return self.service.search_experiences(query, filters, top_k)

    def get_experience(self, experience_id: str) -> dict[str, Any]:
        return self.service.get_experience(experience_id)

    def get_source(self, source_id: str) -> dict[str, Any]:
        return self.service.get_source(source_id)

    def get_related_experiences(self, experience_id: str) -> dict[str, Any]:
        return self.service.get_related_experiences(experience_id)


def _service(
    root: str | Path | None = None,
    embedder: str | None = None,
) -> RetrievalService:
    settings = Settings.from_env(root)
    key = (
        str(settings.root),
        embedder or settings.embedder,
        settings.model_name,
    )
    service = _CACHE.get(key)
    if service is not None:
        return service

    repository = KnowledgeRepository.load(settings.root)
    effective_settings = (
        Settings(
            root=settings.root,
            embedder=embedder,
            model_name=settings.model_name,
            keyword_weight=settings.keyword_weight,
            semantic_weight=settings.semantic_weight,
        )
        if embedder
        else settings
    )
    service = RetrievalService(repository, settings=effective_settings)
    _CACHE[key] = service
    return service


def reset_cache() -> None:
    """Drop cached repositories/indexes (useful after editing YAML)."""

    _CACHE.clear()


def search_experiences(
    query: str,
    filters: dict | None = None,
    top_k: int = 10,
    *,
    root: str | Path | None = None,
    embedder: str | None = None,
) -> dict[str, Any]:
    return _service(root, embedder).search_experiences(query, filters, top_k)


def get_experience(
    experience_id: str,
    *,
    root: str | Path | None = None,
    embedder: str | None = None,
) -> dict[str, Any]:
    return _service(root, embedder).get_experience(experience_id)


def get_source(
    source_id: str,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    return _service(root, None).get_source(source_id)


def get_related_experiences(
    experience_id: str,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    return _service(root, None).get_related_experiences(experience_id)
