#!/usr/bin/env bash
# One motivation-1.3 data point: receiver-side bandwidth cap contention.
#
# Topology: 4 sender VFs (each devlink tx_max=20G) -> ONE receiver VF (vf0),
# all over a VxLAN overlay. Offered 80G on a 200G link (network is NOT the
# bottleneck); the receiver VM's 20G downlink quota is enforced by an OVS
# meter at the receiver DPU's decap point (class-blind policer: drops excess,
# generates NO ECN -- exactly what a naive cloud rate limiter does).
#
# 32 flows: 4 source VFs x (4 RDMA + 4 TCP), all targeting receiver vf0
# (which holds 10.1.{0..3}.2, one IP per source subnet).
#
# usage: OUTROOT=<dir> run_point.sh <tag>
set -e
TAG=$1
BASE=/home/zhaoxiang/hyperfront/hpft-v2/paper/motiv_rx_cap_20260716
DIR=${OUTROOT:-$BASE}/$TAG
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
DUR=60
mkdir -p "$DIR"

# --- counter snapshots -------------------------------------------------
# sender: per-VF RDMA offered bytes (port_xmit_data, 4B units) + loss events;
#         PF rp_cnp_handled = CNPs the sender actually reacted to.
snap_host() {
  for d in mlx5_6 mlx5_7 mlx5_8 mlx5_9; do
    printf "%s.port_xmit_data=%s\n" $d "$(cat /sys/class/infiniband/$d/ports/1/counters/port_xmit_data 2>/dev/null)"
    for c in packet_seq_err out_of_sequence local_ack_timeout_err; do
      printf "%s.%s=%s\n" $d $c "$(cat /sys/class/infiniband/$d/ports/1/hw_counters/$c 2>/dev/null)"
    done
  done
  for c in rp_cnp_handled rp_cnp_ignored; do
    printf "mlx5_3.%s=%s\n" $c "$(cat /sys/class/infiniband/mlx5_3/ports/1/hw_counters/$c 2>/dev/null)"
  done
}
# receiver: CE marks seen + CNPs generated (the DCQCN loop's input),
#           plus RDMA-side loss symptoms at the target VF.
snap_peer() {
  ssh sgpu02 'for c in np_cnp_sent np_ecn_marked_roce_packets; do
    printf "mlx5_3.%s=%s\n" $c "$(cat /sys/class/infiniband/mlx5_3/ports/1/hw_counters/$c 2>/dev/null)"; done
  for c in out_of_sequence duplicate_request packet_seq_err; do
    printf "mlx5_6.%s=%s\n" $c "$(cat /sys/class/infiniband/mlx5_6/ports/1/hw_counters/$c 2>/dev/null)"; done
  printf "mlx5_6.port_rcv_data=%s\n" "$(cat /sys/class/infiniband/mlx5_6/ports/1/counters/port_rcv_data 2>/dev/null)"'
}
# receiver DPU: what actually arrived on the wire vs what the meter passed.
snap_dpu2() {
  ssh hpft-dpu2 'for c in rx_bytes_phy rx_packets_phy rx_discards_phy; do
    printf "p1.%s=%s\n" $c "$(ethtool -S p1 2>/dev/null | awk -v k=$c "\$1==k\":\" {print \$2}")"; done
  printf "meter.byte_in=%s\n" "$(sudo ovs-ofctl -O OpenFlow13 meter-stats ovsbr-p1 2>/dev/null | grep -oE "byte_in_count:[0-9]+" | cut -d: -f2)"
  printf "meter.pkt_in=%s\n" "$(sudo ovs-ofctl -O OpenFlow13 meter-stats ovsbr-p1 2>/dev/null | grep -oE "packet_in_count:[0-9]+" | cut -d: -f2)"'
}

# --- hygiene (pkill and target launch in SEPARATE ssh invocations) ------
ssh sgpu02 'pkill -f "ib_write_b[w]" 2>/dev/null; pkill -x iperf3 2>/dev/null; true'
pkill -f "ib_write_b[w]" 2>/dev/null || true
sleep 1

# servers on the receiver: 16 iperf3 (ports 5301..5316) + 16 perftest
ssh sgpu02 'for p in $(seq 5301 5316); do nohup iperf3 -s -p $p >/dev/null 2>&1 & done; true'
ssh sgpu02 "for v in 0 1 2 3; do for i in 0 1 2 3; do
  nohup $PT -d mlx5_6 -p \$((19000+v*4+i)) --report_gbits -D $DUR > /tmp/m13_s\${v}_\${i}.log 2>&1 &
done; done; true"
sleep 3

snap_host  > "$DIR/host_pre.txt"
snap_peer  > "$DIR/peer_pre.txt"
snap_dpu2  > "$DIR/dpu2_pre.txt"
date +%s.%N > "$DIR/t0.txt"

# receiver-side hardware per-class rate series (1 Hz)
ssh hpft-dpu2 "nohup python3 /tmp/vpm_sample.py $((DUR+8)) /tmp/vpm_series.csv >/dev/null 2>&1 & true"
( sleep 10; ping -c 40 -i 1 -W 2 10.1.0.2 > "$DIR/ping.txt" 2>&1 ) &

# jakiro DHTB branch counters at the receiver DPU (pre)
ssh hpft-dpu2 'cd /home/ubuntu/bzx/jakiro_dhtb; for b in roce_to_leaf tcp_to_leaf from_leaf_green from_roce_borrow from_tcp_borrow ce_marked_roce_forward tcp_greedy_root_red_drop forward_to_jakiro; do v=$(grep -aE "branch=$b " run/jakiro_dhtb.log | tail -1 | grep -oE "total_packets=[0-9]+" | cut -d= -f2); echo "$b=${v:-0}"; done' > "$DIR/jakiro_pre.txt"

# --- 32 clients: 4 source VFs (10.1.0.11..14) -> receiver vf0 (10.1.0.2) --
# jakiro is a single Jakiro = single overlay IP, so ALL flows target 10.1.0.2.
for v in 0 1 2 3; do
  for i in 0 1 2 3; do
    $PT -d mlx5_$((6+v)) -p $((19000+v*4+i)) --report_gbits -D $DUR 10.1.0.2 \
      > "$DIR/rdma_v${v}_f${i}.log" 2>&1 &
  done
done
for v in 0 1 2 3; do
  for j in 0 1 2 3; do
    iperf3 -c 10.1.0.2 -p $((5301+v*4+j)) -B 10.1.0.$((11+v)) -t $DUR -J \
      > "$DIR/tcp_v${v}_f${j}.json" 2>&1 &
  done
done
wait
date +%s.%N > "$DIR/t1.txt"

snap_host > "$DIR/host_post.txt"
snap_peer > "$DIR/peer_post.txt"
snap_dpu2 > "$DIR/dpu2_post.txt"
ssh hpft-dpu2 'cd /home/ubuntu/bzx/jakiro_dhtb; for b in roce_to_leaf tcp_to_leaf from_leaf_green from_roce_borrow from_tcp_borrow ce_marked_roce_forward tcp_greedy_root_red_drop forward_to_jakiro; do v=$(grep -aE "branch=$b " run/jakiro_dhtb.log | tail -1 | grep -oE "total_packets=[0-9]+" | cut -d= -f2); echo "$b=${v:-0}"; done' > "$DIR/jakiro_post.txt"
scp -q hpft-dpu2:/tmp/vpm_series.csv "$DIR/vpm_series.csv" 2>/dev/null || true
echo "$TAG done"
