"""Schema and cross-record integrity validation.

Two layers are enforced:

1. JSON Schema validation against ``schemas/*.schema.json`` (the portable
   contract other tools can reuse).
2. Referential integrity between experiences, sources, relations and the
   taxonomy (which JSON Schema cannot express).

Evidence levels and knowledge status are validated as independent
dimensions, as required by the project specification.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from jsonschema import Draft202012Validator

from cca_kb.models import (
    EVIDENCE_LEVEL_RANK,
    DesignExperience,
    Relation,
    Source,
)


@dataclass(frozen=True)
class Issue:
    severity: str  # "error" | "warning"
    code: str
    location: str
    message: str


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)

    def error(self, code: str, location: str, message: str) -> None:
        self.issues.append(Issue("error", code, location, message))

    def warning(self, code: str, location: str, message: str) -> None:
        self.issues.append(Issue("warning", code, location, message))

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    def format(self) -> str:
        if not self.issues:
            return "no validation issues"
        lines = []
        for issue in self.issues:
            lines.append(
                f"[{issue.severity.upper()}] {issue.code} @ {issue.location}: "
                f"{issue.message}"
            )
        return "\n".join(lines)


def load_schema(path: Path) -> Draft202012Validator:
    with path.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def load_validators(schemas_dir: Path) -> tuple[Draft202012Validator, Draft202012Validator]:
    experience = load_schema(schemas_dir / "design_experience.schema.json")
    source = load_schema(schemas_dir / "source.schema.json")
    return experience, source


def validate_instances(
    instances: list[tuple[str, dict]],
    validator: Draft202012Validator,
    report: ValidationReport,
) -> None:
    for location, instance in instances:
        for error in sorted(validator.iter_errors(instance), key=lambda e: list(e.path)):
            path = "/".join(str(part) for part in error.path) or "<root>"
            report.error("SCHEMA", f"{location}#{path}", error.message)


def best_evidence_level(exp: DesignExperience) -> str:
    ranks = [EVIDENCE_LEVEL_RANK[e.evidence_level] for e in exp.evidence]
    return min(ranks) if ranks else 3


def check_integrity(
    experiences: dict[str, DesignExperience],
    sources: dict[str, Source],
    relations: list[Relation],
    taxonomy_tags: set[str] | None,
    report: ValidationReport,
) -> None:
    for exp in experiences.values():
        if exp.id in exp.related_experiences:
            report.error(
                "REL-SELF",
                exp.id,
                "related_experiences must not reference the same experience",
            )
        for related_id in exp.related_experiences:
            if related_id not in experiences:
                report.error(
                    "REL-MISSING",
                    f"{exp.id}.related_experiences",
                    f"unknown experience id '{related_id}'",
                )

        for index, evidence in enumerate(exp.evidence):
            source = sources.get(evidence.source_id)
            if source is None:
                report.error(
                    "SRC-MISSING",
                    f"{exp.id}.evidence[{index}]",
                    f"unknown source id '{evidence.source_id}'",
                )
                continue
            if evidence.source_type != source.source_type:
                report.warning(
                    "SRC-TYPE",
                    f"{exp.id}.evidence[{index}]",
                    f"evidence source_type '{evidence.source_type}' differs from "
                    f"source record '{source.source_type}'",
                )

        if exp.knowledge_status == "cross_source_synthesis":
            distinct = {e.source_id for e in exp.evidence}
            if len(distinct) < 2:
                report.warning(
                    "SYNTH-SINGLE-SOURCE",
                    exp.id,
                    "cross_source_synthesis should cite at least two distinct sources",
                )

        if exp.knowledge_status == "source_reported":
            level = best_evidence_level(exp)
            if EVIDENCE_LEVEL_RANK["C"] <= level:
                report.warning(
                    "STATUS-EVIDENCE",
                    exp.id,
                    "source_reported entry relies only on C/D evidence; "
                    "consider cross_source_synthesis or explaining the evidence basis",
                )

        if taxonomy_tags:
            unknown = sorted(set(exp.metadata.tags) - taxonomy_tags)
            if unknown:
                report.warning(
                    "TAXONOMY-UNKNOWN",
                    exp.id,
                    f"tags not present in taxonomy: {', '.join(unknown)}",
                )

    for source in sources.values():
        for related_id in source.related_experiences:
            if related_id not in experiences:
                report.error(
                    "SRC-REL-MISSING",
                    source.source_id,
                    f"related experience '{related_id}' does not exist",
                )

    seen_pairs: set[tuple[str, str, str]] = set()
    for index, relation in enumerate(relations):
        if relation.from_id not in experiences:
            report.error(
                "RELN-FROM-MISSING",
                f"relations[{index}]",
                f"unknown from_id '{relation.from_id}'",
            )
        if relation.to_id not in experiences:
            report.error(
                "RELN-TO-MISSING",
                f"relations[{index}]",
                f"unknown to_id '{relation.to_id}'",
            )
        if relation.from_id == relation.to_id:
            report.error(
                "RELN-SELF",
                f"relations[{index}]",
                "a relation cannot point an experience to itself",
            )
        key = (relation.from_id, relation.to_id, relation.relation)
        if key in seen_pairs:
            report.warning(
                "RELN-DUPLICATE",
                f"relations[{index}]",
                "duplicate relation",
            )
        seen_pairs.add(key)
