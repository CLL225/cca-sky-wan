# 项目使用说明

本项目用于生成、编译和评估 QUIC 拥塞控制算法。整体流程是：

1. 使用 `cca_generate` 调用大模型生成初始拥塞控制算法。
2. 将生成的 Rust 算法文件注入 `tquic`。
3. 通过 Mahimahi 或 `tc + network namespace` 回放网络场景并运行测试。
4. 根据测试结果、fairness 结果和 convergence 结果继续变异优化算法。

注意每次使用脚本进行测试后/bin/results下的output.csv、convergence_summary.csv等文件并不会清理，会不断向文件中写入测试结果数据，mutate_algorithm依赖results中csv文件的结果数据进行变异优化，因此请注意自行清理。

## 目录结构

| 路径 | 作用 |
| --- | --- |
| `cca_generate/` | 算法生成与变异优化脚本。 |
| `congestion_control_algorithm/` | 保存生成的拥塞控制算法文件，例如 `cca_generated1.rs`。 |
| `tquic/` | 修改后的 TQUIC 源码，测试脚本会将算法复制到 `tquic/src/congestion_control/llmgen.rs` 并编译。 |
| `bin/` | 测试运行目录，包含证书、trace、编译后的客户端/服务端以及结果目录。 |
| `bin/traces/` | 网络 trace 数据，按场景放在 `cellular/`、`satellite/` 等子目录中。 |
| `bin/results/` | 默认测试结果目录。 |
| `logs/` | 生成算法、变异优化、自动修复和编译检查日志。 |
| `mahimahi/` | Mahimahi 源码与示例 trace。 |
| `run_evaluation.sh` | Mahimahi 场景测试入口，主要用于 cellular trace。 |
| `run_tc.sh` | `tc + namespace` 场景测试入口，主要用于 satellite 和 WAN。 |
| `run_fairness_suite.sh` | 公平性测试入口。 |
| `run_convergence.sh` | 单独的运行时收敛性测试入口。 |
| `prepare_trace.py`、`scale_trace.py` | Starlink/LENS trace 转换与缩放工具。 |

## 环境准备

推荐在 Docker 容器中运行，因为测试脚本需要 Mahimahi、`tc`、network namespace 和 root 权限。

### 构建镜像

`IMAGENAME` 替换为自己的镜像名：

```bash
docker build -f dockerfile -t IMAGENAME .
```

### 创建并进入容器

`CONTAINERNAME` 和 `IMAGENAME` 按实际情况替换。下面命令会将当前项目目录挂载到容器的 `/home`：

```bash
docker run --privileged -itd -v "$(pwd)":/home --name CONTAINERNAME IMAGENAME /bin/bash
docker exec -it CONTAINERNAME bash
cd /home
```

如不使用 Docker，请确保本机已安装：

- Python 3 与 `requirements.txt` 中的依赖。
- Rust/Cargo。
- Mahimahi。
- `iproute2`、`tc`、network namespace 相关工具。
- 运行网络测试所需的 root 权限。

Python 依赖安装：

```bash
pip install -r requirements.txt
```

## 配置大模型接口

算法生成和变异优化脚本会读取 `cca_generate/Config.py`。使用前请修改：

```python
self.model_name = "模型名称"
self.api_key = "API-KEY"
self.base_url = "BASE_URL"
```

## 生成初始算法

进入 `cca_generate` 目录后运行：

```bash
cd cca_generate
python run_algorithm_generation.py --app stream --network cellular --strategy cot
```

常用参数：

| 参数 | 可选值 | 说明 |
| --- | --- | --- |
| `--app` | `stream`、`rpc`、`video` | 目标应用类型。 |
| `--network` | `cellular`、`satellite`、`wan` | 目标网络场景。 |
| `--strategy` | `zero-shot`、`one-shot`、`few-shot`、`cot` | 初始生成提示策略。 |
| `--repair-attempts` | 整数 | 生成后自动编译修复次数，默认读取 `LLMGEN_REPAIR_ATTEMPTS` 或使用 `2`。 |

生成结果会保存为：

- 算法代码：`congestion_control_algorithm/cca_generatedN.rs`
- 生成日志：`logs/cca_optimization_logN.md`
- 编译检查日志：`logs/cca_compile_repairN_attemptM.log`

查看更多参数：

