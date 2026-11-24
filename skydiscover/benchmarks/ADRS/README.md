# ADRS: AI-Driven Research for Systems

This directory contains the systems optimization benchmarks from the **AI-Driven Research for Systems (ADRS)** initiative at UC Berkeley.

ADRS investigates how AI — large language models, evolutionary algorithms, and multi-agent architectures — can autonomously design, optimize, and evaluate computer systems. Instead of treating systems research as a purely manual process, ADRS frames it as a closed-loop optimization problem: propose candidate algorithms, evaluate them against system-level objectives, analyze failure modes, adapt the search strategy, and iterate.

Each benchmark below defines a concrete systems task with a provided evaluator, initial program, and configuration. Solutions are evolved using SkyDiscover's evolutionary search loop.

## Benchmarks

### Cloudcast — Multi-Cloud Data Transfer

**Directory:** `cloudcast/`

Given a network of cloud regions with heterogeneous egress pricing and bandwidth, broadcast a dataset from a source region to multiple destinations at minimum total cost. The evolved algorithm must construct routing topologies (e.g., relay trees, Steiner-like structures) that exploit shared intermediate hops across transfers.

### Expert Parallelism Load Balancer (EPLB)

**Directory:** `eplb/`

In Mixture-of-Experts (MoE) model inference, a small subset of experts handles each token, leading to GPU load imbalance when certain experts become disproportionately popular. This task evolves an algorithm that decides how many replicas each expert should have and how to assign them across GPUs, optimizing both load-balance quality and rebalancing runtime.

### Model Placement (Prism)

**Directory:** `prism/`

Assign multiple LLM models to a fixed GPU cluster (80 GB per GPU) such that the worst-case KV-cache pressure ratio across GPUs is minimized. Lower pressure means more memory headroom for serving, improving throughput and stability under varying request loads.

### LLM-SQL — Column Reordering for Prefix Caching

**Directory:** `llm_sql/`

When rows of a table are serialized into LLM prompts sequentially, consecutive rows that share leading column values can reuse cached prefixes. This task evolves a column-reordering strategy that maximizes prefix-cache hit rates across multiple real-world datasets without altering the underlying data.

### Transaction Scheduling (TXN)

**Directory:** `txn_scheduling/`

Given a set of database transactions with read/write dependencies on shared keys, find an execution ordering that minimizes the total makespan. The evolved scheduler must respect conflict constraints (read-write and write-write on the same key) while compressing the overall completion time.

### Telemetry Repair

**Coming soon.** The Telemetry Repair benchmark is under active development and will be released in a future update. 

## Quick Start

Each benchmark directory contains:
- `initial_program.py` — the seed solution for evolution
- `evaluator.py` — the scoring function
- `config.yaml` — run configuration

Run any benchmark from the repo root:

```bash
uv run skydiscover optimize \
  benchmarks/ADRS/cloudcast/initial_program.py \
  benchmarks/ADRS/cloudcast/evaluator.py \
  -c benchmarks/ADRS/cloudcast/config.yaml \
  -s [your_algorithm] \
  -i 100
```

See the individual benchmark directories for task-specific setup instructions (e.g., dataset downloads, GPU dependencies).

# ADRS：面向系统的 AI 驱动研究

本目录包含来自 UC Berkeley **AI 驱动系统研究（AI-Driven Research for Systems，ADRS）** 计划的系统优化基准。

ADRS 研究 AI —— 大语言模型、进化算法和多智能体架构 —— 如何自主设计、优化和评估计算机系统。ADRS 不把系统研究当作纯手工过程，而是把它构建为一个闭环优化问题：提出候选算法、根据系统级目标评估它们、分析失败模式、调整搜索策略、然后迭代。

下面每个基准都定义了一个具体的系统任务，并提供了评估器、初始程序和配置。解通过 SkyDiscover 的进化搜索循环进行进化。

## 基准

### Cloudcast —— 多云数据传输

**目录：** `cloudcast/`

给定一个由异构出口定价和带宽的云区域组成的网络，将数据集从源区域广播到多个目的地，使总成本最小。进化出的算法必须构造路由拓扑（例如中继树、类 Steiner 结构），利用传输之间共享的中间跳。

### 专家并行负载均衡器（EPLB）

**目录：** `eplb/`

在混合专家（MoE）模型推理中，每个 token 由一小部分专家处理，当某些专家变得特别热门时，会导致 GPU 负载不均衡。该任务进化一个算法，决定每个专家应该有多少副本以及如何在 GPU 之间分配它们，同时优化负载均衡质量和再均衡运行时间。

### 模型放置（Prism）

**目录：** `prism/`

将多个 LLM 模型分配到固定的 GPU 集群（每 GPU 80 GB），使各 GPU 上最坏情况的 KV 缓存压力比最小。压力越低，意味着服务时可用的内存余量越多，从而在不同请求负载下提高吞吐和稳定性。

### LLM-SQL —— 用于前缀缓存的列重排序

**目录：** `llm_sql/`

当表的行被顺序序列化为 LLM 提示时，共享前导列值的连续行可以复用缓存前缀。该任务进化一种列重排序策略，在不改变底层数据的前提下，最大化多个真实数据集上的前缀缓存命中率。

### 事务调度（TXN）

**目录：** `txn_scheduling/`

给定一组对共享键有读/写依赖的数据库事务，找到一个执行顺序，使总完工时间最小。进化出的调度器必须遵守冲突约束（同一键上的读-写和写-写），同时压缩整体完成时间。

### 遥测修复

**即将推出。** 遥测修复基准正在积极开发中，将在未来更新中发布。

## 快速开始

每个基准目录包含：
- `initial_program.py` —— 进化的种子解
- `evaluator.py` —— 评分函数
- `config.yaml` —— 运行配置

从仓库根目录运行任意基准：

```bash
uv run skydiscover optimize \
  benchmarks/ADRS/cloudcast/initial_program.py \
  benchmarks/ADRS/cloudcast/evaluator.py \
  -c benchmarks/ADRS/cloudcast/config.yaml \
  -s [your_algorithm] \
  -i 100

用 SkyDiscover 的 Optimize 功能，跑 Cloudcast 这个基准任务，迭代 100 轮。

1. 把 [your_algorithm] 替换成具体算法
可选的搜索算法（来自 optimize --help）：choose from 'evox', 'adaevolve', 'best_of_n', 'beam_search', 'topk', 'openevolve_native', 'openevolve', 'shinkaevolve', 'gepa', 'gepa_native', 'alphaevolve', 'claude_code'
adaevolve（推荐，解决你的停滞问题）
uv run skydiscover optimize \
  benchmarks/ADRS/cloudcast/initial_program.py \
  benchmarks/ADRS/cloudcast/evaluator/evaluator.py \
  -c benchmarks/ADRS/cloudcast/config.yaml \
  -s adaevolve \
  -i 100
2. 确认路径正确
你需要在 SkyDiscover 仓库根目录下运行，也就是 /workspace/skydiscover/