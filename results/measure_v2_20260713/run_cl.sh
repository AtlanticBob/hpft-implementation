#!/usr/bin/env bash
# Step-4 closed-loop runs: production rx_agent now runs the vport-meter
# attribution (hpft-vport-meter systemd unit is the single mmap writer -
# do NOT start a second helper). Records prod jsonl slice + host ground
# truth only. usage: run_cl.sh i8c|flipc
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
TAG=$1

prep() {  # <gt_secs>
  PROD_OFF=$(ssh hpft-dpu2 'stat -c%s /tmp/hpft_rxagent_e.jsonl 2>/dev/null || echo 0')
  ssh -f sgpu02 "python3 /tmp/gt_sampler4.py 0.02 $1 > /tmp/${TAG}_gt.tsv 2>/dev/null"
  sleep 2
}
finish() {
  ssh hpft-dpu2 "sudo tail -c +$((PROD_OFF+1)) /tmp/hpft_rxagent_e.jsonl > /tmp/${TAG}_prod.jsonl"
  scp -q "hpft-dpu2:/tmp/${TAG}_prod.jsonl" "sgpu02:/tmp/${TAG}_gt.tsv" "$DIR/"
  echo "$TAG-done"
}

case $TAG in
i8c)
  D=92
  prep 118
  ssh sgpu02 'pkill -f "ib_write_b[w].*264" 2>/dev/null; true'; sleep 1
  SRV=""; for n in 0 1 2 3; do SRV+="nohup $PT -d mlx5_$((6+n)) -p $((26400+n)) --report_gbits -D $D >/tmp/${TAG}_srv$n 2>&1 & "; done
  ssh sgpu02 "$SRV true"
  sleep 8
  date -u +%s.%N > "$DIR/${TAG}_t0.txt"
  for n in 0 1 2 3; do $PT -d mlx5_$((6+n)) -p $((26400+n)) --report_gbits -D $D 10.1.$n.2 > "$DIR/${TAG}_rdma$n.log" 2>&1 & done
  for n in 0 1 2 3; do iperf3 -B "10.1.$n.1%dpu1vf$n" -c 10.1.$n.2 -p $((5201+4*n)) -P4 -b 0 -t $((D-7)) -J > "$DIR/${TAG}_iperf$n.json" 2>&1 & done
  wait
  sleep 12
  finish
  ;;
flipc)
  prep 72
  iperf3 -B 10.1.0.1%dpu1vf0 -c 10.1.0.2 -p 5201 -t 56 -J > "$DIR/${TAG}_iperf.json" 2>&1 &
  sleep 4
  date -u +%s.%N > "$DIR/${TAG}_t0.txt"
  for i in 1 2 3 4 5 6; do
    ssh -f sgpu02 "$PT -d mlx5_6 -p $((18730+i)) --report_gbits -D 3 > /tmp/${TAG}_srv$i 2>&1"
    sleep 1
    $PT -d mlx5_6 -p $((18730+i)) --report_gbits -D 3 10.1.0.2 > "$DIR/${TAG}_rdma$i.log" 2>&1
    sleep 3.5
  done
  wait
  sleep 10
  finish
  ;;
esac
