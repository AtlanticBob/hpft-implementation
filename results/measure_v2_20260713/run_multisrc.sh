#!/usr/bin/env bash
# Step-3b hard case: intra-class multi-sender split. dst = sgpu02/vf0.
# TCP vf0->vf0 continuous; RDMA vf0->vf0 continuous 42s; RDMA vf3->vf0
# joins at t=+18 for 15s then leaves (vf0/vf3 pair has no flowtag
# collision). Class totals come from the meter; the per-sender sub-split
# is still megaflow-mix - this run measures that residual lag.
set -u
source "$(dirname "$0")/common.sh"
TAG=ms
start_recorders $TAG 70
ssh -f sgpu02 "$PT -d mlx5_6 -p 26410 --report_gbits -D 42 > /tmp/${TAG}_srv0 2>&1"
sleep 2
date -u +%s.%N > "$DIR/${TAG}_t0.txt"
iperf3 -B 10.1.0.1%dpu1vf0 -c 10.1.0.2 -p 5201 -t 42 -J > "$DIR/${TAG}_iperf.json" 2>&1 &
$PT -d mlx5_6 -p 26410 --report_gbits -D 42 10.1.0.2 > "$DIR/${TAG}_rdma_vf0.log" 2>&1 &
sleep 16
ssh -f sgpu02 "$PT -d mlx5_6 -p 26411 --report_gbits -D 15 > /tmp/${TAG}_srv1 2>&1"
sleep 2
date -u +%s.%N > "$DIR/${TAG}_tjoin.txt"
$PT -d mlx5_9 -p 26411 --report_gbits -D 15 10.1.0.2 > "$DIR/${TAG}_rdma_vf3.log" 2>&1
date -u +%s.%N > "$DIR/${TAG}_tleave.txt"
wait
sleep 10
collect $TAG
echo "$TAG-done"
