#!/bin/bash

# ==========================================
# TQUIC 拥塞控制算法自动化测试脚本 (Docker Root 专用版)
# ==========================================

# 1. 定义测试参数 (你可以通过环境变量覆盖或直接在这里修改)
CCAFILENAME=${1:-"my_cca_plugin"}   # 注入的 CCA 文件名或路径
CONGESTION_CONTROL_ALGORITHM=${2:-"bbr"}                                   # 拥塞控制算法名称
TRACEDIR=${TRACEDIR:-"./traces"}
LOGMODE='append' # 'append' or 'overwrite'
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

LOGFILE="./bin/results/output.csv"

if [ -f "$LOGFILE" ]; then
    echo "$(date '+%F %T') 文件已存在： $LOGFILE"
else
    touch "$LOGFILE"
    echo "algorithm,trace,duration,throughput,srtt,min_rtt,rttvar" >> "$LOGFILE"
fi

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


# TQUIC 可执行文件路径
SERVER_BIN="./tquic_server"
CLIENT_BIN="./tquic_client"

# 虚拟网络配置
SERVER_IP="10.0.0.1"
CLIENT_IP="10.0.0.2"
PORT="8443"

# ==========================================
# 2. 定义清理函数 (Trap)
# ==========================================
# 当脚本正常结束、被 Ctrl+C 中断或发生错误时，确保清理环境
cleanup() {
    echo ""
    echo "[*] 测试结束，正在清理网络环境和后台进程..."
    
    # 终止 TQUIC 服务端
    if [ -n "$SERVER_PID" ]; then
        kill $SERVER_PID 2>/dev/null
        wait $SERVER_PID 2>/dev/null
    fi

    # 终止可能还在运行的 Python Trace 重放脚本 (使用 pkill 强制清理)
    pkill -f "replay_lens.py" 2>/dev/null
    if [ -n "$TC_SCRIPT_PID" ]; then
        kill $TC_SCRIPT_PID 2>/dev/null
    fi

    # 删除网络命名空间 (这会自动销毁相关的 veth 虚拟网卡)
    ip netns delete server_ns 2>/dev/null
    ip netns delete client_ns 2>/dev/null
    
    echo "[*] 环境清理完毕。"
}

# 捕获退出信号
trap cleanup EXIT INT TERM

# ==========================================
# 3. 构建虚拟网络环境
# ==========================================
echo "[1/4] 正在初始化虚拟网络 (Network Namespaces)..."

# 清理可能残留的旧环境
ip netns delete server_ns 2>/dev/null
ip netns delete client_ns 2>/dev/null

# 创建命名空间
ip netns add server_ns
ip netns add client_ns

# 创建并连接 veth pair
ip link add veth_server type veth peer name veth_client
ip link set veth_server netns server_ns
ip link set veth_client netns client_ns

# 配置 IP 并启动网卡
ip -n server_ns addr add ${SERVER_IP}/24 dev veth_server
ip -n server_ns link set veth_server up
ip -n server_ns link set lo up

ip -n client_ns addr add ${CLIENT_IP}/24 dev veth_client
ip -n client_ns link set veth_client up
ip -n client_ns link set lo up

# ==========================================
# 4. 启动 TQUIC 服务端
# ==========================================
echo "[2/4] 正在启动 TQUIC 服务端..."
# 在 server_ns 命名空间中启动服务端，监听 0.0.0.0
ip netns exec server_ns ${SERVER_BIN} -s -l 0.0.0.0:${PORT} --mode $MODE >> server.log 2>&1 &
SERVER_PID=$!

# 给服务端 1 秒钟的启动时间
sleep 1

# 检查服务端是否成功拉起
if ! kill -0 $SERVER_PID 2>/dev/null; then
    echo "[!] 错误: TQUIC 服务端启动失败，请检查 server.log"
    exit 1
fi
echo "[+] 服务端启动成功 (PID: $SERVER_PID)"

# ==========================================
# 5. 启动 TQUIC 客户端进行测试
# ==========================================
echo "[3/4] 正在启动 TQUIC 客户端，开始注入测试..."
echo "      CCA文件:  ${CCAFILENAME}"
echo "      测试目标: ${SERVER_IP}:${PORT}"

