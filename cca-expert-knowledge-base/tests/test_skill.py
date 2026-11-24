import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "skill" / "cca_kb.py"


@pytest.fixture(scope="session")
def skill():
    spec = importlib.util.spec_from_file_location("cca_skill", SKILL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ids(result: dict) -> list[str]:
    return [item["experience_id"] for item in result["experiences"]]


def test_bundle_files_exist():
    assert (ROOT / "skill/knowledge.json").is_file()
    assert (ROOT / "skill/SKILL.md").is_file()
    assert (ROOT / "context_pack/catalog.md").is_file()
    assert (ROOT / "context_pack/design_experience_pack.md").is_file()


def test_bundle_matches_yaml_source(skill, repository):
    bundle = json.loads(
        (ROOT / "skill/knowledge.json").read_text(encoding="utf-8")
    )
    bundled = {item["id"]: item for item in bundle["experiences"]}
    assert set(bundled) == set(repository.experiences)
    for experience_id, exp in repository.experiences.items():
        assert bundled[experience_id]["title"] == exp.title
        assert (
            bundled[experience_id]["knowledge_status"]
            == exp.knowledge_status
        )
    assert {item["source_id"] for item in bundle["sources"]} == set(
        repository.sources
    )


def test_skill_search_bbr(skill):
    result = skill.search_experiences(
        "BBR bandwidth estimation and pacing", top_k=3
    )
    assert "exp-001" in _ids(result)
    assert result["retrieval"]["semantic"] is False


def test_skill_search_chinese(skill):
    result = skill.search_experiences(
        "随机丢包 拥塞控制 过度降速", top_k=5
    )
    assert set(_ids(result)) & {"exp-007", "exp-001"}


def test_skill_search_returns_traceable_evidence(skill):
    result = skill.search_experiences("DCTCP ECN multi-bit feedback", top_k=3)
    assert "exp-003" in _ids(result)
    for exp in result["experiences"]:
        assert exp["knowledge_status"] in {
            "source_reported",
            "cross_source_synthesis",
            "inference",
        }
        assert exp["evidence"]
        for evidence in exp["evidence"]:
            assert evidence["source_id"].startswith("src-")
            assert evidence["locator"]
            assert evidence["evidence_level"] in {"A", "B", "C", "D"}


def test_skill_filter_by_source(skill):
    result = skill.search_experiences(
        "congestion window", filters={"source_id": "src-010"}, top_k=10
    )
    assert _ids(result) == ["exp-005"]


def test_skill_filter_by_status_and_tag(skill):
    result = skill.search_experiences(
        "ECN",
        filters={
            "knowledge_status": "cross_source_synthesis",
            "tags": ["dctcp"],
        },
        top_k=10,
    )
    assert _ids(result) == ["exp-003"]


def test_skill_get_and_source(skill):
    payload = skill.get_experience("exp-001")
    assert payload["experience_id"] == "exp-001"
    assert payload["sources"]
    assert payload["related_experiences"]

    source = skill.get_source("src-004")
    used_by = {item["experience_id"] for item in source["used_by"]}
    assert {"exp-001", "exp-007", "exp-008"} <= used_by


def test_skill_related(skill):
    payload = skill.get_related_experiences("exp-001")
    relations = {
        item["experience_id"]: item["relation"]
        for item in payload["related_experiences"]
    }
    assert relations["exp-005"] == "alternative_to"


def test_skill_unknown_ids(skill):
    with pytest.raises(KeyError):
        skill.get_experience("exp-999")
    with pytest.raises(KeyError):
        skill.get_source("src-999")


def test_skill_rejects_empty_query(skill):
    with pytest.raises(ValueError, match="query must not be empty"):
        skill.search_experiences("   ")


def test_skill_rejects_unknown_filter(skill):
    with pytest.raises(ValueError, match="unknown filter"):
        skill.search_experiences("pacing", filters={"bogus": True})


def test_skill_catalog_lists_every_experience(skill):
    payload = skill.catalog()
    assert payload["count"] == 8
    assert {item["experience_id"] for item in payload["experiences"]} == {
        f"exp-{index:03d}" for index in range(1, 9)
    }


def test_skill_runs_in_isolated_mode():
    completed = subprocess.run(
        [sys.executable, "-I", str(SKILL_PATH), "stats"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["experiences"] == 8
    assert payload["sources"] == 10


def test_skill_imports_only_stdlib():
    tree = ast.parse(SKILL_PATH.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    forbidden = {"numpy", "pydantic", "yaml", "jsonschema", "sentence_transformers"}
    assert not modules & forbidden


def test_context_pack_covers_all_experiences():
    pack = (ROOT / "context_pack/design_experience_pack.md").read_text(
        encoding="utf-8"
    )
    catalog = (ROOT / "context_pack/catalog.md").read_text(encoding="utf-8")
    for index in range(1, 9):
        experience_id = f"exp-{index:03d}"
        assert experience_id in pack
        assert experience_id in catalog
