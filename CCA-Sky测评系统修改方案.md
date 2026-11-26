# CCA-Sky 测评系统修改方案

仅修改网络模拟、应用负载、评分函数、公平性、收敛性和实验管理模块。保留 TQUIC 拥塞控制接口、SkyDiscover/AdaEvolve 演化框架及九种独立演化任务。

## 1. 网络模拟环境

### 1.1 广域网（WAN）

涉及文件：`replay_wan.py`、`run_tc.sh`

| 修改内容 | 参考依据 |
|---|---|
| 将单一 netem 改为独立的带宽限制、瓶颈队列和传播时延模块 | [Google Transperf](https://github.com/google/transperf)、[QUICbench，IMC 2022](https://www.comp.nus.edu.sg/~bleong/publications/imc2022-quicbench.pdf) |
| 增加独立 Router namespace，避免端侧发送与网络模拟耦合 | Google Transperf |
| 双向配置传播时延，明确 RTT 为 75 ms 或 150 ms | QUICbench |
| 暂时取消 5 ms 随机 jitter，分别进行 jitter、loss、queue 消融实验，定位 BBR 吞吐波动 | [netem 官方文档](https://man7.org/linux/man-pages/man8/tc-netem.8.html) |
| 修复三个 profile 无法切换的问题 | 当前源码审查 |
| 增加不同带宽、RTT、丢包率、缓存大小及动态带宽场景 | [ccperf](https://ccperf.net/benchmarks) |

建议初始 WAN 测试场景：

| 场景 | 带宽 | RTT | 随机丢包 | 瓶颈缓存 |
|---|---:|---:|---:|---|
| 城际网络 | 100 Mbps | 40 ms | 0 | 1 BDP |
| 高速 WAN | 500 Mbps | 75 ms | 0 | 1 BDP |
| 跨洋网络 | 500 Mbps | 150 ms | 0 | 1 BDP |
| 随机丢包 | 500 Mbps | 150 ms | 0.1% | 1 BDP |
| 高丢包网络 | 50 Mbps | 100 ms | 2% | 1 BDP |
| 浅缓存 | 500 Mbps | 150 ms | 0 | 0.5 BDP |
| 深缓存 | 500 Mbps | 150 ms | 0 | 4 BDP |
| 动态带宽 | 100–500 Mbps | 150 ms | 0 | 固定容量 |

以上为建议的初始配置，并非论文规定的标准参数。

### 1.2 蜂窝网（Cellular）

涉及文件：`run_evaluation.sh`、trace 转换脚本。

| 修改内容 | 参考依据 |
|---|---|
| 保留 Mahimahi 网络模拟方式 | [Mahimahi，USENIX ATC 2015](https://github.com/ravinet/mahimahi) |
| 使用独立的上下行 trace；缺少数据时明确标注为合成对称模型 | [Pantheon，USENIX ATC 2018](https://github.com/StanfordSNR/pantheon) |
| 将固定双向 1% 丢包改为可配置参数 | Pantheon |
| 明确瓶颈队列类型和容量，增加不同 BDP 缓存配置 | [微软 CC Optimization，2025](https://arxiv.org/html/2508.16074v1) |
| 增加静止、步行、车载等不同网络轨迹 | [5Gophers 公开数据集](https://networking.umn.edu/5gophers) |
| 支持不同 trace 和重放起始位置 | 微软 CC Optimization |

### 1.3 卫星网（Satellite）

涉及文件：`replay_lens.py`、`run_tc.sh`

| 修改内容 | 参考依据 |
|---|---|
| 修复单向注入 RTT/2 导致实际 RTT 减半的问题 | 当前源码审查 |
| 保留动态 HTB + netem，分离瓶颈缓存与传播时延队列 | Google Transperf |
| 校准带宽、RTT 的动态更新时间及实际网络行为 | 当前源码审查 |
| 区分低带宽与真实链路断连 | [StarQUIC，2024](https://onlineacademiccommunity.uvic.ca/starlink/wp-content/uploads/sites/8876/2024/09/leonet24-victor.pdf) |
| 增加不同 trace 和重放起始位置 | [LENS 公开数据集](https://github.com/clarkzjw/LENS) |
| 增加周期性重配置、容量突降、RTT 尖峰和断连恢复场景 | StarQUIC、[WetLinks 数据集论文](https://tma.ifip.org/2024/wp-content/uploads/sites/13/2024/05/tma2024-final10.pdf) |

## 2. 应用负载

涉及文件：`CCA5/tquic/iperf/src/main.rs`

| 应用 | 修改内容 | 参考依据 |
|---|---|---|
| Bulk | 保留持续饱和发送模式 | Pantheon |
| Bulk | 使用接收端有效吞吐量 Goodput，增加固定大小文件传输测试 | QUICbench |
| RPC | 保留原有 Bursty 模式，新增真实请求—响应模式 | [Microsoft SecNetPerf](https://github.com/microsoft/msquic/blob/main/src/perf/readme.md) |
| RPC | 参考微软采用 512 B 请求、4 KB 响应，增加不同并发度 | [微软 CC Optimization](https://arxiv.org/html/2508.16074v1) |
| RPC | 统计 RPS、P50/P95/P99 请求完成时延、超时率 | Microsoft SecNetPerf |
| Video | 保留周期性帧生成，增加不同视频码率和背景竞争流量 | [RFC 8593](https://www.rfc-editor.org/rfc/rfc8593)、[RFC 8867](https://www.rfc-editor.org/rfc/rfc8867) |
| Video | 统计帧级延迟、按时交付率、冻结时间等 QoE 指标 | [RFC 8868](https://www.rfc-editor.org/rfc/rfc8868) |
| 公共 | 固定随机种子，将应用数据生成与 writable callback 解耦，保证不同 CC 面对可比较的负载 | 当前源码审查 |

新增真实 RPC 后，需要同时配置双向网络环境，并明确客户端和服务端使用的 CC。

## 3. 评分函数

涉及文件：`CCA5/cca_generate/mutate_algorithm.py`、`skydiscover/benchmarks/CCA5/evaluator/evaluator.py`。

保留三套应用评分函数，但修改其核心评价指标。

| 指标 | Bulk | RPC | Video |
|---|---|---|---|
| Goodput | 核心 | 辅助 | 辅助 |
| P95 排队时延 | 核心 | 辅助 | 辅助 |
| 重传开销 | 重要 | 辅助 | 辅助 |
| RPS | — | 核心 | — |
| P99 请求完成时延 | — | 核心 | — |
| 请求失败率 | — | 核心 | — |
| 按时帧交付率 | — | — | 核心 |
| 冻结时间 | — | — | 核心 |
| 帧级延迟与抖动 | — | — | 核心 |

| 修改内容 | 参考依据 |
|---|---|
| 删除 RPC 中基于固定 20 秒 duration 的完成度奖励 | 当前源码审查 |
| 将 Video 已有的 QoE 指标真正纳入评分 | RFC 8868 |
| 使用完整时序统计代替结束时的 SRTT、RTTvar | QUICbench |
| 动态网络使用随时间变化的基础 RTT 估计排队时延 | 当前源码审查 |
| 建立固定版本的基线参考值，避免缓存刷新导致评分漂移 | 微软 CC Optimization 的相对基线评价方法 |
| 候选算法与基线采用相同评分口径 | [RFC 5166](https://www.rfc-editor.org/rfc/rfc5166) |
| 修复部分场景测试失败仍能获得高分的问题 | 当前源码审查 |
| 修复收敛性日志完全缺失时不扣分的问题 | 当前源码审查 |

## 4. 公平性测试

涉及文件：`run_fairness.sh`、`run_fairness_suite.sh`

| 修改内容 | 参考依据 |
|---|---|
| 使用两条饱和 Bulk 流测试 CC 自公平性 | Pantheon |
| 测试候选 CC 与 BBR、CUBIC、Copa 的竞争 | Google Transperf |
| 增加不同 RTT 流竞争、不同启动时间流竞争 | ccperf |
| 增加 RPC/Video 与 Bulk 背景流的共存测试 | RFC 8867 |
| 低负载应用不再强制获得相同带宽 | 应用负载语义 |
| 保留统一公平性测试，并增加目标网络下的公平性测试 | ccperf |

## 5. 收敛性测试

涉及文件：`analyze_convergence.py`

| 修改内容 | 参考依据 |
|---|---|
| 静态网络使用预设有效容量目标，避免低吞吐算法更容易达到自身稳态 | Google Transperf |
| 动态网络增加带宽跟踪误差 | ccperf |
| 增加带宽突增、突降后的响应时间 | ccperf |
| 卫星网络增加断连后的恢复时间 | StarQUIC |
| RPC 和 Video 不再直接使用饱和 Bulk 的吞吐收敛标准 | RFC 8868 |
| 将连续三个采样点改为明确的时间窗口 | 当前源码审查 |

## 6. 实验可靠性与测评效率

涉及文件：`evaluator.py`、`run_tc.sh`、`run_evaluation.sh` 及网络重放脚本。

| 修改内容 | 参考依据 |
|---|---|
| 检查编译、tc 配置、客户端及服务端进程返回值 | 当前源码审查 |
| 增加实际带宽、RTT、丢包率及队列容量校准 | Pantheon 的 Calibrated Network Emulator |
| 基线重复测量，保存均值、标准差及性能分布 | [Pantheon 实验方法](https://pantheon.stanford.edu/faq/) |
| 保存随机种子、代码版本、网络配置及实验参数 | Pantheon |
| 保存吞吐、cwnd、pacing rate、带宽估计、丢包等时序数据 | Google Transperf |
| 保存 qdisc backlog、drop 等网络统计 | Google Transperf |
| 区分基础设施失败和算法自身失败 | 当前源码审查 |
| 划分演化集、验证集和独立测试集 | 防止测评过拟合 |
| 采用少量场景初筛、完整场景复核的多级测评 | 微软 CC Optimization |

## 7. 修改优先级

| 优先级 | 修改任务 |
|---|---|
| P0 | 修复 WAN 网络模拟及卫星 RTT 配置 |
| P0 | 定位 WAN-Bulk 中 BBR 吞吐波动 |
| P0 | 修复实验失败、日志缺失及评分口径问题 |
| P0 | 建立固定版本的基线性能库 |
| P1 | 重构 RPC、Video 应用负载及指标采集 |
| P1 | 修改三套应用评分函数 |
| P1 | 重构公平性与收敛性测试 |
| P2 | 扩充三类网络的公开数据集及测试场景 |
| P2 | 建立多级测评和统计筛选机制 |

## 8. 核心参考资料

| 论文或开源项目 | 主要借鉴内容 |
|---|---|
| [Microsoft — Congestion Control System Optimization with Large Language Models，2025](https://arxiv.org/html/2508.16074v1) | 多网络轨迹、应用测评、评分函数、统计筛选 |
| [Stanford — Pantheon，USENIX ATC 2018](https://github.com/StanfordSNR/pantheon) | 网络校准、重复实验、公开实验数据 |
| [Google — Transperf](https://github.com/google/transperf) | 网络模拟拓扑、BBR 测试、公平性及收敛性 |
| [QUICbench — Understanding Speciation in QUIC Congestion Control，IMC 2022](https://www.comp.nus.edu.sg/~bleong/publications/imc2022-quicbench.pdf) | QUIC 测评环境、吞吐与延迟分布 |
| [ccperf](https://ccperf.net/benchmarks) | 系统化 CC 测评场景分类 |
| [Microsoft — SecNetPerf](https://github.com/microsoft/msquic/blob/main/src/perf/readme.md) | RPC 负载及请求完成时延测评 |
| [Mahimahi，USENIX ATC 2015](https://github.com/ravinet/mahimahi) | 动态网络轨迹重放 |
| [IETF RFC 5166](https://www.rfc-editor.org/rfc/rfc5166) | 拥塞控制评价指标 |
| [IETF RFC 8867](https://www.rfc-editor.org/rfc/rfc8867)、[RFC 8868](https://www.rfc-editor.org/rfc/rfc8868) | 实时视频拥塞控制测试与评价 |

**本次改造仅限测评系统，不涉及 Prompt、Agent、Skills、Wiki、搜索策略及 RSI 演化机制。** 上述方案尚未实施，BBR 波动的具体根因、最终网络参数及评分权重需要通过校准实验确定。
