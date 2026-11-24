# TCP/QUIC 拥塞控制专家设计经验包

本文件由 `tools/export_skill.py` 从 `knowledge/` 生成，请勿手工编辑。

使用方式：在让 LLM 生成新的 CCA 之前，把本文件（或按问题检索出的相关段落）
放入 system prompt。要求模型：

1. 先引用相关的 `exp-NNN` 经验，说明借鉴了哪个问题、动机或机制。
2. 区分 `knowledge_status`：`source_reported` 是原作者结论，
   `cross_source_synthesis` 是知识库归纳，`inference` 是推论，不得当作原文事实。
3. 给出设计时引用证据的 `source_id` 与 locator。
4. 如果经验不适用，明确说明原因，不要强行套用。

## 目录

- `exp-001` 基于瓶颈带宽与传播时延模型的拥塞控制（BBR）（mechanism_design, source_reported）
- `exp-002` 用时间驱动的三次窗口增长替代线性 AIMD（CUBIC）（mechanism_design, cross_source_synthesis）
- `exp-003` 从单比特 ECN 标记中恢复多比特拥塞程度（DCTCP）（mechanism_design, cross_source_synthesis）
- `exp-004` 以排队时延目标和速率反馈控制窗口（Copa）（mechanism_design, source_reported）
- `exp-005` 加性增长/乘性减小（AIMD）作为拥塞控制基线（mechanism_design, cross_source_synthesis）
- `exp-006` QUIC 的丢包检测、PTO 与持续拥塞判定（mechanism_design, source_reported）
- `exp-007` 问题分析：为什么“丢包即拥塞”的假设会误导设计（problem_analysis, cross_source_synthesis）
- `exp-008` 用 Pacing 把窗口预算摊平到时间轴上（mechanism_design, cross_source_synthesis）

---

## exp-001 — 基于瓶颈带宽与传播时延模型的拥塞控制（BBR）

- 类型: `mechanism_design`
- 知识状态: `source_reported`（source_reported=原作者明确结论；cross_source_synthesis=跨来源归纳；inference=知识库推论）
- 标签: high-bdp, random-loss, bufferbloat, bandwidth-variation, bandwidth-estimation, min-rtt-estimation, pacing, rate-control, gain-control, state-machine, startup-exit, bbr, throughput, low-latency, production-trace, metrics-throughput-delay

### 问题背景
BBR 作者认为，基于丢包的拥塞控制在建模层面有缺陷：它把丢包等同于拥塞， 而可用发送速率实际上由瓶颈带宽和传播时延决定。丢包可能来自随机链路错误、 缓冲区不足或突发，而不是瓶颈已经饱和。

观察到的现象:
- 丢包触发的拥塞控制把非拥塞丢包当成拥塞信号，导致吞吐量骤降
- 在高 BDP 路径上，丢包式 AIMD 需要很多个 RTT 才能重新填满管道
- 队列长期处于接近满的状态，排队时延高（bufferbloat）
- 浅缓冲区路径上，少量突发即可造成丢包

### 设计动机与分析
让发送方稳定运行在“瓶颈带宽 × 传播时延”的 BDP 附近，在保持高吞吐的同时 避免把瓶颈队列填满；并且不要求网络侧支持 ECN 或 AQM。

问题分析:
论文用两个路径参数描述网络：瓶颈带宽 BtlBw 与往返传播时延 RTprop。 两者乘积给出在途数据量的最优值。只要持续估计这两个量，并让发送速率与 在途数据量跟随估计值，就可以在不依赖丢包信号的情况下接近最优工作点。

推理链:
- 丢包只在队列溢出或非拥塞原因下发生，因此丢包是滞后且含混的拥塞信号。
- 路径长期的最大交付速率趋近瓶颈带宽；最小 RTT 趋近传播时延。
- 用 ACK 交付速率样本估计 BtlBw，用最小 RTT 窗口估计 RTprop。
- 用 pacing 速率控制发包时刻，用 cwnd 限制在途字节数。
- 用 Startup/Drain/ProbeBW/ProbeRTT 状态机处理启动、排空、带宽探测和 RTT 刷新。

### 设计决策
模型驱动的速率与窗口控制：BtlBw 与 RTprop 估计、pacing rate、 cwnd 上限以及 Startup/Drain/ProbeBW/ProbeRTT 四状态机。

为什么这样选:
直接建模路径的 BDP，使发送方可以运行在瓶颈速率附近并维持较小队列； 同时不要求网络支持 ECN 或 AQM，可由发送端单边部署。

考虑过的替代方案:
- 基于丢包的 AIMD（Reno/CUBIC）：损失信号滞后，且会把随机丢包误判为拥塞。
- 纯时延/队列控制器：依赖队列时延信号，在缓冲区膨胀时难以区分传播时延与排队时延。

### 机制
BBR 在 ACK 上维护交付速率样本和 RTT 样本：BtlBw 取近期窗口内的最大交付速率， RTprop 取较长时间窗口（Linux 实现约 10 秒）内的最小 RTT。发送时以 pacing_rate = pacing_gain × BtlBw 控制发送间隔，以 cwnd = cwnd_gain × BtlBw × RTprop 限制在途数据。状态机包括 Startup （以高增益快速提升速率）、Drain（排空启动阶段形成的队列）、ProbeBW （周期性用不同增益探测带宽）和 ProbeRTT（周期性压低在途数据以刷新 RTprop）。

控制信号:
- ACK 交付速率样本（delivery rate）
- RTT 样本与最小 RTT
- 在途字节数 inflight
- 是否达到满带宽 full_bw_reached

控制动作:
- 设置 pacing 速率
- 设置拥塞窗口
- 状态机阶段切换
- ProbeRTT 期间限制在途数据

### 权衡
收益:
- 在高 BDP 路径上可以达到更高的吞吐量
- 在最优工作点附近运行时瓶颈队列较小，排队时延低于丢包式算法
- 不要求网络支持 ECN 或 AQM，便于端到端部署
- 把随机丢包与拥塞区分开，非拥塞丢包不会直接触发窗口减半

代价:
- 发送端需要具备 pacing 能力与较细粒度的发送调度
- 依赖 ACK 交付速率样本；ACK 聚合、压缩或反向路径拥塞会污染估计
- ProbeRTT 周期性排空队列，会带来短时的吞吐下降和时延尖峰
- Startup 阶段的高增益可能造成短时超发

限制:
- 模型假设瓶颈带宽与传播时延在估计窗口内近似可辨识
- ACK 时钟不可用或严重失真的路径上，交付速率估计的可靠性下降
- ProbeRTT 的测量结果有窗口有效期，路径快速变化时可能滞后

### 失败模式
- Startup 期间 pacing_gain 约为 2.885；若路径缓冲很小，快速提速率可能先造成丢包。
- ACK 聚合使交付速率样本失真，BtlBw 估计可能偏高或偏低。
- ProbeRTT 压低在途数据期间，吞吐暂时下降并出现时延尖峰。
- 路径频繁变化时，最小 RTT 窗口可能保留过期的 RTprop，直到 ProbeRTT 刷新。

### 适用条件
适用:
- 能够从 ACK 估计交付速率和 RTT 的路径
- 发送端可以 pacing 的传输实现
- 高 BDP、随机丢包或 bufferbloat 明显，且希望降低排队时延的场景

不适用:
- 无法获得有意义 ACK 交付速率的场景
- 路径变化极快且无法及时刷新 RTprop 的场景（需要额外验证）

假设:
- 瓶颈链路速率在估计窗口内近似稳定
- 传播时延存在可观测的下界
- ACK 能反映数据交付进度

