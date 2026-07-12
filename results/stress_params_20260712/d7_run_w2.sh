#!/usr/bin/env bash
# Stress D7 workload W2: root-regime focus axes. Can parameters damp the
# D4 synchronized relaxation oscillation? 4 straight-pair RDMA flows at
# full demand, caps lifted, p1 at 100G (root 97G binds). One p1 flap in,
# one out (ops rule: minimize speed transitions). Points: defaults, V x
# {4,8,16}, A x {0.5x,2x}, beta 0.15. Metrics: aggregate vs 97G, per-flow
# per-second sd (D4 baseline: 53.6G, sd 10.2).
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
POINTS="none:default v_periods:4 v_periods:8 v_periods:16 a_frac_linerate:0.00025 a_frac_linerate:0.001 beta:0.15"

distribute() {
    scp -q "$REPO/config/lab-registry.json" hpft-dpu:/tmp/lr.json
    ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/tmp/lr.json
    ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" sgpu02:hpft/lab-registry.json
}

cd "$REPO"
python3 - <<'EOF'
import json
r = json.load(open("config/lab-registry.json"))
for vm, p in r["policy"]["vms"].items():
    p["max_rate_bps"] = None
    p["class_weights"] = {"tcp": 1, "rdma": 1}
    p["weight"] = 1
json.dump(r, open("config/lab-registry.json", "w"), indent=2)
EOF

ssh hpft-dpu2 "sudo ethtool -s p1 speed 100000 duplex full" 2>/dev/null
sleep 15
ssh hpft-dpu2 "ethtool p1 | grep Speed"

rm -f "$DIR/w2_runs.tsv"
n=0
for pt in $POINTS; do
    param=${pt%%:*}; value=${pt##*:}
    n=$((n + 1))
    tag=$(printf "w2p%02d_%s_%s" "$n" "$param" "$value")
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
    CMD=""
    for i in 0 1 2 3; do
        CMD+="nohup $PT -d mlx5_$((6 + i)) -p $((21100 + 4 * n + i)) --report_gbits -D 60 > /tmp/w2_srv$i.log 2>&1 & "
    done
    ssh sgpu02 "$CMD true"
    sleep 2
    t0=$(date +%s.%N)
    for i in 0 1 2 3; do
        $PT -d mlx5_$((6 + i)) -p $((21100 + 4 * n + i)) --report_gbits -D 60 \
            "10.1.$i.2" > "$DIR/${tag}_vf$i.log" 2>&1 &
    done
    wait
    t1=$(date +%s.%N)
    scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${tag}_rx.jsonl"
    echo -e "$tag\t$param\t$value\t$t0\t$t1" >> "$DIR/w2_runs.tsv"
    echo "$tag done"
done

ssh hpft-dpu2 "sudo ethtool -s p1 speed 200000 duplex full" 2>/dev/null
sleep 15
ssh hpft-dpu2 "ethtool p1 | grep Speed"
echo w2-sweep-done
