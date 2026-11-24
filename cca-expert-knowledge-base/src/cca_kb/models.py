"""Typed models mirroring the YAML schemas.

Pydantic is used at the storage boundary so that malformed records fail
loudly instead of silently reaching retrieval.  Fields are allowed to be
empty on purpose: the project rules forbid filling gaps with invented facts.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EXPERIENCE_ID_PATTERN = r"^exp-\d{3}$"
SOURCE_ID_PATTERN = r"^src-\d{3}$"

ExperienceType = Literal[
    "mechanism_design",
    "parameter_design",
    "problem_analysis",
    "tradeoff_analysis",
    "evaluation_method",
    "system_design",
]
KnowledgeStatus = Literal[
    "source_reported",
    "cross_source_synthesis",
    "inference",
]
EvidenceLevel = Literal["A", "B", "C", "D"]
SourceType = Literal[
    "paper",
    "rfc",
    "code",
    "technical_report",
    "experiment",
    "other",
]
RelationType = Literal[
    "extends",
    "contrasts_with",
    "mitigates",
    "complements",
    "alternative_to",
    "synthesizes",
    "related_to",
]
LocatorType = Literal[
    "section",
    "page",
    "figure",
    "table",
    "code_location",
    "url_fragment",
    "line",
]

EVIDENCE_LEVEL_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProblemContext(StrictModel):
    network_conditions: list[str] = Field(default_factory=list)
    observed_symptoms: list[str] = Field(default_factory=list)
    underlying_cause: str = ""


class DesignRationale(StrictModel):
    motivation: str = ""
    problem_analysis: str = ""
    reasoning_chain: list[str] = Field(default_factory=list)


class DesignDecision(StrictModel):
    selected_mechanism: str = ""
    alternatives_considered: list[str] = Field(default_factory=list)
    why_selected: str = ""


class Mechanism(StrictModel):
    description: str = ""
    control_signals: list[str] = Field(default_factory=list)
    control_actions: list[str] = Field(default_factory=list)
    state_variables: list[str] = Field(default_factory=list)


class Tradeoffs(StrictModel):
    benefits: list[str] = Field(default_factory=list)
    costs: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class Applicability(StrictModel):
    suitable_conditions: list[str] = Field(default_factory=list)
    unsuitable_conditions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class ParameterGuidance(StrictModel):
    parameter: str = ""
    guidance: str = ""
    rationale: str = ""
    source_ids: list[str] = Field(default_factory=list)


class Evaluation(StrictModel):
    metrics: list[str] = Field(default_factory=list)
    expected_effects: list[str] = Field(default_factory=list)
    evaluation_conditions: list[str] = Field(default_factory=list)


class EvidenceRef(StrictModel):
    source_id: str = Field(pattern=SOURCE_ID_PATTERN)
    source_type: SourceType
    locator: str = ""
    supports: list[str] = Field(default_factory=list)
    evidence_level: EvidenceLevel


class ExperienceMetadata(StrictModel):
    created_at: str = ""
    updated_at: str = ""
    tags: list[str] = Field(default_factory=list)


class DesignExperience(StrictModel):
    id: str = Field(pattern=EXPERIENCE_ID_PATTERN)
    title: str = Field(min_length=1)
    experience_type: ExperienceType
    problem_context: ProblemContext = Field(default_factory=ProblemContext)
    design_rationale: DesignRationale = Field(default_factory=DesignRationale)
    design_decision: DesignDecision = Field(default_factory=DesignDecision)
    mechanism: Mechanism = Field(default_factory=Mechanism)
    tradeoffs: Tradeoffs = Field(default_factory=Tradeoffs)
    failure_modes: list[str] = Field(default_factory=list)
    applicability: Applicability = Field(default_factory=Applicability)
    parameter_guidance: list[ParameterGuidance] = Field(default_factory=list)
    evaluation: Evaluation = Field(default_factory=Evaluation)
    evidence: list[EvidenceRef] = Field(min_length=1)
    knowledge_status: KnowledgeStatus
    related_experiences: list[str] = Field(default_factory=list)
    metadata: ExperienceMetadata = Field(default_factory=ExperienceMetadata)


class SourceLocator(StrictModel):
    type: LocatorType
    value: str = ""


class SourceMetadata(StrictModel):
    collected_at: str = ""
    notes: str = ""


class Source(StrictModel):
    source_id: str = Field(pattern=SOURCE_ID_PATTERN)
    title: str = Field(min_length=1)
    source_type: SourceType
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    url: str = ""
    doi: str = ""
    venue: str = ""
    version: str = ""
    locators: list[SourceLocator] = Field(default_factory=list)
    related_experiences: list[str] = Field(default_factory=list)
    metadata: SourceMetadata = Field(default_factory=SourceMetadata)


class Relation(StrictModel):
    from_id: str = Field(pattern=EXPERIENCE_ID_PATTERN)
    to_id: str = Field(pattern=EXPERIENCE_ID_PATTERN)
    relation: RelationType
    note: str = ""
    knowledge_status: KnowledgeStatus = "source_reported"