### 参数指导
- high_gain: Startup 阶段使用约 2.885（2/ln2）的 pacing 增益，使每 RTT 带宽约翻倍。 (理由: 在启动阶段快速逼近 BtlBw，同时 Drain 阶段用其倒数排空形成的队列。; 来源: src-005)
- cwnd_gain: Linux BBR 使用 2 作为 cwnd 增益，允许最多约 2 BDP 的在途数据。 (理由: 为 ACK 聚合和路径波动留出余量，避免因在途不足而低估带宽。; 来源: src-005)
- probe_bw_cycle: ProbeBW 使用 8 个相位的增益序列（1.25、0.75、1.0 …），周期性地探测更高带宽。 (理由: 先轻微超发探测带宽，再回落到估计速率，避免长期填满队列。; 来源: src-004, src-005)
- probe_rtt_interval: Linux 实现约每 10 秒进入一次 ProbeRTT，持续约 200 ms，并限制在途数据。 (理由: 刷新 RTprop 的下界；若长期不刷新，最小 RTT 可能包含旧路径的排队时延。; 来源: src-005)
- min_rtt_window: RTprop 使用约 10 秒的滑动窗口最小值。 (理由: 窗口太短会受瞬时排队污染，太长则无法及时反映路径变化。; 来源: src-005)

### 评估
指标:
- 吞吐量
- 排队时延
- 丢包率
- 带宽利用率

预期效果:
- 在高 BDP 路径上接近瓶颈带宽
- 相比丢包式算法降低稳态排队时延
- 在存在随机丢包时避免吞吐量大幅下降

### 证据
- [A] src-004 — BBR: Congestion-Based Congestion Control; 定位: Modeling the Network Path; The BBR Algorithm; Startup; Drain; ProbeBW; ProbeRTT
- [A] src-005 — Linux TCP BBR implementation (net/ipv4/tcp_bbr.c); 定位: net/ipv4/tcp_bbr.c::bbr_update_bw / bbr_update_min_rtt / bbr_check_full_bw_reached / bbr_check_drain / bbr_update_cycle_phase / bbr_check_probe_rtt_done / bbr_set_pacing_rate / bbr_set_cwnd

### 关联经验
- exp-005 (alternative_to): BBR replaces loss-triggered AIMD with bandwidth/RTT model-based control.
- exp-004 (alternative_to, incoming): Copa uses queueing delay and a target-rate objective rather than a bandwidth/RTT model.
- exp-007 (synthesizes, incoming): The problem analysis draws on BBR's critique of loss as a congestion proxy.
- exp-008 (complements, incoming): BBR's rate control depends on pacing; pacing is the enforcement mechanism.


## exp-002 — 用时间驱动的三次窗口增长替代线性 AIMD（CUBIC）

- 类型: `mechanism_design`
- 知识状态: `cross_source_synthesis`（source_reported=原作者明确结论；cross_source_synthesis=跨来源归纳；inference=知识库推论）
- 标签: high-bdp, congestion-loss, rtt-variation, cwnd-control, loss-response, multiplicative-decrease-factor, tcp-cubic, throughput, fairness, convergence, stability, control-oscillation, queue-buildup

### 问题背景
标准 AIMD 的线性增长在高速长距网络中不足；窗口增长应当与真实时间挂钩， 而不是与 RTT 次数挂钩，同时仍要保留与 Reno 竞争时的公平下界。

观察到的现象:
- Reno 的窗口每个 RTT 只增加约一个 MSS，高 BDP 路径上恢复和探测带宽极慢
- 窗口增长率以 RTT 次数计量，RTT 不同的流获得不同的增长机会
- 丢包后窗口减半，吞吐呈锯齿状，利用率不稳定

### 设计动机与分析
提高高速长距网络上的吞吐量和带宽利用率，同时保持与 Reno 的兼容性， 并改善不同 RTT 流之间的公平性。

问题分析:
CUBIC 用自上次拥塞事件以来的时间 t 构造窗口目标 W_cubic(t) = C · (t - K)^3 + W_max，其中 K 是再次达到 W_max 所需时间。 由于增长由真实时间驱动，RTT 不同的流在相同时间内获得更可比的窗口增长； 三次曲线在接近 W_max 时增长放缓，在超过 W_max 后重新加速探测新带宽。

推理链:
- Reno 在高 BDP 下窗口增长太慢，需要更激进的增长函数。
- 使用时间的三次函数：接近上次饱和点时增长慢，远离时增长快。
- 单纯激进会损害与 Reno 的公平性，因此保留 TCP-friendly 区域。
- 新流需要更快获得带宽，因此加入 fast convergence 机制。

### 设计决策
三次窗口增长函数 + Reno-friendly 区域 + 乘性减小因子 β + fast convergence。

为什么这样选:
三次函数在靠近上次饱和点时降低增长速率，减少丢包；远离饱和点时加快增长， 便于利用空闲带宽。Reno-friendly 区域保证在窗口较小时不劣于标准 TCP。

考虑过的替代方案:
- Reno AIMD：高 BDP 下增长过慢。
- BIC-TCP：CUBIC 的直接前身，使用二分搜索式增长，CUBIC 将其简化为三次函数。
- HSTCP/STCP 等高带宽算法：改变 AIMD 参数或使用不同增长律。

### 机制
CUBIC 维护 W_max（上次拥塞事件前的窗口）、epoch 起始时间和 K。 收到新 ACK 时计算 target = W_cubic(t + RTT)。若当前 cwnd 低于 Reno 在同一时刻应达到的窗口 W_est，则进入 Reno-friendly 区域，按等价 AIMD 增长；否则根据 cwnd 与 W_max 的关系处于凹区（cwnd < W_max）或凸区 （cwnd > W_max），按三次函数更新窗口。检测到丢包或 ECN 时，W_max 更新 （启用 fast convergence 时进一步下调），cwnd 乘以 β。论文使用 C=0.4、 β=0.2；RFC 9438 推荐 C=0.4、β=0.7。

控制信号:
- 新 ACK 确认的数据量
- 平滑 RTT
- 距上次拥塞事件的时间
- 丢包或 ECN 拥塞事件

控制动作:
- 更新 cwnd
- 更新 ssthresh
- 更新 W_max 与 epoch 状态
- 在 TCP-friendly/凹区/凸区之间切换

### 权衡
收益:
- 高 BDP 路径上的窗口增长显著快于 Reno
- 接近 W_max 时增长放缓，降低丢包概率
- Reno-friendly 区域保持与标准 TCP 的兼容性
- fast convergence 让新流更快获得带宽

代价:
- β 小于 0.5 会减慢收敛速度，论文明确承认这一副作用
- 仍然以丢包/ECN 为主要拥塞信号，不能避免缓冲区膨胀
- 需要维护 W_max、K、epoch 等状态，实现比 Reno 复杂
- 三次根计算在早期实现中成本较高，需要使用 Newton-Raphson 等优化

限制:
- 对 RTT 公平性的改善是相对的，不是严格 RTT 公平
- 依赖准确的丢包或 ECN 事件检测
- β 的选择在收敛速度与丢包持续时间之间存在权衡

### 失败模式
- 浅缓冲区路径上，窗口增长过快仍可能先造成丢包。
- 多流 RTT 差异很大时，CUBIC 不能完全消除 RTT 不公平。
- fast convergence 设计用于多流环境；单流且无其他流量时应关闭（RFC 9438）。
- 应用受限或 flight_size 远小于 cwnd 时，用 cwnd 计算新 ssthresh 可能把窗口压得过低（RFC 9438 讨论）。

### 适用条件
适用:
- 高速长距或高 BDP 路径
- 丢包或 ECN 能作为主要拥塞信号
- 需要与 Reno 共存并保持兼容性

不适用:
- 以低时延为第一目标的数据中心网络（DCTCP 等更合适）
- 非拥塞丢包严重的无线路径，需要额外的丢包区分机制

假设:
- 丢包或 ECN 事件能够反映拥塞
- RTT 可测量且网络允许窗口式控制
- 发送方能够维护每次拥塞事件的历史状态

