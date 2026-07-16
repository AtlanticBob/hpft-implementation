#!/usr/bin/env bash
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
# rebaseline: md_anchor=zero (confirm refactor neutral vs gw_mimd 47.1%)
echo ">>> gw2_zero_r1"; bash "$DIR/giveway_run.sh" mimd gw2_zero_r1 zero 0.3 2>&1 | tail -1
# soft-floor variants L = {0.3,0.5,0.8} e_hat, x3
for frac in 0.3 0.5 0.8; do for rep in 1 2 3; do
  tag="gw2_f${frac}_r${rep}"; echo ">>> $tag"; bash "$DIR/giveway_run.sh" mimd "$tag" floor "$frac" 2>&1 | tail -1
done; done
echo "SOFTFLOOR-ALL-DONE"
