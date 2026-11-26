#!/bin/bash
# tc / network-namespace evaluation for WAN and satellite scenarios.
#
# Topology (3 network namespaces, router in the middle):
#
#     client_ns(10.0.1.2) --r_cli--[router_ns]--r_srv-- server_ns(10.0.2.2)
#
# The router owns the emulated bottleneck, so end-host send/receive is not
# coupled with tc.  replay_wan.py / replay_lens.py configure both router
# egress directions (uplink = r_srv, downlink = r_cli).
#
# Usage: run_tc.sh CCAFILENAME "ALGOS" APP NETWORK DATASIZE DURATION
#
# Environment:
#   WAN_PROFILE        WAN scenario profile (default trans_oceanic)
#   TRACE_START_S      trace replay offset in seconds (default 0)
#   RUN_CONVERGENCE    1 to collect client timeseries (default 1)
#   RESULTS_DIR        output directory
#   SKIP_BUILD         1 to reuse existing binaries
set -u

CCAFILENAME=${1:-"my_cca_plugin"}
CONGESTION_CONTROL_ALGORITHM=${2:-"bbr"}
MODE=$(printf '%s' "${3:-}" | tr '[:lower:]' '[:upper:]')
NETWORK=$4
DATASIZE=$5
DURATION=$6
CCA_DIR=${CCA_DIR:-./congestion_control_algorithm}
RESULTS_DIR=${RESULTS_DIR:-./results}
RUN_CONVERGENCE=${RUN_CONVERGENCE:-1}
CONVERGENCE_SAMPLE_INTERVAL_MS=${CONVERGENCE_SAMPLE_INTERVAL_MS:-1000}
CONVERGENCE_STEADY_TAIL_RATIO=${CONVERGENCE_STEADY_TAIL_RATIO:-0.2}
CONVERGENCE_THRESHOLD_RATIO=${CONVERGENCE_THRESHOLD_RATIO:-0.9}
CONVERGENCE_HOLD_SAMPLES=${CONVERGENCE_HOLD_SAMPLES:-3}
SKIP_BUILD=${SKIP_BUILD:-0}
TRACEDIR=${TRACEDIR:-"./traces"}

SERVER_IP="10.0.2.2"
CLIENT_IP="10.0.1.2"
ROUTER_CLIENT_IP="10.0.1.1"
ROUTER_SERVER_IP="10.0.2.1"
PORT="8443"

SERVER_NS="server_ns"
ROUTER_NS="router_ns"
CLIENT_NS="client_ns"
REPLAY_PID=""

export RESULTS_DIR DURATION

LOGFILE="$RESULTS_DIR/output.csv"
mkdir -p "$RESULTS_DIR" "$RESULTS_DIR/convergence"
chmod 777 "$RESULTS_DIR" "$RESULTS_DIR/convergence" 2>/dev/null || true
if [ ! -f "$LOGFILE" ]; then
    printf 'algorithm,trace,duration,throughput,srtt,min_rtt,rttvar\n' > "$LOGFILE"
fi

