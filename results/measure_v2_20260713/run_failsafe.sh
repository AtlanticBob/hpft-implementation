#!/usr/bin/env bash
# Step-3c fail-safe drill: kill vport_meter mid-run, verify the bypass
# agent degrades to the megaflow fallback within STALE_S and recovers when
# the helper restarts. Traffic: RDMA vf0 + TCP vf1, single class per VF
# (so the megaflow fallback itself is exact and any r_f glitch is the
# handover, not the fallback).
set -u
source "$(dirname "$0")/common.sh"
TAG=fs
start_recorders $TAG 45
ssh -f sgpu02 "$PT -d mlx5_6 -p 18720 --report_gbits -D 32 > /tmp/${TAG}_srv 2>&1"
sleep 2
$PT -d mlx5_6 -p 18720 --report_gbits -D 32 10.1.0.2 > "$DIR/${TAG}_rdma.log" 2>&1 &
iperf3 -B 10.1.1.1%dpu1vf1 -c 10.1.1.2 -p 5205 -t 32 -J > "$DIR/${TAG}_iperf.json" 2>&1 &
sleep 10
date -u +%s.%N > "$DIR/${TAG}_tkill.txt"
ssh hpft-dpu2 'sudo pkill -x vport_meter'
sleep 8
date -u +%s.%N > "$DIR/${TAG}_trestart.txt"
ssh -f hpft-dpu2 "sudo timeout 30 /tmp/vport_meter mlx5_1 /dev/shm/hpft_vpm 1,2,3,4 1 >> /tmp/${TAG}_vpm.log 2>&1"
wait
sleep 10
collect $TAG
echo "$TAG-done"
