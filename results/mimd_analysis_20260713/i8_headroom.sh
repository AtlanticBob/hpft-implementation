#!/usr/bin/env bash
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-v2; D=results/mimd_analysis_20260713
echo ">>> diag_i8_hr (incast8 cap=8G, 聚合64G<<97G root, 无饱和)"
bash "$REPO/$D/incast8_run.sh" mimd zero 0.3 diag_i8_hr 8 2>&1 | tail -1
scp -q hpft-dpu:/tmp/hpft_txagent_e.jsonl "$REPO/$D/diag_i8_hr_tx.jsonl"
echo "I8HR-DONE"
