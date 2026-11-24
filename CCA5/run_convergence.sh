#!/usr/bin/env bash

if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

set -euo pipefail

# Runtime convergence test for one TQUIC congestion-control algorithm.
#
# Usage:
#   sudo ./run_convergence.sh CCAFILENAME ALGO [MODE] [NETWORK_LABEL] [DURATION] [BANDWIDTH_MBIT] [DELAY_MS] [LOSS_PERCENT] [DATASIZE]
#
# Examples:
#   sudo ./run_convergence.sh cca_generated1.rs llmgen stream wan 30
#   sudo SKIP_BUILD=1 ./run_convergence.sh cca_generated1.rs mutant stream fairness 60 50 50 0

CCAFILENAME=${1:-cca_generated2.rs}
ALGO=${2:-llmgen}
MODE=$(printf '%s' "${3:-stream}" | tr '[:lower:]' '[:upper:]')
NETWORK_LABEL=${4:-convergence}
DURATION=${5:-20}
BANDWIDTH_MBIT=${6:-50}
DELAY_MS=${7:-50}
LOSS_PERCENT=${8:-0}
DATASIZE=${9:-0}
SAMPLE_INTERVAL_MS=${SAMPLE_INTERVAL_MS:-1000}
QUEUE_PKTS=${QUEUE_PKTS:-1000}
PORT=${PORT:-8443}

ROOT_DIR=$(cd "$(dirname "$0")" && pwd)
BIN_DIR="$ROOT_DIR/bin"
RESULTS_DIR="$BIN_DIR/results"
CONVERGENCE_DIR="$RESULTS_DIR/convergence"
SUMMARY_CSV="$RESULTS_DIR/convergence_summary.csv"
RUN_ID="$(date +%Y%m%d_%H%M%S)_${ALGO}_${NETWORK_LABEL}"
RUN_DIR="$CONVERGENCE_DIR/$RUN_ID"
TIMESERIES_CSV="$RUN_DIR/timeseries.csv"

SERVER_NS="conv_server_ns"
CLIENT_NS="conv_client_ns"
SERVER_IP="10.80.0.1"
CLIENT_IP="10.80.0.2"
SERVER_BIN="$BIN_DIR/tquic_server"
CLIENT_BIN="$BIN_DIR/tquic_client"

SERVER_PID=""
DROP_PID="" # 新增：用于记录带宽突变触发器的进程ID

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "缺少命令: $1"
        exit 1
    fi
}

cleanup() {
    set +e
    echo ""
    echo "[*] 清理收敛测试环境..."

    # 清理带宽突发触发器后台进程
    if [ -n "$DROP_PID" ]; then
        kill "$DROP_PID" 2>/dev/null || true
        wait "$DROP_PID" 2>/dev/null || true
    fi

    if [ -n "$SERVER_PID" ]; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi

    for ns in "$CLIENT_NS" "$SERVER_NS"; do
        if ip netns list | awk '{print $1}' | grep -qx "$ns"; then
            ip netns pids "$ns" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
            ip netns delete "$ns" 2>/dev/null || true
        fi
    done

    echo "[*] 清理完成。"
}

copy_llmgen_if_needed() {
    if printf ' %s ' "$ALGO" | grep -qi ' llmgen '; then
        local src="$ROOT_DIR/congestion_control_algorithm/$CCAFILENAME"
        if [ ! -f "$src" ]; then
            echo "llmgen 需要文件: $src"
            exit 1
        fi
        cp "$src" "$ROOT_DIR/tquic/src/congestion_control/llmgen.rs"
        echo "[+] 已复制 llmgen 文件: $CCAFILENAME"
    fi
}

build_binaries() {
    if [ "${SKIP_BUILD:-0}" = "1" ]; then
        if [ ! -x "$SERVER_BIN" ] || [ ! -x "$CLIENT_BIN" ]; then
            echo "SKIP_BUILD=1 但缺少可执行文件: $SERVER_BIN 或 $CLIENT_BIN"
            exit 1
        fi
        echo "[+] 跳过编译，复用已有二进制。"
        return
    fi

    copy_llmgen_if_needed

    echo "[1/5] 编译 tquic_perf..."
    cd "$ROOT_DIR/tquic"
    cargo build --all --release

    mkdir -p "$BIN_DIR"
    cp "$ROOT_DIR/tquic/target/release/tquic_perf" "$SERVER_BIN"
    cp "$ROOT_DIR/tquic/target/release/tquic_perf" "$CLIENT_BIN"
    chmod +x "$SERVER_BIN" "$CLIENT_BIN"
}

