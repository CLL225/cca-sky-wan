---
name: cca-design-experience
description: Use before designing, comparing, or debugging a TCP/QUIC congestion-control algorithm. Retrieves traceable expert design experiences (problem, motivation, mechanism, tradeoffs, failure modes, applicability, source locators) so the model can ground a new CCA design in existing expert reasoning instead of guessing.
---

# CCA 专家设计经验知识库

这个 skill 提供 TCP/QUIC 拥塞控制（CCA）领域的**专家设计经验**，不是算法生成器，
也不是论文摘要库。它回答的是：

- 专家遇到过什么问题，如何分析；
- 为什么选择某种机制，考虑过哪些替代方案；
- 设计的收益、代价、失败模式和适用条件；
- 每条结论对应哪篇论文/RFC/源码的哪个位置。

**在生成新的 CCA 设计之前先检索。** 不要把本知识库当作事实数据库直接背诵；
把它当作“专家案例参考”，并保留 `knowledge_status` 与证据定位。

## 何时使用

- 用户要求“设计一个新的拥塞控制算法/机制/参数策略”；
- 需要在 Reno、CUBIC、BBT、DCTCP、Copa、AIMD、pacing、QUIC 丢失检测之间比较；
- 需要分析随机丢包、bufferbloat、高 BDP、浅缓冲区、Incast 等场景；
- 需要解释某个设计的动机、代价或失效条件。

## 怎么调用

目录结构：

```text
skill/
├── SKILL.md
├── knowledge.json   # 生成好的知识快照
└── cca_kb.py        # 零依赖检索脚本（仅标准库）
```

所有命令都只需要 `python3`，不需要 `pip install`、Docker 或模型下载。

```bash
# 1. 不确定有哪些经验时，先看目录
python3 skill/cca_kb.py catalog --text

# 2. 用设计问题检索（默认输出 JSON，适合直接注入 prompt）
python3 skill/cca_kb.py search "高 BDP、随机丢包路径上如何避免把非拥塞丢包当成拥塞" --top-k 3

# 3. 取某条经验的全文（问题、动机、机制、权衡、参数、证据）
python3 skill/cca_kb.py get exp-001

# 4. 跟随带类型的关系
python3 skill/cca_kb.py related exp-001

# 5. 查证据来源
python3 skill/cca_kb.py source src-004
```

常用过滤：

```bash
--status source_reported           # 只看原作者明确结论
--status cross_source_synthesis    # 只看跨来源归纳
--type mechanism_design            # 按经验类型
--tag bufferbloat                  # 任一标签匹配
--source src-008                   # 只查引用了某来源的经验
--min-evidence A                   # 最低证据等级
--has-failure-modes                # 只查记录了失败模式的
```

## 接入 OpenAI 提示词流程（推荐）

在调用 OpenAI 之前先检索，把返回的 JSON 作为上下文注入。示例：

```python
import json
import subprocess
import sys

SKILL = "/path/to/repo/skill/cca_kb.py"


def retrieve(question: str, top_k: int = 3) -> dict:
    result = subprocess.run(
        [sys.executable, SKILL, "search", question, "--top-k", str(top_k)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def build_messages(question: str) -> list[dict]:
    context = retrieve(question)
    system = (
        "你是 TCP/QUIC 拥塞控制设计助手。回答前先参考下面的专家设计经验。\n"
        "规则：\n"
        "1. 引用经验时写出 exp-ID，并说明借鉴了哪个问题/动机/机制。\n"
        "2. knowledge_status=source_reported 是原作者结论；"
        "cross_source_synthesis 是知识库归纳；inference 是推论，不得当作原文事实。\n"
        "3. 给出设计时引用证据的 source_id 与 locator。\n"
        "4. 如果经验不适用，明确说明原因，不要强行套用。\n"
        "5. 如果检索结果为空或 limitations 提示低相关度，先说明知识库没有直接经验。\n\n"
        f"检索结果：\n{json.dumps(context, ensure_ascii=False, indent=2)}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]
```

如果一次要问多个设计问题，对每个问题分别检索，或先用 `catalog` 让模型挑选
`exp-ID`，再 `get` 取全文。

## 纯提示词模式（不能运行脚本时）

使用仓库里的静态知识包：

- `context_pack/catalog.md`：8 条经验的目录和摘要，适合先让模型选择。
- `context_pack/design_experience_pack.md`：完整经验包，直接放进 system prompt。

知识包越大，越应该先检索再注入；全量注入只适合条目较少或做基线对比。

## 回答时必须保留的区分

| 字段 | 含义 | 使用方式 |
| --- | --- | --- |
| `knowledge_status: source_reported` | 原作者在来源中明确表达的结论 | 可表述为“论文/RFC 指出” |
| `knowledge_status: cross_source_synthesis` | 知识库跨多个来源的归纳 | 表述为“综合这些来源可以看出” |
| `knowledge_status: inference` | 知识库基于已有知识做出的推论 | 必须标注为推论，不得当作原文 |
| `evidence_level: A` | 直接、可定位的原始证据 | 可引用具体章节/代码位置 |
| `evidence_level: B/C/D` | 间接、有限或缺少验证 | 降低断言强度，说明不确定性 |
| `limitations` | 检索结果的警告 | 必须阅读，不能忽略低相关度提示 |

## 设计工作流

1. **检索问题类型**：用“网络条件 + 症状 + 目标”描述问题，而不是只写算法名。
2. **读 top 1–3 条**：先用 `search` 的摘要筛选，再 `get` 取需要的全文。
3. **提取设计推理**：问题分析 → 动机 → 候选机制 → 选择理由 → 权衡 → 失败模式。
4. **检查适用性**：对照 `suitable_conditions` / `unsuitable_conditions` / `assumptions`。
5. **写新设计**：明确借用了哪些经验、哪些地方不同、为什么。
6. **标证据**：每条关键论断附 `exp-ID` 和来源 locator；没有证据的地方写明“需要验证”。

## 维护

- `knowledge/` 下的 YAML 是唯一事实源；`skill/knowledge.json` 和
  `context_pack/` 是生成物，不要手工编辑。
- 修改 YAML 后，在项目根目录运行：

```bash
python3 tools/export_skill.py
```

- 然后运行 `cca-kb validate --strict && pytest -q`（需要项目开发环境）或
  至少运行 `python3 skill/cca_kb.py stats` 做冒烟检查。