### 参数指导
- C: CUBIC 增长常数，RFC 9438 推荐 C=0.4；论文也使用 0.4。 (理由: 决定远离饱和点时的增长速度，需要在高 BDP 利用率与稳定性之间平衡。; 来源: src-006, src-007)
- beta_cubic: 论文使用 β=0.2；RFC 9438 推荐 β=0.7，部署时必须明确采用的版本。 (理由: β 越小，丢包后窗口降得越多、收敛越慢；β 越大，恢复更快但丢包可能持续多个 RTT。; 来源: src-006, src-007)
- alpha_cubic: Reno-friendly 区域的等价 AIMD 加性因子取 α = 3(1-β)/(1+β)。 (理由: 使 CUBIC 在 TCP-friendly 区域的平均窗口与 Reno 相当。; 来源: src-006, src-007)
- fast_convergence: 在多流环境启用；当 cwnd < W_max 时把 W_max 下调为 cwnd·(1+β)/2。 (理由: 向新流释放带宽，加速新流收敛；单流环境启用会带来不必要的窗口收缩。; 来源: src-007)

### 评估
指标:
- 吞吐量
- 丢包率
- Jain 公平性指数
- 收敛时间

预期效果:
- 高 BDP 路径上吞吐量高于 Reno
- 相比 BIC 具有更平滑的窗口曲线
- 与 Reno 共存时保持 Reno-friendly

### 证据
- [A] src-006 — CUBIC: A New TCP-Friendly High-Speed TCP Variant; 定位: 3. CUBIC Congestion Control; 3.3 TCP-friendly region; 3.4 Concave region; 3.5 Convex region; 3.6 Multiplicative decrease; 3.7 Fast Convergence
- [A] src-007 — CUBIC for Fast and Long-Distance Networks; 定位: 4.2. Window Increase Function; 4.3. Reno-Friendly Region; 4.6. Multiplicative Decrease; 4.7. Fast Convergence

### 关联经验
- exp-005 (extends): CUBIC keeps Reno's loss response but replaces the linear window growth law.
- exp-007 (synthesizes, incoming): CUBIC illustrates a scaled loss-based response on high-BDP paths.


## exp-003 — 从单比特 ECN 标记中恢复多比特拥塞程度（DCTCP）

- 类型: `mechanism_design`
- 知识状态: `cross_source_synthesis`（source_reported=原作者明确结论；cross_source_synthesis=跨来源归纳；inference=知识库推论）
- 标签: datacenter-low-latency, incast, queue-buildup, shallow-buffer, ecn-handling, multi-bit-feedback, explicit-signal, cwnd-control, marking-threshold, smoothing-coefficient, dctcp, low-latency, throughput, queue-control

### 问题背景
标准 TCP 把拥塞标记当作二值信号，只反应“是否拥塞”，不反应“拥塞程度”。 在浅缓冲区、高线速的数据中心网络中，队列波动很快，二值反应会导致 窗口和队列大幅振荡，并浪费共享缓冲区。

观察到的现象:
- 数据中心交换机缓冲区浅，队列一堆积就会丢包或造成毫秒级排队时延
- 标准 TCP 对单个拥塞标记就窗口减半，造成队列大幅振荡和吞吐损失
- 多个端口共享缓冲区时，少数拥塞端口的队列会挤压其他端口的可用缓冲
- Incast 场景下大量同步短流冲击同一队列，容易超时

### 设计动机与分析
在商品化浅缓冲区交换机上同时获得高突发容忍度、低时延和高吞吐。

问题分析:
作者指出，关键不是某个固定的控制律，而是从单比特 CE 标记序列中恢复 “队列超过标记阈值的概率”，即多比特的拥塞程度信息。接收方准确回传 标记序列，发送方据此估计标记比例 α，并按比例减小窗口。

推理链:
- 队列长度超过阈值 K 即标记 CE，使发送方尽早获知队列超调。
- 接收方逐包反馈是否被标记，保留标记序列而不是只回传一个二值信号。
- 发送方估计最近一个窗口的标记比例 α。
- 窗口按 (1 - α/2) 减小：轻微拥塞时轻微降速，严重拥塞时接近减半。
- 低队列占用为突发流量保留缓冲区余量，减少丢包和超时。

### 设计决策
基于 ECN 的多比特反馈：交换机按瞬时队列阈值标记 CE，接收方精确回传标记， 发送方维护标记比例 α 并按比例减小窗口。

为什么这样选:
复用现有 ECN 机制，只需交换机按固定阈值标记、接收方如实回传， 就能把单比特序列转化为拥塞程度估计；无需修改交换机硬件语义。

考虑过的替代方案:
- 标准 TCP 的 ECN 反应：每次标记都窗口减半，反应过强。
- 基于排队时延的控制器：高线速下测量微小队列时延噪声大。
- 调整 RED 参数：仍只提供二值反应。

### 机制
DCTCP 有三部分。交换机：到达包的瞬时队列占用超过固定阈值 K 时标记 CE， 否则不标记。接收方：使用 ECN-Echo 精确反映标记序列；在延迟 ACK 下用 两状态状态机决定是否设置 ECE，发送方可据此重建标记游程。发送方： 每个窗口更新一次 α ← (1-g)·α + g·F，其中 F 是本窗口被标记包的比例， g 是平滑权重；收到标记 ACK 时执行 cwnd ← cwnd·(1 - α/2)。其余 TCP 机制（慢启动、加性增长、丢包恢复）保持不变。

控制信号:
- CE 标记序列（由接收方通过 ECN-Echo 回传）
- 每个窗口的标记比例 F
- 平滑后的标记概率 α

控制动作:
- 按 α 比例减小拥塞窗口
- 保持加性增长与标准恢复机制

### 权衡
收益:
- 在浅缓冲区交换机上维持低队列占用和低时延
- 按拥塞程度比例降速，窗口振荡远小于标准 TCP
- 提前标记减少突发导致的丢包与超时
- 复用标准 ECN 头字段与交换机能力，部署改动小

代价:
- 需要支持 ECN 的交换机和正确配置的标记阈值 K
- 需要接收方准确回传标记序列，延迟 ACK 实现更复杂
- α 是统计估计，参数 g 和 K 会影响响应速度和稳定性
- 主要针对 RTT 较小、流数量有限的数据中心环境

限制:
- 交换机必须支持按瞬时队列长度标记 CE
- 标记阈值与缓冲区配置耦合，跨厂商配置需要校准
- 对非 DCTCP 的竞争流不具备同样的比例反应能力

### 失败模式
- Incast 规模超过缓冲区可吸收范围时，DCTCP 也无法避免丢包。
- K 设置过高会延迟拥塞反馈，导致队列堆积；过低则频繁标记、降低吞吐。
- ECN 配置或回传错误会使 α 估计失真。
- RTT 差异大或流数量很多时，按窗口估计 α 的假设变弱。

### 适用条件
适用:
- 数据中心等低 RTT、浅缓冲区、支持 ECN 的网络
- 多短流、Incast 和共享缓冲区的场景
- 希望同时降低排队时延并保持吞吐的部署

不适用:
- 网络设备不支持 ECN 或无法配置标记阈值
- RTT 很大且流数量极多的广域网络（DCTCP 的设计假设不成立）

假设:
- 交换机可按瞬时队列长度设置 CE
- 接收方能够回传每个包的标记信息
- 发送方每个窗口可统计一次标记比例

### 参数指导
- K: 交换机标记阈值，按瞬时队列占用超过 K 即标记 CE。 (理由: K 决定开始反馈的队列水平；论文在浅缓冲区数据中心场景中选择很小的 K。; 来源: src-008)
- g: α 的 EWMA 权重，0 < g < 1，每个窗口更新一次。 (理由: g 越大，对最近一个窗口的拥塞程度反应越快，但估计噪声也越大。; 来源: src-008)
- window_decrease: cwnd ← cwnd·(1 - α/2)。 (理由: α 接近 0 时轻微降速，α=1 时等价于 TCP 的减半，实现按拥塞程度比例反应。; 来源: src-008)

### 评估
指标:
- 排队时延
- 吞吐量
- 丢包率与超时
- 短流完成时间

预期效果:
- 队列占用保持在标记阈值 K 附近
- 相比 TCP+RED 显著降低排队时延和丢包
- 在 Incast 和缓冲区压力场景中减少超时

