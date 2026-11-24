#!/bin/bash

CONGESTION_CONTROL_ALGORITHM=$2   # 'bbr', 'copa', 'cubic', 'llmgen', 'mutant'
DELAY=10
LOSS=0.01
PACKET_BUFFER=1000
LOGMODE='append'    # 'append' or 'overwrite'
MODE=$(printf '%s' "${3:-}" | tr '[:lower:]' '[:upper:]')
CCAFILENAME=$1
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

trace_dir="./traces/$NETWORK"

LOGFILE="./bin/results/output.csv"

if [ -f "$LOGFILE" ]; then
    echo "$(date '+%F %T') 文件已存在： $LOGFILE"
else
    touch "$LOGFILE"
    echo "algorithm,trace,duration,throughput,srtt,min_rtt,rttvar" >> "$LOGFILE"
fi

cleanup() {
    echo ""
    echo "[*] 测试结束，正在清理网络环境和后台进程..."
    sleep 5
    
    # 终止 TQUIC 服务端
    if [ ! -z "$SERVER_PID" ]; then
    echo "Server Killed PID:$SERVER_PID"
    # Gracefully kill and suppress error if it already exited
    kill -9 "$SERVER_PID" 2>/dev/null || true 
    fi
    
    echo "[*] 环境清理完毕。"
}

# 捕获退出信号
trap cleanup EXIT INT TERM

if printf ' %s ' "$CONGESTION_CONTROL_ALGORITHM" | grep -qi ' llmgen '; then
    if [ ! -f "$CCA_DIR/$CCAFILENAME" ]; then
        echo "❌ llmgen requires $CCA_DIR/$CCAFILENAME"
        exit 1
    fi

    cp "$CCA_DIR/$CCAFILENAME" ./tquic/src/congestion_control/llmgen.rs
    echo "cp $CCAFILENAME completed"
fi

cd ./tquic || exit
cargo build --all --release
cp ./target/release/tquic_perf ../bin/tquic_server
cp ./target/release/tquic_perf ../bin/tquic_client

# Clean target cache if needed
# cargo clean

cd ../bin || exit
mkdir -p "$RESULTS_DIR"
mkdir -p "$RESULTS_DIR/convergence"
chmod 777 "$RESULTS_DIR"
chmod 777 "$RESULTS_DIR/convergence"

prepare_convergence_logging() {
    CONVERGENCE_CLIENT_ARGS=""
    TIMESERIES_CSV=""

    if [ "$RUN_CONVERGENCE" = "0" ]; then
        return
    fi

    RUN_ID=$(printf '%s' "$(date '+%Y%m%d_%H%M%S')_${ALGO}_${NETWORK}_${TRACE}_$$" | tr -c 'A-Za-z0-9_.-' '_')
    CONVERGENCE_RUN_DIR="$RESULTS_DIR/convergence/$RUN_ID"
    TIMESERIES_CSV="$CONVERGENCE_RUN_DIR/timeseries.csv"

    mkdir -p "$CONVERGENCE_RUN_DIR"
    chmod 777 "$CONVERGENCE_RUN_DIR"
    CONVERGENCE_CLIENT_ARGS="--timeseries-output $TIMESERIES_CSV --sample-interval-ms $CONVERGENCE_SAMPLE_INTERVAL_MS"
}

analyze_convergence_if_available() {
    if [ "$RUN_CONVERGENCE" = "0" ]; then
        return
    fi

    if [ -s "$TIMESERIES_CSV" ]; then
        python3 ../analyze_convergence.py "$TIMESERIES_CSV" \
            --output "$RESULTS_DIR/convergence_summary.csv" \
            --append \
            --steady-tail-ratio "$CONVERGENCE_STEADY_TAIL_RATIO" \
            --threshold-ratio "$CONVERGENCE_THRESHOLD_RATIO" \
            --hold-samples "$CONVERGENCE_HOLD_SAMPLES"
    else
        echo "⚠️ 未生成收敛时间序列，跳过收敛分析: $TIMESERIES_CSV"
    fi
}

if pgrep -f "tquic_server" > /dev/null
then
    echo "✅ server 正在运行 (PID: $(pgrep -f 'tquic_server'))"
    SERVER_PID=$(pgrep -f 'tquic_server')
else
    # ./tquic_server -c cert.crt -k cert.key -l 0.0.0.0:8443 --congestion-control-algor $CONGESTION_CONTROL_ALGORITHM > sever_log.log 2>&1 &
    ./tquic_server -s -l 0.0.0.0:8443 --mode $MODE >> server.log 2>&1 &
    SERVER_PID=$!
    echo "Server Running PID:$SERVER_PID"
    sleep 2
    
    # Check if the server actually stayed alive after 2 seconds
    if ! kill -0 $SERVER_PID 2>/dev/null; then
        echo "❌ Server failed to start! Check sever_log.log for details:"
        cat sever_log.log
        exit 1
    fi
fi

run_trace_test() {
    UPLINK_LOG="./results/uplink.log"
    DOWNLINK_LOG="./results/downlink.log"
    TRACE_UL="$trace_dir/$TRACE"
    TRACE_DL="$trace_dir/$TRACE"

    if [ "$LOGMODE" = "append" ]; then
        for ALGO in $CONGESTION_CONTROL_ALGORITHM; do
            prepare_convergence_logging
            su testuser -c "mm-delay $DELAY \
            mm-loss uplink $LOSS \
            mm-loss downlink $LOSS \
            mm-link \"$TRACE_UL\" \"$TRACE_DL\" \
            --uplink-queue=droptail --downlink-queue=droptail \
            --uplink-queue-args=packets=$PACKET_BUFFER --downlink-queue-args=packets=$PACKET_BUFFER \
            --uplink-log=\"$UPLINK_LOG\" --downlink-log=\"$DOWNLINK_LOG\" \
            -- \
            sh -c './tquic_client --connect-to \${MAHIMAHI_BASE}:8443 -c $ALGO --cca-filename $CCAFILENAME -d $DURATION --trace $TRACE --data-size $DATASIZE --mode $MODE $CONVERGENCE_CLIENT_ARGS'" >> "$RESULTS_DIR/output.csv" 2>&1
            analyze_convergence_if_available
        done
    else
        for ALGO in $CONGESTION_CONTROL_ALGORITHM; do
            prepare_convergence_logging
            su testuser -c "mm-delay $DELAY \
            mm-loss uplink $LOSS \
            mm-loss downlink $LOSS \
            mm-link \"$TRACE_UL\" \"$TRACE_DL\" \
            --uplink-queue=droptail --downlink-queue=droptail \
            --uplink-queue-args=packets=$PACKET_BUFFER --downlink-queue-args=packets=$PACKET_BUFFER \
            --uplink-log=\"$UPLINK_LOG\" --downlink-log=\"$DOWNLINK_LOG\" \
            -- \
            sh -c './tquic_client --connect-to \${MAHIMAHI_BASE}:8443 -c $ALGO --cca-filename $CCAFILENAME -d $DURATION --trace $TRACE --data-size $DATASIZE --mode $MODE $CONVERGENCE_CLIENT_ARGS'" > "$RESULTS_DIR/output.csv" 2>&1
            analyze_convergence_if_available
        done
    fi
    
}

for file in "$trace_dir"/*; do
  [ -f "$file" ] || continue   # 只处理普通文件
  TRACE=$(basename "$file")
  run_trace_test
done

# ./tquic_client --congestion-control-algor $CONGESTION_CONTROL_ALGORITHM \
#     --connect-to \${MAHIMAHI_BASE}:8443 \
#     https://example.org
