#!/usr/bin/env bash
# One M4 sensitivity run: set params, restart agents, 45s RDMA with a
# 6G->4G policy step at t=25, collect rx jsonl.
# usage: sweep_run.sh <name> <a_frac> <beta> <v_periods>
set -e
NAME=$1; AF=$2; BETA=$3; VP=$4
DIR=/home/zhaoxiang/hyperfront/hpft-shaper-v2/results/m4_robustness_20260709
PT=~/hyperfront/perftest-26015/ib_write_bw

ssh hpft-dpu2 "python3 - <<EOF
import json
p = '/opt/hpft/lab-registry.json'
r = json.load(open(p))
r['e_params']['a_frac_linerate'] = $AF
r['e_params']['beta'] = $BETA
r['e_params']['v_periods'] = $VP
r['policy']['vms']['sgpu02/vf0']['max_rate_bps'] = 6000000000
json.dump(r, open(p, 'w'))
EOF"
ssh hpft-dpu "python3 - <<EOF
import json
p = '/opt/hpft/lab-registry.json'
r = json.load(open(p))
r['e_params']['a_frac_linerate'] = $AF
r['e_params']['beta'] = $BETA
r['e_params']['v_periods'] = $VP
json.dump(r, open(p, 'w'))
EOF"
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e python3 /opt/hpft/rx_agent.py' >/dev/null
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e python3 /opt/hpft/tx_agent_e.py' >/dev/null
sleep 2
ssh -f sgpu02 "$PT -d mlx5_6 -p 18515 --report_gbits -D 45 > /tmp/sweep_server.log 2>&1"
sleep 2
date +%s.%N > $DIR/${NAME}_t0.txt
( $PT -d mlx5_6 -p 18515 --report_gbits -D 45 10.1.0.2 > $DIR/${NAME}_client.log 2>&1 &
  sleep 25 && date +%s.%N > $DIR/${NAME}_step.txt && \
  ssh hpft-dpu2 "python3 -c \"
import json
p = '/opt/hpft/lab-registry.json'
r = json.load(open(p))
r['policy']['vms']['sgpu02/vf0']['max_rate_bps'] = 4000000000
json.dump(r, open(p, 'w'))\""
  wait )
date +%s.%N > $DIR/${NAME}_t1.txt
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl $DIR/${NAME}_rx.jsonl
echo "run $NAME done (A=$AF beta=$BETA vp=$VP)"
