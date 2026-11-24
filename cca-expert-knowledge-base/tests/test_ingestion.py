import shutil
from pathlib import Path

import pytest
import yaml

from cca_kb.repository import KnowledgeError, KnowledgeRepository

ROOT = Path(__file__).resolve().parents[1]


def _copy_project(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "knowledge", tmp_path / "knowledge")
    shutil.copytree(ROOT / "schemas", tmp_path / "schemas")
    return tmp_path


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def test_dangling_source_reference_is_rejected(tmp_path):
    root = _copy_project(tmp_path)
    path = root / "knowledge/experiences/exp-001.yaml"
    data = _load_yaml(path)
    data["evidence"][0]["source_id"] = "src-999"
    _write_yaml(path, data)

    with pytest.raises(KnowledgeError, match="unknown source id|SRC-MISSING"):
        KnowledgeRepository.load(root)


def test_duplicate_experience_id_is_rejected(tmp_path):
    root = _copy_project(tmp_path)
    source = root / "knowledge/experiences/exp-001.yaml"
    copy = root / "knowledge/experiences/exp-001-copy.yaml"
    copy.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(KnowledgeError, match="duplicate experience id"):
        KnowledgeRepository.load(root)


def test_dangling_related_experience_is_rejected(tmp_path):
    root = _copy_project(tmp_path)
    path = root / "knowledge/experiences/exp-001.yaml"
    data = _load_yaml(path)
    data["related_experiences"] = ["exp-999"]
    _write_yaml(path, data)

    with pytest.raises(KnowledgeError, match="unknown experience id|REL-MISSING"):
        KnowledgeRepository.load(root)


def test_invalid_yaml_is_rejected(tmp_path):
    root = _copy_project(tmp_path)
    path = root / "knowledge/experiences/exp-001.yaml"
    path.write_text("id: [unclosed\n", encoding="utf-8")

    with pytest.raises(KnowledgeError, match="invalid YAML"):
        KnowledgeRepository.load(root)


def test_self_relation_is_rejected(tmp_path):
    root = _copy_project(tmp_path)
    path = root / "knowledge/experiences/exp-001.yaml"
    data = _load_yaml(path)
    data["related_experiences"] = ["exp-001"]
    _write_yaml(path, data)

    with pytest.raises(KnowledgeError, match="must not reference the same experience"):
        KnowledgeRepository.load(root)


def test_partial_load_never_succeeds(tmp_path):
    root = _copy_project(tmp_path)
    path = root / "knowledge/experiences/exp-001.yaml"
    data = _load_yaml(path)
    data["evidence"] = []
    _write_yaml(path, data)

    with pytest.raises(KnowledgeError):
        KnowledgeRepository.load(root)
