#!/usr/bin/env bash
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
for cfg in mimd:floor:0.5 mimd:floor:0.8 aimd:zero:0.3; do
  IFS=: read law mda mdf <<< "$cfg"
  tag="i8_${law}_${mda}${mdf}"; echo ">>> $tag"
  bash "$DIR/incast8_run.sh" "$law" "$mda" "$mdf" "$tag" 2>&1 | tail -1
done
echo "INCAST8-SWEEP-DONE"