### 证据
- [A] src-008 — Data Center TCP (DCTCP); 定位: 3.1. Algorithm; 3.2. Benefits; 3.3. Analysis; 3.4. Guidelines for choosing parameters
- [A] src-003 — The Addition of Explicit Congestion Notification (ECN) to IP; 定位: 5. Explicit Congestion Notification in IP; 6.1. TCP; 6.1.2. The TCP Sender; 6.1.3. The TCP Receiver

### 关联经验
- exp-005 (alternative_to): DCTCP keeps TCP's window structure but uses ECN extent instead of loss as the signal.
- exp-007 (synthesizes, incoming): DCTCP illustrates an explicit, early, proportional congestion signal.


## exp-004 — 以排队时延目标和速率反馈控制窗口（Copa）

- 类型: `mechanism_design`
- 知识状态: `source_reported`（source_reported=原作者明确结论；cross_source_synthesis=跨来源归纳；inference=知识库推论）
- 标签: bufferbloat, queue-buildup, bandwidth-variation, rtt-variation, rtt-measurement, min-rtt-estimation, rate-control, cwnd-control, pacing, gain-control, copa, low-latency, fairness, coexistence

### 问题背景
现有算法或者只优化吞吐（填满队列），或者只优化时延而在竞争中吃亏。 Copa 从网络效用最大化目标出发，把吞吐和排队时延放进同一个目标函数， 并用目标速率与当前速率的比较来调整窗口。

观察到的现象:
- 以填满缓冲区为目标的拥塞控制造成持续高排队时延
- 纯时延控制器（如 Vegas）在缓冲区填满型算法竞争下失去带宽
- 不同 RTT 的流在吞吐上存在不公平
- 应用既希望低时延，又希望在多种网络下保持吞吐

### 设计动机与分析
提出一种既能在多种网络条件下保持高吞吐、又能维持很低排队时延的 端到端拥塞控制，并在与缓冲区填充型算法竞争时不处于劣势。

问题分析:
Copa 选择效用函数 U = log λ - δ·log d，其中 λ 是平均吞吐，d 是 （超出传播时延的）排队时延，δ 控制时延相对吞吐的权重。在该模型下， 最大化 U 的目标速率为 λ_t = 1/(δ·d_q)，d_q 是测得的平均排队时延。 Copa 通过比较当前速率 λ = cwnd/RTT_standing 与 λ_t，决定增加还是减小 cwnd。

推理链:
- 显式建模吞吐与时延的效用权衡，而不是把丢包当作唯一信号。
- 用近期最小 RTT 估计 standing queue，用长期最小 RTT 估计传播时延。
- 以目标速率 λ_t 为反馈目标，向它调整 cwnd。
- 用 velocity 参数加快离开稳态时的收敛，稳态时 v=1。
- 检测到缓冲区填充型竞争流时切换到 competitive mode，调整 δ。

### 设计决策
基于排队时延的目标速率控制：用 RTT_standing 与 RTT_min 估计 d_q， 计算 λ_t = 1/(δ·d_q)，按 (λ - λ_t) 的符号调整 cwnd，并配合 velocity 参数、pacing 与 competitive mode。

为什么这样选:
目标函数直接表达吞吐与时延的权衡；目标速率只依赖可观测的 RTT 与 cwnd； 竞争模式使低时延策略在存在填满型流时仍能获得带宽。

考虑过的替代方案:
- Cubic/NewReno 等缓冲区填充型算法：时延高。
- Vegas 等纯时延算法：在与填满型算法竞争时吞吐吃亏。
- BBR 等基于带宽/RTT 模型的算法：Copa 认为其目标不同，且论文报告其在某些公平性指标上的差异。

### 机制
Copa 在每个 ACK 上估计 RTT_standing（最近 srtt/2 时间窗口内的最小 RTT） 和 RTT_min（较长时间窗口内的最小 RTT，论文取 10 秒与流龄的较小值）， 得到 d_q = RTT_standing - RTT_min 与 λ_t = 1/(δ·d_q)。若 λ = cwnd/RTT_standing 不超过 λ_t，则 cwnd ← cwnd + v/(δ·cwnd)；否则 cwnd ← cwnd - v/(δ·cwnd)。 velocity v 初始为 1，方向连续保持三个 RTT 后翻倍，方向改变则重置为 1。 发送方以约 2·cwnd/RTT_standing 的速率 pacing。默认模式 δ=0.5； 若最近 5 个 RTT 内没有观察到“接近空队列”，则切换到 competitive mode， 对 1/δ 做 AIMD，以匹配缓冲区填充型算法的激进程度。

控制信号:
- RTT_standing（近期最小 RTT）
- RTT_min（长期最小 RTT）
- 排队时延 d_q
- 当前速率 λ = cwnd/RTT_standing
- 最近 5 个 RTT 的队列空置情况

控制动作:
- 按目标速率调整 cwnd
- 设置 pacing 速率
- 在默认模式与 competitive mode 之间切换
- 对 1/δ 执行 AIMD

### 权衡
收益:
- 在稳态下维持很低的排队时延
- 通过速度参数在离开稳态时快速收敛
- 论文报告与 Cubic 共存时 Copa 获益而 Cubic 平均吞吐不受显著损害
- 在 RTT 公平性实验中优于 Cubic 等基线

代价:
- 依赖对 RTT_min 与 RTT_standing 的可靠估计；ACK 压缩和路径抖动会影响判断
- competitive mode 需要对 1/δ 做额外控制，增加状态和参数
- pacing 需要发送端支持较细粒度的发送调度
- 目标速率模型假设排队时延可测且与拥塞相关

限制:
- 在路径变化频繁时，长期 RTT_min 可能过期（论文用 10 秒窗口处理路径变化）
- 与高度不同的 RTT 或极端无线环境下的表现需要额外验证
- competitive mode 的切换依赖启发式空队列检测

### 失败模式
- ACK 压缩或反向路径排队会抬高 RTT_standing，使 d_q 估计偏大。
- 路径改变后 RTT_min 未及时刷新，目标速率计算出现偏差。
- 竞争流存在但空队列检测误判时，Copa 可能在默认模式与竞争模式间振荡。
- δ 设置不当会在时延与吞吐之间偏离期望工作点。

### 适用条件
适用:
- 能够测量 RTT 并区分传播时延与排队时延的路径
- 希望显式控制排队时延、同时保持吞吐的通用互联网场景
- 存在 Cubic/NewReno 等缓冲区填充型竞争流的部署

不适用:
- 路径排队时延不可观测或 RTT 测量严重失真的环境
- 需要严格控制发送突发但无法 pacing 的实现

假设:
- 排队时延近似等于 RTT_standing - RTT_min
- 发送速率可由 cwnd/RTT_standing 表示
- 接收端 ACK 能及时反映数据交付

### 参数指导
- delta: 默认模式 δ=0.5；competitive mode 中 δ ≤ 0.5，并随竞争检测动态调整。 (理由: δ 决定吞吐与时延的相对权重；δ 越大越偏好低时延，但可能降低吞吐。; 来源: src-009)
- velocity: v 初始为 1；方向连续保持三个 RTT 后翻倍，方向改变则重置。 (理由: 加速远离稳态时的收敛，同时在稳态保持 v=1 以避免过度振荡。; 来源: src-009)
- rtt_windows: RTT_standing 取最近 srtt/2 窗口的最小值；RTT_min 取 10 秒与流龄的较小值。 (理由: 近期窗口用于抵御 ACK 压缩和抖动，长期窗口用于估计传播时延并适应路径变化。; 来源: src-009)
- pacing_rate: 以约 2·cwnd/RTT_standing 的速率 pacing。 (理由: 避免突发；2 倍系数用于补偿 pacing 实现的不精确。; 来源: src-009)
- empty_queue_detector: 若最近 5 个 RTT 内出现 d_q < 0.1·(RTT_max - RTT_min)，视为接近空队列。 (理由: 仅 Copa 流共存时队列应周期性接近空；若长期不空，说明存在填满型竞争流。; 来源: src-009)

