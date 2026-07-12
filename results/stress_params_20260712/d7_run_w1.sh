#!/usr/bin/env bash
# Stress D7 workload W1: single-axis parameter sweep on the single-pair
# two-class contention protocol (vf0, 6G cap, both classes full demand,
# 60s per point). One parameter overridden per point (d7_points_w1.tsv),
# all others at defaults; fresh rx+RP+tx per point so every e_param
# (rx-side V/rate_window included) is picked up cleanly. Cliff metrics
# per point: class ratio, per-second stdev, weak-class min, utilization.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw

distribute() {
    scp -q "$REPO/config/lab-registry.json" hpft-dpu:/tmp/lr.json
    ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/tmp/lr.json
    ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" sgpu02:hpft/lab-registry.json
}

cd "$REPO"
# W1 policy: vf0 pair 6G, 1:1 (constant across the sweep)
python3 - <<'EOF'
import json
r = json.load(open("config/lab-registry.json"))
for vm in ("sgpu01/vf0", "sgpu02/vf0"):
    r["policy"]["vms"][vm]["class_weights"] = {"tcp": 1, "rdma": 1}
r["policy"]["vms"]["sgpu02/vf0"]["max_rate_bps"] = 6000000000
json.dump(r, open("config/lab-registry.json", "w"), indent=2)
EOF

rm -f "$DIR/w1_runs.tsv"
n=0
while IFS=$'\t' read -r param value <&3; do
    n=$((n + 1))
    tag=$(printf "p%02d_%s_%s" "$n" "$param" "$value")
    python3 - "$param" "$value" <<'EOF'
import json, sys
DEFAULTS = {"a_frac_linerate": 0.0005, "beta": 0.3, "v_periods": 2,
            "rate_window_s": 0.02, "probe_over_grant": 1.05,
            "probe_gain": 2.0, "rdma_bud_slew_per_s": 0.35}
r = json.load(open("config/lab-registry.json"))
for k, v in DEFAULTS.items():
    r["e_params"][k] = v
if sys.argv[1] != "none":
    r["e_params"][sys.argv[1]] = float(sys.argv[2])
json.dump(r, open("config/lab-registry.json", "w"), indent=2)
EOF
    distribute
    ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
    ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
    ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
    sleep 4
    ssh sgpu02 'pkill -f "ib_write_[b]"; true'
    p=$((21000 + n))
    ssh -f sgpu02 "$PT -d mlx5_6 -p $p --report_gbits -D 60 > /tmp/d7_srv.log 2>&1"
    sleep 1
    t0=$(date +%s.%N)
    $PT -d mlx5_6 -p $p --report_gbits -D 60 10.1.0.2 > "$DIR/${tag}_rdma.log" 2>&1 &
    iperf3 -B 10.1.0.1 -c 10.1.0.2 -p 5201 -b 0 -t 60 -J > "$DIR/${tag}_tcp.json" 2>&1
    wait
    t1=$(date +%s.%N)
    scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${tag}_rx.jsonl"
    echo -e "$tag\t$param\t$value\t$t0\t$t1" >> "$DIR/w1_runs.tsv"
    echo "$tag done"
done 3< "$DIR/d7_points_w1.tsv"
echo w1-sweep-done
