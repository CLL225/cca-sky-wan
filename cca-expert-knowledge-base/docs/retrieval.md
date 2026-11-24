# 检索设计 / Retrieval

## 1. 目标

检索不是“返回标题含关键词的论文”，而是返回与当前设计问题相关的**设计经验**：
问题背景、设计动机、机制、权衡、适用条件与证据定位。

查询示例：

> 在随机丢包环境中，如何避免拥塞控制算法过度降低发送速率？

期望结果：exp-007（丢包信号假设分析）、exp-001（BBR 的带宽/RTT 模型）、
exp-005（AIMD 基线及其局限），而不是只有标题匹配的条目。

## 2. 混合打分

对每条经验构造检索文档（见 `src/cca_kb/text.py`）：

```text
score = 0.55 × BM25_normalized
      + 0.45 × cosine_normalized
      + 0.05 × title_token_overlap
```

- **BM25**：英文按词、中文按字符二元组；内置中英术语词表桥接
  （如“随机丢包” → random / loss）。IDF 使用 `log(1 + (N-df+0.5)/(df+0.5))`。
- **向量余弦**：默认使用 `intfloat/multilingual-e5-small`；文档与查询分别加
  `passage:` / `query:` 前缀。无模型时回退到确定性哈希 n-gram 向量。
- **标题加成**：查询 token 与标题 token 的重合比例，权重较小，避免标题党。
- 权重可通过 `CCA_KB_KEYWORD_WEIGHT` / `CCA_KB_SEMANTIC_WEIGHT` 调整。

向量后端不可用时不会假装具备语义能力：结果中的 `retrieval.semantic=false`，
并在 `limitations` 中明确声明。

## 3. 元数据过滤

在排序前执行。支持的过滤键：

| 过滤键 | CLI | 语义 |
| --- | --- | --- |
| `experience_type` | `--type` | 经验类型 |
| `knowledge_status` | `--status` | 知识状态 |
| `tags` | `--tag`（可重复） | 任一标签匹配 |
| `all_tags` | `--all-tag`（可重复） | 全部标签匹配 |
| `source_id` | `--source`（可重复） | 证据引用指定来源 |
| `source_type` | `--source-type` | 证据来源类型 |
| `min_evidence_level` | `--min-evidence` | 最高证据等级不低于给定值 |
| `network_condition` | `--network-condition` | 与问题背景文本的 token 重合 ≥ 0.5 |
| `suitable_condition` | `--suitable-condition` | 与适用条件文本的 token 重合 ≥ 0.5 |
| `has_failure_modes` | `--has-failure-modes` | 是否有失败模式 |

注意：`min_evidence_level: B` 表示只保留“最佳证据为 A 或 B”的经验。
证据等级和知识状态不会互相转换。

## 4. 返回格式

```json
{
  "query": "random loss",
  "filters": {},
  "retrieval": {
    "backend": "intfloat/multilingual-e5-small",
    "semantic": true,
    "returned": 3
  },
  "experiences": [
    {
      "experience_id": "exp-007",
      "title": "...",
      "problem_context": {},
      "design_rationale": {},
      "design_decision": {},
      "mechanism": {},
      "tradeoffs": {},
      "applicability": {},
      "failure_modes": [],
      "knowledge_status": "cross_source_synthesis",
      "evidence": [
        {
          "source_id": "src-004",
          "locator": "Congestion Control on the Internet",
          "supports": ["..."],
          "evidence_level": "A",
          "source": {"title": "...", "url": "...", "doi": "..."}
        }
      ],
      "related_experiences": [],
      "retrieval": {
        "score": 0.83,
        "keyword_score": 0.91,
        "semantic_score": 0.74,
        "matched_terms": ["random", "loss"]
      }
    }
  ],
  "related_experiences": [],
  "limitations": [
    "结果包含知识库推论（inference）：...。这些内容不是原作者明确结论。",
    "结果包含仅有 C/D 级证据的经验：..."
  ]
}
```

`limitations` 是接口契约的一部分，不是可选装饰。以下情况必须出现说明：

- 使用了非语义向量后端；
- 结果包含 `inference` 条目；
- 结果只有 C/D 级证据；
- 最高分过低，检索可能不相关；
- 应用了元数据过滤，未返回结果不代表知识库没有相关记录。

## 5. Python 接口

```python
from cca_kb.api import (
    search_experiences,
    get_experience,
    get_source,
    get_related_experiences,
)

result = search_experiences(
    "在随机丢包环境中如何避免过度降速",
    filters={"tags": ["random-loss"]},
    top_k=5,
)
exp = get_experience("exp-007")
src = get_source("src-004")
related = get_related_experiences("exp-001")
```

可选 `root=` 与 `embedder=` 参数用于测试或多知识库实例：

```python
search_experiences("pacing", embedder="hashing", root="/path/to/kb")
```

`embedder` 取值：`auto`、`sentence_transformers`、`hashing`。

## 6. CLI

零依赖运行时（仅标准库，GitHub clone 后直接可用）：

```bash
python3 skill/cca_kb.py search "随机丢包 过度降速" --top-k 5
python3 skill/cca_kb.py search "ECN proportional response" --tag ecn-handling
python3 skill/cca_kb.py get exp-003
python3 skill/cca_kb.py source src-008
python3 skill/cca_kb.py related exp-001
```

完整开发栈/容器内的等价命令：

```bash
cca-kb search "随机丢包 过度降速" --top-k 5
cca-kb search "ECN proportional response" --tag ecn-handling --json
cca-kb get exp-003
cca-kb source src-008
cca-kb related exp-001
```

## 6.1 便携运行时

`skill/cca_kb.py` 提供与 `cca_kb.api` 同名的四个函数及 `catalog` / `stats`，
读取由 `tools/export_skill.py` 生成的 `skill/knowledge.json`。它的打分是：

```text
score = 0.95 × BM25_normalized + 0.05 × title_token_overlap
```

- 没有向量后端，`retrieval.semantic` 恒为 `false`，`limitations` 会声明这一点。
- 中英桥接依赖 `knowledge.json` 中的 `bilingual_lexicon`，与完整栈同源。
- 过滤键与返回结构与完整栈保持一致，便于在两种运行时之间切换。

对几十条精选经验，BM25 + 词表 + 元数据过滤已经足够；需要跨语言语义匹配时再使用
完整栈的 E5 后端。

## 7. 可替换性

`Embedder` 协议只要求三个成员：

```python
name: str
is_semantic: bool
dimension: int
embed_documents(texts) -> np.ndarray
embed_query(text) -> np.ndarray
```

替换为 Qdrant、Chroma、OpenAI embeddings 或本地向量库时，只需：

1. 实现 `Embedder`；
2. 在 `build_embedder()` 或调用处注入；
3. 保持 `search_experiences` 的返回结构不变。

YAML 知识与检索实现解耦，因此更换后端不需要重新整理知识内容。

## 8. 评估检索质量

建议使用一组设计问题（见 `examples/example_query.md`）人工检查：

- 是否返回了真正相关的设计经验，而非仅关键词相似；
- 是否能解释专家为什么采用某机制；
- 是否提供收益、代价与适用条件；
- 是否给出可定位证据；
- 是否明确区分推论与事实。

这些都是 MVP 验收标准，不以算法性能提升为验收依据。
