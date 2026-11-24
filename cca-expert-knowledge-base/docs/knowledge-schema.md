# 知识模型与 Schema

## 1. 核心对象

| 对象 | 文件位置 | 作用 |
| --- | --- | --- |
| `DesignExperience` | `knowledge/experiences/exp-NNN.yaml` | 一条专家设计经验 |
| `Source` | `knowledge/sources/src-NNN.yaml` | 论文/RFC/代码/报告的元数据与定位 |
| `Relation` | `knowledge/relations/relations.yaml` | 经验之间的带类型关系 |
| `Taxonomy` | `knowledge/taxonomy/taxonomy.yaml` | 分类标签 |

机器可读契约位于：

- `schemas/design_experience.schema.json`
- `schemas/source.schema.json`

JSON Schema（Draft 2020-12）与 Pydantic 模型同时存在：

- JSON Schema 面向外部工具和其他语言实现。
- Pydantic 在加载时提供更友好的错误信息并禁止未知字段。

## 2. DesignExperience 字段

### 2.1 身份与类型

| 字段 | 要求 | 说明 |
| --- | --- | --- |
| `id` | `^exp-[0-9]{3}$` | 稳定 ID，不复用 |
| `title` | 非空 | 面向人的经验标题，应体现设计选择而非仅算法名 |
| `experience_type` | 枚举 | `mechanism_design` / `parameter_design` / `problem_analysis` / `tradeoff_analysis` / `evaluation_method` / `system_design` |

### 2.2 设计经验主体

| 字段 | 回答的问题 |
| --- | --- |
| `problem_context.network_conditions` | 在什么网络条件下 |
| `problem_context.observed_symptoms` | 观察到什么现象 |
| `problem_context.underlying_cause` | 专家认为根因是什么 |
| `design_rationale.motivation` | 为什么值得解决 |
| `design_rationale.problem_analysis` | 专家如何理解和建模问题 |
| `design_rationale.reasoning_chain` | 从问题到设计的推理步骤 |
| `design_decision.selected_mechanism` | 选择了什么机制 |
| `design_decision.alternatives_considered` | 考虑过哪些替代方案 |
| `design_decision.why_selected` | 为什么这样选 |
| `mechanism.description` | 机制如何工作 |
| `mechanism.control_signals` | 使用哪些观测信号 |
| `mechanism.control_actions` | 执行哪些控制动作 |
| `mechanism.state_variables` | 维护哪些状态 |
| `tradeoffs.benefits/costs/limitations` | 收益、代价、限制 |
| `failure_modes` | 已知或可能的失效情况 |
| `applicability.*` | 适用条件、不适用条件、前提假设 |
| `parameter_guidance` | 参数取值、行为与依据 |
| `evaluation.*` | 指标、预期效果、评估条件 |

### 2.3 证据与知识状态

```yaml
evidence:
  - source_id: src-004
    source_type: paper
    locator: "Modeling the Network Path"
    supports:
      - "用 BtlBw 与 RTprop 建模路径"
    evidence_level: A
knowledge_status: source_reported
```

`knowledge_status` 与 `evidence_level` 是两个独立维度：

| knowledge_status | 含义 |
| --- | --- |
| `source_reported` | 来源明确陈述 |
| `cross_source_synthesis` | 多来源对比归纳 |
| `inference` | 知识库推论，需 `reasoning_chain` |

| evidence_level | 含义 |
| --- | --- |
| A | 原始论文、标准或权威文档中的直接证据 |
| B | 多个独立来源相互支持 |
| C | 单一来源的间接证据或有限实验支持 |
| D | 基于已有知识得到、缺少直接验证的推论 |

**不能用证据等级自动决定知识状态。** 一条 A 级证据上的推论仍然必须标记为
`inference`；一条 `source_reported` 的结论也可能只有 C 级证据。

`supports` 不是装饰字段：它必须说明“这个来源具体支持了本条经验中的哪项结论”，
禁止只在条目末尾罗列论文。

### 2.4 关系与元数据

- `related_experiences`：无类型的自由关联。
- `relations.yaml`：带类型的关系，检索结果会带方向与类型返回。
- `metadata.tags`：必须来自 taxonomy；未知标签会产生质量警告。

## 3. Source 字段

| 字段 | 说明 |
| --- | --- |
| `source_id` | `^src-[0-9]{3}$` |
| `title` / `authors` / `year` | 文献元数据 |
| `source_type` | `paper` / `rfc` / `code` / `technical_report` / `experiment` / `other` |
| `url` / `doi` / `venue` / `version` | 稳定定位信息 |
| `locators` | 章节、页码、图、表、代码位置 |
| `related_experiences` | 反向索引，便于从来源查经验 |
| `metadata.collected_at` / `notes` | 采集时间与备注 |

来源记录必须尽量保留可复查的定位信息。只有 URL 不够，至少写清具体章节或代码函数。

## 4. 空字段与“未知”

字段允许为空。以下写法是合法的：

```yaml
tradeoffs:
  benefits: []
  costs: []
  limitations: []
```

但质量门禁会把关键空缺报告为错误或警告。原则是：

- 来源没有提供的信息，留空。
- 不确定的内容，写成 `inference` 并说明推理依据。
- 不把知识库的推测改写为作者观点。

## 5. 示例

完整示例见 `examples/example_experience.yaml`；生产数据见
`knowledge/experiences/exp-001.yaml` 至 `exp-008.yaml`。

新增记录后的最小校验流程：

```bash
cca-kb validate --strict
cca-kb quality
pytest
```
