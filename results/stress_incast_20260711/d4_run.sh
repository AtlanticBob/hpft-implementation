#!/usr/bin/env bash
# Stress D4: incast (M3@1ms first validation) + inter-tenant root weights.
#   D4a  p1->100G, 4 straight-pair RDMA full demand, caps lifted -> root
#        (97G) is the binding layer; expect weight-split ±10%, near-zero
#        physical queueing (ping), spread comparable to M3@50ms (0.05%).
#   D4b  p1->25G deep shortage (shares 6.06G) - crosses into the low-budget
#        regime on top of the new RP dig floor.
#   D4c  inter-tenant weights at root: vf0:vf1 = 5:1 then 10:1 (two flows).
#   D4d  churn join: 3 flows steady, vf3 joins with optimistic start;
#        re-convergence time + physical transient (ping max).
# In-band telemetry rides p1: the speed flap blackout (~10s) exercises
# fail-open; during saturated 25G runs telemetry contends with data - tx
# mode transitions are themselves an observable.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw

set_policy() { # w0 (vf0 weight; others 1). caps lifted on all VMs.
    python3 - "$1" <<'EOF'
import json, sys
r = json.load(open("config/lab-registry.json"))
for vm, p in r["policy"]["vms"].items():
    p["max_rate_bps"] = None
    p["class_weights"] = {"tcp": 1, "rdma": 1}
    p["weight"] = 1
r["policy"]["vms"]["sgpu02/vf0"]["weight"] = int(sys.argv[1])
r["policy"]["vms"]["sgpu01/vf0"]["weight"] = int(sys.argv[1])
json.dump(r, open("config/lab-registry.json", "w"), indent=2)
EOF
    scp -q "$REPO/config/lab-registry.json" hpft-dpu:/tmp/lr.json
    ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/tmp/lr.json
    ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" sgpu02:hpft/lab-registry.json
}

p1_speed() { # Mbps
    ssh hpft-dpu2 "sudo ethtool -s p1 speed $1 duplex full" 2>/dev/null
    sleep 15
    ssh hpft-dpu2 "ethtool p1 | grep Speed"
}

fresh() {
    ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
    ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
    ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
    sleep 4
}

servers() { # duration flows...
    local d=$1; shift
    local cmd=""
    for i in "$@"; do
        cmd+="nohup $PT -d mlx5_$((6 + i)) -p $((20000 + i)) --report_gbits -D $d > /tmp/d4_srv$i.log 2>&1 & "
    done
    ssh sgpu02 "$cmd true"
    sleep 2
}

phase() { # tag duration ping_target flows...
    local tag=$1 d=$2 pt=$3; shift 3
    servers "$d" "$@"
    ( ping -i 0.2 -c $((d * 5)) "$pt" > "$DIR/${tag}_ping.txt" 2>&1 ) &
    ssh hpft-dpu 'for i in $(seq 1 '"$d"'); do for j in 0 1 2 3; do echo "0xdeb $j" > /tmp/rp_fifo; sleep 0.05; done; sleep 0.8; done; grep -a HPFT_RSP /tmp/pcc_rp.log | tail -'"$((d * 4))"'' > "$DIR/${tag}_rp.txt" &
    date +%s.%N > "$DIR/${tag}_t0.txt"
    for i in "$@"; do
        $PT -d mlx5_$((6 + i)) -p $((20000 + i)) --report_gbits -D "$d" \
            "10.1.$i.2" > "$DIR/${tag}_vf$i.log" 2>&1 &
    done
    wait
    scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${tag}_rx.jsonl"
    echo "${tag}-done"
}

cd "$REPO"
set_policy 1

# D4a: 100G root shortage
p1_speed 100000
fresh
phase d4a 120 10.1.1.2 0 1 2 3

# D4b: 25G deep shortage
p1_speed 25000
fresh
phase d4b 120 10.1.1.2 0 1 2 3

# D4c: inter-tenant weights (two flows, 25G root for contrast)
set_policy 5
fresh
phase d4c5 90 10.1.1.2 0 1
set_policy 10
fresh
phase d4c10 90 10.1.1.2 0 1

# D4d: churn join at 100G, equal weights
set_policy 1
p1_speed 100000
fresh
servers 150 0 1 2
( ping -i 0.2 -c 700 10.1.1.2 > "$DIR/d4d_ping.txt" 2>&1 ) &
date +%s.%N > "$DIR/d4d_t0.txt"
for i in 0 1 2; do
    $PT -d mlx5_$((6 + i)) -p $((20000 + i)) --report_gbits -D 150 \
        "10.1.$i.2" > "$DIR/d4d_vf$i.log" 2>&1 &
done
sleep 60
servers 60 3
date +%s.%N > "$DIR/d4d_join.txt"
$PT -d mlx5_9 -p 20003 --report_gbits -D 60 10.1.3.2 > "$DIR/d4d_vf3.log" 2>&1 &
wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/d4d_rx.jsonl"
ssh hpft-dpu "sudo journalctl -u hpft-txagent-e --since '-25 min' --no-pager" | grep -aE "pace-escape|fail_open|frozen" > "$DIR/d4_txmodes.txt" || true

# restore: line speed + stable policy
p1_speed 200000
echo d4-done
