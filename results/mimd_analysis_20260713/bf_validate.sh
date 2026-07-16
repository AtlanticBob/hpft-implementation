#!/usr/bin/env bash
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-v2
echo ">>> f3n_mimd_bf (28fs 3v4, backlog_floor ON)"
bash "$REPO/results/miad_experiment_20260712/fair3way_run.sh" mimd 0.05 0.15 0.1 f3n_mimd_bf 2>&1 | tail -1
echo ">>> i8_mimd_bf (incast8 1v1, ON)"
bash "$REPO/results/mimd_analysis_20260713/incast8_run.sh" mimd zero 0.3 i8_mimd_bf 2>&1 | tail -1
echo ">>> gw_mimd_bf (give-way, ON, borrowing check)"
bash "$REPO/results/mimd_analysis_20260713/giveway_run.sh" mimd gw_mimd_bf zero 0.3 2>&1 | tail -1
echo "BF-VALIDATE-DONE"