### 评估
指标:
- 吞吐量
- 排队时延
- Jain 公平性指数
- RTT 公平性

预期效果:
- 稳态排队时延显著低于缓冲区填充型算法
- 与 Cubic 共存时获得较好带宽份额
- 在 RTT 不同的流之间实现更好的公平性

### 证据
- [A] src-009 — Copa: Practical Delay-Based Congestion Control for the Internet; 定位: 2. Copa Algorithm; 2.1. Target Rate and Update Rule; 2.2. Competing with Buffer-Filling Schemes; 3. Dynamics of Copa; 5.3. RTT-fairness

### 关联经验
- exp-001 (alternative_to): Copa uses queueing delay and a target-rate objective rather than a bandwidth/RTT model.
- exp-007 (synthesizes, incoming): Copa illustrates delay-based control against buffer-filling behaviour.


## exp-005 — 加性增长/乘性减小（AIMD）作为拥塞控制基线

- 类型: `mechanism_design`
- 知识状态: `cross_source_synthesis`（source_reported=原作者明确结论；cross_source_synthesis=跨来源归纳；inference=知识库推论）
- 标签: congestion-loss, random-loss, high-bdp, multi-flow-competition, loss-response, cwnd-control, tcp-reno, fairness, stability, convergence, throughput, control-oscillation, queue-buildup

### 问题背景
网络本身不向端主机报告可用带宽；发送方必须通过“增加负载直到出现拥塞信号， 再后退”的闭环来发现可用容量。丢包（或后来的 ECN）被用作拥塞信号。

观察到的现象:
- 网络进入拥塞后若发送方不后退，会出现吞吐崩溃（congestion collapse）
- 多个发送方需要一种去中心化的方式共享瓶颈带宽
- 突发式增长会迅速把队列推到溢出，造成大量丢包
- 发送方无法直接知道可用带宽，只能通过试探发现

### 设计动机与分析
在不要求网络逐流维护状态的条件下，让互相竞争的发送方稳定、公平地共享 瓶颈带宽，并避免拥塞崩溃。

问题分析:
Jacobson 从一阶时间序列模型出发：拥塞时队列会指数增长，发送方必须至少 以同样快的速度后退，因此采用乘性减小；而在无拥塞时，乘性增长会导致 严重过冲和振荡，因为把网络推入饱和容易、恢复难，所以采用加性增长。 发送方还应以 ACK 自时钟（self-clocking）保持管道中的包数量守恒。

推理链:
- 网络不报告剩余带宽，只能通过丢包等信号间接观察拥塞。
- 过估计可用带宽的代价很高，所以一旦拥塞就乘性减小。
- 指数增长必然过冲，所以无拥塞时用小而固定的加性增长。
- ACK 返回节奏反映路径速率，可作为发送节拍（self-clocking）。
- 慢启动在连接开始时快速找到近似可用窗口，再进入拥塞避免。

### 设计决策
慢启动 + 拥塞避免（AIMD）：每个 ACK 加性增加窗口，检测到丢包时乘性减小窗口； 配合快速重传/快速恢复减少等待超时。

为什么这样选:
AIMD 只需端主机状态，能稳定收敛到公平份额；慢启动缩短初始探测时间； 快速重传避免每次丢包都等待超时。

考虑过的替代方案:
- 乘性增长/乘性减小：增长过快，导致剧烈振荡和吞吐损失。
- 纯速率控制：缺少 ACK 自时钟，难以维持管道守恒。
- 依赖网关逐流状态的方案：部署成本高。

### 机制
连接以慢启动开始：每收到一个确认新数据的 ACK，窗口按确认字节数增加， 约每个 RTT 翻倍，直到达到 ssthresh 或检测到拥塞。进入拥塞避免后， 每个 RTT 窗口约增加一个 MSS（实现上每 ACK 增加约 1/cwnd）。检测到丢包时， ssthresh 设为当前在途数据的约一半，cwnd 减半；快速重传在收到三个重复 ACK 时重传丢失段并进入快速恢复，避免等待重传超时。RFC 5681 形式化了这些规则。

控制信号:
- ACK 确认的新数据量
- 重复 ACK（三个重复 ACK 触发快速重传）
- 重传超时
- 丢包事件（后续可用 ECN 替代或补充）

控制动作:
- 慢启动阶段指数增长 cwnd
- 拥塞避免阶段加性增长 cwnd
- 丢包时乘性减小 cwnd 与 ssthresh
- 快速重传与快速恢复

### 权衡
收益:
- 端到端、无需网络逐流状态，部署极其广泛
- 多个 AIMD 流能收敛到大致公平的带宽份额
- 对网络变化有基本的自适应能力
- 慢启动和快速恢复降低了连接启动与单包丢失的代价

代价:
- 吞吐量随 RTT 和丢包率下降，在高 BDP 路径上扩展性差
- 为了发现可用带宽必须周期性把队列推到丢包点，造成排队时延和锯齿
- 把所有丢包都当作拥塞，随机丢包会导致不必要的降速
- 丢包是滞后信号，拥塞已经发生后才反应

限制:
- 标准 AIMD 在高 BDP、长 RTT 或高随机丢包路径上效率低
- 与基于时延或显式信号的机制竞争时，可能持续填满缓冲区
- 对 ACK 路径和 ACK 时钟的质量有依赖

### 失败模式
- 随机丢包被误判为拥塞，吞吐量显著下降。
- 高 BDP 路径上窗口恢复慢，链路利用率低。
- 浅缓冲区下，窗口增长过快造成丢包，快速恢复也可能失败。
- ACK 压缩、ACK 丢失或反向路径拥塞会破坏自时钟，导致突发。
- 多个流同步减窗时，队列和吞吐出现同步振荡。

### 适用条件
适用:
- 丢包近似反映拥塞的路径
- 需要简单、端到端、广泛兼容的基线实现
- 中低 BDP 或对时延不敏感的场景

不适用:
- 高 BDP 长距路径（需要 CUBIC 等扩展）
- 随机丢包严重或需要低排队时延的场景
- 数据中心等需要浅缓冲区低时延的环境

假设:
- 丢包（几乎）总是由拥塞引起
- ACK 能按路径速率返回，提供可用的自时钟
- 发送方能够检测丢包（重复 ACK 或超时）

### 参数指导
- multiplicative_decrease: 丢包时 cwnd 约减半（d=0.5）。 (理由: 乘性减小足够快，能在拥塞持续时指数后退；0.5 是 Jacobson 实现的选择。; 来源: src-010, src-001)
- additive_increase: 拥塞避免阶段每个 RTT 约增加一个 MSS（每 ACK 约 1/cwnd）。 (理由: 小而固定的增长避免指数过冲，使多个流逐步收敛到公平份额。; 来源: src-010, src-001)
- fast_retransmit_threshold: 收到三个重复 ACK 时触发快速重传（RFC 5681）。 (理由: 在等待超时之前恢复单个丢包，减少吞吐空窗。; 来源: src-001)

### 评估
指标:
- 吞吐量
- 丢包率
- 公平性
- 收敛行为

预期效果:
- 多个流收敛到大致公平的带宽份额
- 在丢包式网络中维持稳定吞吐
- 相比无拥塞控制的发送方避免拥塞崩溃

### 证据
- [A] src-010 — Congestion Avoidance and Control; 定位: 1 Getting to Equilibrium: Slow-start; 2 Conservation at equilibrium: round-trip timing; 3 Adapting to the path: congestion avoidance
- [A] src-001 — TCP Congestion Control; 定位: 3.1 Slow Start and Congestion Avoidance; 3.2 Fast Retransmit/Fast Recovery

