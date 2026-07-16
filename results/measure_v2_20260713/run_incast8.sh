#!/usr/bin/env bash
# Step-2 main experiment: proper TCP+RDMA incast8 (100G root, 4 straight
# pairs, each RDMA + TCP -> 8 flows, 90s) under triple recording.
# Unlike incast8_run.sh this does NOT touch the registry or restart agents:
# the live production stack keeps controlling; we only record.
set -u
source "$(dirname "$0")/common.sh"
TAG=i8m
D=92
start_recorders $TAG 118
ssh sgpu02 'pkill -f "ib_write_b[w].*264" 2>/dev/null; true'; sleep 1
SRV=""; for n in 0 1 2 3; do SRV+="nohup $PT -d mlx5_$((6+n)) -p $((26400+n)) --report_gbits -D $D >/tmp/${TAG}_srv$n 2>&1 & "; done
ssh sgpu02 "$SRV true"
sleep 8    # idle lead-in (edge detection) + server settle
date -u +%s.%N > "$DIR/${TAG}_t0.txt"
for n in 0 1 2 3; do $PT -d mlx5_$((6+n)) -p $((26400+n)) --report_gbits -D $D 10.1.$n.2 > "$DIR/${TAG}_rdma$n.log" 2>&1 & done
for n in 0 1 2 3; do iperf3 -B "10.1.$n.1%dpu1vf$n" -c 10.1.$n.2 -p $((5201+4*n)) -P4 -b 0 -t $((D-7)) -J > "$DIR/${TAG}_iperf$n.json" 2>&1 & done
wait
sleep 12
collect $TAG
echo "$TAG-done"
