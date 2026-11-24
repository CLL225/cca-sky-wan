# 分类体系 / Taxonomy

Taxonomy 用于组织和检索，不替代数据 Schema。一条经验可同时属于多个分类，
因此 `metadata.tags` 是列表，检索时支持“任一标签”（`--tag`）与“全部标签”
（`--all-tag`）两种过滤。

## 1. 分类

| 分类 ID | 名称 | 用途 |
| --- | --- | --- |
| `network_problem` | 网络问题与现象 | 高 BDP、随机丢包、bufferbloat、浅缓冲区、Incast 等 |
| `design_goal` | 设计目标与约束 | 吞吐、时延、公平性、收敛、稳定性等 |
| `problem_analysis` | 设计动机与问题分析 | 现有算法不足、假设局限、关键信号、目标冲突 |
| `mechanism` | 控制机制设计 | 带宽估计、ECN、pacing、窗口/速率控制、状态机等 |
| `algorithm` | 算法案例 | Reno、CUBIC、BBR、Copa、DCTCP、QUIC |
| `parameter` | 参数与控制策略 | 增益、阈值、周期、平滑系数、参数耦合 |
| `tradeoff` | 权衡与失败模式 | 吞吐 vs 时延、利用率 vs 公平性、振荡、参数敏感性 |
| `evaluation` | 评估方法与证据 | 仿真、模拟器、测试床、生产测量、分析模型 |

完整标签见 `knowledge/taxonomy/taxonomy.yaml`。

## 2. 标签使用规则

1. **标签是稳定标识符**：使用小写英文与连字符，例如 `high-bdp`、`min-rtt-estimation`。
   `label` 是中文/英文展示名，可以修改；`id` 不应随意改名。
2. **一条经验建议 8–15 个标签**：覆盖问题、机制、算法、目标、权衡，但避免堆砌。
3. **不要用标签编码结论**：标签用于检索，结论写在字段正文。
4. **未知标签会产生警告**：新增标签必须先加入 taxonomy。
5. **算法名作为案例标签**：`bbr`、`tcp-cubic` 等用于按案例检索，不用于判断知识类型。

## 3. 检索示例

```bash
# 任一标签为 pacing 或 ecn-handling
cca-kb search "拥塞信号" --tag pacing --tag ecn-handling

# 同时具备 random-loss 与 bufferbloat
cca-kb search "丢包" --all-tag random-loss --all-tag bufferbloat

# 问题分析类
cca-kb search "为什么丢包信号不够" --type problem_analysis
```

## 4. 扩展

新增分类时：

1. 在 `taxonomy.yaml` 增加 category 与 tags（`id`、`label`、可选 `description`）。
2. 更新本文档的分类表。
3. 运行 `cca-kb validate` 与 `cca-kb quality`，确认没有 `TAXONOMY-UNKNOWN` 警告。

不建议把所有细分概念都变成标签。只有当它确实能帮助检索或过滤时才新增。