```bash
python run_algorithm_generation.py --help
```

## 变异优化算法

变异脚本会读取已有测试结果、历史日志和算法代码，选择并优化指定版本：

```bash
cd cca_generate
python mutate_algorithm.py --num 1 --app stream --network cellular
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--num` | 指定要作为父版本优化的算法编号。例如已有 `cca_generated1.rs`、`cca_generated2.rs`、`cca_generated3.rs`，传入 `--num 2` 表示以 `cca_generated2.rs` 为父版本，并参考最新测试记录继续变异。 |
| `--name` | 算法标签名前缀，默认 `llmgen`。一般无需修改，除非你也修改了 TQUIC 中的算法命名逻辑。 |
| `--app` | 优化面向的应用类型：`stream`、`rpc`、`video`。 |
| `--network` | 优化面向的网络场景：`cellular`、`satellite`、`wan`。 |
| `--repair-attempts` | 自动编译修复次数。 |

查看更多参数：

```bash
python mutate_algorithm.py --help
```

## 运行 Mahimahi 测试

`run_evaluation.sh` 会编译 `tquic`，启动 TQUIC 服务端，遍历 `bin/traces/$NETWORK` 下的 trace，并通过 Mahimahi 运行客户端测试。

用法：

```bash
sh ./run_evaluation.sh GENERATED_FILENAME "CCANAME" APP NETWORK DATASIZE DURATION
```

参数说明：

| 参数 | 说明 |
| --- | --- |
| `GENERATED_FILENAME` | 要测试的生成算法文件名，例如 `cca_generated1.rs`。当 `CCANAME` 包含 `llmgen` 时，脚本会将该文件复制到 `tquic/src/congestion_control/llmgen.rs`。 |
| `CCANAME` | 要测试的拥塞控制算法列表，例如 `"bbr cubic copa llmgen"`。也可以只测 `"bbr"` 或 `"llmgen"`。 |
| `APP` | 应用类型：`stream`、`rpc`、`video`。脚本会转换为 TQUIC 所需的大写 mode。 |
| `NETWORK` | trace 子目录名。Mahimahi 测试通常使用 `cellular`。 |
| `DATASIZE` | 传输数据量限制。设为 `0` 表示不按数据量停止。 |
| `DURATION` | 测试持续时间，单位为秒。 |

示例：

```bash
sh ./run_evaluation.sh cca_generated1.rs "bbr cubic copa llmgen" stream cellular 0 5
```

结果默认写入：

- `bin/results/output.csv`
- `bin/results/convergence/`
- `bin/results/convergence_summary.csv`

## 运行 tc + namespace 测试

`run_tc.sh` 不使用 Mahimahi，而是通过 `tc + network namespace + Python trace replay` 搭建网络环境，适合 satellite 和 WAN 场景。

用法与 `run_evaluation.sh` 基本一致：

```bash
sh ./run_tc.sh GENERATED_FILENAME "CCANAME" APP NETWORK DATASIZE DURATION
```

其中 `NETWORK` 当前主要支持：

- `satellite`：读取 `bin/traces/satellite/` 下的 LENS/Starlink CSV trace，并调用 `replay_lens.py`。
- `wan`：使用 `replay_wan.py` 中的 WAN 配置。

示例：

```bash
sh ./run_tc.sh cca_generated1.rs "bbr cubic copa llmgen" stream satellite 0 30
```

结果同样默认写入 `bin/results/`。

## 公平性测试

公平性测试用于检查生成算法与其他算法共享瓶颈时的吞吐占比和 Jain fairness index。

用法：

```bash
./run_fairness_suite.sh GENERATED_FILENAME APP NETWORK DATASIZE DURATION
```

示例：

```bash
./run_fairness_suite.sh cca_generated1.rs stream cellular 0 30
```

默认对手算法由环境变量 `FAIRNESS_OPPONENTS` 控制：

```bash
FAIRNESS_OPPONENTS="bbr cubic copa llmgen"
```

