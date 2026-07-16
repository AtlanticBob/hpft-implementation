#!/usr/bin/env bash
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
for cfg in zero:0.3 floor:0.5 floor:0.8; do
  mda=${cfg%:*}; mdf=${cfg#*:}; tag="ci_${mda}${mdf}"
  echo ">>> $tag"; bash "$DIR/crashincast_run.sh" "$mda" "$mdf" "$tag" 2>&1 | tail -1
done
echo "CRASHINCAST-ALL-DONE"