### 关联经验
- exp-001 (alternative_to, incoming): BBR replaces loss-triggered AIMD with bandwidth/RTT model-based control.
- exp-002 (extends, incoming): CUBIC keeps Reno's loss response but replaces the linear window growth law.
- exp-003 (alternative_to, incoming): DCTCP keeps TCP's window structure but uses ECN extent instead of loss as the signal.
- exp-006 (extends, incoming): QUIC's default controller is NewReno-like, with clarified loss detection and recovery.
- exp-007 (synthesizes, incoming): Reno/AIMD defines the loss-based baseline whose assumptions are examined.


## exp-006 — QUIC 的丢包检测、PTO 与持续拥塞判定

- 类型: `mechanism_design`
- 知识状态: `source_reported`（source_reported=原作者明确结论；cross_source_synthesis=跨来源归纳；inference=知识库推论）
- 标签: path-change, rtt-variation, congestion-loss, burst-traffic, loss-detection, ack-based-recovery, packet-threshold, time-threshold, loss-response, persistent-congestion, quic, rtt-measurement, pacing, cwnd-control, robustness

### 问题背景
TCP 的序号与重传机制使丢失检测存在歧义；RTO 与 TLP 等机制分散且难以 精确描述。QUIC 使用单调递增的包号和 ACK 区间，使丢失检测语义更清晰， 并需要一套明确的 PTO 与持续拥塞规则来驱动恢复和拥塞控制。

观察到的现象:
- 基于重传的丢包检测存在重传歧义，难以判断 ACK 对应哪次发送
- 仅靠重传超时（RTO）恢复会带来长时吞吐空窗
- 乱序和 ACK 丢失会让发送方误判丢包或过度等待
- 路径切换后旧路径的拥塞状态可能不再适用

### 设计动机与分析
为 QUIC 定义一套与传输层解耦、可被不同拥塞控制算法复用的丢包检测与恢复机制， 减少重传歧义、避免长时间等待 RTO，并在严重拥塞时可靠地重置状态。

问题分析:
QUIC 包号单调递增且不重传同一个包号，因此 ACK 能无歧义地确认包； ACK 帧可携带多个区间，便于在乱序和 ACK 丢失时仍建立丢失事件。 基于包数阈值和时间阈值检测丢失；当没有足够 ACK 反馈时，用 PTO 主动探测；当长时间没有确认任何包时，判定持续拥塞并重置拥塞状态。

推理链:
- 单调包号与 ACK 区间消除重传歧义，使丢失检测更明确。
- 包阈值适合快速恢复少量丢失；时间阈值适应乱序和不同 RTT。
- PTO 在没有足够 ACK 时探测，替代 RTO/TLP 的分散机制。
- 持续拥塞判定允许发送方在长时间无反馈后重置，避免保留失效状态。
- 拥塞控制本身可以是 NewReno 类或可替换算法（如 CUBIC）。

### 设计决策
基于 ACK 的丢失检测（包阈值与时间阈值）+ Probe Timeout（PTO）+ 持续拥塞判定； 默认拥塞控制器为 NewReno 类，慢启动/恢复/拥塞避免状态明确，并建议 pacing。

为什么这样选:
ACK 区间与单调包号使丢失检测更精确；包阈值与时间阈值互补； PTO 提供明确的探测机制；持续拥塞判定覆盖长时间无反馈的极端情况。

考虑过的替代方案:
- 仅使用 RTO：恢复慢，且难以覆盖乱序与 ACK 丢失。
- 沿用 TCP 的重传序号语义：存在重传歧义。
- 固定包数阈值：对不同 RTT 和乱序程度适应性差。

### 机制
QUIC 维护 RTT 估计（smoothed_rtt、rttvar、min_rtt）。当包号小于某个 已确认包、且差距达到包阈值（kPacketThreshold=3），或发送时间早于 当前时间减去时间阈值（约 9/8 × max(smoothed_rtt, latest_rtt)）时， 判为丢失。若没有足够 ACK，则在 PTO 到期时发送探测包；PTO 由 smoothed_rtt、rttvar 与 max_ack_delay 计算。若在持续拥塞持续时间内 没有任何包被确认，则判定持续拥塞：把 cwnd 重置为最小窗口， 并以慢启动重新开始。拥塞控制在慢启动、恢复与拥塞避免之间切换， 默认每丢包事件把 cwnd 减半；发送方应 pacing，避免突发。

控制信号:
- ACK 确认的包号与 ACK 区间
- RTT 样本、smoothed_rtt、rttvar、min_rtt
- PTO 定时器
- 持续拥塞持续时间
- ECN-CE 计数（若启用）

控制动作:
- 声明丢包并触发恢复
- 发送 PTO 探测包
- 调整 cwnd 与 ssthresh
- 持续拥塞时重置 cwnd
- pacing 发送

### 权衡
收益:
- 单调包号与 ACK 区间消除重传歧义，丢失检测语义清晰
- 包阈值与时间阈值互补，兼顾快速恢复与乱序容忍
- PTO 替代分散的 RTO/TLP，机制更统一
- 持续拥塞判定避免在长时间无反馈后保留失效的拥塞状态
- 拥塞控制器可替换（NewReno、CUBIC 等）

代价:
- 需要维护 ACK 区间、PTO 与持续拥塞等额外状态
- 默认控制器仍是丢包式，继承其对随机丢包和 bufferbloat 的局限
- PTO 探测可能在拥塞时注入额外包
- 时间阈值与持续拥塞阈值是权衡参数

限制:
- 默认 NewReno 类控制器在高 BDP 路径上扩展性有限，需要替换控制器
- 乱序严重的路径上，包阈值可能导致误判
- 持续拥塞阈值过小会不必要地重置，过大则对严重拥塞反应迟钝

### 失败模式
- 乱序超过包阈值时，正常包被误判为丢失，触发不必要重传和降窗。
- PTO 在严重拥塞时连续探测，可能加重拥塞（需要与拥塞控制协同）。
- 持续拥塞阈值配置不当：过小导致吞吐损失，过大导致持续激进发送。
- ACK 反馈长期缺失时，若持续拥塞判定不触发，可能一直保留旧窗口。

### 适用条件
适用:
- QUIC 端点，且需要明确的丢失检测与恢复语义
- 存在乱序、ACK 丢失或路径切换的互联网路径
- 希望复用统一恢复机制并替换拥塞控制器的实现

不适用:
- 非 QUIC 传输（TCP 有自己的 RFC 5681/6675 等机制）
- 无法提供包号单调性与 ACK 区间的自定义传输

假设:
- 包号单调递增且不重用
- ACK 能确认已接收的包号区间
- RTT 与 max_ack_delay 可估计

### 参数指导
- kPacketThreshold: 包阈值 3：包号落后已确认包 3 个即判为丢失。 (理由: 与 TCP 的三个重复 ACK 经验一致，兼顾快速恢复与少量乱序容忍。; 来源: src-002)
- time_threshold: 时间阈值约为 9/8 × max(smoothed_rtt, latest_rtt)。 (理由: 为 RTT 估计误差和乱序留出余量，避免过早判丢。; 来源: src-002)
- PTO: PTO 由 smoothed_rtt + max(4·rttvar, kGranularity) + max_ack_delay 组成并带指数退避。 (理由: 在没有足够 ACK 时主动探测；包含 max_ack_delay 以适配延迟 ACK。; 来源: src-002)
- kPersistentCongestionThreshold: 推荐值 3；持续拥塞持续时间 = (smoothed_rtt + max(4·rttvar, kGranularity) + max_ack_delay) × 3。 (理由: 近似等价于 TCP 在两次 TLP 后声明 RTO；过小造成不必要重置，过大造成反应迟钝。; 来源: src-002)
- minimum_window: 最小拥塞窗口为 2 个包（约 2 × max_datagram_size）。 (理由: 允许发送方在恢复期间继续探测，同时避免窗口降到 0 后无法前进。; 来源: src-002)

### 评估
指标:
- 恢复时间
- 吞吐量
- 丢包误判率
- PTO 触发次数

预期效果:
- 比仅依赖 RTO 更快地从丢包中恢复
- 在乱序和 ACK 丢失下减少误判
- 长时间无反馈后可靠地重置拥塞状态