run_trace_test() {
    if [ "$LOGMODE" = "append" ]; then
        for ALGO in $CONGESTION_CONTROL_ALGORITHM; do
            # 移除 sudo，直接后台运行
            if [ "$NETWORK" = "satellite" ]; then
                echo "启动网络损伤模拟 (Trace: $TRACE, Algo: $ALGO)..."
                python3 ../replay_lens.py -t "$TRACEDIR/$NETWORK/$TRACE" &
                TC_SCRIPT_PID=$!
                sleep 1
            else
                TRACE="wan"
                echo "启动网络损伤模拟 (Trace: $TRACE, Algo: $ALGO)..."
                python3 ../replay_wan.py -t "$TRACEDIR/$NETWORK/$TRACE" &
                TC_SCRIPT_PID=$!
                sleep 1
            fi

            prepare_convergence_logging
            
            # 在 client_ns 命名空间中启动客户端
            ip netns exec client_ns ${CLIENT_BIN} \
                --connect-to ${SERVER_IP}:${PORT} \
                -c ${ALGO} \
                --cca-filename ${CCAFILENAME} \
                -d ${DURATION} \
                --trace ${TRACE} \
                --data-size ${DATASIZE} \
                --mode ${MODE} \
                ${CONVERGENCE_CLIENT_ARGS} >> "$RESULTS_DIR/output.csv" 2>&1

            CLIENT_EXIT_CODE=$?
            analyze_convergence_if_available

            if [ $CLIENT_EXIT_CODE -eq 0 ]; then
                echo "[+] 测试成功完成！"
            else
                echo "[!] 客户端异常退出，退出码: ${CLIENT_EXIT_CODE}"
            fi

            # 每次客户端运行完，立刻终止该 Trace 的 Python 重放进程
            echo "正在停止当前网络损伤模拟..."
            pkill -f "replay_lens.py" 2>/dev/null
            if [ -n "$TC_SCRIPT_PID" ]; then
                kill $TC_SCRIPT_PID 2>/dev/null
            fi
            # 稍作等待确保网卡规则清空
            sleep 1
        done
    else
        for ALGO in $CONGESTION_CONTROL_ALGORITHM; do
            if [ "$NETWORK" = "satellite" ]; then
                echo "启动网络损伤模拟 (Trace: $TRACE, Algo: $ALGO)..."
                python3 ../replay_lens.py -t "$TRACEDIR/$NETWORK/$TRACE" &
                TC_SCRIPT_PID=$!
                sleep 1
            else
                TRACE="wan"
                echo "启动网络损伤模拟 (Trace: $TRACE, Algo: $ALGO)..."
                python3 ../replay_wan.py &
                TC_SCRIPT_PID=$!
                sleep 1
            fi

            prepare_convergence_logging

            # 在 client_ns 命名空间中启动客户端
            ip netns exec client_ns ${CLIENT_BIN} \
                --connect-to ${SERVER_IP}:${PORT} \
                -c ${ALGO} \
                --cca-filename ${CCAFILENAME} \
                -d ${DURATION} \
                --trace ${TRACE} \
                --data-size ${DATASIZE} \
                --mode ${MODE} \
                ${CONVERGENCE_CLIENT_ARGS} > "$RESULTS_DIR/output.csv" 2>&1

            CLIENT_EXIT_CODE=$?
            analyze_convergence_if_available

            if [ $CLIENT_EXIT_CODE -eq 0 ]; then
                echo "[+] 测试成功完成！"
            else
                echo "[!] 客户端异常退出，退出码: ${CLIENT_EXIT_CODE}"
            fi

            # 每次客户端运行完，立刻终止该 Trace 的 Python 重放进程
            echo "正在停止当前网络损伤模拟..."
            pkill -f "replay_lens.py" 2>/dev/null
            if [ -n "$TC_SCRIPT_PID" ]; then
                kill $TC_SCRIPT_PID 2>/dev/null
            fi
            # 稍作等待确保网卡规则清空
            sleep 1
        done
    fi
}

# ==========================================
# 6. 批量执行 Trace 测试
# ==========================================
for file in "$TRACEDIR/$NETWORK"/*; do
    [ -f "$file" ] || continue   # 只处理普通文件
    TRACE=$(basename "$file")
    run_trace_test
done

# 脚本执行完毕，将自动触发 trap 中的 cleanup 函数
