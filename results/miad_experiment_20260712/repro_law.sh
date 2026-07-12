#!/usr/bin/env bash
set -u
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw; LAW=$1
cd "$REPO"
python3 - "$LAW" <<'EOF'
import json,sys
r=json.load(open("config/lab-registry.json"))
r["e_params"]["law_skeleton"]=sys.argv[1]
for vm in ("sgpu01/vf0","sgpu02/vf0"):
    r["policy"]["vms"][vm]["class_weights"]={"rdma":1,"tcp":1}
    r["policy"]["vms"][vm]["max_rate_bps"]=6000000000
json.dump(r,open("config/lab-registry.json","w"),indent=2)
EOF
scp -q config/lab-registry.json hpft-dpu:/tmp/lr.json; ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json hpft-dpu2:/tmp/lr.json; ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json sgpu02:hpft/lab-registry.json
ssh hpft-dpu 'bash /tmp/rp_service.sh start' >/dev/null 2>&1
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e; sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' >/dev/null
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' >/dev/null
sleep 4; ssh sgpu02 'pkill -f "ib_write_[b]"; true'; sleep 1
ssh -f sgpu02 "$PT -d mlx5_6 -p 25400 --report_gbits -D 240 > /tmp/rl_s.log 2>&1"; sleep 1
t0=$(date +%s.%N); echo "$t0" > "$DIR/${LAW}_repro_t0.txt"
$PT -d mlx5_6 -p 25400 --report_gbits -D 240 10.1.0.2 > "$DIR/${LAW}_repro_rdma.log" 2>&1 &
iperf3 -B "10.1.0.1%dpu1vf0" -c 10.1.0.2 -p 5201 -b 0 -t 235 -J > "$DIR/${LAW}_repro_tcp.json" 2>&1
wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${LAW}_repro_rx.jsonl"
echo "${LAW}-repro-done"
