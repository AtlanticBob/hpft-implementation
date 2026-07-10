#!/usr/bin/env bash
# usage: run_one.sh <name> <a_json>   e.g. run_one.sh g15 '{"tcp":0.00075,"rdma":0.00075}'
set -e
NAME=$1; AJ=$2
DIR=$(cd "$(dirname "$0")" && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
for h in hpft-dpu2 hpft-dpu; do
  ssh $h "python3 -c \"
import json
p = '/opt/hpft/lab-registry.json'
r = json.load(open(p))
r['e_params']['a_frac_linerate_by_class'] = json.loads('$AJ')
r['policy']['vms']['sgpu02/vf0']['max_rate_bps'] = 6000000000
json.dump(r, open(p, 'w'))\""
done
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e python3 /opt/hpft/rx_agent.py' >/dev/null
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e python3 /opt/hpft/tx_agent_e.py' >/dev/null
sleep 2
ssh sgpu02 'pkill -f "ib_write_bw.*18515" 2>/dev/null; true'
ssh -f sgpu02 "$PT -d mlx5_6 -p 18515 --report_gbits -D 62 > /tmp/ar_server.log 2>&1"
sleep 2
date +%s.%N > $DIR/${NAME}_t0.txt
( $PT -d mlx5_6 -p 18515 --report_gbits -D 62 10.1.0.2 > $DIR/${NAME}_rdma.log 2>&1 &
  iperf3 -c 10.1.0.2 -t 62 -J > $DIR/${NAME}_tcp.json 2>&1 & wait )
date +%s.%N > $DIR/${NAME}_t1.txt
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl $DIR/${NAME}_rx.jsonl
echo "$NAME done"
