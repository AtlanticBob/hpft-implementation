#!/usr/bin/env bash
# Give-way (down-step) replicate. args = law tag. Single pair vf0 1:1 @20G.
# RDMA [0,60]; TCP on[15,60] (stays up -> clean down-step, no leave flakiness).
# Metric: dip in [15,25] vs steady [35,55]. law=mimd uses prod 0.05/0.15.
set -u
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
LAW=$1; TAG=$2; MDA=${3:-zero}; MDF=${4:-0.3}
cd "$REPO"
python3 - "$LAW" "$MDA" "$MDF" <<'EOF'
import json,sys
law,mda,mdf=sys.argv[1],sys.argv[2],float(sys.argv[3])
r=json.load(open("config/lab-registry.json"))
e=r["e_params"]; e["law_skeleton"]=law; e["md_anchor"]=mda; e["md_floor_frac"]=mdf
if law=="mimd": e["mi_alpha"]=0.05; e["beta"]=0.15
for vm in ("sgpu01/vf0","sgpu02/vf0"):
    r["policy"]["vms"][vm]["class_weights"]={"rdma":1,"tcp":1}; r["policy"]["vms"][vm]["max_rate_bps"]=20000000000
json.dump(r,open("config/lab-registry.json","w"),indent=2)
EOF
scp -q config/lab-registry.json hpft-dpu:/tmp/lr.json; ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json hpft-dpu2:/tmp/lr.json; ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json sgpu02:hpft/lab-registry.json
ssh hpft-dpu 'bash /tmp/rp_service.sh start' >/dev/null 2>&1
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e; sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' >/dev/null
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' >/dev/null
sleep 4; ssh sgpu02 'pkill -f "ib_write_[b]"; true'; sleep 1
ssh -f sgpu02 "$PT -d mlx5_6 -p 26300 --report_gbits -D 62 >/tmp/gw 2>&1"; sleep 1
t0=$(date +%s.%N); echo "$t0" > "$DIR/${TAG}_t0.txt"
$PT -d mlx5_6 -p 26300 --report_gbits -D 62 10.1.0.2 >"$DIR/${TAG}_rdma.log" 2>&1 &
( sleep 15; iperf3 -B "10.1.0.1%dpu1vf0" -c 10.1.0.2 -p 5201 -P4 -b 0 -t 45 -J >/dev/null 2>&1 ) &
wait; scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${TAG}_rx.jsonl"; echo "${TAG}-done"