setup_network() {
    echo "[2/5] 创建单流瓶颈拓扑..."
    cleanup >/dev/null 2>&1 || true

    ip netns add "$SERVER_NS"
    ip netns add "$CLIENT_NS"
    ip -n "$SERVER_NS" link set lo up
    ip -n "$CLIENT_NS" link set lo up

    ip link add conv_srv0 type veth peer name conv_cli0
    ip link set conv_srv0 netns "$SERVER_NS"
    ip link set conv_cli0 netns "$CLIENT_NS"
    ip -n "$SERVER_NS" addr add "$SERVER_IP/24" dev conv_srv0
    ip -n "$CLIENT_NS" addr add "$CLIENT_IP/24" dev conv_cli0
    ip -n "$SERVER_NS" link set conv_srv0 up
    ip -n "$CLIENT_NS" link set conv_cli0 up

    # Client upload is the data path for this benchmark.
    ip netns exec "$CLIENT_NS" tc qdisc add dev conv_cli0 root handle 1: htb default 10
    ip netns exec "$CLIENT_NS" tc class add dev conv_cli0 parent 1: classid 1:10 \
        htb rate "${BANDWIDTH_MBIT}mbit" ceil "${BANDWIDTH_MBIT}mbit"
    ip netns exec "$CLIENT_NS" tc qdisc add dev conv_cli0 parent 1:10 handle 10: \
        netem delay "${DELAY_MS}ms" loss "${LOSS_PERCENT}%" limit "$QUEUE_PKTS"

    # ACK path delay only.
    ip netns exec "$SERVER_NS" tc qdisc add dev conv_srv0 root \
        netem delay "${DELAY_MS}ms" loss "${LOSS_PERCENT}%" limit "$QUEUE_PKTS"
}

start_server() {
    echo "[3/5] 启动服务端..."
    mkdir -p "$RUN_DIR"
    cd "$BIN_DIR"

    ip netns exec "$SERVER_NS" "$SERVER_BIN" \
        -s -l "0.0.0.0:${PORT}" \
        --mode "$MODE" > "$RUN_DIR/server.log" 2>&1 &
    SERVER_PID=$!

    sleep 1
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "服务端启动失败，日志: $RUN_DIR/server.log"
        exit 1
    fi
}

# 新增：定时带宽减半触发器
schedule_bandwidth_drop() {
    # 如果运行时间不足10秒，给出警告但仍执行
    if [ "$DURATION" -le 10 ]; then
        echo "[!] 警告: DURATION(${DURATION}s) <= 10s，带宽突降可能不会在测试期间发生。"
    fi

    local HALF_BW=$(awk "BEGIN {print $BANDWIDTH_MBIT / 2}")
    echo "[*] 后台定时器已启动: 10秒后带宽将从 ${BANDWIDTH_MBIT}mbit 突降至 ${HALF_BW}mbit"

    (
        sleep 10
        # 使用 change 平滑修改带宽配置，不影响当前队列
        ip netns exec "$CLIENT_NS" tc class change dev conv_cli0 parent 1: classid 1:10 \
            htb rate "${HALF_BW}mbit" ceil "${HALF_BW}mbit"
            
        echo ""
        echo "[!] 触发器执行完毕: 当前带宽已动态调整为 ${HALF_BW}mbit"
        echo ""
    ) &
    
    DROP_PID=$!
}

run_client() {
    echo "[4/5] 启动客户端并记录时间序列..."
    cd "$BIN_DIR"

    ip netns exec "$CLIENT_NS" "$CLIENT_BIN" \
        --connect-to "${SERVER_IP}:${PORT}" \
        -c "$ALGO" \
        --cca-filename "$CCAFILENAME" \
        -d "$DURATION" \
        --trace "$NETWORK_LABEL" \
        --data-size "$DATASIZE" \
        --mode "$MODE" \
        --timeseries-output "$TIMESERIES_CSV" \
        --sample-interval-ms "$SAMPLE_INTERVAL_MS" > "$RUN_DIR/client.log" 2>&1
}

analyze_results() {
    echo "[5/5] 分析收敛时间..."
    python3 "$ROOT_DIR/analyze_convergence.py" "$TIMESERIES_CSV" \
        --output "$SUMMARY_CSV" \
        --append

    echo ""
    echo "时间序列: $TIMESERIES_CSV"
    echo "最终日志: $RUN_DIR/client.log"
    echo "汇总结果: $SUMMARY_CSV"
    echo ""
    tail -n 1 "$SUMMARY_CSV"
}

main() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "该脚本需要 root 权限运行，例如: sudo ./run_convergence.sh cca_generated1.rs llmgen stream wan 30"
        exit 1
    fi

    require_cmd ip
    require_cmd tc
    require_cmd awk
    require_cmd python3
    if [ "${SKIP_BUILD:-0}" != "1" ]; then
        require_cmd cargo
    fi

    trap cleanup EXIT INT TERM

    echo "运行时收敛测试配置:"
    echo "  algorithm:    $ALGO"
    echo "  mode:         $MODE"
    echo "  duration:     ${DURATION}s"
    echo "  bottleneck:   ${BANDWIDTH_MBIT}mbit (将在10秒后减半)"
    echo "  one-way delay:${DELAY_MS}ms"
    echo "  loss:         ${LOSS_PERCENT}%"
    echo "  sample:       ${SAMPLE_INTERVAL_MS}ms"
    echo ""

    build_binaries
    setup_network
    start_server
    
    # 在启动客户端前一刻启动倒计时
    schedule_bandwidth_drop
    
    run_client
    analyze_results
}

main "$@"