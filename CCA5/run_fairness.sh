#!/usr/bin/env bash

if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

set -euo pipefail

# Fairness test for two concurrent TQUIC flows sharing one bottleneck.
#
# Usage:
#   ./run_fairness.sh ALGO1 ALGO2 [MODE] [NETWORK_LABEL] [DURATION] [BANDWIDTH_MBIT] [DELAY_MS] [LOSS_PERCENT] [DATASIZE] [CCAFILENAME]
#
# Example:
#   ./run_fairness.sh mutant cubic stream wan 30
#   ./run_fairness.sh mutant bbr stream fairness 60 50 50 0
#
# Notes:
#   - Requires root privileges because it creates network namespaces and tc qdiscs.
#   - If either ALGO is "llmgen", CCAFILENAME is copied into tquic/src/congestion_control/llmgen.rs before build.
#   - Set SKIP_BUILD=1 to reuse bin/tquic_server and bin/tquic_client.

ALGO1=${1:-llmgen}
ALGO2=${2:-cubic}
MODE=$(printf '%s' "${3:-stream}" | tr '[:lower:]' '[:upper:]')
NETWORK_LABEL=${4:-fairness}
DURATION=${5:-30}
BANDWIDTH_MBIT=${6:-50}
DELAY_MS=${7:-50}
LOSS_PERCENT=${8:-0}
DATASIZE=${9:-0}
CCAFILENAME=${10:-cca_generated1.rs}
QUEUE_PKTS=${QUEUE_PKTS:-1000}
PORT=${PORT:-8443}

ROOT_DIR=$(cd "$(dirname "$0")" && pwd)
BIN_DIR="$ROOT_DIR/bin"
CCA_DIR=${CCA_DIR:-"$ROOT_DIR/congestion_control_algorithm"}
RESULTS_DIR=${RESULTS_DIR:-"$BIN_DIR/results"}

