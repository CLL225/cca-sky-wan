# 知识加工流程 / Data Processing

本文件定义“从原始资料到可入库设计经验”的流程与质量门禁。目标不是生成论文摘要，
而是提取**专家面对的问题、推理过程、机制选择与权衡**。

## 1. 流程

```text
选择资料
  -> 核对来源与定位
  -> 识别问题背景与网络条件
  -> 提取设计动机与问题分析
  -> 提取设计决策、替代方案与理由
  -> 提取机制细节（信号/动作/状态）
  -> 提取收益、代价、失效模式、适用条件
  -> 建立证据条目（source + locator + supports）
  -> 标注 knowledge_status 与 evidence_level
  -> 建立 related_experiences / relations
  -> validate + quality + 人工复核
  -> 入库
```

## 2. 资料选择

优先：

1. 原始论文（作者自己描述设计与实验）。
2. IETF RFC（标准化的行为与参数）。
3. 代表性开源实现（用于核对实现细节与常量）。
4. 可靠的技术报告与实验研究。

来源必须可定位。论文用 DOI + 章节/图号；RFC 用编号 + 章节；代码用仓库 URL +
提交/版本 + 文件与函数名。

**版权**：默认只在 `sources/` 保存元数据、摘要和定位信息。不要把受版权保护的
论文全文提交到仓库。需要阅读全文时，在容器内临时下载到 `/tmp` 并在加工完成后删除。

## 3. 提取检查表

对每份资料回答：

1. 作者试图解决什么问题？
2. 作者认为现有方法的不足是什么？
3. 作者如何解释网络现象？关键观测信号是什么？
4. 提出了什么设计？包含哪些机制？
5. 为什么选择这些机制？考虑过哪些替代方案？
6. 机制如何影响 cwnd、发送速率、pacing 或其他控制量？
7. 报告了哪些收益？代价与局限是什么？
8. 设计依赖哪些假设？在哪些条件下会失效？
9. 哪些实验支持了哪些结论？
10. 哪些内容是作者明确陈述，哪些是提取者的归纳或推论？

没有答案的问题留空，不得自行补全为事实。

## 4. 知识状态判定

| 情况 | knowledge_status |
| --- | --- |
| 只转述单一（或同类）来源明确陈述的内容 | `source_reported` |
| 对比多个来源后得出共同模式/差异 | `cross_source_synthesis` |
| 把经验迁移到未经直接验证的场景、组合机制、指出潜在冲突 | `inference` |

判定顺序：

1. 逐条写下来源明确支持的结论，放入 `supports`。
2. 如果某条结论需要多个来源共同支撑，标为综合。
3. 如果某条结论没有来源直接陈述，标为推论，并在 `reasoning_chain` 写出依据。
4. 不要因为证据等级高就把推论写成事实。

## 5. 质量门禁

运行：

```bash
cca-kb validate            # Schema + 引用完整性
cca-kb validate --strict   # 警告也视为失败（发布前建议）
cca-kb quality             # 质量检查与完整度
pytest                     # 数据与检索回归
```

### 5.1 阻断错误（E）

| 代码 | 含义 |
| --- | --- |
| `E-NO-PROBLEM` | 没有问题背景 |
| `E-NO-RATIONALE` | 没有设计动机与问题分析 |
| `E-NO-DECISION` | 没有设计决策 |
| `E-NO-MECHANISM` | 没有机制描述 |
| `E-NO-TRADEOFFS` | 没有收益、代价或限制 |
| `E-NO-LOCATOR` | 所有证据都没有可定位信息 |
| `SCHEMA` / `SRC-MISSING` / `REL-MISSING` | Schema 或引用完整性失败 |

### 5.2 常见警告（W）

- `W-NO-MOTIVATION`：没有动机。
- `W-NO-APPLICABILITY`：没有适用条件。
- `W-NO-TAGS` / `TAXONOMY-UNKNOWN`：标签缺失或未登记。
- `W-INFERENCE-NO-CHAIN`：推论没有推理链。
- `W-SIMILAR-MECHANISM`：机制描述与其他条目高度相似，可能重复。
- `W-SOURCE-NO-LOCATOR` / `W-SOURCE-NO-URL`：来源定位不足。
- `W-PARAM-NO-SOURCE`：参数建议没有依据来源。

错误必须修复；警告需要逐条判断：能补则补，不能补就保留并在评审中说明。

## 6. 新增一条经验的步骤

1. 在 `knowledge/sources/` 增加来源（若不存在）。
2. 复制 `examples/example_experience.yaml` 为 `knowledge/experiences/exp-NNN.yaml`。
3. 填写问题、动机、推理链、决策、机制、权衡、适用条件。
4. 为每项关键结论写 `evidence.supports`，并给 `locator`。
5. 标注 `knowledge_status`；如果是推论，补 `reasoning_chain`。
6. 更新 `related_experiences` 与 `knowledge/relations/relations.yaml`。
7. 运行 `cca-kb validate --strict`、`cca-kb quality`、`pytest`。
8. 运行 `python3 tools/export_skill.py` 重新生成 `skill/knowledge.json` 与
   `context_pack/`（它们是生成物，不手工编辑）。
9. 人工复核：是否存在把推论写成作者观点、是否遗漏失效条件。

## 7. 证据追溯的最低要求

每条关键结论都应能回答：

- 来自哪个来源？
- 来源中的哪个章节/页码/代码位置？
- 来源实际支持哪项结论？
- 是作者明确陈述，还是知识库归纳？

只在条目末尾列一串论文编号，不算可追溯。
