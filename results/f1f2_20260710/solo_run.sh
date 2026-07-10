#!/usr/bin/env bash
# usage: solo_run.sh <name> <class:rdma|tcp> <a_frac>
set -e
NAME=$1; CLS=$2; AF=$3; FR2=${4:-True}
DIR=/home/zhaoxiang/hyperfront/hpft-shaper-v2/results/f1f2_20260710
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
for h in hpft-dpu2 hpft-dpu; do
  ssh $h "python3 -c \"
import json
p = '/opt/hpft/lab-registry.json'
r = json.load(open(p))
r['e_params']['a_frac_linerate'] = $AF
r['e_params']['a_frac_linerate_by_class'] = {}
r['e_params']['fast_recovery'] = $FR2
r['e_params']['hai_after'] = 10
r['e_params']['share_floor'] = False
r['policy']['vms']['sgpu02/vf0']['max_rate_bps'] = 6000000000
json.dump(r, open(p, 'w'))\""
done
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e python3 /opt/hpft/rx_agent.py' >/dev/null
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e python3 /opt/hpft/tx_agent_e.py' >/dev/null
sleep 2
date +%s.%N > $DIR/${NAME}_t0.txt
if [ "$CLS" = rdma ]; then
  ssh sgpu02 'pkill -f "ib_write_b[w].*18515" 2>/dev/null; true'
  ssh -f sgpu02 "$PT -d mlx5_6 -p 18515 --report_gbits -D 45 > /tmp/solo_server.log 2>&1"
  sleep 2; date +%s.%N > $DIR/${NAME}_t0.txt
  $PT -d mlx5_6 -p 18515 --report_gbits -D 45 10.1.0.2 > $DIR/${NAME}.log 2>&1
else
  iperf3 -c 10.1.0.2 -p 5205 -t 45 -J > $DIR/${NAME}.json 2>&1
fi
date +%s.%N > $DIR/${NAME}_t1.txt
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl $DIR/${NAME}_rx.jsonl
echo "$NAME done"
