"""Quality gate for design experiences.

Schema validity is necessary but not sufficient.  The project specification
requires each experience to explain a problem, a design motivation, a
mechanism, tradeoffs, applicability and locatable evidence.  This module
reports blocking errors and non-blocking warnings, plus a per-record
completeness score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from cca_kb.repository import KnowledgeRepository
from cca_kb.text import tokenize


@dataclass(frozen=True)
class QualityIssue:
    severity: str
    code: str
    experience_id: str
    message: str


@dataclass
class QualityReport:
    issues: list[QualityIssue] = field(default_factory=list)
    completeness: dict[str, float] = field(default_factory=dict)
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def add(self, severity: str, code: str, experience_id: str, message: str) -> None:
        self.issues.append(QualityIssue(severity, code, experience_id, message))

    @property
    def errors(self) -> list[QualityIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[QualityIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def average_completeness(self) -> float:
        if not self.completeness:
            return 0.0
        return sum(self.completeness.values()) / len(self.completeness)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "errors": [issue.__dict__ for issue in self.errors],
            "warnings": [issue.__dict__ for issue in self.warnings],
            "completeness": dict(sorted(self.completeness.items())),
            "average_completeness": round(self.average_completeness, 3),
        }


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def check_repository(repository: KnowledgeRepository) -> QualityReport:
    report = QualityReport()
    seen_titles: dict[str, str] = {}
    mechanism_tokens: dict[str, set[str]] = {}

    for exp in repository.experiences.values():
        pc = exp.problem_context
        dr = exp.design_rationale
        dd = exp.design_decision
        mech = exp.mechanism
        tr = exp.tradeoffs
        app = exp.applicability

        sections = {
            "problem_context": bool(
                pc.network_conditions or pc.observed_symptoms or pc.underlying_cause
            ),
            "design_rationale": bool(
                dr.motivation or dr.problem_analysis or dr.reasoning_chain
            ),
            "design_decision": bool(
                dd.selected_mechanism or dd.alternatives_considered or dd.why_selected
            ),
            "mechanism": bool(
                mech.description or mech.control_signals or mech.control_actions
            ),
            "tradeoffs": bool(tr.benefits or tr.costs or tr.limitations),
            "applicability": bool(
                app.suitable_conditions
                or app.unsuitable_conditions
                or app.assumptions
            ),
            "evidence_with_locator": any(
                evidence.locator.strip() for evidence in exp.evidence
            ),
            "evaluation": bool(
                exp.evaluation.metrics
                or exp.evaluation.expected_effects
                or exp.evaluation.evaluation_conditions
            ),
        }
        report.completeness[exp.id] = round(
            sum(sections.values()) / len(sections), 3
        )

        blocking = {
            "problem_context": ("E-NO-PROBLEM", "缺少问题背景（网络条件/现象/根因）"),
            "design_rationale": (
                "E-NO-RATIONALE",
                "缺少设计动机与问题分析",
            ),
            "design_decision": (
                "E-NO-DECISION",
                "缺少设计决策（所选机制/替代方案/选择理由）",
            ),
            "mechanism": ("E-NO-MECHANISM", "缺少机制描述"),
            "tradeoffs": ("E-NO-TRADEOFFS", "缺少收益、代价或限制"),
            "evidence_with_locator": (
                "E-NO-LOCATOR",
                "没有任何证据提供可定位信息（章节/页码/代码位置等）",
            ),
        }
        for section, (code, message) in blocking.items():
            if not sections[section]:
                report.add("error", code, exp.id, message)

        if not dr.motivation.strip():
            report.add("warning", "W-NO-MOTIVATION", exp.id, "motivation 为空")
        if not app.suitable_conditions:
            report.add(
                "warning",
                "W-NO-APPLICABILITY",
                exp.id,
                "未说明适用条件",
            )
        if not exp.metadata.tags:
            report.add("warning", "W-NO-TAGS", exp.id, "元数据没有标签")
        if not sections["evaluation"]:
            report.add("warning", "W-NO-EVALUATION", exp.id, "未记录评估指标或条件")
        if not exp.related_experiences:
            report.add(
                "warning", "W-NO-RELATED", exp.id, "未关联其他设计经验"
            )
        if exp.knowledge_status == "inference" and not dr.reasoning_chain:
            report.add(
                "warning",
                "W-INFERENCE-NO-CHAIN",
                exp.id,
                "inference 条目缺少 reasoning_chain，推论依据不清晰",
            )
        if len(mech.description.strip()) < 40:
            report.add(
                "warning",
                "W-SHORT-MECHANISM",
                exp.id,
                "机制描述过短，可能不足以支持设计推理",
            )
        for index, guidance in enumerate(exp.parameter_guidance):
            if not guidance.source_ids:
                report.add(
                    "warning",
                    "W-PARAM-NO-SOURCE",
                    exp.id,
                    f"parameter_guidance[{index}] 未标注依据来源",
                )

        normalized_title = exp.title.strip().lower()
        if normalized_title in seen_titles:
            report.add(
                "warning",
                "W-DUPLICATE-TITLE",
                exp.id,
                f"标题与 {seen_titles[normalized_title]} 重复",
            )
        else:
            seen_titles[normalized_title] = exp.id

        current_tokens = set(tokenize(mech.description))
        for other_id, other_tokens in mechanism_tokens.items():
            similarity = _jaccard(current_tokens, other_tokens)
            if similarity >= 0.85:
                report.add(
                    "warning",
                    "W-SIMILAR-MECHANISM",
                    exp.id,
                    f"机制描述与 {other_id} 高度相似（Jaccard={similarity:.2f}）",
                )
        mechanism_tokens[exp.id] = current_tokens

    for source in repository.sources.values():
        if not source.locators:
            report.add(
                "warning",
                "W-SOURCE-NO-LOCATOR",
                source.source_id,
                "来源记录没有定位信息",
            )
        if not source.url and not source.doi:
            report.add(
                "warning",
                "W-SOURCE-NO-URL",
                source.source_id,
                "来源记录既没有 URL 也没有 DOI",
            )

    return report
