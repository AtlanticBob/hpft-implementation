#!/usr/bin/env bash
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-v2
D=results/mimd_analysis_20260713
echo ">>> diag_f3 (28fs, 20G cap, 线速不饱和)"
bash "$REPO/results/miad_experiment_20260712/fair3way_run.sh" mimd 0.05 0.15 0.1 diag_f3 2>&1 | tail -1
scp -q hpft-dpu:/tmp/hpft_txagent_e.jsonl "$REPO/results/miad_experiment_20260712/diag_f3_tx.jsonl"
echo ">>> diag_i8 (incast8, 100G root 饱和)"
bash "$REPO/results/mimd_analysis_20260713/incast8_run.sh" mimd zero 0.3 diag_i8 2>&1 | tail -1
scp -q hpft-dpu:/tmp/hpft_txagent_e.jsonl "$REPO/$D/diag_i8_tx.jsonl"
echo "RP-DIAG-DONE"
