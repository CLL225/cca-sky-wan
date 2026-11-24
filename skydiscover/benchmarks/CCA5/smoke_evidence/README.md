# cellular 冒烟测试证据

2026-09-20 14:14–14:15，对 SkyDiscover 的 CCA5 种子程序跑的一次链路冒烟。
**目的不是给算法打分**，而是确认"种子 → llmgen.rs → cargo build → mahimahi → output.csv"这条链路今天还通不通。

## 怎么跑的

```bash
cd /workspace/CCA5
CCA_DIR=/tmp/smoke_cca  RESULTS_DIR=/tmp/smoke_results \
  bash ./run_evaluation.sh seed.rs "llmgen" stream cellular 0 5
```

- `seed.rs` = `benchmarks/CCA5/initial_program.py`（COPA 改名成的 LLMGen 种子）
- 只跑 llmgen、不跑基线；每条 trace 5 秒（正式评测用 20 秒）
- `CCA_DIR` 和 `RESULTS_DIR` 都重定向到 /tmp，**不污染 `CCA5/congestion_control_algorithm/` 和 `CCA5/bin/results/`**

## 结论：通过

45 秒跑完（含 7 秒增量编译），5 条 cellular trace 各产出一行数据，吞吐全为正数，
`min_rtt` 稳定在 21–31ms（对应 `mm-delay 10` 的 20ms 基准）。

## 两个评测器必须处理的坑

1. **干净的 `RESULTS_DIR` 里 csv 没有表头。** `run_evaluation.sh` 只往硬编码的
   `./bin/results/output.csv` 写表头（L23-30），重定向后就没了。直接 `csv.DictReader`
   会把第一条 trace 的数据当表头吃掉 —— 每评测一次静默丢一条 trace。
2. **算法列的值是 `llmgen-1` 而不是 `llmgen`。** 客户端从 `--cca-filename` 里抠数字拼在后缀
   （`tquic/iperf/src/main.rs:515-526`），`seed.rs` 没有数字所以兜底成 `-1`。
   解析时要用 `startswith("llmgen")`，不能 `==`。

## 文件

| 文件 | 说明 |
|---|---|
| `smoke.log` | 完整 stdout |
| `smoke_run.sh` | 实际执行的脚本 |
| `smoke_results/output.csv` | 5 行汇总（algorithm,trace,duration,throughput,srtt,min_rtt,rttvar） |
| `smoke_results/convergence_summary.csv` | 收敛分析汇总 |
| `smoke_results/convergence/*/timeseries.csv` | 每条 trace 的 17 列时间序列 |
| `build.log` | 更早一次"种子当 llmgen.rs 编译"验证的日志（0 error） |
| `smoke_cca/seed.rs` | 当时用的种子副本 |
| `prev_session/` | 上一次会话的同类冒烟，解释了 `CCA5/bin/results/output.csv` 为何只有表头 |
