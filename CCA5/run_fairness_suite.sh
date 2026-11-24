#!/usr/bin/env bash

if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

set -uo pipefail

# Run the standard fairness checks for one generated CCA file.
#
# Usage:
#   ./run_fairness_suite.sh CCAFILENAME APP NETWORK DATASIZE DURATION
#
# Environment overrides:
#   RUN_FAIRNESS=0                 disable this suite
#   FAIRNESS_OPPONENTS="bbr cubic copa llmgen"
#   FAIRNESS_DURATION=30
#   FAIRNESS_BANDWIDTH_MBIT=50
#   FAIRNESS_DELAY_MS=50
#   FAIRNESS_LOSS_PERCENT=0
#   FAIRNESS_QUEUE_PKTS=1000
#   FAIRNESS_SKIP_BUILD=1          reuse bin/tquic_server and bin/tquic_client

CCAFILENAME=${1:?missing CCAFILENAME}
APP=${2:-stream}
NETWORK=${3:-fairness}
DATASIZE=${4:-0}
DURATION=${5:-30}

RUN_FAIRNESS=${RUN_FAIRNESS:-1}
FAIRNESS_OPPONENTS=${FAIRNESS_OPPONENTS:-"bbr cubic copa llmgen"}
FAIRNESS_DURATION=${FAIRNESS_DURATION:-$DURATION}
FAIRNESS_BANDWIDTH_MBIT=${FAIRNESS_BANDWIDTH_MBIT:-50}
FAIRNESS_DELAY_MS=${FAIRNESS_DELAY_MS:-50}
FAIRNESS_LOSS_PERCENT=${FAIRNESS_LOSS_PERCENT:-0}
FAIRNESS_QUEUE_PKTS=${FAIRNESS_QUEUE_PKTS:-1000}
FAIRNESS_SKIP_BUILD=${FAIRNESS_SKIP_BUILD:-1}
CCA_DIR=${CCA_DIR:-./congestion_control_algorithm}
RESULTS_DIR=${RESULTS_DIR:-./bin/results}

if [ "$RUN_FAIRNESS" = "0" ]; then
    echo "$(date '+%F %T') RUN_FAIRNESS=0，跳过公平性测试。"
    exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "$(date '+%F %T') 公平性测试需要 root 权限。请用 root/sudo 运行主流程，或设置 RUN_FAIRNESS=0 跳过。"
    exit 1
fi

if [ ! -f "$CCA_DIR/$CCAFILENAME" ]; then
    echo "$(date '+%F %T') 找不到待测 CCA 文件: $CCA_DIR/$CCAFILENAME"
    exit 1
fi

echo "$(date '+%F %T') Running fairness suite for $CCAFILENAME ..."

for opponent in $FAIRNESS_OPPONENTS; do
    echo "$(date '+%F %T') Fairness: llmgen vs $opponent"
    CCA_DIR="$CCA_DIR" RESULTS_DIR="$RESULTS_DIR" \
    QUEUE_PKTS="$FAIRNESS_QUEUE_PKTS" SKIP_BUILD="$FAIRNESS_SKIP_BUILD" \
        ./run_fairness.sh \
        llmgen "$opponent" "$APP" "$NETWORK" "$FAIRNESS_DURATION" \
        "$FAIRNESS_BANDWIDTH_MBIT" "$FAIRNESS_DELAY_MS" "$FAIRNESS_LOSS_PERCENT" \
        "$DATASIZE" "$CCAFILENAME"

    rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "$(date '+%F %T') 公平性测试失败: llmgen vs $opponent, rc=$rc"
        exit "$rc"
    fi
done

echo "$(date '+%F %T') Fairness suite completed for $CCAFILENAME"
