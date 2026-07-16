#!/usr/bin/env bash
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-v2; D=results/mimd_analysis_20260713
sq(){ cat /sys/class/infiniband/mlx5_6/ports/1/hw_counters/packet_seq_err; }
for cfg in "4 i8b2_16" "32 i8b2_256"; do
  qn=${cfg% *}; tag=${cfg#* }; s0=$(sq)
  bash "$REPO/$D/incast8_run.sh" mimd zero 0.3 "$tag" 50 "--tclass=2" "$qn" 2>&1 | tail -1
  echo "  ${tag} seqerr_delta=$(( $(sq) - s0 ))"
done
echo "B2-TEST-DONE"
