#!/usr/bin/env bash
# High-cap check: does two-class fairness hold as the pacing bandwidth
# rises far above the 6G the campaign used? A and V are anchored to LINE
# rate (not the cap), so their relative effect shifts with the cap - this
# sweep (6/20/50G, single vf0 pair, 1:1, V=4 new default, 60s) tests
# whether the class ratio/stability survive. Steady entitlement per class
# = cap/2. TCP single-stream ceiling ~29G, so 50G/2=25G per class is
# reachable; caps above ~58G aggregate would make TCP app-limited.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
CAPS="6 20 50"

distribute() {
    scp -q "$REPO/config/lab-registry.json" hpft-dpu:/tmp/lr.json
    ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/tmp/lr.json
    ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" sgpu02:hpft/lab-registry.json
}

set_cap() { # cap_g
    python3 - "$1" <<'EOF'
import json, sys
r = json.load(open("config/lab-registry.json"))
for vm, p in r["policy"]["vms"].items():
    p["max_rate_bps"] = 20000000000; p["class_weights"] = {"tcp": 1, "rdma": 1}; p["weight"] = 1
r["policy"]["vms"]["sgpu02/vf0"]["max_rate_bps"] = int(float(sys.argv[1]) * 1e9)
r["e_params"]["v_periods"] = 4
json.dump(r, open("config/lab-registry.json", "w"), indent=2)
EOF
    distribute
}

cd "$REPO"
rm -f "$DIR/runs.tsv"
for cap in $CAPS; do
    set_cap "$cap"
    ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
    ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
    ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
    sleep 4
    ssh sgpu02 'pkill -f "ib_write_[b]"; true'
    tag="cap${cap}g"; p=$((23000 + cap))
    ssh -f sgpu02 "$PT -d mlx5_6 -p $p --report_gbits -D 60 > /tmp/hc_srv.log 2>&1"
    sleep 1
    t0=$(date +%s.%N)
    $PT -d mlx5_6 -p $p --report_gbits -D 60 10.1.0.2 > "$DIR/${tag}_rdma.log" 2>&1 &
    iperf3 -B 10.1.0.1 -c 10.1.0.2 -p 5201 -b 0 -t 60 -J > "$DIR/${tag}_tcp.json" 2>&1 &
    wait
    scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${tag}_rx.jsonl"
    echo -e "$tag\t$cap\t$t0" >> "$DIR/runs.tsv"
    echo "$tag done"
done
echo capsweep-done
