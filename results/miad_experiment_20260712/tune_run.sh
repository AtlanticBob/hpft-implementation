#!/usr/bin/env bash
# Tuning run: args = law mi_alpha beta tag. Runs T3@40G both-cap + 240s
# repro under (law, mi_alpha, beta). beta feeds MIMD's MD and MIAD's
# ad_beta is left at default (MIAD tuned via mi_alpha only here).
set -u
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
LAW=$1; MIA=$2; BETA=$3; TAG=$4
cd "$REPO"
setp() { # cap rw tw
python3 - "$LAW" "$MIA" "$BETA" "$1" "$2" "$3" <<'EOF'
import json,sys
law,mia,beta,cap,rw,tw=sys.argv[1],float(sys.argv[2]),float(sys.argv[3]),int(sys.argv[4]),float(sys.argv[5]),float(sys.argv[6])
r=json.load(open("config/lab-registry.json"))
r["e_params"]["law_skeleton"]=law; r["e_params"]["mi_alpha"]=mia; r["e_params"]["beta"]=beta
for vm in ("sgpu01/vf0","sgpu02/vf0"):
    r["policy"]["vms"][vm]["class_weights"]={"rdma":rw,"tcp":tw}
    r["policy"]["vms"][vm]["max_rate_bps"]=cap
json.dump(r,open("config/lab-registry.json","w"),indent=2)
EOF
scp -q config/lab-registry.json hpft-dpu:/tmp/lr.json; ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json hpft-dpu2:/tmp/lr.json; ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json sgpu02:hpft/lab-registry.json
}
fresh() {
ssh hpft-dpu 'bash /tmp/rp_service.sh start' >/dev/null 2>&1
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e; sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' >/dev/null
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' >/dev/null
sleep 4; ssh sgpu02 'pkill -f "ib_write_[b]"; true'; sleep 1
}
# T3@40G
setp 40000000000 7 3; fresh
ssh -f sgpu02 "$PT -d mlx5_6 -p 25600 --report_gbits -D 95 > /tmp/tn_s.log 2>&1"; sleep 1
t0=$(date +%s.%N); echo "$t0" > "$DIR/${TAG}_t3b_t0.txt"; rm -f "$DIR/${TAG}_t3b_events.txt"; echo "$t0 start">>"$DIR/${TAG}_t3b_events.txt"
$PT -d mlx5_6 -p 25600 --report_gbits -D 95 10.1.0.2 > "$DIR/${TAG}_t3b_rdma.log" 2>&1 &
sleep 10; iperf3 -B "10.1.0.1%dpu1vf0" -c 10.1.0.2 -p 5201 -P4 -b 0 -t 30 -J >/dev/null 2>&1 &
sleep 30; echo "$(date +%s.%N) tcp_off">>"$DIR/${TAG}_t3b_events.txt"
sleep 20; echo "$(date +%s.%N) tcp_on">>"$DIR/${TAG}_t3b_events.txt"
iperf3 -B "10.1.0.1%dpu1vf0" -c 10.1.0.2 -p 5201 -P4 -b 0 -t 33 -J >/dev/null 2>&1 &
sleep 35; echo "$(date +%s.%N) end">>"$DIR/${TAG}_t3b_events.txt"; wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${TAG}_t3b_rx.jsonl"
# repro @6G
setp 6000000000 1 1; fresh
ssh -f sgpu02 "$PT -d mlx5_6 -p 25601 --report_gbits -D 240 > /tmp/tn_s.log 2>&1"; sleep 1
t0=$(date +%s.%N); echo "$t0" > "$DIR/${TAG}_repro_t0.txt"
$PT -d mlx5_6 -p 25601 --report_gbits -D 240 10.1.0.2 > "$DIR/${TAG}_repro_rdma.log" 2>&1 &
iperf3 -B "10.1.0.1%dpu1vf0" -c 10.1.0.2 -p 5201 -b 0 -t 235 -J >/dev/null 2>&1
wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${TAG}_repro_rx.jsonl"
echo "${TAG}-tune-done"
