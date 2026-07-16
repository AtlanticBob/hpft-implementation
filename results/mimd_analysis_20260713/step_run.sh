#!/usr/bin/env bash
# Exp 2.2 + 2.3 step-response / scale-invariance. args = law cap_G tag.
# Single-pair vf0 1:1 at cap C. RDMA whole [0,90]; TCP on[15,45] off after.
# t=15 DOWN step (RDMA gives way to C/2), t=45 UP step (RDMA re-grows to C).
# Sweep C in {2,6,20,40} x {mimd,aimd}: settling vs scale + overshoot shape.
set -u
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
LAW=$1; CAPG=$2; TAG=$3; MIA=${4:-0.05}; BETA=${5:-0.15}
cd "$REPO"
python3 - "$LAW" "$CAPG" "$MIA" "$BETA" <<'EOF'
import json,sys
law,capg,mia,beta=sys.argv[1],float(sys.argv[2]),float(sys.argv[3]),float(sys.argv[4])
r=json.load(open("config/lab-registry.json"))
e=r["e_params"]; e["law_skeleton"]=law
# mimd uses passed gains (default production 0.05/0.15); aimd keeps tuned params
if law=="mimd": e["mi_alpha"]=mia; e["beta"]=beta
for vm in ("sgpu01/vf0","sgpu02/vf0"):
    r["policy"]["vms"][vm]["class_weights"]={"rdma":1,"tcp":1}
    r["policy"]["vms"][vm]["max_rate_bps"]=int(capg*1e9)
json.dump(r,open("config/lab-registry.json","w"),indent=2)
EOF
scp -q config/lab-registry.json hpft-dpu:/tmp/lr.json; ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json hpft-dpu2:/tmp/lr.json; ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json sgpu02:hpft/lab-registry.json
ssh hpft-dpu 'bash /tmp/rp_service.sh start' >/dev/null 2>&1
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e; sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' >/dev/null
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' >/dev/null
sleep 4; ssh sgpu02 'pkill -f "ib_write_[b]"; true'; sleep 1
ssh -f sgpu02 "$PT -d mlx5_6 -p 26200 --report_gbits -D 92 >/tmp/ss 2>&1"; sleep 1
t0=$(date +%s.%N); echo "$t0" > "$DIR/${TAG}_t0.txt"
$PT -d mlx5_6 -p 26200 --report_gbits -D 92 10.1.0.2 >"$DIR/${TAG}_rdma.log" 2>&1 &
( sleep 15; iperf3 -B "10.1.0.1%dpu1vf0" -c 10.1.0.2 -p 5201 -P4 -b 0 -t 30 -J >/dev/null 2>&1 ) &
wait; scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${TAG}_rx.jsonl"; echo "${TAG}-done"
