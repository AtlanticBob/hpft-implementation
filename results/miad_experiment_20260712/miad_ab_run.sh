#!/usr/bin/env bash
# MIAD vs AIMD A/B on basic tests. Arg = law ("miad" | "aimd"). Sets
# law_skeleton in the registry, restarts tx, runs:
#   T2a class fairness 1:1 @6G (75s: RDMA whole, TCP [15,45])
#   T2b class fairness 3:1 @6G
#   T3  high-rate churn @40G 7:3 (RDMA whole, TCP on[10,40] off[40,60] on[60,95])
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
LAW=$1

setpol() { # law cap rdma_w tcp_w
    python3 - "$@" <<'EOF'
import json, sys
law, cap, rw, tw = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
r = json.load(open("config/lab-registry.json"))
r["e_params"]["law_skeleton"] = law
for vm in ("sgpu01/vf0", "sgpu02/vf0"):
    r["policy"]["vms"][vm]["class_weights"] = {"rdma": rw, "tcp": tw}
    r["policy"]["vms"][vm]["max_rate_bps"] = cap
json.dump(r, open("config/lab-registry.json", "w"), indent=2)
EOF
    scp -q "$REPO/config/lab-registry.json" hpft-dpu:/tmp/lr.json; ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/tmp/lr.json; ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" sgpu02:hpft/lab-registry.json
}
fresh() {
    ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
    ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
    ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
    sleep 4
    ssh sgpu02 'pkill -f "ib_write_[b]"; true'; sleep 1
}

cd "$REPO"

# ---- T2 class fairness (1:1 and 3:1) ----
for pt in "1x1:1:1" "3x1:3:1"; do
    tag=${pt%%:*}; rest=${pt#*:}; rw=${rest%:*}; tw=${rest#*:}
    setpol "$LAW" 6000000000 "$rw" "$tw"; fresh
    p=25100
    ssh -f sgpu02 "$PT -d mlx5_6 -p $p --report_gbits -D 75 > /tmp/ab_s.log 2>&1"; sleep 1
    t0=$(date +%s.%N); echo "$t0" > "$DIR/${LAW}_t2${tag}_t0.txt"
    $PT -d mlx5_6 -p $p --report_gbits -D 75 10.1.0.2 > "$DIR/${LAW}_t2${tag}_rdma.log" 2>&1 &
    ( sleep 15; iperf3 -B "10.1.0.1%dpu1vf0" -c 10.1.0.2 -p 5201 -P4 -b 0 -t 30 -J > "$DIR/${LAW}_t2${tag}_tcp.json" 2>&1 ) &
    wait
    scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${LAW}_t2${tag}_rx.jsonl"
    echo "${LAW} t2${tag} done"
done

# ---- T3 high-rate churn @40G 7:3 ----
setpol "$LAW" 40000000000 7 3; fresh
p=25200
ssh -f sgpu02 "$PT -d mlx5_6 -p $p --report_gbits -D 95 > /tmp/ab_s.log 2>&1"; sleep 1
t0=$(date +%s.%N); echo "$t0" > "$DIR/${LAW}_t3_t0.txt"; rm -f "$DIR/${LAW}_t3_events.txt"
echo "$t0 start" >> "$DIR/${LAW}_t3_events.txt"
$PT -d mlx5_6 -p $p --report_gbits -D 95 10.1.0.2 > "$DIR/${LAW}_t3_rdma.log" 2>&1 &
sleep 10; iperf3 -B "10.1.0.1%dpu1vf0" -c 10.1.0.2 -p 5201 -P4 -b 0 -t 30 -J > "$DIR/${LAW}_t3_tcp1.json" 2>&1 &
sleep 30; echo "$(date +%s.%N) tcp_off" >> "$DIR/${LAW}_t3_events.txt"
sleep 20; echo "$(date +%s.%N) tcp_on" >> "$DIR/${LAW}_t3_events.txt"
iperf3 -B "10.1.0.1%dpu1vf0" -c 10.1.0.2 -p 5201 -P4 -b 0 -t 33 -J > "$DIR/${LAW}_t3_tcp2.json" 2>&1 &
sleep 35; echo "$(date +%s.%N) end" >> "$DIR/${LAW}_t3_events.txt"
wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${LAW}_t3_rx.jsonl"
echo "${LAW} t3 done"
echo "${LAW}-ab-done"
