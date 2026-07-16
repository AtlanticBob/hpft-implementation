#!/usr/bin/env bash
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
for law in mimd aimd; do for rep in 1 2 3; do
  tag="gw_${law}_r${rep}"; echo ">>> $tag"; bash "$DIR/giveway_run.sh" "$law" "$tag" 2>&1 | tail -1
done; done
echo "GIVEWAY-ALL-DONE"
