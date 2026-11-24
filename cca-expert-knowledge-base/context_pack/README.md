# 静态提示词知识包

这里是从 `knowledge/` 生成的 Markdown，用于**不能运行检索脚本**、只能拼提示词的
场景（例如直接在 OpenAI 网页端或简单脚本里发消息）。

| 文件 | 用途 | 大小 |
| --- | --- | --- |
| `catalog.md` | 8 条经验的目录、类型、状态、标签和摘要 | 约 6 KB |
| `design_experience_pack.md` | 完整经验包：问题、动机、机制、权衡、失败模式、适用性、参数、证据 | 约 58 KB |

## 用法

### 先选后取（推荐）

1. 把 `catalog.md` 放进 system prompt；
2. 让模型选出与当前设计问题最相关的 1–3 个 `exp-ID`；
3. 再把这些 ID 对应的段落（或 `skill/cca_kb.py get exp-ID` 的输出）放进 prompt。

### 全量注入

条目少、上下文预算充足时，可以直接把 `design_experience_pack.md` 放进
system prompt。注意：它会让提示词变长，且模型可能被不相关的经验干扰。

## 约束

- 这些文件是生成物，不要手工编辑；改 `knowledge/` 后运行
  `python3 tools/export_skill.py`。
- 提示词中要求模型保留 `knowledge_status` 和 `evidence_level` 的区分，
  不要把 `inference` 写成原作者结论。
- 如果问题超出这 8 条经验的范围，要求模型明确说“知识库没有直接经验”，
  而不是编造引用。
