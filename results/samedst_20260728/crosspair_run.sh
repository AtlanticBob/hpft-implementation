#!/usr/bin/env bash
# Is a CROSS-PAIR RDMA flow paced at all?
#
# The same-dst run showed the control plane commanding 15G to each of two
# flows while the wire delivered 0.4G to one and 34.7G to the other. The
# allocator's numbers were right, so the question moves to the executor -
# and the one structural difference between these two flows is that
# vf3->vf0 is a CROSS pair. Its PCC budget is keyed by a per-(src,dst)
# flowtag; every straight pair has been exercised for weeks, cross pairs
# far less. If a lone cross-pair flow ignores its budget, that alone
# explains the whole oscillation and it has nothing to do with same-dst
# contention.
#
# Control: the same flow on a straight pair (vf3 -> vf3), same everything
# else. Both run alone against a 30G VM cap on a 100G downlink, so an
# unenforced flow has ~90G of room to show itself in.
set -u
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd)
OUT="$DIR/results"; PT=$HOME/hyperfront/perftest-26015/ib_write_bw
D=25; QN=4; mkdir -p "$OUT"; cd "$REPO"

run_one() { # run_one <tag> <dst-ip> <srv-dev>
  local tag=$1 dst=$2 sdev=$3
  ssh sgpu02 'pkill -f "ib_write_[b]"; true'; sleep 1
  ssh sgpu02 "nohup $PT -d $sdev -p 26430 -q $QN --report_gbits -D $D >/tmp/cp_srv 2>&1 & true"
  sleep 2
  ( while :; do echo "$(date +%s.%N) $(ethtool -S dpu1vf3 | awk '/tx_vport_rdma_unicast_bytes/{print $2}')"; sleep 1; done ) \
      > "$OUT/cp_${tag}_bytes.txt" &
  local s=$!
  date +%s.%N > "$OUT/cp_${tag}_t0.txt"
  $PT -d mlx5_9 -p 26430 -q $QN --report_gbits -D $D "$dst" > "$OUT/cp_${tag}.txt" 2>&1
  kill $s 2>/dev/null
}

run_one cross    10.1.0.2 mlx5_6    # vf3 -> vf0   (cross pair)
run_one straight 10.1.3.2 mlx5_9    # vf3 -> vf3   (straight pair, control)
ssh hpft-dpu 'sudo cat /tmp/hpft_txagent_e.jsonl' > "$OUT/cp_tx.jsonl"
echo "cp-done"
