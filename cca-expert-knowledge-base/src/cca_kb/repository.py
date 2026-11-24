"""YAML-backed knowledge repository.

YAML files are the single source of truth.  Loading performs schema
validation, typed parsing and referential-integrity checks before any
record is exposed to retrieval.  A repository either loads completely or
raises with an aggregated error report; partial knowledge is never served.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from cca_kb.models import DesignExperience, Relation, Source
from cca_kb.validation import (
    ValidationReport,
    check_integrity,
    load_validators,
    validate_instances,
)


class KnowledgeError(RuntimeError):
    """Raised when the knowledge base cannot be loaded safely."""


@dataclass(frozen=True)
class Taxonomy:
    categories: list[dict[str, Any]] = field(default_factory=list)
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def tags(self) -> set[str]:
        return set(self.entries)


def _read_yaml(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except OSError as exc:
        raise KnowledgeError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise KnowledgeError(f"invalid YAML in {path}: {exc}") from exc


def _yaml_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.suffix in {".yaml", ".yml"} and not path.name.startswith(".")
    )


def _load_taxonomy(path: Path) -> Taxonomy:
    if not path.is_file():
        return Taxonomy()
    raw = _read_yaml(path) or {}
    categories = raw.get("categories", [])
    entries: dict[str, dict[str, Any]] = {}
    for category in categories:
        category_id = category.get("id", "")
        for tag in category.get("tags", []):
            tag_id = tag.get("id")
            if not tag_id:
                continue
            entries[tag_id] = {
                "id": tag_id,
                "label": tag.get("label", tag_id),
                "description": tag.get("description", ""),
                "category": category_id,
                "category_name": category.get("name", category_id),
            }
    return Taxonomy(categories=categories, entries=entries)


def _load_relations(path: Path) -> list[Relation]:
    if not path.is_file():
        return []
    raw = _read_yaml(path) or {}
    items = raw.get("relations", []) if isinstance(raw, dict) else raw
    relations: list[Relation] = []
    for index, item in enumerate(items or []):
        try:
            relations.append(Relation.model_validate(item))
        except ValidationError as exc:
            raise KnowledgeError(f"{path} relations[{index}]: {exc}") from exc
    return relations


@dataclass
class KnowledgeRepository:
    root: Path
    experiences: dict[str, DesignExperience]
    sources: dict[str, Source]
    relations: list[Relation]
    taxonomy: Taxonomy = field(default_factory=Taxonomy)
    report: ValidationReport = field(default_factory=ValidationReport)

    @classmethod
    def load(
        cls,
        root: str | Path,
        *,
        validate_schema: bool = True,
        taxonomy_path: Path | None = None,
        relations_path: Path | None = None,
    ) -> "KnowledgeRepository":
        root = Path(root).resolve()
        knowledge = root / "knowledge"
        experiences_dir = knowledge / "experiences"
        sources_dir = knowledge / "sources"

        if not experiences_dir.is_dir():
            raise KnowledgeError(f"missing experiences directory: {experiences_dir}")
        if not sources_dir.is_dir():
            raise KnowledgeError(f"missing sources directory: {sources_dir}")

        report = ValidationReport()
        raw_experiences: list[tuple[str, dict[str, Any]]] = []
        raw_sources: list[tuple[str, dict[str, Any]]] = []

        for path in _yaml_files(experiences_dir):
            data = _read_yaml(path)
            if not isinstance(data, dict):
                raise KnowledgeError(f"{path}: expected a YAML mapping")
            raw_experiences.append((str(path.relative_to(root)), data))

        for path in _yaml_files(sources_dir):
            data = _read_yaml(path)
            if not isinstance(data, dict):
                raise KnowledgeError(f"{path}: expected a YAML mapping")
            raw_sources.append((str(path.relative_to(root)), data))

        if validate_schema:
            experience_validator, source_validator = load_validators(
                root / "schemas"
            )
            validate_instances(raw_experiences, experience_validator, report)
            validate_instances(raw_sources, source_validator, report)
            if report.errors:
                raise KnowledgeError(
                    "schema validation failed:\n" + report.format()
                )

        experiences: dict[str, DesignExperience] = {}
        sources: dict[str, Source] = {}

        for location, data in raw_experiences:
            try:
                exp = DesignExperience.model_validate(data)
            except ValidationError as exc:
                report.error("MODEL", location, str(exc))
                continue
            if exp.id in experiences:
                report.error(
                    "ID-DUPLICATE", location, f"duplicate experience id '{exp.id}'"
                )
                continue
            experiences[exp.id] = exp

        for location, data in raw_sources:
            try:
                source = Source.model_validate(data)
            except ValidationError as exc:
                report.error("MODEL", location, str(exc))
                continue
            if source.source_id in sources:
                report.error(
                    "ID-DUPLICATE",
                    location,
                    f"duplicate source id '{source.source_id}'",
                )
                continue
            sources[source.source_id] = source

        taxonomy = _load_taxonomy(
            taxonomy_path or (knowledge / "taxonomy" / "taxonomy.yaml")
        )
        relations = _load_relations(
            relations_path or (knowledge / "relations" / "relations.yaml")
        )

        if report.errors:
            raise KnowledgeError("record parsing failed:\n" + report.format())

        check_integrity(
            experiences,
            sources,
            relations,
            taxonomy.tags or None,
            report,
        )
        if report.errors:
            raise KnowledgeError("integrity validation failed:\n" + report.format())

        if not experiences:
            raise KnowledgeError("knowledge base contains no experiences")

        return cls(
            root=root,
            experiences=experiences,
            sources=sources,
            relations=relations,
            taxonomy=taxonomy,
            report=report,
        )

    def experience(self, experience_id: str) -> DesignExperience:
        try:
            return self.experiences[experience_id]
        except KeyError as exc:
            raise KeyError(f"unknown experience id: {experience_id}") from exc

    def source(self, source_id: str) -> Source:
        try:
            return self.sources[source_id]
        except KeyError as exc:
            raise KeyError(f"unknown source id: {source_id}") from exc

    def related(self, experience_id: str) -> list[Relation]:
        self.experience(experience_id)
        return [
            relation
            for relation in self.relations
            if relation.from_id == experience_id or relation.to_id == experience_id
        ]

    def stats(self) -> dict[str, Any]:
        status_counts: dict[str, int] = {}
        type_counts: dict[str, int] = {}
        tag_counts: dict[str, int] = {}
        for exp in self.experiences.values():
            status_counts[exp.knowledge_status] = (
                status_counts.get(exp.knowledge_status, 0) + 1
            )
            type_counts[exp.experience_type] = (
                type_counts.get(exp.experience_type, 0) + 1
            )
            for tag in exp.metadata.tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        return {
            "experiences": len(self.experiences),
            "sources": len(self.sources),
            "relations": len(self.relations),
            "taxonomy_tags": len(self.taxonomy.tags),
            "by_knowledge_status": dict(sorted(status_counts.items())),
            "by_experience_type": dict(sorted(type_counts.items())),
            "by_tag": dict(sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
            "warnings": len(self.report.warnings),
        }
