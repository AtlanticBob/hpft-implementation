#!/usr/bin/env bash
# Validation of the realization-aware probe margin (probe_cap, stress fix
# for the episodic marking storm). Three phases, ~30 min:
#   V1  240s repro protocol (1:1 @6G, both classes full demand, 1Hz 0xdeb)
#       - the exact protocol that reproduced the collapse under the old
#       anchor; expect: no periodic collapse, band residence holds.
#   V2  3:1 @6G x 10 reps x 90s (the point that collapsed twice in D2
#       sweeps) - expect 0/10 episodes, ratio ~3.1, utilization not lower
#       than the healthy 5.8-5.9G baseline.
#   V3  D3c policy staircase (copied runner) - expect hold windows now
#       stay inside the +-10% band (before: every window punctured).
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw

set_policy() { # cap_bps rdma_w tcp_w   (vf0 pair, both sides' weights)
    python3 - "$1" "$2" "$3" <<'EOF'
import json, sys
r = json.load(open("config/lab-registry.json"))
cap, R, T = int(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
for vm in ("sgpu01/vf0", "sgpu02/vf0"):
    r["policy"]["vms"][vm]["class_weights"] = {"tcp": T, "rdma": R}
r["policy"]["vms"]["sgpu02/vf0"]["max_rate_bps"] = cap
json.dump(r, open("config/lab-registry.json", "w"), indent=2)
EOF
    scp -q "$REPO/config/lab-registry.json" hpft-dpu:/tmp/lr.json
    ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/tmp/lr.json
    ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" sgpu02:hpft/lab-registry.json
}

fresh_agents() {
    ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
    ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
    sleep 3
}

cd "$REPO"

# ---- V1: repro protocol ----
set_policy 6000000000 1 1
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
fresh_agents
ssh sgpu02 'pkill -f "ib_write_[b]"; true'
ssh -f sgpu02 "$PT -d mlx5_6 -p 19500 --report_gbits -D 240 > /tmp/repro_srv.log 2>&1"
sleep 1
ssh hpft-dpu 'for i in $(seq 1 250); do echo "0xdeb 0" > /tmp/rp_fifo; sleep 1; done; grep -a HPFT_RSP /tmp/pcc_rp.log' > "$DIR/repro_rp.txt" &
date +%s.%N > "$DIR/repro_t0.txt"
$PT -d mlx5_6 -p 19500 --report_gbits -D 240 10.1.0.2 > "$DIR/repro_rdma.log" 2>&1 &
iperf3 -B 10.1.0.1 -c 10.1.0.2 -p 5201 -b 0 -t 235 -J > "$DIR/repro_tcp.json" 2>&1
wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/repro_rx.jsonl"
echo V1-done

# ---- V2: 3:1 x 10 reps ----
set_policy 6000000000 3 1
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
sleep 2
rm -f "$DIR/runs.tsv"
for rep in $(seq 1 10); do
    fresh_agents
    p=$((19200 + rep))
    ssh -f sgpu02 "$PT -d mlx5_6 -p $p --report_gbits -D 90 > /tmp/v2_srv.log 2>&1"
    sleep 1
    t0=$(date +%s.%N)
    $PT -d mlx5_6 -p $p --report_gbits -D 90 10.1.0.2 > "$DIR/rdma_3x1_r${rep}.log" 2>&1 &
    iperf3 -B 10.1.0.1 -c 10.1.0.2 -p 5201 -b 0 -t 90 -J > "$DIR/tcp_3x1_r${rep}.json" 2>&1
    wait
    t1=$(date +%s.%N)
    echo -e "3:1\t$rep\t$t0\t$t1" >> "$DIR/runs.tsv"
    sleep 3
done
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/ratio_rx.jsonl"
echo V2-done

echo all-done
