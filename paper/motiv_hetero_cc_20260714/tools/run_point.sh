#!/usr/bin/env bash
# One motivation-1.2 data point: 4 VF pairs x (4 RDMA + 4 TCP) = 32 flows
# through the 100G bottleneck (switch egress swp37s0), 60 s.
# Collects: per-flow goodput, switch per-TC counter deltas (frames/ECN/drops),
# sender packet_seq_err + rp_cnp_handled deltas, receiver np_cnp_sent deltas.
# usage: run_point.sh <tag>     (environment/knobs must be set by caller)
# NTCP=0 runs the RDMA-only variant (16 RDMA flows, no TCP).
# OUTROOT=<dir> sets where the run directory is created (default: this
#   experiment folder). The manual points it at exp<N>/runs/.
set -e
TAG=$1
BASE=/home/zhaoxiang/hyperfront/hpft-v2/paper/motiv_hetero_cc_20260714
DIR=${OUTROOT:-$BASE}/$TAG
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
DUR=60
NTCP=${NTCP:-4}
mkdir -p "$DIR"

snap_sw() { ssh sn5600 "nv show interface swp37s0 counters qos 2>/dev/null" 2>/dev/null \
  | grep -v Welcome | sed -n '/Egress Queue/,/PFC/p' | grep -E "^\s+[0-9]"; }
snap_ifc() { ssh sn5600 "nv show interface swp37s0 counters 2>/dev/null; echo ----; nv show interface swp37s1 counters 2>/dev/null" 2>/dev/null | grep -v Welcome; }
snap_host() { for d in mlx5_6 mlx5_7 mlx5_8 mlx5_9; do
  for c in packet_seq_err out_of_sequence local_ack_timeout_err; do
    printf "%s.%s=%s\n" $d $c "$(cat /sys/class/infiniband/$d/ports/1/hw_counters/$c 2>/dev/null)"; done; done
  for c in rp_cnp_handled rp_cnp_ignored; do
    printf "mlx5_3.%s=%s\n" $c "$(cat /sys/class/infiniband/mlx5_3/ports/1/hw_counters/$c 2>/dev/null)"; done; }
snap_peer() { ssh sgpu02 'for d in mlx5_6 mlx5_7 mlx5_8 mlx5_9; do
  for c in out_of_buffer duplicate_request implied_nak_seq_err; do
    printf "%s.%s=%s\n" $d $c "$(cat /sys/class/infiniband/$d/ports/1/hw_counters/$c 2>/dev/null)"; done; done
  for c in np_cnp_sent np_ecn_marked_roce_packets; do
    printf "mlx5_3.%s=%s\n" $c "$(cat /sys/class/infiniband/mlx5_3/ports/1/hw_counters/$c 2>/dev/null)"; done'; }

# stale process hygiene (pkill and target starts in separate ssh invocations)
ssh sgpu02 'pkill -f "ib_write_b[w]" 2>/dev/null; pkill -x iperf3 2>/dev/null; true'
pkill -f "ib_write_b[w]" 2>/dev/null || true
sleep 1

# iperf3 servers: 16 fresh ones (ports 5301..5316)
[ "$NTCP" -gt 0 ] && ssh sgpu02 'for p in $(seq 5301 5316); do nohup iperf3 -s -p $p >/dev/null 2>&1 & done; true'

# perftest servers: 16, ports 19000 + v*4 + i, device mlx5_(6+v)
# (GBN/SR is selected beforehand via tools/cc_mode.sh gbn|sr -- ROCE_ACCL register)
ssh sgpu02 "for v in 0 1 2 3; do for i in 0 1 2 3; do
  nohup $PT -d mlx5_\$((6+v)) -p \$((19000+v*4+i)) --report_gbits -D $DUR > /tmp/mv12_s\${v}_\${i}.log 2>&1 &
done; done; true"
sleep 3

snap_host > "$DIR/host_pre.txt"
snap_peer > "$DIR/peer_pre.txt"
snap_sw   > "$DIR/q_pre.txt"
snap_ifc  > "$DIR/ifc_pre.txt"
date +%s.%N > "$DIR/t0.txt"

# receiver-side hardware per-VF/class rate series (vport-meter, 1 Hz)
ssh hpft-dpu2 "nohup python3 /tmp/vpm_sample.py $((DUR+8)) /tmp/vpm_series.csv >/dev/null 2>&1 & true"

# in-run RTT through the bottleneck queue (bufferbloat evidence)
( sleep 10; ping -c 40 -i 1 -W 2 10.1.0.2 > "$DIR/ping.txt" 2>&1 ) &
# in-run buffer occupancy samples (per-TC egress, slow-polled by cumulus)
( for i in 1 2 3; do sleep 15; ssh sn5600 "nv show interface swp37s0 qos buffer egress-traffic-class 2>/dev/null" 2>/dev/null | grep -vE "Welcome" | grep -E "^\s*[05]\s" >> "$DIR/buf_samples.txt"; done ) &

# launch all clients as fast as possible
for v in 0 1 2 3; do
  for i in 0 1 2 3; do
    $PT -d mlx5_$((6+v)) -p $((19000+v*4+i)) --report_gbits -D $DUR 10.1.$v.2 \
      > "$DIR/rdma_v${v}_f${i}.log" 2>&1 &
  done
done
if [ "$NTCP" -gt 0 ]; then
  for v in 0 1 2 3; do
    for j in $(seq 0 $((NTCP-1))); do
      iperf3 -c 10.1.$v.2 -p $((5301+v*4+j)) -t $DUR -J \
        > "$DIR/tcp_v${v}_f${j}.json" 2>&1 &
    done
  done
fi
wait
date +%s.%N > "$DIR/t1.txt"

snap_host > "$DIR/host_post.txt"
snap_peer > "$DIR/peer_post.txt"
scp -q hpft-dpu2:/tmp/vpm_series.csv "$DIR/vpm_series.csv" 2>/dev/null || true
sleep 25   # cumulus qos counters are slow-polled
snap_sw   > "$DIR/q_post.txt"
snap_ifc  > "$DIR/ifc_post.txt"
echo "$TAG done"
