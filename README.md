# cca-sky-wan

CCA5 / SkyDiscover 实验代码快照（来自 `cca-sky9` 工作区，卫星网络 + 视频场景）。

本仓库保存拥塞控制算法进化实验的完整源码、测试脚本和轨迹数据，**不包含**编译产物、实验结果、日志和模型 checkpoints。

## 目录结构

```
CCA5/                         QUIC 测试床与评测脚本
  cca_generate/               评分、日志解析、算法变异相关代码
  congestion_control_algorithm/  候选算法 .rs 文件
  mahimahi/                   Mahimahi 源码与 traces
  tquic/                      tquic 源码（含 iperf 客户端/服务端）
  bin/traces/                 测试用网络轨迹
  run_evaluation.sh           单流评测（Mahimahi）
  run_tc.sh                   tc/netns 评测（WAN / 卫星）
  run_fairness_suite.sh       公平性测试
  run_convergence.sh          收敛性测试
  replay_wan.py / replay_lens.py  链路损伤重放
skydiscover/                  SkyDiscover 进化框架
  benchmarks/CCA5/            CCA5 evaluator
  skydiscover/                框架源码
cca-expert-knowledge-base/    领域知识库
cll.md                        容器/环境说明
supplement_watcher.sh         补充实验 watcher
```

## 快速开始

### 1. 构建 tquic

```bash
cd CCA5/tquic
cargo build --all --release

# iperf 客户端/服务端是独立 crate，需要单独构建
cd iperf
CARGO_TARGET_DIR=../target cargo build --release

cp ../target/release/tquic_perf ../bin/tquic_server
cp ../target/release/tquic_perf ../bin/tquic_client
```

### 2. 蜂窝网络单流评测

```bash
cd CCA5
RESULTS_DIR=/tmp/results bash run_evaluation.sh \
  cca_generated1.rs "bbr cubic copa llmgen" stream cellular 0 20
```

### 3. WAN / 卫星评测

```bash
cd CCA5
RESULTS_DIR=/tmp/results bash run_tc.sh \
  cca_generated1.rs "bbr cubic copa llmgen" video satellite 0 20
```

### 4. 进化实验

```bash
cd skydiscover
python -m skydiscover.benchmarks.CCA5 ...
```

具体的启动参数请参考 `cll.md` 和 `skydiscover/README.md`。

## 环境依赖

- Linux（需要 root 才能创建 netns / mahimahi）
- Docker（可选，`CCA5/Dockerfile`）
- Rust toolchain（tquic）
- Python 3.10+
- mahimahi（`CCA5/mahimahi` 源码）
- `tc`, `iproute2`, `iperf3` 等网络工具

## 数据说明

- `CCA5/mahimahi/traces/`：Mahimahi 原始轨迹（约 29 MB）
- `CCA5/bin/traces/`：实验用配对轨迹和卫星轨迹（约 8.5 MB）

以下内容被有意排除，可自行生成或从实验环境获取：

- `CCA5/tquic/target/`、`CCA5/tquic/iperf/target/`：Rust 编译产物
- `CCA5/bin/tquic_server`、`CCA5/bin/tquic_client`：编译后的二进制
- `CCA5/bin/results/`、`skydiscover/outputs/`：实验结果、日志、checkpoints
- `final_test_*/`、`test_top10/`：复测结果
- `*.log`、证书私钥（`cert.key`）等运行期文件

## 第三方代码

本仓库包含以下开源项目的代码，各自保留原有许可证：

- [tquic](https://github.com/tencent/tquic)（Apache-2.0）
- [mahimahi](https://github.com/ravinet/mahimahi)
- [SkyDiscover](https://github.com/skydiscover/skydiscover)

`CCA5/tquic/src/tls/testdata/` 和 `CCA5/tquic/fuzz/conf/` 下的 `.key` 文件是上游项目自带的测试证书，不是生产密钥。
