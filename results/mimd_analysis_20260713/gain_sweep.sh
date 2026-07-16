#!/usr/bin/env bash
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
GRID="0.05:0.15 0.3:0.15 0.6:0.15 1.2:0.15 0.05:0.3 0.05:0.6 0.05:1.2 0.6:0.6 1.2:1.2"
for ab in $GRID; do
  a=${ab%:*}; b=${ab#*:}; tag="gm_a${a}_b${b}"
  echo ">>> $tag"; bash "$DIR/gain_run.sh" "$a" "$b" "$tag" 2>&1 | tail -1
done
echo "GAIN-SWEEP-ALL-DONE"
