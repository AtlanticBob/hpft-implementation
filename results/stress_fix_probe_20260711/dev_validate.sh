#!/usr/bin/env bash
# Validation of the device-code dig-floor change (bud>>7 -> bud>>2):
#   phase 1: 64KB repro x3 (the episodic-collapse protocol; expect 0
#            episodes and lvl/bud never below ~0.25)
#   phase 2: 3:1 @6G x10 (ratio regression; expect all reps clean)
#   phase 3: 6G single-flow RDMA 60s (M1b-style cap regression)
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw

set_policy() { # cap rdma_w tcp_w
    python3 - "$1" "$2" "$3" <<'EOF'
import json, sys
r = json.load(open("config/lab-registry.json"))
for vm in ("sgpu01/vf0", "sgpu02/vf0"):
    r["policy"]["vms"][vm]["class_weights"] = {"tcp": float(sys.argv[3]), "rdma": float(sys.argv[2])}
r["policy"]["vms"]["sgpu02/vf0"]["max_rate_bps"] = int(sys.argv[1])
json.dump(r, open("config/lab-registry.json", "w"), indent=2)
EOF
    scp -q "$REPO/config/lab-registry.json" hpft-dpu:/tmp/lr.json
    ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/tmp/lr.json
    ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" sgpu02:hpft/lab-registry.json
}

fresh() {
    ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
    ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
    sleep 3
}

repro_run() { # outdir port
    local out=$1 p=$2
    mkdir -p "$out"
    fresh
    ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
    sleep 3
    ssh sgpu02 'pkill -f "ib_write_[b]"; true'
    ssh -f sgpu02 "$PT -d mlx5_6 -p $p --report_gbits -D 240 > /tmp/dv_srv.log 2>&1"
    sleep 1
    ssh hpft-dpu 'for i in $(seq 1 250); do echo "0xdeb 0" > /tmp/rp_fifo; sleep 1; done; grep -a HPFT_RSP /tmp/pcc_rp.log' > "$out/repro_rp.txt" &
    date +%s.%N > "$out/repro_t0.txt"
    $PT -d mlx5_6 -p $p --report_gbits -D 240 10.1.0.2 > "$out/repro_rdma.log" 2>&1 &
    iperf3 -B 10.1.0.1 -c 10.1.0.2 -p 5201 -b 0 -t 235 -J > "$out/repro_tcp.json" 2>&1
    wait
    scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$out/repro_rx.jsonl"
    echo "$(basename "$out")-done"
}

cd "$REPO"
set_policy 6000000000 1 1
for n in 1 2 3; do repro_run "$DIR/dev_r$n" $((19910 + n)); sleep 5; done

set_policy 6000000000 3 1
mkdir -p "$DIR/dev31"
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
sleep 3
rm -f "$DIR/dev31/runs.tsv"
for rep in $(seq 1 10); do
    fresh
    p=$((19930 + rep))
    ssh -f sgpu02 "$PT -d mlx5_6 -p $p --report_gbits -D 90 > /tmp/dv_srv.log 2>&1"
    sleep 1
    t0=$(date +%s.%N)
    $PT -d mlx5_6 -p $p --report_gbits -D 90 10.1.0.2 > "$DIR/dev31/rdma_3x1_r${rep}.log" 2>&1 &
    iperf3 -B 10.1.0.1 -c 10.1.0.2 -p 5201 -b 0 -t 90 -J > "$DIR/dev31/tcp_3x1_r${rep}.json" 2>&1
    wait
    t1=$(date +%s.%N)
    echo -e "3:1\t$rep\t$t0\t$t1" >> "$DIR/dev31/runs.tsv"
    sleep 3
done
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/dev31/ratio_rx.jsonl"

set_policy 6000000000 1 1
fresh
ssh -f sgpu02 "$PT -d mlx5_6 -p 19950 --report_gbits -D 60 > /tmp/dv_srv.log 2>&1"
sleep 1
$PT -d mlx5_6 -p 19950 --report_gbits -D 60 10.1.0.2 > "$DIR/dev_m1b.log" 2>&1
echo dev-validate-done
