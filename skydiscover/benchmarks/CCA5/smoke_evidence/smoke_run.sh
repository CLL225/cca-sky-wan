#!/bin/bash
# 冒烟第一级：只跑 llmgen，cellular 全部 trace，每条 5 秒
set -u
cd /workspace/CCA5 || exit 1

mkdir -p /tmp/smoke_cca
cp /workspace/skydiscover/benchmarks/CCA5/initial_program.py /tmp/smoke_cca/seed.rs
rm -rf /tmp/smoke_results

cp -p tquic/src/congestion_control/llmgen.rs /tmp/llmgen.rs.bak
echo "[备份] llmgen.rs $(md5sum /tmp/llmgen.rs.bak | cut -d' ' -f1)"

echo "[起跑] $(date +%T)  CCA_DIR=/tmp/smoke_cca  RESULTS_DIR=/tmp/smoke_results"
CCA_DIR=/tmp/smoke_cca RESULTS_DIR=/tmp/smoke_results \
  bash ./run_evaluation.sh seed.rs "llmgen" stream cellular 0 5 > /tmp/smoke.log 2>&1
rc=$?
echo "[结束] $(date +%T)  退出码=$rc"

echo
echo "=== smoke.log 尾部 45 行 ==="
tail -45 /tmp/smoke.log

echo
echo "=== /tmp/smoke_results 产出的文件 ==="
find /tmp/smoke_results -type f | sort | head -20

echo
echo "=== output.csv ==="
if [ -f /tmp/smoke_results/output.csv ]; then
    ls -l /tmp/smoke_results/output.csv
    echo "--- 内容 ---"
    cat /tmp/smoke_results/output.csv
    echo "--- 数据行数（减表头）---"
    expr "$(wc -l < /tmp/smoke_results/output.csv)" - 1
else
    echo "*** output.csv 不存在 —— 链路没出数据 ***"
fi

echo
echo "=== 还有没有残留的 client/server 进程 ==="
pgrep -af "tquic[_]clien[t]" || echo "  无 client"
pgrep -af "tquic[_]serve[r]" || echo "  无 server"

echo
cp -p /tmp/llmgen.rs.bak tquic/src/congestion_control/llmgen.rs
echo "[还原] llmgen.rs $(stat -c%s tquic/src/congestion_control/llmgen.rs) B  $(md5sum tquic/src/congestion_control/llmgen.rs | cut -d' ' -f1)"
