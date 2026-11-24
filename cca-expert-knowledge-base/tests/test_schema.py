from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from cca_kb.models import DesignExperience, Source
from cca_kb.repository import KnowledgeRepository
from cca_kb.validation import load_validators

ROOT = Path(__file__).resolve().parents[1]


def _raw_experience() -> dict:
    with (ROOT / "knowledge/experiences/exp-001.yaml").open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_repository_loads_with_expected_records(repository):
    assert len(repository.experiences) == 8
    assert len(repository.sources) == 10
    assert len(repository.relations) >= 10
    assert repository.taxonomy.tags


def test_all_ids_follow_convention(repository):
    for experience_id in repository.experiences:
        assert experience_id.startswith("exp-")
        assert len(experience_id) == 7
    for source_id in repository.sources:
        assert source_id.startswith("src-")
        assert len(source_id) == 7


def test_every_experience_has_locatable_evidence(repository):
    for exp in repository.experiences.values():
        assert exp.evidence
        assert any(evidence.locator.strip() for evidence in exp.evidence), exp.id


def test_every_evidence_reference_resolves(repository):
    for exp in repository.experiences.values():
        for evidence in exp.evidence:
            assert evidence.source_id in repository.sources


def test_json_schema_rejects_unknown_knowledge_status():
    experience_validator, _ = load_validators(ROOT / "schemas")
    data = _raw_experience()
    data["knowledge_status"] = "fact"
    assert list(experience_validator.iter_errors(data))


def test_json_schema_rejects_unknown_field():
    experience_validator, _ = load_validators(ROOT / "schemas")
    data = _raw_experience()
    data["made_up_field"] = "not allowed"
    assert list(experience_validator.iter_errors(data))


def test_json_schema_rejects_missing_evidence():
    experience_validator, _ = load_validators(ROOT / "schemas")
    data = _raw_experience()
    data["evidence"] = []
    assert list(experience_validator.iter_errors(data))


def test_pydantic_rejects_extra_fields():
    data = _raw_experience()
    data["made_up_field"] = "not allowed"
    with pytest.raises(ValidationError):
        DesignExperience.model_validate(data)


def test_source_model_round_trip(repository):
    source = repository.sources["src-004"]
    assert isinstance(source, Source)
    assert source.source_type == "paper"
    assert source.doi


def test_shipped_example_is_valid():
    experience_validator, _ = load_validators(ROOT / "schemas")
    with (ROOT / "examples/example_experience.yaml").open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert not list(experience_validator.iter_errors(data))
    DesignExperience.model_validate(data)


def test_schema_files_are_valid_json_schema():
    # load_validators performs Draft202012Validator.check_schema.
    experience_validator, source_validator = load_validators(ROOT / "schemas")
    assert experience_validator is not None
    assert source_validator is not None


def test_repository_fails_closed_on_bad_schema(tmp_path):
    import shutil

    shutil.copytree(ROOT / "knowledge", tmp_path / "knowledge")
    shutil.copytree(ROOT / "schemas", tmp_path / "schemas")
    exp_path = tmp_path / "knowledge/experiences/exp-001.yaml"
    data = yaml.safe_load(exp_path.read_text(encoding="utf-8"))
    data["knowledge_status"] = "fact"
    exp_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(Exception, match="schema validation failed|knowledge_status"):
        KnowledgeRepository.load(tmp_path)
