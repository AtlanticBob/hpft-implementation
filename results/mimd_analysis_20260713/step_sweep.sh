#!/usr/bin/env bash
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
# main scale sweep: mimd/aimd x {2,6,20,40}G at production gain
for law in mimd aimd; do for cap in 2 6 20 40; do
  tag="st_${law}_c${cap}"; echo ">>> $tag"; bash "$DIR/step_run.sh" "$law" "$cap" "$tag" 2>&1 | tail -1
done; done
# transient gain-margin probe: mimd @20G with elevated gains
for ab in 0.3:0.6 0.6:0.6 1.2:1.2; do
  a=${ab%:*}; b=${ab#*:}; tag="stg_a${a}_b${b}"
  echo ">>> $tag"; bash "$DIR/step_run.sh" mimd 20 "$tag" "$a" "$b" 2>&1 | tail -1
done
echo "STEP-SWEEP-ALL-DONE"