常用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RUN_FAIRNESS` | `1` | 设为 `0` 可跳过公平性测试。 |
| `FAIRNESS_DURATION` | 与传入 `DURATION` 相同 | 公平性测试持续时间。 |
| `FAIRNESS_BANDWIDTH_MBIT` | `50` | 瓶颈带宽。 |
| `FAIRNESS_DELAY_MS` | `50` | 单向/配置延迟参数。 |
| `FAIRNESS_LOSS_PERCENT` | `0` | 丢包率。 |
| `FAIRNESS_QUEUE_PKTS` | `1000` | 队列长度。 |
| `FAIRNESS_SKIP_BUILD` | `1` | 默认复用已有 `bin/tquic_server` 和 `bin/tquic_client`。若还未编译过，请设为 `0`。 |

如果之前没有编译并复制过 TQUIC 可执行文件，可以这样运行：

```bash
FAIRNESS_SKIP_BUILD=0 ./run_fairness_suite.sh cca_generated1.rs stream cellular 0 30
```

公平性结果默认写入：

```text
bin/results/fairness.csv
```

## 收敛性测试

`run_evaluation.sh` 和 `run_tc.sh` 默认已经开启收敛性记录，会生成时间序列并汇总到 `convergence_summary.csv`。

如需单独测试运行时收敛性，可以使用：

```bash
sudo ./run_convergence.sh CCAFILENAME ALGO APP NETWORK_LABEL DURATION BANDWIDTH_MBIT DELAY_MS LOSS_PERCENT DATASIZE
```

示例：

```bash
sudo ./run_convergence.sh cca_generated1.rs llmgen stream wan 30
```

也可以复用已编译好的二进制：

```bash
sudo SKIP_BUILD=1 ./run_convergence.sh cca_generated1.rs llmgen stream wan 30
```

结果位置：

- 时间序列：`bin/results/convergence/<RUN_ID>/timeseries.csv`
- 汇总结果：`bin/results/convergence_summary.csv`

## Trace 使用说明

### Mahimahi trace

`mahimahi/traces/` 中提供了更多 Mahimahi 可用 trace。Mahimahi 测试脚本会读取：

```text
bin/traces/$NETWORK/
```

例如 cellular 场景读取：

```text
bin/traces/cellular/
```

如果你找到新的 Mahimahi trace，可以放入对应目录后运行测试。目录名只是脚本参数的一部分，真正的网络含义以 trace 内容为准。

示例：将某个卫星网络 trace 放入 `bin/traces/cellular/` 后，也可以通过下面命令用 Mahimahi 方式测试：

```bash
sh ./run_evaluation.sh cca_generated1.rs "bbr cubic copa llmgen" stream cellular 0 5
```

### LENS/Starlink trace

`prepare_trace.py` 和 `scale_trace.py` 主要用于将 LENS/Starlink 数据转换为 `tc` 可回放格式。

从 ping 文本生成 CSV：

```bash
python prepare_trace.py -p ping.txt -o bin/traces/satellite/trace.csv --base_bw 100 --jitter 10
```

压缩 trace 时间尺度：

```bash
python scale_trace.py -i bin/traces/satellite/trace.csv -o bin/traces/satellite/trace_scaled.csv -c 60
```

当前项目中已有一个转换后的示例：

```text
bin/traces/satellite/starlink-kanazawa.csv
```

## 推荐实验流程

一次完整迭代通常按下面顺序执行：

```bash
cd cca_generate
python run_algorithm_generation.py --app stream --network cellular --strategy cot
cd ..

sh ./run_evaluation.sh cca_generated1.rs "bbr cubic copa llmgen" stream cellular 0 30
./run_fairness_suite.sh cca_generated1.rs stream cellular 0 30

cd cca_generate
python mutate_algorithm.py --num 1 --app stream --network cellular
```

随后将新生成的 `cca_generatedN.rs` 再次投入测试，循环比较 `bin/results/` 与 `logs/` 中的记录。

## 常见注意事项

- 涉及 `tc`、network namespace、公平性测试和收敛性测试的脚本通常需要 root 权限，Docker 容器请使用 `--privileged` 启动。
- `llmgen` 测试依赖 `congestion_control_algorithm/` 中对应的 `cca_generatedN.rs` 文件。
- `run_evaluation.sh` 和 `run_tc.sh` 会重新编译 `tquic`，耗时较长是正常现象。
- 多次测试默认以 append 方式写入 `bin/results/output.csv`，对比实验前请确认是否需要手动备份或清理旧结果。
- 如果只想测试已有二进制，可在支持的脚本中使用 `SKIP_BUILD=1` 或对应的 `*_SKIP_BUILD` 环境变量。
