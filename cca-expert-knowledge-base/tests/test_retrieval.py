import shutil
from pathlib import Path

import pytest
import yaml

from cca_kb.config import Settings
from cca_kb.embeddings import HashingEmbedder
from cca_kb.repository import KnowledgeRepository
from cca_kb.retrieval import RetrievalService

ROOT = Path(__file__).resolve().parents[1]


def _ids(result: dict) -> list[str]:
    return [item["experience_id"] for item in result["experiences"]]


def test_bbr_query_hits_bbr_experience(service):
    result = service.search_experiences("BBR bandwidth estimation and pacing", top_k=5)
    assert "exp-001" in _ids(result)


def test_dctcp_query_hits_ecn_experience(service):
    result = service.search_experiences(
        "DCTCP derives multi-bit feedback from ECN marks", top_k=5
    )
    assert "exp-003" in _ids(result)


def test_cubic_query_hits_cubic_experience(service):
    result = service.search_experiences("CUBIC cubic window growth", top_k=5)
    assert "exp-002" in _ids(result)


def test_copa_query_hits_delay_based_experience(service):
    result = service.search_experiences(
        "Copa queueing delay target rate", top_k=5
    )
    assert "exp-004" in _ids(result)


def test_chinese_query_hits_random_loss_analysis(service):
    result = service.search_experiences("随机丢包 拥塞控制 过度降速", top_k=5)
    assert set(_ids(result)) & {"exp-001", "exp-007"}


def test_result_keeps_status_and_evidence(service):
    result = service.search_experiences("random loss", top_k=3)
    assert result["experiences"]
    for exp in result["experiences"]:
        assert exp["knowledge_status"] in {
            "source_reported",
            "cross_source_synthesis",
            "inference",
        }
        assert exp["evidence"]
        for evidence in exp["evidence"]:
            assert evidence["source_id"].startswith("src-")
            assert "locator" in evidence
            assert evidence["evidence_level"] in {"A", "B", "C", "D"}
            assert evidence["source"]["title"]


def test_hashing_backend_is_declared_as_non_semantic(service):
    result = service.search_experiences("pacing", top_k=3)
    assert result["retrieval"]["semantic"] is False
    assert any("hashing" in limitation for limitation in result["limitations"])


def test_filter_by_experience_type(service):
    result = service.search_experiences(
        "congestion window", filters={"experience_type": "mechanism_design"}, top_k=10
    )
    assert result["experiences"]
    assert all(
        exp["experience_type"] == "mechanism_design" for exp in result["experiences"]
    )


def test_filter_by_source_id(service):
    result = service.search_experiences(
        "congestion window", filters={"source_id": "src-010"}, top_k=10
    )
    assert _ids(result) == ["exp-005"]


def test_filter_by_tag_is_any_match(service):
    result = service.search_experiences(
        "delay", filters={"tags": ["copa", "dctcp"]}, top_k=10
    )
    assert set(_ids(result)) <= {"exp-003", "exp-004"}
    assert "exp-004" in _ids(result)


def test_filter_min_evidence_level(service):
    result = service.search_experiences(
        "congestion", filters={"min_evidence_level": "A"}, top_k=10
    )
    assert result["experiences"]


def test_unknown_filter_is_rejected(service):
    with pytest.raises(ValueError, match="unknown filter"):
        service.search_experiences("pacing", filters={"bogus": True})


def test_top_k_is_respected(service):
    result = service.search_experiences("congestion control", top_k=2)
    assert len(result["experiences"]) <= 2


def test_empty_query_is_rejected(service):
    with pytest.raises(ValueError, match="query must not be empty"):
        service.search_experiences("   ")


def test_get_experience_includes_sources(service):
    payload = service.get_experience("exp-001")
    assert payload["experience_id"] == "exp-001"
    assert payload["evidence"]
    assert payload["sources"]
    assert "locators" in next(iter(payload["sources"].values()))


def test_get_unknown_experience_raises(service):
    with pytest.raises(KeyError):
        service.get_experience("exp-999")


def test_get_source_lists_usage(service):
    payload = service.get_source("src-004")
    assert payload["source_id"] == "src-004"
    usage_ids = {item["experience_id"] for item in payload["related_from_experiences"]}
    assert {"exp-001", "exp-007", "exp-008"} <= usage_ids


def test_get_related_experiences(service):
    payload = service.get_related_experiences("exp-001")
    related = {
        item["experience_id"]: item["relation"]
        for item in payload["related_experiences"]
    }
    assert related["exp-005"] == "alternative_to"


def test_inference_result_is_flagged(tmp_path):
    shutil.copytree(ROOT / "knowledge", tmp_path / "knowledge")
    shutil.copytree(ROOT / "schemas", tmp_path / "schemas")
    exp_path = tmp_path / "knowledge/experiences/exp-001.yaml"
    data = yaml.safe_load(exp_path.read_text(encoding="utf-8"))
    data["knowledge_status"] = "inference"
    data["design_rationale"]["reasoning_chain"] = [
        "Test fixture: explicit reasoning for an inference record."
    ]
    for evidence in data["evidence"]:
        evidence["evidence_level"] = "D"
    exp_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    repository = KnowledgeRepository.load(tmp_path)
    settings = Settings(root=tmp_path, embedder="hashing")
    service = RetrievalService(
        repository, settings=settings, embedder=HashingEmbedder()
    )
    result = service.search_experiences("BBR bandwidth", top_k=3)
    assert result["experiences"]
    assert any(
        "inference" in limitation for limitation in result["limitations"]
    )
    assert any("C/D" in limitation for limitation in result["limitations"])
