"""Export the YAML knowledge base into the zero-dependency skill bundle.

Run this after editing ``knowledge/``:

    python tools/export_skill.py

The generated ``skill/knowledge.json`` is what the portable skill
(``skill/cca_kb.py``) reads. The YAML files remain the source of truth;
this script is the only place that needs the full authoring toolchain.
"""

from __future__ import annotations

import json
from pathlib import Path

from cca_kb.config import Settings
from cca_kb.repository import KnowledgeRepository
from cca_kb.text import BILINGUAL_LEXICON, STOPWORDS, experience_search_text


def _summary(exp) -> str:
    problem = (
        exp.problem_context.underlying_cause
        or " ".join(exp.problem_context.observed_symptoms)
    )
    decision = exp.design_decision.selected_mechanism
    text = " ".join(part.strip() for part in (problem, decision) if part.strip())
    return " ".join(text.split())[:280]


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _bullets(values: list[str], limit: int | None = None) -> str:
    selected = values if limit is None else values[:limit]
    if not selected:
        return "- （未记录）"
    return "\n".join(f"- {_clean(value)}" for value in selected)


def _related_notes(repository: KnowledgeRepository, experience_id: str) -> str:
    lines = []
    for relation in repository.relations:
        if relation.from_id == experience_id:
            lines.append(
                f"- {relation.to_id} ({relation.relation}): {_clean(relation.note)}"
            )
        elif relation.to_id == experience_id:
            lines.append(
                f"- {relation.from_id} ({relation.relation}, incoming): "
                f"{_clean(relation.note)}"
            )
    return "\n".join(lines) or "- （无）"


def _experience_pack(repository: KnowledgeRepository, exp) -> str:
    evidence_lines = []
    for evidence in exp.evidence:
        source = repository.sources.get(evidence.source_id)
        title = source.title if source else evidence.source_id
        evidence_lines.append(
            f"- [{evidence.evidence_level}] {evidence.source_id} — {title}; "
            f"定位: {_clean(evidence.locator)}"
        )
    parameters = []
    for guidance in exp.parameter_guidance:
        parameters.append(
            f"- {guidance.parameter}: {_clean(guidance.guidance)} "
            f"(理由: {_clean(guidance.rationale)}; 来源: "
            f"{', '.join(guidance.source_ids) or '未标注'})"
        )

    return f"""## {exp.id} — {exp.title}

- 类型: `{exp.experience_type}`
- 知识状态: `{exp.knowledge_status}`（source_reported=原作者明确结论；cross_source_synthesis=跨来源归纳；inference=知识库推论）
- 标签: {', '.join(exp.metadata.tags) or '（无）'}

### 问题背景
{_clean(exp.problem_context.underlying_cause) or '（未记录根因）'}

观察到的现象:
{_bullets(exp.problem_context.observed_symptoms)}

### 设计动机与分析
{_clean(exp.design_rationale.motivation) or '（未记录动机）'}

问题分析:
{_clean(exp.design_rationale.problem_analysis) or '（未记录）'}

推理链:
{_bullets(exp.design_rationale.reasoning_chain)}

### 设计决策
{_clean(exp.design_decision.selected_mechanism) or '（未记录）'}

为什么这样选:
{_clean(exp.design_decision.why_selected) or '（未记录）'}

考虑过的替代方案:
{_bullets(exp.design_decision.alternatives_considered)}

### 机制
{_clean(exp.mechanism.description) or '（未记录）'}

控制信号:
{_bullets(exp.mechanism.control_signals)}

控制动作:
{_bullets(exp.mechanism.control_actions)}

### 权衡
收益:
{_bullets(exp.tradeoffs.benefits)}

代价:
{_bullets(exp.tradeoffs.costs)}

限制:
{_bullets(exp.tradeoffs.limitations)}

### 失败模式
{_bullets(exp.failure_modes)}

### 适用条件
适用:
{_bullets(exp.applicability.suitable_conditions)}

不适用:
{_bullets(exp.applicability.unsuitable_conditions)}

假设:
{_bullets(exp.applicability.assumptions)}

### 参数指导
{chr(10).join(parameters) if parameters else '- （无）'}

### 评估
指标:
{_bullets(exp.evaluation.metrics)}

预期效果:
{_bullets(exp.evaluation.expected_effects)}

### 证据
{chr(10).join(evidence_lines)}

### 关联经验
{_related_notes(repository, exp.id)}
"""


