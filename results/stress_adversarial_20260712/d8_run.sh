#!/usr/bin/env bash
# Stress D8: adversarial on-off timing. Attacker = TCP pump (attack_pump)
# on the vf0 pair, exploiting idle-clears-VQ + AI/FR regrowth; compliant
# competitor = RDMA at full demand. 6G cap, 1:1 class weights: steady
# entitlement 3G each. Runs: control (steady attacker) + three pump
# periods. Judged on the attacker's long-run wire share vs 3G (+10% =
# robustness bound) and the harm to the compliant class.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
DUR=120
RUNS="control:0:0 fast:150:30 mid:400:50 slow:1000:100"

cd "$REPO"
python3 - <<'EOF'
import json
r = json.load(open("config/lab-registry.json"))
DEFAULTS = {"a_frac_linerate": 0.0005, "beta": 0.3, "v_periods": 2,
            "rate_window_s": 0.02, "probe_over_grant": 1.05,
            "probe_gain": 2.0, "rdma_bud_slew_per_s": 0.35}
for k, v in DEFAULTS.items():
    r["e_params"][k] = v
for vm, p in r["policy"]["vms"].items():
    p["max_rate_bps"] = 20000000000
    p["class_weights"] = {"tcp": 1, "rdma": 1}
    p["weight"] = 1
for vm in ("sgpu01/vf0", "sgpu02/vf0"):
    r["policy"]["vms"][vm]["class_weights"] = {"tcp": 1, "rdma": 1}
r["policy"]["vms"]["sgpu02/vf0"]["max_rate_bps"] = 6000000000
json.dump(r, open("config/lab-registry.json", "w"), indent=2)
EOF
scp -q config/lab-registry.json hpft-dpu:/tmp/lr.json
ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json hpft-dpu2:/tmp/lr.json
ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json sgpu02:hpft/lab-registry.json

scp -q "$DIR/attack_sink.py" sgpu02:/tmp/attack_sink.py
ssh sgpu02 'pkill -f "attack_sin[k]"; true'
ssh -f sgpu02 'nohup python3 /tmp/attack_sink.py 5399 > /tmp/sink.log 2>&1'

rm -f "$DIR/d8_runs.tsv"
n=0
for run in $RUNS; do
    name=${run%%:*}; rest=${run#*:}; on=${rest%%:*}; off=${rest##*:}
    n=$((n + 1))
    ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
    ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
    ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
    sleep 4
    ssh sgpu02 'pkill -f "ib_write_[b]"; true'
    p=$((21500 + n))
    ssh -f sgpu02 "$PT -d mlx5_6 -p $p --report_gbits -D $DUR > /tmp/d8_srv.log 2>&1"
    sleep 1
    t0=$(date +%s.%N)
    $PT -d mlx5_6 -p $p --report_gbits -D $DUR 10.1.0.2 > "$DIR/${name}_rdma.log" 2>&1 &
    if [ "$name" = control ]; then
        iperf3 -B 10.1.0.1 -c 10.1.0.2 -p 5201 -b 0 -t $DUR -J > "$DIR/control_tcp.json" 2>&1 &
    else
        python3 "$DIR/attack_pump.py" 10.1.0.2 5399 "$on" "$off" $DUR 10.1.0.1 \
            > "$DIR/${name}_pump.log" 2>&1 &
    fi
    wait
    t1=$(date +%s.%N)
    scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${name}_rx.jsonl"
    echo -e "$name\t$on\t$off\t$t0\t$t1" >> "$DIR/d8_runs.tsv"
    echo "$name done"
done
ssh sgpu02 'pkill -f "attack_sin[k]"; true'
echo d8-done
