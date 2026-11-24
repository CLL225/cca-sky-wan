# CCA 设计经验目录

用于让 LLM 先判断哪些经验与当前设计问题相关，再按 ID 取全文。

## exp-001 — 基于瓶颈带宽与传播时延模型的拥塞控制（BBR）

- 类型: `mechanism_design`
- 知识状态: `source_reported`
- 标签: high-bdp, random-loss, bufferbloat, bandwidth-variation, bandwidth-estimation, min-rtt-estimation, pacing, rate-control, gain-control, state-machine, startup-exit, bbr, throughput, low-latency, production-trace, metrics-throughput-delay
- 摘要: BBR 作者认为，基于丢包的拥塞控制在建模层面有缺陷：它把丢包等同于拥塞， 而可用发送速率实际上由瓶颈带宽和传播时延决定。丢包可能来自随机链路错误、 缓冲区不足或突发，而不是瓶颈已经饱和。 模型驱动的速率与窗口控制：BtlBw 与 RTprop 估计、pacing rate、 cwnd 上限以及 Startup/Drain/ProbeBW/ProbeRTT 四状态机。

## exp-002 — 用时间驱动的三次窗口增长替代线性 AIMD（CUBIC）

- 类型: `mechanism_design`
- 知识状态: `cross_source_synthesis`
- 标签: high-bdp, congestion-loss, rtt-variation, cwnd-control, loss-response, multiplicative-decrease-factor, tcp-cubic, throughput, fairness, convergence, stability, control-oscillation, queue-buildup
- 摘要: 标准 AIMD 的线性增长在高速长距网络中不足；窗口增长应当与真实时间挂钩， 而不是与 RTT 次数挂钩，同时仍要保留与 Reno 竞争时的公平下界。 三次窗口增长函数 + Reno-friendly 区域 + 乘性减小因子 β + fast convergence。

## exp-003 — 从单比特 ECN 标记中恢复多比特拥塞程度（DCTCP）

- 类型: `mechanism_design`
- 知识状态: `cross_source_synthesis`
- 标签: datacenter-low-latency, incast, queue-buildup, shallow-buffer, ecn-handling, multi-bit-feedback, explicit-signal, cwnd-control, marking-threshold, smoothing-coefficient, dctcp, low-latency, throughput, queue-control
- 摘要: 标准 TCP 把拥塞标记当作二值信号，只反应“是否拥塞”，不反应“拥塞程度”。 在浅缓冲区、高线速的数据中心网络中，队列波动很快，二值反应会导致 窗口和队列大幅振荡，并浪费共享缓冲区。 基于 ECN 的多比特反馈：交换机按瞬时队列阈值标记 CE，接收方精确回传标记， 发送方维护标记比例 α 并按比例减小窗口。

## exp-004 — 以排队时延目标和速率反馈控制窗口（Copa）

- 类型: `mechanism_design`
- 知识状态: `source_reported`
- 标签: bufferbloat, queue-buildup, bandwidth-variation, rtt-variation, rtt-measurement, min-rtt-estimation, rate-control, cwnd-control, pacing, gain-control, copa, low-latency, fairness, coexistence
- 摘要: 现有算法或者只优化吞吐（填满队列），或者只优化时延而在竞争中吃亏。 Copa 从网络效用最大化目标出发，把吞吐和排队时延放进同一个目标函数， 并用目标速率与当前速率的比较来调整窗口。 基于排队时延的目标速率控制：用 RTT_standing 与 RTT_min 估计 d_q， 计算 λ_t = 1/(δ·d_q)，按 (λ - λ_t) 的符号调整 cwnd，并配合 velocity 参数、pacing 与 competitive mode。

## exp-005 — 加性增长/乘性减小（AIMD）作为拥塞控制基线

- 类型: `mechanism_design`
- 知识状态: `cross_source_synthesis`
- 标签: congestion-loss, random-loss, high-bdp, multi-flow-competition, loss-response, cwnd-control, tcp-reno, fairness, stability, convergence, throughput, control-oscillation, queue-buildup
- 摘要: 网络本身不向端主机报告可用带宽；发送方必须通过“增加负载直到出现拥塞信号， 再后退”的闭环来发现可用容量。丢包（或后来的 ECN）被用作拥塞信号。 慢启动 + 拥塞避免（AIMD）：每个 ACK 加性增加窗口，检测到丢包时乘性减小窗口； 配合快速重传/快速恢复减少等待超时。

## exp-006 — QUIC 的丢包检测、PTO 与持续拥塞判定

- 类型: `mechanism_design`
- 知识状态: `source_reported`
- 标签: path-change, rtt-variation, congestion-loss, burst-traffic, loss-detection, ack-based-recovery, packet-threshold, time-threshold, loss-response, persistent-congestion, quic, rtt-measurement, pacing, cwnd-control, robustness
- 摘要: TCP 的序号与重传机制使丢失检测存在歧义；RTO 与 TLP 等机制分散且难以 精确描述。QUIC 使用单调递增的包号和 ACK 区间，使丢失检测语义更清晰， 并需要一套明确的 PTO 与持续拥塞规则来驱动恢复和拥塞控制。 基于 ACK 的丢失检测（包阈值与时间阈值）+ Probe Timeout（PTO）+ 持续拥塞判定； 默认拥塞控制器为 NewReno 类，慢启动/恢复/拥塞避免状态明确，并建议 pacing。

## exp-007 — 问题分析：为什么“丢包即拥塞”的假设会误导设计

- 类型: `problem_analysis`
- 知识状态: `cross_source_synthesis`
- 标签: random-loss, bufferbloat, high-bdp, shallow-buffer, congestion-loss, limitation-of-existing, assumption-limits, symptom-cause, key-signal, goal-conflict, throughput-vs-latency, utilization-vs-fairness
- 摘要: 丢包是一个滞后、二值且原因含混的信号。它隐含地假设“丢包几乎总是由 瓶颈拥塞引起”，但该假设在随机丢包、深缓冲区、高 BDP 和浅缓冲区等 环境中并不成立。信号与真实拥塞程度之间的偏差，是不同算法设计分歧的根源。 本条经验不主张单一机制，而是给出信号选择框架：按部署环境评估丢包、 ECN、排队时延、带宽/RTT 模型各自的假设、提前量和可观测性，再选择或组合信号。

## exp-008 — 用 Pacing 把窗口预算摊平到时间轴上

- 类型: `mechanism_design`
- 知识状态: `cross_source_synthesis`
- 标签: burst-traffic, shallow-buffer, queue-buildup, datacenter-low-latency, pacing, rate-control, low-overhead, parameter-coupling, pacing-rate, bbr, quic, low-latency, throughput
- 摘要: 窗口式拥塞控制只限制“在途字节数”，不限制“发送时刻”。当应用或协议栈 一次把整个窗口交给网络时，即使平均速率合适，瞬时突发也可能超过路径 可吸收能力，造成短期拥塞、丢包和队列振荡。 发送节拍器（pacer）：按拥塞控制器给出的速率计算包间隔， 使用漏桶/时间片调度发送；限制瞬时突发（如不超过初始窗口）， 并与 cwnd 检查协同。

