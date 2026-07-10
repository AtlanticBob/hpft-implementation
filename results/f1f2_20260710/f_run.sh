#!/usr/bin/env bash
# usage: f_run.sh <name> <hold_s> <fr> <hai_after> [delta_boost] [peak_ref_s]
set -e
NAME=$1; HOLD=$2; FR=$3; HAI=$4; DB=${5:-0}; PR=${6:-0}; SF=${7:-False}; GR=${8:-0.5}
DIR=/home/zhaoxiang/hyperfront/hpft-shaper-v2/results/f1f2_20260710
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
for h in hpft-dpu2 hpft-dpu; do
  ssh $h "python3 -c \"
import json
p = '/opt/hpft/lab-registry.json'
r = json.load(open(p))
r['e_params']['demand_hold_s'] = $HOLD
r['e_params']['fast_recovery'] = $FR
r['e_params']['hai_after'] = $HAI
r['e_params']['delta_boost'] = $DB
r['e_params']['peak_ref_s'] = $PR
r['e_params']['share_floor'] = $SF
r['e_params']['share_floor_grace_s'] = $GR
r['policy']['vms']['sgpu02/vf0']['max_rate_bps'] = 6000000000
json.dump(r, open(p, 'w'))\""
done
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e python3 /opt/hpft/rx_agent.py' >/dev/null
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e python3 /opt/hpft/tx_agent_e.py' >/dev/null
sleep 2
ssh sgpu02 'pkill -f "ib_write_b[w].*18515" 2>/dev/null; pgrep -c iperf3 >/dev/null || for p in $(seq 5201 5216); do nohup iperf3 -s -p $p >/dev/null 2>&1 & done; true'
ssh -f sgpu02 "$PT -d mlx5_6 -p 18515 --report_gbits -D 100 > /tmp/f_server.log 2>&1"
sleep 2
date +%s.%N > $DIR/${NAME}_t0.txt
( $PT -d mlx5_6 -p 18515 --report_gbits -D 100 10.1.0.2 > $DIR/${NAME}_rdma.log 2>&1 &
  iperf3 -c 10.1.0.2 -p 5205 -t 60 -J > $DIR/${NAME}_tcp1.json 2>&1 &
  sleep 80 && date +%s.%N > $DIR/${NAME}_tret.txt && \
    iperf3 -c 10.1.0.2 -p 5205 -t 18 -J > $DIR/${NAME}_tcp2.json 2>&1 &
  wait )
date +%s.%N > $DIR/${NAME}_t1.txt
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl $DIR/${NAME}_rx.jsonl
echo "$NAME done"
