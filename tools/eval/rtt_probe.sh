#!/usr/bin/env bash
# Measure this fabric's RTT as the PCC device sees it.
#
# ZTR-RTTCC compares its measurement against BASE_RTT, so BASE_RTT has to
# be this deployment's base RTT in the DEVICE's own timer units -- not a
# number carried over from the reference algorithm's testbed. The device
# records {last, min, count} per pair on every RTT event; probe 0xdec
# returns rtt_last/rtt_min in words 6/7, global rtt_events in word 8 and
# the pair's rtt_n in word 10; this script reads them off the RP log.
#
# usage: rtt_probe.sh [seconds] [qps]
#   Runs one RDMA flow-set so RTT events flow, then samples the probe.
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-implementation
. "$REPO/tools/eval/eval_lib.sh"
DUR=${1:-20}; QPS=${2:-4}

traffic_clear
ssh hpft-dpu 'sudo truncate -s 0 /tmp/pcc_rp.log 2>/dev/null; true'
rdma_server mlx5_6 29800 "$QPS" "$DUR"; sleep 1.5
rdma_client mlx5_6 29800 "$QPS" "$DUR" 10.1.0.2 /tmp/rttprobe_flow.log &
sleep $((DUR / 2))
for i in 0 1 2 3; do ssh hpft-dpu "echo '0xdec $i' > /tmp/rp_fifo"; sleep 0.3; done
wait
traffic_clear

echo "== device-observed RTT (device timer units) =="
ssh hpft-dpu 'grep -a "HPFT_RSP" /tmp/pcc_rp.log | tail -8'
echo "== flow rate during the probe =="
grep " 65536" /tmp/rttprobe_flow.log | tail -1 | awk '{print "  ", $4, "Gb/s"}'
