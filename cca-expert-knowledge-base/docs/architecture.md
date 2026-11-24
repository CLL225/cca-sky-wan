# 架构说明 / Architecture

## 1. 定位

本系统交付 **TCP/QUIC 拥塞控制专家设计经验知识库**，不是算法生成、仿真或评估系统。
系统边界严格遵循 README 第 1.2 节：

- 做：资料整理、设计经验结构化、证据追溯、知识检索。
- 不做：生成/进化算法、运行仿真、评估性能、编排算法设计 Agent。

所有代码与数据都服从一个原则：**知识记录比检索基础设施更重要**。因此第一版选择
可读、可版本管理的 YAML，而不是数据库或图存储。

## 2. 三层结构

```text
┌──────────────────────────────────────────────────────────────┐
│ sources/                   原始资料层（仅元数据/定位）          │
│   papers/ rfcs/ code/ reports/                               │
└──────────────────────────────────────────────────────────────┘
                              │ 人工/Agent 加工
                              ▼
┌──────────────────────────────────────────────────────────────┐
│ knowledge/                 结构化知识层（唯一事实源）           │
│   experiences/   exp-NNN.yaml                                │
│   sources/       src-NNN.yaml                                │
│   taxonomy/      taxonomy.yaml                               │
│   relations/     relations.yaml                              │
└──────────────────────────────────────────────────────────────┘
                              │ 加载 + 校验 + 建索引
                              ▼
┌──────────────────────────────────────────────────────────────┐
│ src/cca_kb/                检索层（可替换基础设施）             │
│   validation.py  schema + integrity                          │
│   repository.py  YAML -> typed models                        │
│   bm25.py        keyword index                               │
│   embeddings.py  pluggable vector backend                    │
│   index.py       hybrid ranking + filters                    │
│   retrieval.py   LLM-facing structured results               │
│   api.py         stable public functions                     │
│   cli.py         operations inside the container             │
└──────────────────────────────────────────────────────────────┘
```

### 2.1 原始资料层

`sources/` 只保存元数据、摘要、合法获得的文本和定位信息。默认 **不复制受版权保护的
论文全文**。需要验证内容时，在容器内临时下载到 `/tmp`，提取事实与章节位置后丢弃。

### 2.2 结构化知识层

- 每条经验一个稳定 ID（`exp-NNN`），每个来源一个稳定 ID（`src-NNN`）。
- 经验与来源分开存储，通过 `source_id` 引用。
- 关系独立于记录正文，记为带类型的边（`extends`、`alternative_to` …）。
- 所有知识都有 `knowledge_status`，所有证据都有 `evidence_level`；二者是独立维度。
- 字段允许为空；不允许为了填满 Schema 编造内容。

### 2.3 检索层

- YAML 是唯一事实源，索引是派生数据，可随时重建。
- 关键词（BM25）与向量余弦相似度按权重融合，再加标题匹配加成。
- 元数据过滤在排序前执行。
- 检索结果携带来源定位、证据等级、知识状态和显式 `limitations`。
- 向量后端可替换：默认使用烘焙在镜像内的多语言 E5 模型；无模型时回退到
  确定性哈希向量，并在结果中声明其局限。

## 3. 模块职责

| 模块 | 职责 | 不负责 |
| --- | --- | --- |
| `config.py` | 路径、权重、环境变量 | 业务逻辑 |
| `models.py` | Pydantic 结构模型 | 跨记录校验 |
| `validation.py` | JSON Schema + 引用完整性 | 质量评分 |
| `repository.py` | 加载 YAML、组装仓库、统计 | 排名 |
| `text.py` | 分词、中英词表桥接、文档构造 | 语义理解 |
| `bm25.py` | 关键词打分 | 过滤 |
| `embeddings.py` | 可插拔向量后端 | 索引生命周期 |
| `index.py` | 混合打分、元数据过滤 | 结果格式化 |
| `retrieval.py` | 面向 LLM 的结构化结果、limitations | 数据加工 |
| `quality.py` | 质量门禁与完整度 | Schema 校验 |
| `api.py` | 稳定公共接口与缓存 | 内部实现细节 |
| `cli.py` | 容器内运维入口 | 领域知识 |

## 4. 数据流

```text
YAML files
  │  yaml.safe_load
  ▼
JSON Schema validation (schemas/*.json)
  │  pydantic model_validate
  ▼
Referential integrity (sources, relations, taxonomy)
  │  fail closed: 有错误则拒绝启动
  ▼
KnowledgeRepository
  │  text construction
  ▼
BM25 index + vector matrix
  │  query
  ▼
filters -> fusion -> top-k
  │
  ▼
Structured response with evidence + limitations
```

**Fail closed**：只要有任何错误，`KnowledgeRepository.load()` 抛出 `KnowledgeError`，
不会返回部分知识。质量不合格的条目可以留在分支或草稿目录，但不能被检索服务提供。

## 5. 分发形态：零依赖 skill 与可选完整栈

知识库的使用者通常不是维护者。为了让 GitHub clone 后立即可用，仓库提供两个
运行层：

| 层 | 入口 | 依赖 | 用途 |
| --- | --- | --- | --- |
| 便携 skill | `skill/cca_kb.py` + `skill/knowledge.json` | 仅 Python 标准库 | 检索后注入 OpenAI/任意 LLM 的 prompt |
| 静态知识包 | `context_pack/*.md` | 无 | 只能拼提示词时直接放入 system prompt |
| 完整开发栈 | `src/cca_kb/` | PyYAML、Pydantic、jsonschema、numpy | YAML 校验、质量门禁、混合/语义检索、测试 |

`skill/knowledge.json` 与 `context_pack/` 由 `tools/export_skill.py` 从
`knowledge/` 生成，**不是手工维护的第二份事实源**。便携 skill 使用 BM25 +
中英词表 + 元数据过滤；它明确声明 `semantic=false`，不假装是向量语义检索。
这对几十条精选经验足够，也避免了 2 GB 的 PyTorch 运行时。

完整开发栈的语义检索（E5）是可选增强，适合离线实验或需要跨语言语义匹配的场景。

## 6. 容器化

镜像基于 `python:3.11-slim`：

- 安装 CPU 版 PyTorch 与 `sentence-transformers`。
- 构建时从 `hf-mirror.com` 下载 `intfloat/multilingual-e5-small` 并烘焙进镜像，
  运行时无需外网。
- 项目目录通过 bind mount 挂载到 `/app`，YAML 修改立即生效。
- 所有校验、索引、检索和测试都在容器内运行。

好处是可复现、宿主环境零污染、模型与依赖版本固定。检索服务本身不依赖容器，
在任意 Python 3.10+ 环境中也可以 `pip install -e .` 后使用。

## 7. 明确不做的扩展

- 不引入图数据库：8 条经验和十几条关系用 YAML + 内存索引足够。
- 不自研向量数据库：`Embedder` 接口已经为 Qdrant/Chroma 留出替换点。
- 不引入 FastAPI/微服务：MVP 以 CLI 与 Python API 交付，任何 Agent 框架都可直接导入。
- 不做自动论文抓取流水线：资料获取是人工判断的一部分，避免把低质量来源自动入库。
