#!/usr/bin/env bash
# Step-3a hard case: second-scale class alternation on ONE VF (vf0).
# TCP runs continuously; RDMA square-waves (6 cycles of ~3s on / ~4s off).
# This is the shape that megaflow-mix attribution historically smeared
# 50/50 for ~2s per flip.
set -u
source "$(dirname "$0")/common.sh"
TAG=flip
start_recorders $TAG 72
iperf3 -B 10.1.0.1%dpu1vf0 -c 10.1.0.2 -p 5201 -t 56 -J > "$DIR/${TAG}_iperf.json" 2>&1 &
sleep 4
date -u +%s.%N > "$DIR/${TAG}_t0.txt"
for i in 1 2 3 4 5 6; do
  ssh -f sgpu02 "$PT -d mlx5_6 -p $((18710+i)) --report_gbits -D 3 > /tmp/${TAG}_srv$i 2>&1"
  sleep 1
  $PT -d mlx5_6 -p $((18710+i)) --report_gbits -D 3 10.1.0.2 > "$DIR/${TAG}_rdma$i.log" 2>&1
  sleep 3.5
done
wait
sleep 10
collect $TAG
echo "$TAG-done"
