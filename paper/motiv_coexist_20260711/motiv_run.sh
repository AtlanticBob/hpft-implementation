#!/usr/bin/env bash
# One motivation data point: R rdma flows + T tcp flows through the 25G
# bottleneck (switch egress swp37s0), 60 s, per-flow bandwidth + per-TC
# switch counter deltas.
# usage: motiv_run.sh <tag> <R> <T> <tclass>   (tclass 0 = default/shared)
set -e
TAG=$1; R=$2; T=$3; TCLASS=$4
DIR=/home/zhaoxiang/hyperfront/hpft-v2/paper/motiv_coexist_20260711/$TAG
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
mkdir -p $DIR
TCA=""; [ "$TCLASS" != 0 ] && TCA="--tclass=$TCLASS"

snap() { ssh sn5600 "nv show interface swp37s0 counters qos 2>/dev/null" 2>/dev/null \
  | grep -v Welcome | sed -n '/Egress Queue/,/PFC/p' | grep -E "^\s+[0-9]"; }

ssh sgpu02 'pkill -f "ib_write_b[w]" 2>/dev/null; pgrep -c iperf3 >/dev/null || for p in 5205 5206 5207 5208; do nohup iperf3 -s -p $p >/dev/null 2>&1 & done; true'
for i in $(seq 0 $((R-1))); do
  ssh -o BatchMode=yes -f sgpu02 "$PT -d mlx5_6 -p $((18521+i)) $TCA --report_gbits -D 60 > /tmp/mv_s$i.log 2>&1"
done
sleep 2
cat /sys/class/infiniband/mlx5_6/ports/1/hw_counters/packet_seq_err > $DIR/seqerr_pre.txt
snap > $DIR/q_pre.txt
date +%s.%N > $DIR/t0.txt
for i in $(seq 0 $((R-1))); do
  $PT -d mlx5_6 -p $((18521+i)) $TCA --report_gbits -D 60 10.1.0.2 > $DIR/rdma$i.log 2>&1 &
done
for j in $(seq 0 $((T-1))); do
  iperf3 -c 10.1.0.2 -p $((5205+j)) -t 60 -J > $DIR/tcp$j.json 2>&1 &
done
wait
date +%s.%N > $DIR/t1.txt
cat /sys/class/infiniband/mlx5_6/ports/1/hw_counters/packet_seq_err > $DIR/seqerr_post.txt
sleep 25
snap > $DIR/q_post.txt
echo "$TAG done"
