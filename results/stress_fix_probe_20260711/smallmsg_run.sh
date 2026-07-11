#!/usr/bin/env bash
# Small-message control for the RC-stall hypothesis: the V1 repro protocol
# (240s, vf0 pair, 1:1 @6G, both classes full demand, 1Hz 0xdeb) with the
# RDMA message size as the ONLY variable vs iter2 V1 (64KB baseline, 3
# collapse episodes). If collapses vanish at 16KB/8KB, the "in-flight
# large messages x fast deep pace -> RC retransmit window" mechanism is
# confirmed and the device-code investigation has a precise target.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw

set_policy() {
    python3 - <<'EOF'
import json
r = json.load(open("config/lab-registry.json"))
for vm in ("sgpu01/vf0", "sgpu02/vf0"):
    r["policy"]["vms"][vm]["class_weights"] = {"tcp": 1, "rdma": 1}
r["policy"]["vms"]["sgpu02/vf0"]["max_rate_bps"] = 6000000000
json.dump(r, open("config/lab-registry.json", "w"), indent=2)
EOF
    scp -q "$REPO/config/lab-registry.json" hpft-dpu:/tmp/lr.json
    ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/tmp/lr.json
    ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" sgpu02:hpft/lab-registry.json
}

one_run() { # msgsize outdir port
    local sz=$1 out=$2 p=$3
    ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
    ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
    ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
    sleep 4
    ssh sgpu02 'pkill -f "ib_write_[b]"; true'
    ssh -f sgpu02 "$PT -d mlx5_6 -p $p -s $sz --report_gbits -D 240 > /tmp/sm_srv.log 2>&1"
    sleep 1
    ssh hpft-dpu 'for i in $(seq 1 250); do echo "0xdeb 0" > /tmp/rp_fifo; sleep 1; done; grep -a HPFT_RSP /tmp/pcc_rp.log' > "$out/repro_rp.txt" &
    date +%s.%N > "$out/repro_t0.txt"
    $PT -d mlx5_6 -p $p -s $sz --report_gbits -D 240 10.1.0.2 > "$out/repro_rdma.log" 2>&1 &
    iperf3 -B 10.1.0.1 -c 10.1.0.2 -p 5201 -b 0 -t 235 -J > "$out/repro_tcp.json" 2>&1
    wait
    scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$out/repro_rx.jsonl"
    echo "run-$sz-done"
}

cd "$REPO"
set_policy
one_run 16384 "$DIR/sm16" 19600
sleep 5
one_run 8192 "$DIR/sm8" 19601
echo smallmsg-done