### 证据
- [A] src-002 — QUIC Loss Detection and Congestion Control; 定位: 5. Estimating the Round-Trip Time; 6.1.1. Packet Threshold; 6.1.2. Time Threshold; 6.2. Probe Timeout; 7.3. Congestion Control States; 7.6. Persistent Congestion; 7.7. Pacing
- [A] src-001 — TCP Congestion Control; 定位: 3.1 Slow Start and Congestion Avoidance; 3.2 Fast Retransmit/Fast Recovery

### 关联经验
- exp-005 (extends): QUIC's default controller is NewReno-like, with clarified loss detection and recovery.
- exp-007 (synthesizes, incoming): QUIC's default loss-based controller shows the assumption persists in new transports.
- exp-008 (complements, incoming): RFC 9002 recommends pacing alongside window-based congestion control.


## exp-007 — 问题分析：为什么“丢包即拥塞”的假设会误导设计

- 类型: `problem_analysis`
- 知识状态: `cross_source_synthesis`（source_reported=原作者明确结论；cross_source_synthesis=跨来源归纳；inference=知识库推论）
- 标签: random-loss, bufferbloat, high-bdp, shallow-buffer, congestion-loss, limitation-of-existing, assumption-limits, symptom-cause, key-signal, goal-conflict, throughput-vs-latency, utilization-vs-fairness

### 问题背景
丢包是一个滞后、二值且原因含混的信号。它隐含地假设“丢包几乎总是由 瓶颈拥塞引起”，但该假设在随机丢包、深缓冲区、高 BDP 和浅缓冲区等 环境中并不成立。信号与真实拥塞程度之间的偏差，是不同算法设计分歧的根源。

观察到的现象:
- 同样是一次丢包，随机链路错误与瓶颈队列溢出被发送方同等对待
- 以丢包为信号的算法在无线或随机丢包路径上吞吐量骤降
- 算法为了探测带宽反复填满队列，造成持续高排队时延
- 在高 BDP 路径上，丢包式算法的恢复周期很长，带宽利用率不足

### 设计动机与分析
把“选择什么拥塞信号”本身作为设计问题来分析，说明不同算法为什么在 不同网络条件下表现不同，并给出面向新设计的问题拆解框架。

问题分析:
基于丢包的控制器把路由器的队列溢出当作拥塞的充分信号。该假设在四类 条件下失效： （1）随机丢包：丢包与瓶颈负载无关，降速只会浪费容量； （2）深缓冲区：队列必须先膨胀到溢出才产生信号，导致 bufferbloat； （3）高 BDP：AIMD 每 RTT 的增长有限，从丢包中恢复需要很多个 RTT； （4）浅缓冲区：少量突发即可溢出，丢包可能发生在平均利用率并不高时。 ECN 把信号提前到队列超过阈值时，并把“是否拥塞”扩展为“拥塞程度”； 时延/排队时延信号直接反映队列；带宽/RTT 模型则不依赖丢包来估计路径容量。

推理链:
- 丢包式控制把‘队列溢出’当作拥塞的唯一证据，这需要丢包主要由拥塞引起的假设。
- 随机丢包破坏该假设，导致不必要的降速。
- 深缓冲路径上，丢包出现得太晚，算法会先制造大量排队时延。
- 高 BDP 路径上，丢包后的 AIMD 恢复速度与 BDP 不匹配。
- ECN、排队时延与带宽/RTT 模型分别提供了更早、更直接或更完整的信号。
- 因此不存在对所有环境最优的单一信号；设计应显式列出信号假设与失效条件。

### 设计决策
本条经验不主张单一机制，而是给出信号选择框架：按部署环境评估丢包、 ECN、排队时延、带宽/RTT 模型各自的假设、提前量和可观测性，再选择或组合信号。

为什么这样选:
不同失效条件对应不同信号；把假设显式化，可以避免把一个环境中的 成功经验错误迁移到另一个环境，也便于组合多种信号。

考虑过的替代方案:
- 丢包信号：部署最广、无需网络支持，但滞后且原因含混。
- ECN/多比特反馈：信号更早且可表达程度，但需要网络设备支持。
- 排队时延：直接反映队列，但测量噪声与 ACK 压缩会影响估计。
- 带宽/RTT 模型：不依赖丢包，但对交付速率样本与 pacing 有要求。

### 机制
可用信号谱系： - 丢包：二值、滞后，适合“丢包≈拥塞”的路径，代表 Reno/CUBIC 基线； - ECN：在队列超过阈值时提前标记，单比特可经 DCTCP 类机制恢复程度信息； - 排队时延：由 RTT_standing - RTT_min 估计，直接对应队列，代表 Copa； - 带宽/RTT 模型：由交付速率与最小 RTT 估计 BDP，代表 BBR。 设计新算法时，应先明确目标网络中的主要失效条件，再选择能观测到该条件 且假设成立的最小信号集合。

控制信号:
- 丢包事件（重复 ACK / 超时）
- ECN-CE 标记与标记比例
- 排队时延（RTT_standing - RTT_min）
- 交付速率与最小 RTT

控制动作:
- 按所选信号调整窗口或速率
- 在多种信号之间切换或组合
- 为信号失效设置降级路径

### 权衡
收益:
- 把算法差异解释为信号假设差异，便于跨场景迁移和比较
- 能在设计早期识别失效条件，而不是等实验暴露问题
- 支持组合信号：例如用 ECN 提前反馈，用丢包作为后备

代价:
- 多信号组合增加状态、参数和实现复杂度
- 不同信号的更新周期与噪声特性不同，融合需要额外设计
- 信号选择依赖部署条件，难以给出普适最优方案

限制:
- 本条是跨来源综合，不是某一篇论文作者的单一结论
- 信号谱系不覆盖所有可能的拥塞指示（如基于机器学习的策略）
- 组合信号之间的相互干扰仍需逐案验证

### 失败模式
- 在随机丢包路径上继续使用丢包信号，导致吞吐量与利用率下降。
- 在深缓冲路径上只使用丢包信号，长期维持高排队时延。
- 在浅缓冲路径上使用激进的窗口增长，突发造成丢包。
- ECN 部署不完整时，算法在 ECN 与丢包模式之间行为不一致。
- 时延信号被 ACK 压缩或反向路径排队污染，导致误判。

### 适用条件
适用:
- 设计新拥塞控制算法或把已有算法迁移到新网络环境
- 需要解释不同算法在相同场景下表现差异
- 需要在丢包、ECN、时延和模型信号之间做选择

不适用:
- 只需要复现某个标准算法的具体行为（应直接查阅该标准）
- 网络不支持所选信号且无法修改部署

假设:
- 设计者能够描述目标网络的主要失效条件
- 所选信号在目标环境中可观测且测量误差可控

### 参数指导
- （无）

### 评估
指标:
- 吞吐量
- 排队时延
- 丢包率
- 公平性
- 收敛速度

预期效果:
- 按失效条件选择信号后，算法在目标环境中的关键指标应优于盲目沿用丢包信号
- 信号假设被显式记录后，跨环境迁移的风险更可评估

### 证据
- [A] src-001 — TCP Congestion Control; 定位: 3.1 Slow Start and Congestion Avoidance
- [A] src-002 — QUIC Loss Detection and Congestion Control; 定位: 7. Congestion Control
- [A] src-003 — The Addition of Explicit Congestion Notification (ECN) to IP; 定位: 5. Explicit Congestion Notification in IP; 6.1. TCP
- [A] src-004 — BBR: Congestion-Based Congestion Control; 定位: Congestion Control on the Internet; Modeling the Network Path
- [A] src-006 — CUBIC: A New TCP-Friendly High-Speed TCP Variant; 定位: 3. CUBIC Congestion Control
- [A] src-008 — Data Center TCP (DCTCP); 定位: 3.1. Algorithm
- [A] src-009 — Copa: Practical Delay-Based Congestion Control for the Internet; 定位: 2. Copa Algorithm; 2.2. Competing with Buffer-Filling Schemes