case "$CCA_DIR" in
    /*) ;;
    *) CCA_DIR="$ROOT_DIR/$CCA_DIR" ;;
esac

case "$RESULTS_DIR" in
    /*) ;;
    *) RESULTS_DIR="$ROOT_DIR/$RESULTS_DIR" ;;
esac

FAIRNESS_CSV="$RESULTS_DIR/fairness.csv"
RUN_ID="$(date +%Y%m%d_%H%M%S)_${ALGO1}_vs_${ALGO2}"
RUN_DIR="$RESULTS_DIR/fairness/$RUN_ID"

SERVER_NS="fair_server_ns"
ROUTER_NS="fair_router_ns"
CLIENT1_NS="fair_client1_ns"
CLIENT2_NS="fair_client2_ns"

SERVER_IP="10.70.0.2"
ROUTER_SERVER_IP="10.70.0.1"
CLIENT1_IP="10.70.1.2"
ROUTER_CLIENT1_IP="10.70.1.1"
CLIENT2_IP="10.70.2.2"
ROUTER_CLIENT2_IP="10.70.2.1"

SERVER_BIN="$BIN_DIR/tquic_server"
CLIENT_BIN="$BIN_DIR/tquic_client"

SERVER_PID=""
CLIENT1_PID=""
CLIENT2_PID=""

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "缺少命令: $1"
        exit 1
    fi
}

cleanup() {
    set +e
    echo ""
    echo "[*] 清理公平性测试环境..."

    for pid in "$CLIENT1_PID" "$CLIENT2_PID" "$SERVER_PID"; do
        if [ -n "$pid" ]; then
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
    done

    for ns in "$CLIENT1_NS" "$CLIENT2_NS" "$SERVER_NS" "$ROUTER_NS"; do
        if ip netns list | awk '{print $1}' | grep -qx "$ns"; then
            ip netns pids "$ns" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
            ip netns delete "$ns" 2>/dev/null || true
        fi
    done

    echo "[*] 清理完成。"
}

copy_llmgen_if_needed() {
    if printf ' %s %s ' "$ALGO1" "$ALGO2" | grep -qi ' llmgen '; then
        local src="$CCA_DIR/$CCAFILENAME"
        if [ ! -f "$src" ]; then
            echo "llmgen 需要文件: $src"
            exit 1
        fi
        cp "$src" "$ROOT_DIR/tquic/src/congestion_control/llmgen.rs"
        echo "[+] 已复制 llmgen 文件: $CCAFILENAME"
    fi
}

build_binaries() {
    mkdir -p "$RESULTS_DIR" "$RESULTS_DIR/fairness"

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

create_veth_pair() {
    local left_ns=$1
    local left_if=$2
    local left_ip=$3
    local right_ns=$4
    local right_if=$5
    local right_ip=$6

    ip link add "$left_if" type veth peer name "$right_if"
    ip link set "$left_if" netns "$left_ns"
    ip link set "$right_if" netns "$right_ns"

    ip -n "$left_ns" addr add "$left_ip/24" dev "$left_if"
    ip -n "$right_ns" addr add "$right_ip/24" dev "$right_if"

    ip -n "$left_ns" link set "$left_if" up
    ip -n "$right_ns" link set "$right_if" up
}

setup_network() {
    echo "[2/5] 创建共享瓶颈拓扑..."
    cleanup >/dev/null 2>&1 || true

    ip netns add "$SERVER_NS"
    ip netns add "$ROUTER_NS"
    ip netns add "$CLIENT1_NS"
    ip netns add "$CLIENT2_NS"

    for ns in "$SERVER_NS" "$ROUTER_NS" "$CLIENT1_NS" "$CLIENT2_NS"; do
        ip -n "$ns" link set lo up
    done

    create_veth_pair "$SERVER_NS" "srv0" "$SERVER_IP" "$ROUTER_NS" "r_srv0" "$ROUTER_SERVER_IP"
    create_veth_pair "$CLIENT1_NS" "cli10" "$CLIENT1_IP" "$ROUTER_NS" "r_cli10" "$ROUTER_CLIENT1_IP"
    create_veth_pair "$CLIENT2_NS" "cli20" "$CLIENT2_IP" "$ROUTER_NS" "r_cli20" "$ROUTER_CLIENT2_IP"

    ip -n "$SERVER_NS" route add default via "$ROUTER_SERVER_IP"
    ip -n "$CLIENT1_NS" route add default via "$ROUTER_CLIENT1_IP"
    ip -n "$CLIENT2_NS" route add default via "$ROUTER_CLIENT2_IP"
    ip netns exec "$ROUTER_NS" sysctl -w net.ipv4.ip_forward=1 >/dev/null

    # Shared data-path bottleneck: both clients contend on router -> server egress.
    ip netns exec "$ROUTER_NS" tc qdisc add dev r_srv0 root handle 1: htb default 10
    ip netns exec "$ROUTER_NS" tc class add dev r_srv0 parent 1: classid 1:10 \
        htb rate "${BANDWIDTH_MBIT}mbit" ceil "${BANDWIDTH_MBIT}mbit"
    ip netns exec "$ROUTER_NS" tc qdisc add dev r_srv0 parent 1:10 handle 10: \
        netem delay "${DELAY_MS}ms" loss "${LOSS_PERCENT}%" limit "$QUEUE_PKTS"

    # Reverse path delay for ACKs. These are not bottlenecks, only RTT shaping.
    ip netns exec "$ROUTER_NS" tc qdisc add dev r_cli10 root \
        netem delay "${DELAY_MS}ms" loss "${LOSS_PERCENT}%" limit "$QUEUE_PKTS"
    ip netns exec "$ROUTER_NS" tc qdisc add dev r_cli20 root \
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

run_clients() {
    echo "[4/5] 同时启动两个客户端..."
    local trace1="${NETWORK_LABEL}_${ALGO1}_vs_${ALGO2}_flow1"
    local trace2="${NETWORK_LABEL}_${ALGO1}_vs_${ALGO2}_flow2"

    cd "$BIN_DIR"

    ip netns exec "$CLIENT1_NS" "$CLIENT_BIN" \
        --connect-to "${SERVER_IP}:${PORT}" \
        -c "$ALGO1" \
        --cca-filename "$CCAFILENAME" \
        -d "$DURATION" \
        --trace "$trace1" \
        --data-size "$DATASIZE" \
        --mode "$MODE" > "$RUN_DIR/client1.log" 2>&1 &
    CLIENT1_PID=$!

    ip netns exec "$CLIENT2_NS" "$CLIENT_BIN" \
        --connect-to "${SERVER_IP}:${PORT}" \
        -c "$ALGO2" \
        --cca-filename "$CCAFILENAME" \
        -d "$DURATION" \
        --trace "$trace2" \
        --data-size "$DATASIZE" \
        --mode "$MODE" > "$RUN_DIR/client2.log" 2>&1 &
    CLIENT2_PID=$!

    local rc1=0
    local rc2=0
    wait "$CLIENT1_PID" || rc1=$?
    wait "$CLIENT2_PID" || rc2=$?
    CLIENT1_PID=""
    CLIENT2_PID=""

    if [ "$rc1" -ne 0 ] || [ "$rc2" -ne 0 ]; then
        echo "客户端异常退出: client1=$rc1 client2=$rc2"
        echo "日志目录: $RUN_DIR"
        exit 1
    fi
}

extract_throughput() {
    local log_file=$1
    awk -F',' '
        NF >= 7 && $4 ~ /^[0-9.]+$/ {
            throughput = $4
        }
        END {
            if (throughput != "") {
                print throughput
            }
        }
    ' "$log_file"
}

write_results() {
    echo "[5/5] 计算 Jain fairness index..."
    local t1
    local t2
    t1=$(extract_throughput "$RUN_DIR/client1.log")
    t2=$(extract_throughput "$RUN_DIR/client2.log")

    if [ -z "$t1" ] || [ -z "$t2" ]; then
        echo "无法解析客户端吞吐。日志目录: $RUN_DIR"
        exit 1
    fi

    local total
    local jain
    local share1
    local share2
    total=$(awk -v x="$t1" -v y="$t2" 'BEGIN { printf "%.3f", x + y }')
    jain=$(awk -v x="$t1" -v y="$t2" 'BEGIN { ss = x*x + y*y; s = x + y; if (ss == 0) printf "0.000000"; else printf "%.6f", (s*s) / (2*ss) }')
    share1=$(awk -v x="$t1" -v y="$t2" 'BEGIN { s = x + y; if (s == 0) printf "0.000000"; else printf "%.6f", x / s }')
    share2=$(awk -v x="$t1" -v y="$t2" 'BEGIN { s = x + y; if (s == 0) printf "0.000000"; else printf "%.6f", y / s }')

    mkdir -p "$RESULTS_DIR"
    if [ ! -f "$FAIRNESS_CSV" ]; then
        echo "run_id,network,mode,algo1,algo2,cca_filename,duration_s,bandwidth_mbit,delay_ms,loss_percent,queue_pkts,flow1_kbps,flow2_kbps,total_kbps,jain,flow1_share,flow2_share,log_dir" > "$FAIRNESS_CSV"
    fi

    echo "$RUN_ID,$NETWORK_LABEL,$MODE,$ALGO1,$ALGO2,$CCAFILENAME,$DURATION,$BANDWIDTH_MBIT,$DELAY_MS,$LOSS_PERCENT,$QUEUE_PKTS,$t1,$t2,$total,$jain,$share1,$share2,$RUN_DIR" >> "$FAIRNESS_CSV"

    echo ""
    echo "flow1_algo,flow2_algo,flow1_kbps,flow2_kbps,total_kbps,jain,flow1_share,flow2_share"
    echo "$ALGO1,$ALGO2,$t1,$t2,$total,$jain,$share1,$share2"
    echo ""
    echo "结果已写入: $FAIRNESS_CSV"
    echo "日志目录: $RUN_DIR"
}

main() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "该脚本需要 root 权限运行，例如: sudo ./run_fairness.sh mutant cubic stream wan 30"
        exit 1
    fi

    require_cmd ip
    require_cmd tc
    require_cmd awk
    require_cmd sysctl
    if [ "${SKIP_BUILD:-0}" != "1" ]; then
        require_cmd cargo
    fi

    trap cleanup EXIT INT TERM

    echo "公平性测试配置:"
    echo "  flow1:        $ALGO1"
    echo "  flow2:        $ALGO2"
    echo "  mode:         $MODE"
    echo "  duration:     ${DURATION}s"
    echo "  bottleneck:   ${BANDWIDTH_MBIT}mbit"
    echo "  one-way delay:${DELAY_MS}ms"
    echo "  loss:         ${LOSS_PERCENT}%"
    echo "  queue:        ${QUEUE_PKTS} packets"
    echo ""

    build_binaries
    setup_network
    start_server
    run_clients
    write_results
}

main "$@"
