#!/usr/bin/env bash
# Confirms the small-N end of bench_scale.py against the real agent, and
# exercises the flow guards end to end.
#
# 20 flow-sets: a 4x4 TCP mesh (16) plus the 4 straight RDMA pairs. TCP
# carries no PCC flowtag constraint and no QP-death exposure, so this is
# the largest shape the lab can build without stepping on either. RDMA is
# rate-limited so aggregate offered load stays under the bottleneck from
# the first packet (ops_notes 2026-07-27).
set -u
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd)
OUT="$DIR/results"; PT=$HOME/hyperfront/perftest-26015/ib_write_bw
TAG=${1:-lab20}; D=60
mkdir -p "$OUT"; cd "$REPO"

echo "== preflight =="
bash tools/lab-infra/flow_preflight.sh "0,0 1,1 2,2 3,3" || { echo "ABORT: dead pair"; exit 1; }

ssh hpft-dpu 'bash /tmp/rp_service.sh start' >/dev/null 2>&1
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; true'
ssh hpft-dpu  'sudo systemctl stop hpft-txagent-e 2>/dev/null; true'
sleep 1
ssh hpft-dpu2 'sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' >/dev/null
sleep 3
ssh hpft-dpu  'sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' >/dev/null
sleep 4

ssh sgpu02 'pkill -f "ib_write_[b]"; true'; sleep 1
SRV=""; for n in 0 1 2 3; do SRV+="nohup $PT -d mlx5_$((6+n)) -p $((27100+n)) --report_gbits -D $D >/dev/null 2>&1 & "; done
ssh sgpu02 "$SRV true"
ssh sgpu02 'for p in $(seq 5201 5216); do pgrep -f "iperf3 -s -p $p\$" >/dev/null || (setsid nohup iperf3 -s -p $p >/dev/null 2>&1 &); done; true'
sleep 3

t0=$(date +%s.%N); echo "$t0" > "$OUT/${TAG}_t0.txt"
for n in 0 1 2 3; do $PT -d mlx5_$((6+n)) -p $((27100+n)) --report_gbits --rate_limit=5 --rate_units=g -D $D 10.1.$n.2 >/dev/null 2>&1 & sleep 0.2; done
for i in 0 1 2 3; do for j in 0 1 2 3; do
  iperf3 -B "10.1.$i.1%dpu1vf$i" -c 10.1.$j.2 -p $((5201+4*i+j)) -b 2G -t $((D-8)) -J >/dev/null 2>&1 & sleep 0.1
done; done
sleep $((D-14))
ssh hpft-dpu2 'sudo journalctl -u hpft-rxagent-e --no-pager -n 25 -o cat | grep -a "ticks/s" | tail -6' > "$OUT/${TAG}_ticks.txt"
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$OUT/${TAG}_rx.jsonl"
scp -q hpft-dpu:/tmp/hpft_txagent_e.jsonl  "$OUT/${TAG}_tx.jsonl"
wait 2>/dev/null
ssh sgpu02 'pkill -f "ib_write_[b]"; true'
echo "${TAG}-done"