### 关联经验
- exp-001 (synthesizes): The problem analysis draws on BBR's critique of loss as a congestion proxy.
- exp-002 (synthesizes): CUBIC illustrates a scaled loss-based response on high-BDP paths.
- exp-003 (synthesizes): DCTCP illustrates an explicit, early, proportional congestion signal.
- exp-004 (synthesizes): Copa illustrates delay-based control against buffer-filling behaviour.
- exp-005 (synthesizes): Reno/AIMD defines the loss-based baseline whose assumptions are examined.
- exp-006 (synthesizes): QUIC's default loss-based controller shows the assumption persists in new transports.


## exp-008 — 用 Pacing 把窗口预算摊平到时间轴上

- 类型: `mechanism_design`
- 知识状态: `cross_source_synthesis`（source_reported=原作者明确结论；cross_source_synthesis=跨来源归纳；inference=知识库推论）
- 标签: burst-traffic, shallow-buffer, queue-buildup, datacenter-low-latency, pacing, rate-control, low-overhead, parameter-coupling, pacing-rate, bbr, quic, low-latency, throughput

### 问题背景
窗口式拥塞控制只限制“在途字节数”，不限制“发送时刻”。当应用或协议栈 一次把整个窗口交给网络时，即使平均速率合适，瞬时突发也可能超过路径 可吸收能力，造成短期拥塞、丢包和队列振荡。

观察到的现象:
- 即使平均速率不超过瓶颈，集中发送的突发也会造成瞬时排队和丢包
- 突发在浅缓冲区路径上尤其容易触发丢包
- 大量包同时到达会加剧 ACK 压缩和测量偏差
- 仅靠拥塞窗口限制在途量，无法约束包在时间上的分布

### 设计动机与分析
通过在时间上均匀发送，把窗口预算摊平，降低瞬时突发造成的丢包和排队时延， 同时保持拥塞窗口所允许的平均发送速率。

问题分析:
对窗口式控制器，平均速率可以近似为 cwnd / RTT。若发送方以很短的间隔 发出一串包，局部速率远高于平均值；在瓶颈处这会形成突发队列。 Pacing 用漏桶或时间片调度按目标速率逐个发送，使到达过程更平滑， 也降低 ACK 压缩和测量噪声。

推理链:
- 窗口只给出在途预算，不给出包间间隔。
- 突发会让瞬时到达速率超过瓶颈，即使长期平均速率合规。
- 按 cwnd/RTT 或估计带宽计算包间隔并匀速发送，可消除突发。
- 节奏器需要与拥塞控制器协同，不能绕过 cwnd 限制。
- 过慢的 pacing 会低估可用带宽，因此需要略高于 1 的补偿系数和突发上限。

### 设计决策
发送节拍器（pacer）：按拥塞控制器给出的速率计算包间隔， 使用漏桶/时间片调度发送；限制瞬时突发（如不超过初始窗口）， 并与 cwnd 检查协同。

为什么这样选:
Pacing 是发送端可独立部署的机制，能与任何窗口式控制器组合； 它直接作用于突发这一根因，而不是事后依赖队列或丢包反馈。

考虑过的替代方案:
- 不做 pacing，直接在 cwnd 允许时突发发送：实现简单但突发风险高。
- 仅依赖 AQM/队列管理吸收突发：需要网络侧支持，且浅缓冲区无法完全吸收。
- 固定低速 pacing：会低估可用带宽，降低吞吐。

### 机制
Pacing 为发送队列维护目标速率与下一个可发送时刻。对窗口式控制器， 目标速率可近似为 N × cwnd / smoothed_rtt（N 略大于 1，例如 1.25）， 或直接采用控制器给出的 pacing rate（如 BBR 的 pacing_gain × BtlBw）。 发送方按包大小与速率计算包间隔，使用漏桶/时间轮/高精度定时器调度； 漏桶容量限制最大突发。ACK-only 包通常不应被 pacing，以免延迟 ACK 返回。 实现上常与 TSO/GSO 等分段卸载协同，避免一次注入过多包。

控制信号:
- 拥塞窗口或控制器给出的 pacing rate
- smoothed_rtt
- 包大小与可用发送信用
- 当前调度时间

控制动作:
- 计算并执行包间间隔
- 限制瞬时突发大小
- 在 cwnd 允许范围内调度发送

### 权衡
收益:
- 显著降低突发造成的瞬时队列和丢包
- 在浅缓冲区路径上提高稳定性
- 使到达过程更平滑，减少 ACK 压缩与测量偏差
- 可与不同拥塞控制器组合

代价:
- 需要较细粒度的定时与调度，增加 CPU 和实现复杂度
- 定时器精度不足或调度抖动会让实际速率偏离目标
- pacing 速率设置过低会浪费窗口、降低吞吐
- 与 TSO/GSO、多队列和中断合并的交互需要小心

限制:
- pacing 只能平滑发送，不能弥补错误的窗口或速率判断
- 过大的突发上限会削弱 pacing 效果
- ACK-only 包不适合 pacing，需单独处理
- 路径 RTT 快速变化时，基于 cwnd/RTT 的速率需要及时更新

### 失败模式
- pacing 速率明显低于可用带宽，导致链路利用率不足。
- 定时器粒度粗，包仍以批次发出，突发没有被真正消除。
- 漏桶容量过大，允许一次发送过多包。
- pacing 与 cwnd 检查顺序错误，可能绕过拥塞窗口限制。
- 对 ACK 包也做 pacing，延迟 ACK 返回并拖慢恢复。

### 适用条件
适用:
- 窗口式拥塞控制且发送端可控制发包时刻
- 浅缓冲区、突发敏感或需要低排队时延的路径
- 高线速、需要平滑到达过程的数据中心或骨干网络

不适用:
- 无法获得可靠时钟或定时精度的实现
- 应用本身以极低速率发送且突发不构成问题

假设:
- 发送端能够按包或按字节调度发送时刻
- cwnd、RTT 或控制器速率估计可用
- pacing 不会绕过拥塞窗口约束

### 参数指导
- pacing_rate: 窗口式控制器可令 rate = N × cwnd / smoothed_rtt，N 取略大于 1（RFC 9002 示例为 1.25）。 (理由: N>1 补偿 RTT 抖动和调度误差，避免因速率略低而低估窗口；N 过大则增加队列压力。; 来源: src-002)
- burst_limit: 突发上限建议不超过初始拥塞窗口；只有确认路径能吸收更大突发时才提高。 (理由: 限制漏桶瞬时容量，避免 pacing 退化为突发发送。; 来源: src-002)
- bbr_pacing_gain: BBR 以 pacing_gain × BtlBw 作为 pacing rate；不同状态使用不同增益。 (理由: 把速率控制与状态机结合：Startup 用高增益快速探测，ProbeBW 用 1.25/0.75/1.0 循环。; 来源: src-004, src-005)
- ack_only_packets: 只包含 ACK 的包通常不应被 pacing。 (理由: 避免延迟 ACK 返回，影响对端恢复和本端测量。; 来源: src-002)

### 评估
指标:
- 突发造成的丢包率
- 排队时延
- 吞吐量与链路利用率
- ACK 压缩程度

预期效果:
- 瞬时队列峰值下降，浅缓冲区丢包减少
- 在相同平均速率下获得更平滑的到达过程
- 配合 BBR 等速率控制器时减少超发突发

### 证据
- [A] src-002 — QUIC Loss Detection and Congestion Control; 定位: 7.7. Pacing
- [A] src-004 — BBR: Congestion-Based Congestion Control; 定位: The BBR Algorithm; ProbeBW; Startup
- [A] src-005 — Linux TCP BBR implementation (net/ipv4/tcp_bbr.c); 定位: net/ipv4/tcp_bbr.c::bbr_set_pacing_rate / bbr_update_cycle_phase / bbr_tso_segs_goal

### 关联经验
- exp-001 (complements): BBR's rate control depends on pacing; pacing is the enforcement mechanism.
- exp-006 (complements): RFC 9002 recommends pacing alongside window-based congestion control.

