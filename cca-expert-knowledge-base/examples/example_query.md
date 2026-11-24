# Example queries

零依赖用法（推荐）：只需要 `python3`，在仓库根目录运行下面的命令。
如果使用完整开发栈/容器，把 `python3 skill/cca_kb.py` 换成 `cca-kb`，
参数相同（见 `QUICKSTART.md`）。

## 1. Design question in English

```bash
python3 skill/cca_kb.py search \
  "How should a sender estimate bandwidth instead of treating loss as congestion?" \
  --top-k 3
```

Expected: `exp-001` (BBR model-based control) near the top, with `exp-007`
(the problem analysis of loss-as-congestion) and `exp-004` (delay-based
control) as related material.

## 2. Same question in Chinese

```bash
python3 skill/cca_kb.py search "如何用带宽估计代替丢包信号判断拥塞" --top-k 3
```

The tokenizer keeps CJK bigrams and maps bilingual terms (`带宽` -> bandwidth,
`丢包` -> loss, `拥塞` -> congestion), so the same design experiences are
retrieved without a translation step. 这是词表桥接，不是向量语义检索；
结果中 `retrieval.semantic` 为 `false`。

## 3. Filter by knowledge status

```bash
python3 skill/cca_kb.py search "ECN" --status source_reported --top-k 5
```

Only directly attributable records are returned. Use
`--status cross_source_synthesis` to see records that combine several sources.

## 4. Filter by source and evidence level

```bash
python3 skill/cca_kb.py search "congestion window" --source src-010 --min-evidence A
```

`src-010` is the RACK/TLP RFC, so this narrows the result to the experience
that cites it (`exp-005`).

## 5. Inspect a record and its provenance

```bash
python3 skill/cca_kb.py get exp-001
python3 skill/cca_kb.py source src-004
python3 skill/cca_kb.py related exp-001
```

`get` returns the full record plus the resolved source records. `source` shows
which experiences use a source. `related` follows the typed relations in
`knowledge/relations/relations.yaml`.

## 6. Use the Python API

零依赖 skill（任意 Python 3.10+，无需安装）：

```python
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "cca_skill", Path("skill/cca_kb.py")
)
skill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill)

result = skill.search_experiences(
    "random loss on a high-BDP path",
    top_k=3,
    filters={"min_evidence_level": "A"},
)
for hit in result["experiences"]:
    print(hit["experience_id"], hit["title"], hit["knowledge_status"])

record = skill.get_experience("exp-007")
print(record["limitations"])
```

完整开发栈（已 `pip install -e .` 或使用容器）：

```python
from cca_kb.api import search_experiences, get_experience

result = search_experiences(
    "random loss on a high-BDP path",
    top_k=3,
    filters={"min_evidence_level": "A"},
)
```

The response always includes `retrieval` (scores and backend) and
`limitations` (warnings about weak evidence, inference records, or a
non-semantic backend), so an LLM consumer can decide how much to trust the
result.

## 7. Ask a question the knowledge base cannot answer

```bash
python3 skill/cca_kb.py search "What is the best CCA for a satellite link with a 600 ms RTT?"
```

No record covers that exact case. The response is not empty if any record is
topically close, but `limitations` reports a low top score and the answer
should be treated as related background rather than a recommendation. This is
intentional: the knowledge base records *design experience*, not answers it
has not verified.

## 8. 静态提示词（不能运行脚本时）

使用 `context_pack/catalog.md` 先让模型挑选相关 `exp-ID`，再取
`context_pack/design_experience_pack.md` 中的对应段落。说明见
`context_pack/README.md`。
