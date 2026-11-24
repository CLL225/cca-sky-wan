# Quickstart

## 1. 零依赖用法（推荐）

只需要 `python3`，不需要 pip、Docker、PyTorch 或模型下载：

```bash
git clone <repo-url> cca-expert-knowledge-base
cd cca-expert-knowledge-base

python3 skill/cca_kb.py catalog --text
python3 skill/cca_kb.py search "高 BDP、随机丢包路径上如何避免误判拥塞" --top-k 3
python3 skill/cca_kb.py get exp-001
python3 skill/cca_kb.py related exp-001
python3 skill/cca_kb.py source src-004
```

`skill/` 里只有：

```text
skill/
├── SKILL.md        # 给 LLM/Agent 的使用说明
├── knowledge.json  # 生成好的知识快照
└── cca_kb.py       # 仅标准库的 BM25 + 双语词表检索
```

### 接入 OpenAI 提示词流程

在调用 OpenAI 之前运行一次检索，把 JSON 注入 system prompt：

```python
import json
import subprocess
import sys

SKILL = "/path/to/cca-expert-knowledge-base/skill/cca_kb.py"


def retrieve(question: str, top_k: int = 3) -> dict:
    result = subprocess.run(
        [sys.executable, SKILL, "search", question, "--top-k", str(top_k)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


question = "设计一个在随机丢包和高 BDP 路径上稳定的 CCA"
context = retrieve(question)
messages = [
    {
        "role": "system",
        "content": (
            "你是 CCA 设计助手。先参考下面的专家经验，引用时写出 exp-ID、"
            "source_id 和 locator；inference 必须标注为推论。\n"
            + json.dumps(context, ensure_ascii=False, indent=2)
        ),
    },
    {"role": "user", "content": question},
]
```

更完整的接入说明、过滤参数和回答约束见
[`skill/SKILL.md`](skill/SKILL.md)。

### 纯提示词模式

如果不能运行脚本，使用静态知识包：

- `context_pack/catalog.md`：目录与摘要，先让模型挑选相关 `exp-ID`。
- `context_pack/design_experience_pack.md`：完整经验包，直接放入 system prompt。

说明见 [`context_pack/README.md`](context_pack/README.md)。

## 2. 修改知识后重新生成

`knowledge/` 下的 YAML 是唯一事实源。修改后需要重新生成
`skill/knowledge.json` 与 `context_pack/`：

```bash
python3 tools/export_skill.py
```

这个生成步骤需要项目开发环境（会用到 PyYAML、Pydantic 等）。可以使用下面的
Docker 容器，也可以在一台安装了开发依赖的机器上运行：

```bash
pip install -r requirements.txt
python3 tools/export_skill.py
```

## 3. 可选：容器开发与语义检索

这一层**只用于开发、校验、跑测试或实验语义检索**，不是使用知识库的必要条件。

### 构建镜像

```bash
cd /home/cll/cca-expert-knowledge-base
docker build -f docker/Dockerfile -t cca-kb:dev .
```

镜像安装 CPU 版 PyTorch 与 `sentence-transformers`，并内置
`intfloat/multilingual-e5-small`，因此约 2 GB。`HF_ENDPOINT` 默认使用
`hf-mirror.com`；`--build-arg EMBEDDING_MODEL=...` 可换模型。

### 启动容器

```bash
docker run -d \
  --name cca-kb \
  --workdir /app \
  -v /home/cll/cca-expert-knowledge-base:/app \
  -e CCA_KB_ROOT=/app \
  -e CCA_KB_EMBEDDER=sentence_transformers \
  -e HF_ENDPOINT=https://hf-mirror.com \
  -e HF_HOME=/opt/hf-cache \
  cca-kb:dev sleep infinity
```

### 校验与测试

```bash
docker exec cca-kb bash -lc '
  cd /app &&
  cca-kb validate --strict &&
  cca-kb quality &&
  pytest -q
'
```

### 语义检索（可选）

```bash
docker exec cca-kb cca-kb search "bandwidth estimation instead of loss" --top-k 3
docker exec cca-kb cca-kb search "随机丢包 拥塞控制" --top-k 3
docker exec cca-kb cca-kb search "ECN" --json --status cross_source_synthesis
```

Interactive shell:

```bash
docker exec -it cca-kb bash
```

Stop and remove:

```bash
docker stop cca-kb && docker rm cca-kb
```

## 4. 环境变量（仅完整开发栈）

| Variable | Default | Meaning |
| --- | --- | --- |
| `CCA_KB_ROOT` | detected | Project root containing `knowledge/` and `schemas/` |
| `CCA_KB_EMBEDDER` | `auto` | `auto`, `hashing`, or `sentence_transformers` |
| `CCA_KB_MODEL` | `intfloat/multilingual-e5-small` | sentence-transformers model |
| `CCA_KB_KEYWORD_WEIGHT` | `0.55` | BM25 weight in the hybrid score |
| `CCA_KB_SEMANTIC_WEIGHT` | `0.45` | vector-similarity weight |
| `HF_ENDPOINT` / `HF_HOME` | `hf-mirror.com`, `/opt/hf-cache` | baked into the image; override at `docker run` if needed |

零依赖 skill 不受这些变量影响。

## 5. 排错

- **`python3 skill/cca_kb.py` 报 `knowledge bundle not found`** — 确认
  `skill/knowledge.json` 存在；缺失时运行 `python3 tools/export_skill.py`。
- **修改 YAML 后 skill 没变化** — 生成物不会自动更新，重新运行
  `python3 tools/export_skill.py`。
- **`cca-kb validate` 失败** — 加载器是 fail-closed；根据错误码
  （`SCHEMA-*`、`SRC-MISSING`、`REL-MISSING`、`DUP-*`）修复记录，不会提供部分知识。
- **模型下载失败** — 只在可选容器层出现；设置可访问的 `HF_ENDPOINT` 后重建镜像。
