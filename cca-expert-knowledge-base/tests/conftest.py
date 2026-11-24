from pathlib import Path

import pytest

from cca_kb.config import Settings
from cca_kb.embeddings import HashingEmbedder
from cca_kb.repository import KnowledgeRepository
from cca_kb.retrieval import RetrievalService

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def repository() -> KnowledgeRepository:
    return KnowledgeRepository.load(ROOT)


@pytest.fixture(scope="session")
def service(repository: KnowledgeRepository) -> RetrievalService:
    settings = Settings(
        root=ROOT,
        embedder="hashing",
        keyword_weight=0.55,
        semantic_weight=0.45,
    )
    return RetrievalService(
        repository, settings=settings, embedder=HashingEmbedder()
    )
