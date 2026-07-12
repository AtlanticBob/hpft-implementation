#!/usr/bin/env bash
# T3 only, both caps 40G (fixed sender+receiver). Arg = law.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
LAW=$1
cd "$REPO"
python3 - "$LAW" <<'EOF'
import json, sys
r = json.load(open("config/lab-registry.json"))
r["e_params"]["law_skeleton"] = sys.argv[1]
for vm in ("sgpu01/vf0", "sgpu02/vf0"):
    r["policy"]["vms"][vm]["class_weights"] = {"rdma": 7, "tcp": 3}
    r["policy"]["vms"][vm]["max_rate_bps"] = 40000000000
json.dump(r, open("config/lab-registry.json", "w"), indent=2)
EOF
scp -q config/lab-registry.json hpft-dpu:/tmp/lr.json; ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json hpft-dpu2:/tmp/lr.json; ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json sgpu02:hpft/lab-registry.json
ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
sleep 4
ssh sgpu02 'pkill -f "ib_write_[b]"; true'; sleep 1
p=25300
ssh -f sgpu02 "$PT -d mlx5_6 -p $p --report_gbits -D 95 > /tmp/t3_s.log 2>&1"; sleep 1
t0=$(date +%s.%N); echo "$t0" > "$DIR/${LAW}_t3b_t0.txt"; rm -f "$DIR/${LAW}_t3b_events.txt"
echo "$t0 start" >> "$DIR/${LAW}_t3b_events.txt"
$PT -d mlx5_6 -p $p --report_gbits -D 95 10.1.0.2 > "$DIR/${LAW}_t3b_rdma.log" 2>&1 &
sleep 10; iperf3 -B "10.1.0.1%dpu1vf0" -c 10.1.0.2 -p 5201 -P4 -b 0 -t 30 -J > "$DIR/${LAW}_t3b_tcp1.json" 2>&1 &
sleep 30; echo "$(date +%s.%N) tcp_off" >> "$DIR/${LAW}_t3b_events.txt"
sleep 20; echo "$(date +%s.%N) tcp_on" >> "$DIR/${LAW}_t3b_events.txt"
iperf3 -B "10.1.0.1%dpu1vf0" -c 10.1.0.2 -p 5201 -P4 -b 0 -t 33 -J > "$DIR/${LAW}_t3b_tcp2.json" 2>&1 &
sleep 35; echo "$(date +%s.%N) end" >> "$DIR/${LAW}_t3b_events.txt"
wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${LAW}_t3b_rx.jsonl"
echo "${LAW}-t3b-done"
