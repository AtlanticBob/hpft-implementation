#!/usr/bin/env bash
# 28fs re-run with NEW vport direct-read measurement. aimd/mimd/miad-ceil/miad-line.
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-v2
F3=$REPO/results/miad_experiment_20260712/fair3way_run.sh
setmode(){ python3 -c "import json;r=json.load(open('$REPO/config/lab-registry.json'));r['e_params']['ad_mode']='$1';json.dump(r,open('$REPO/config/lab-registry.json','w'),indent=2)"; }
echo ">>> f3n_aimd";      bash "$F3" aimd 0.05 0.3  0.1 f3n_aimd     2>&1 | tail -1
echo ">>> f3n_mimd";      bash "$F3" mimd 0.05 0.15 0.1 f3n_mimd     2>&1 | tail -1
setmode ceil; echo ">>> f3n_miadceil"; bash "$F3" miad 0.05 0.3 0.1 f3n_miadceil 2>&1 | tail -1
setmode line; echo ">>> f3n_miadline"; bash "$F3" miad 0.05 0.3 0.1 f3n_miadline 2>&1 | tail -1
echo "F3REDO-ALL-DONE"