cleanup() {
    echo ""
    echo "[*] 清理网络环境..."
    if [ -n "$SERVER_PID" ]; then
        kill -9 "$SERVER_PID" 2>/dev/null || true
    fi
    pkill -f "replay_wan.py" 2>/dev/null || true
    pkill -f "replay_lens.py" 2>/dev/null || true
    if [ -n "$REPLAY_PID" ]; then
        kill "$REPLAY_PID" 2>/dev/null || true
    fi
    for ns in "$CLIENT_NS" "$ROUTER_NS" "$SERVER_NS"; do
        ip netns delete "$ns" 2>/dev/null || true
    done
    echo "[*] 清理完成。"
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------- build
if [ "$SKIP_BUILD" != "1" ]; then
    if printf ' %s ' "$CONGESTION_CONTROL_ALGORITHM" | grep -qi ' llmgen '; then
        if [ ! -f "$CCA_DIR/$CCAFILENAME" ]; then
            echo "❌ llmgen requires $CCA_DIR/$CCAFILENAME"
            exit 1
        fi
        cp "$CCA_DIR/$CCAFILENAME" ./tquic/src/congestion_control/llmgen.rs
        echo "cp $CCAFILENAME completed"
    fi
    ( cd ./tquic && cargo build --all --release ) || exit 1
    ( cd ./tquic/iperf && CARGO_TARGET_DIR=../target cargo build --release ) || exit 1
    cp ./tquic/target/release/tquic_perf ./bin/tquic_server || exit 1
    cp ./tquic/target/release/tquic_perf ./bin/tquic_client || exit 1
fi
cd ./bin || exit 1

# ---------------------------------------------------------------- topology
echo "[1/4] 初始化网络拓扑 (client_ns / router_ns / server_ns)..."
for ns in "$CLIENT_NS" "$ROUTER_NS" "$SERVER_NS"; do
    ip netns delete "$ns" 2>/dev/null || true
    ip netns add "$ns" || exit 1
    ip -n "$ns" link set lo up
done

ip link add cli0 netns "$CLIENT_NS" type veth peer name r_cli netns "$ROUTER_NS" || exit 1
ip link add srv0 netns "$SERVER_NS" type veth peer name r_srv netns "$ROUTER_NS" || exit 1

ip -n "$CLIENT_NS" addr add "$CLIENT_IP/24" dev cli0
ip -n "$CLIENT_NS" link set cli0 up
ip -n "$CLIENT_NS" route add default via "$ROUTER_CLIENT_IP"

ip -n "$ROUTER_NS" addr add "$ROUTER_CLIENT_IP/24" dev r_cli
ip -n "$ROUTER_NS" addr add "$ROUTER_SERVER_IP/24" dev r_srv
ip -n "$ROUTER_NS" link set r_cli up
ip -n "$ROUTER_NS" link set r_srv up
ip netns exec "$ROUTER_NS" sysctl -w net.ipv4.ip_forward=1 >/dev/null

# Disable reverse-path filtering: both directions traverse the router, and a
# strict rp_filter would drop packets whose reverse route uses the other leg.
for ns in "$CLIENT_NS" "$ROUTER_NS" "$SERVER_NS"; do
    ip netns exec "$ns" sysctl -w net.ipv4.conf.all.rp_filter=0 >/dev/null 2>&1 || true
    for dev in lo cli0 srv0 r_cli r_srv; do
        ip netns exec "$ns" sysctl -w "net.ipv4.conf.${dev}.rp_filter=0" >/dev/null 2>&1 || true
    done
done

ip -n "$SERVER_NS" addr add "$SERVER_IP/24" dev srv0
ip -n "$SERVER_NS" link set srv0 up
ip -n "$SERVER_NS" route add default via "$ROUTER_SERVER_IP"

# ---------------------------------------------------------------- server
echo "[2/4] 启动 TQUIC 服务端..."
ip netns exec "$SERVER_NS" ./tquic_server -s -l "0.0.0.0:${PORT}" --mode "$MODE" \
    >> server.log 2>&1 &
SERVER_PID=$!
sleep 1
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "[!] 服务端启动失败:"; tail -20 server.log; exit 1
fi
echo "[+] 服务端 PID:$SERVER_PID ($SERVER_NS:$SERVER_IP)"

# ---------------------------------------------------------------- traces
TRACE_LIST=()
if [ "$NETWORK" = "satellite" ]; then
    for f in "$TRACEDIR/$NETWORK"/*.csv; do
        [ -f "$f" ] && TRACE_LIST+=("$(basename "$f")")
    done
else
    TRACE_LIST=("wan")
fi
if [ ${#TRACE_LIST[@]} -eq 0 ]; then
    echo "❌ no traces in $TRACEDIR/$NETWORK"; exit 2
fi
echo "[*] traces: ${TRACE_LIST[*]}"

# ---------------------------------------------------------------- run
echo "[3/4] 开始测试..."
for TRACE in "${TRACE_LIST[@]}"; do
    for ALGO in $CONGESTION_CONTROL_ALGORITHM; do
        CONVERGENCE_CLIENT_ARGS=""
        CONV_DIR=""
        if [ "$RUN_CONVERGENCE" != "0" ]; then
            RUN_ID=$(printf '%s' "$(date '+%Y%m%d_%H%M%S')_${ALGO}_${NETWORK}_${TRACE}_$$" | tr -c 'A-Za-z0-9_.-' '_')
            CONV_DIR="$RESULTS_DIR/convergence/$RUN_ID"
            mkdir -p "$CONV_DIR"; chmod 777 "$CONV_DIR" 2>/dev/null || true
            CONVERGENCE_CLIENT_ARGS="--timeseries-output $CONV_DIR/timeseries.csv --sample-interval-ms $CONVERGENCE_SAMPLE_INTERVAL_MS"
        fi

        echo "[$(date '+%H:%M:%S')] $ALGO trace=$TRACE mode=$MODE"
        if [ "$NETWORK" = "satellite" ]; then
            SATELLITE_TRACE="$TRACEDIR/$NETWORK/$TRACE" python3 ../replay_lens.py \
                -t "$TRACEDIR/$NETWORK/$TRACE" --duration "$DURATION" >> "$RESULTS_DIR/replay.log" 2>&1 &
        else
            WAN_PROFILE=${WAN_PROFILE:-trans_oceanic} python3 ../replay_wan.py \
                -p "${WAN_PROFILE:-trans_oceanic}" --duration "$DURATION" >> "$RESULTS_DIR/replay.log" 2>&1 &
        fi
        REPLAY_PID=$!

        # Wait until the router actually carries the bottleneck qdisc.
        ready=0
        for _ in $(seq 1 50); do
            if ip netns exec "$ROUTER_NS" tc qdisc show dev r_srv 2>/dev/null | grep -q htb; then
                ready=1; break
            fi
            sleep 0.1
        done
        if [ "$ready" != "1" ]; then
            echo "[!] tc setup failed; see $RESULTS_DIR/replay.log"
            tail -20 "$RESULTS_DIR/replay.log"
            exit 1
        fi

        ip netns exec "$CLIENT_NS" ./tquic_client \
            --connect-to "${SERVER_IP}:${PORT}" \
            -c "$ALGO" --cca-filename "$CCAFILENAME" \
            -d "$DURATION" --trace "$TRACE" --data-size "$DATASIZE" --mode "$MODE" \
            $CONVERGENCE_CLIENT_ARGS >> "$RESULTS_DIR/output.csv" 2>&1
        CLIENT_EXIT=$?

        if [ -n "$CONV_DIR" ] && [ -s "$CONV_DIR/timeseries.csv" ]; then
            python3 ../analyze_convergence.py "$CONV_DIR/timeseries.csv" \
                --output "$RESULTS_DIR/convergence_summary.csv" --append \
                --steady-tail-ratio "$CONVERGENCE_STEADY_TAIL_RATIO" \
                --threshold-ratio "$CONVERGENCE_THRESHOLD_RATIO" \
                --hold-samples "$CONVERGENCE_HOLD_SAMPLES" >> "$RESULTS_DIR/replay.log" 2>&1
        fi

        echo "[$(date '+%H:%M:%S')]   client exit=$CLIENT_EXIT  rows=$(grep -cE '^(bbr|cubic|copa|llmgen[0-9]+|mutant),' "$LOGFILE" 2>/dev/null)"
        kill "$REPLAY_PID" 2>/dev/null || true
        pkill -f "replay_wan.py" 2>/dev/null || true
        pkill -f "replay_lens.py" 2>/dev/null || true
        sleep 1
    done
done
echo "[4/4] 完成。"
