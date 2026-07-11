#!/usr/bin/env bash
# Stress D2: class-weight ratio tracking at the extremes, 1ms.
# Single pair (vf0), dst MaxRate 6G, both classes at full demand for 90s.
# Points: rdma:tcp = 1:1, 3:1 (historical anchor 3.24), 4:1, 8:1, 1:4.
# 3 reps per point, fresh RP + tx agent per rep (batch protocol).
# Weights are set on BOTH sides (sender tree reads src VM, receiver reads
# dst VM); registry edited in-repo, pushed to all three copies, restored
# by the caller afterwards (git checkout + redistribute).
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
POINTS="1,1 3,1 4,1 8,1 1,4"

set_weights() { # rdma tcp
    python3 - "$1" "$2" <<'EOF'
import json, sys
p = "config/lab-registry.json"
r = json.load(open(p))
R, T = float(sys.argv[1]), float(sys.argv[2])
for vm in ("sgpu01/vf0", "sgpu02/vf0"):
    r["policy"]["vms"][vm]["class_weights"] = {"tcp": T, "rdma": R}
r["policy"]["vms"]["sgpu02/vf0"]["max_rate_bps"] = 6000000000
json.dump(r, open(p, "w"), indent=2)
EOF
    scp -q "$REPO/config/lab-registry.json" hpft-dpu:/tmp/lr.json
    ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/tmp/lr.json
    ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" sgpu02:hpft/lab-registry.json
}

cd "$REPO"
rm -f "$DIR/runs.tsv"
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null

for pt in $POINTS; do
    R=${pt%,*}; T=${pt#*,}
    set_weights "$R" "$T"
    for rep in 1 2 3; do
        ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
        ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
        sleep 3
        p=$((19000 + rep))
        ssh -f sgpu02 "$PT -d mlx5_6 -p $p --report_gbits -D 90 > /tmp/ratio_srv.log 2>&1"
        sleep 1
        t0=$(date +%s.%N)
        $PT -d mlx5_6 -p $p --report_gbits -D 90 10.1.0.2 \
            > "$DIR/rdma_${R}x${T}_r${rep}.log" 2>&1 &
        iperf3 -B 10.1.0.1 -c 10.1.0.2 -p 5201 -b 0 -t 90 -J \
            > "$DIR/tcp_${R}x${T}_r${rep}.json" 2>&1 &
        wait
        t1=$(date +%s.%N)
        echo -e "${R}:${T}\t$rep\t$t0\t$t1" >> "$DIR/runs.tsv"
        sleep 3
    done
done

scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/ratio_rx.jsonl"
echo ratio-done
