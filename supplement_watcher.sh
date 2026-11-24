#!/bin/bash
# Wait for the main run to reach checkpoint_99 and exit, then run the one
# remaining iteration (100) from that checkpoint.
# Usage: supplement_watcher.sh <run-dir-name>
set -u
BASE="${1:?usage: supplement_watcher.sh <run-dir-name>}"
DIR="/workspace/skydiscover/outputs/adaevolve/$BASE"

while [ ! -f "$DIR/checkpoints/checkpoint_99/metadata.json" ]; do
    sleep 60
done

# Wait until the main optimize process has exited.
while ps -eo args --no-headers \
    | awk '$2 ~ /bin\/python/ && /skydiscover optimize/ {found=1} END {exit found ? 0 : 1}'; do
    sleep 30
done

cd /workspace/skydiscover
exec uv run skydiscover optimize \
    benchmarks/CCA5/initial_program.py benchmarks/CCA5/evaluator/evaluator.py \
    -c benchmarks/CCA5/config.yaml \
    --checkpoint "$DIR/checkpoints/checkpoint_99" \
    --output "$DIR" \
    --iterations 1 \
    >> "$DIR/logs/supplement_$(date +%Y%m%d_%H%M%S).log" 2>&1