def _context_pack(repository: KnowledgeRepository) -> str:
    header = """# TCP/QUIC 拥塞控制专家设计经验包

本文件由 `tools/export_skill.py` 从 `knowledge/` 生成，请勿手工编辑。

使用方式：在让 LLM 生成新的 CCA 之前，把本文件（或按问题检索出的相关段落）
放入 system prompt。要求模型：

1. 先引用相关的 `exp-NNN` 经验，说明借鉴了哪个问题、动机或机制。
2. 区分 `knowledge_status`：`source_reported` 是原作者结论，
   `cross_source_synthesis` 是知识库归纳，`inference` 是推论，不得当作原文事实。
3. 给出设计时引用证据的 `source_id` 与 locator。
4. 如果经验不适用，明确说明原因，不要强行套用。

## 目录

"""
    catalog_lines = [
        f"- `{exp.id}` {exp.title}（{exp.experience_type}, {exp.knowledge_status}）"
        for exp in repository.experiences.values()
    ]
    body = "\n\n".join(
        _experience_pack(repository, exp)
        for exp in repository.experiences.values()
    )
    return header + "\n".join(catalog_lines) + "\n\n---\n\n" + body


def _catalog_markdown(repository: KnowledgeRepository) -> str:
    lines = [
        "# CCA 设计经验目录",
        "",
        "用于让 LLM 先判断哪些经验与当前设计问题相关，再按 ID 取全文。",
        "",
    ]
    for exp in repository.experiences.values():
        lines.extend(
            [
                f"## {exp.id} — {exp.title}",
                "",
                f"- 类型: `{exp.experience_type}`",
                f"- 知识状态: `{exp.knowledge_status}`",
                f"- 标签: {', '.join(exp.metadata.tags)}",
                f"- 摘要: {_summary(exp)}",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> int:
    root = Settings.from_env().root
    repository = KnowledgeRepository.load(root)

    experiences = []
    for experience_id in sorted(repository.experiences):
        exp = repository.experiences[experience_id]
        payload = exp.model_dump()
        payload["search_text"] = experience_search_text(exp)
        payload["summary"] = _summary(exp)
        experiences.append(payload)

    data = {
        "schema_version": 1,
        "generated_from": "knowledge/",
        "bilingual_lexicon": {
            key: list(values) for key, values in BILINGUAL_LEXICON.items()
        },
        "stopwords": sorted(STOPWORDS),
        "taxonomy": repository.taxonomy.categories,
        "experiences": experiences,
        "sources": [
            repository.sources[source_id].model_dump()
            for source_id in sorted(repository.sources)
        ],
        "relations": [relation.model_dump() for relation in repository.relations],
    }

    target = root / "skill" / "knowledge.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    pack_dir = root / "context_pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    full_pack = pack_dir / "design_experience_pack.md"
    full_pack.write_text(_context_pack(repository) + "\n", encoding="utf-8")
    catalog_pack = pack_dir / "catalog.md"
    catalog_pack.write_text(_catalog_markdown(repository) + "\n", encoding="utf-8")

    print(
        f"wrote {target} ({target.stat().st_size} bytes, "
        f"{len(experiences)} experiences, {len(data['sources'])} sources)"
    )
    print(f"wrote {full_pack} ({full_pack.stat().st_size} bytes)")
    print(f"wrote {catalog_pack} ({catalog_pack.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
