#!/usr/bin/env bash
# Stress D3c: online policy steps and rapid flapping (receiver hot-reload).
# Both classes at full demand on vf0 throughout. Three phases:
#   P1 MaxRate steps: 20 -> 6 -> 20 -> 3 -> 20 G, hold 20s each
#   P2 class-weight steps at 6G: 1:1 -> 4:1 -> 1:4 -> 1:1, hold 20s each
#   P3 rapid flapping: 6 <-> 12 G back-to-back x 10 (as fast as the
#      distribution channel allows, ~2-3s/edit), then settle at 6G 30s.
# Judged on: first-entry/full-settle time per step, overshoot depth, last
# write wins after flapping, no oscillation residue, alarm quiet.
# Sender-side registry copies are kept in sync (truth discipline) but the
# tx agent reads policy at start only - the binding path here is the
# receiver, which hot-reloads on mtime.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
LOG="$DIR/policy_steps.tsv"

apply() { # cap_bps rdma_w tcp_w tag
    python3 - "$1" "$2" "$3" <<'EOF'
import json, sys
r = json.load(open("config/lab-registry.json"))
vm = r["policy"]["vms"]["sgpu02/vf0"]
vm["max_rate_bps"] = int(sys.argv[1])
vm["class_weights"] = {"rdma": float(sys.argv[2]), "tcp": float(sys.argv[3])}
json.dump(r, open("config/lab-registry.json", "w"), indent=2)
EOF
    scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/tmp/lr.json
    ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    echo -e "$4\t$1\t$2:$3\t$(date +%s.%N)" >> "$LOG"
    scp -q "$REPO/config/lab-registry.json" hpft-dpu:/tmp/lr.json
    ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" sgpu02:hpft/lab-registry.json
}

cd "$REPO"
rm -f "$LOG"
apply 20000000000 1 1 init
ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
sleep 4
ssh sgpu02 'pkill -f "ib_write_[b]"; true'
ssh -f sgpu02 "$PT -d mlx5_6 -p 19400 --report_gbits -D 260 > /tmp/pol_srv.log 2>&1"
sleep 1
$PT -d mlx5_6 -p 19400 --report_gbits -D 260 10.1.0.2 > "$DIR/rdma_pol.log" 2>&1 &
iperf3 -B 10.1.0.1 -c 10.1.0.2 -p 5201 -b 0 -t 255 -J > "$DIR/tcp_pol.json" 2>&1 &
sleep 15

for step in "6000000000 p1_20to6" "20000000000 p1_6to20" \
            "3000000000 p1_20to3" "20000000000 p1_3to20"; do
    read -r cap tag <<< "$step"
    apply "$cap" 1 1 "$tag"
    sleep 20
done
for step in "4 1 p2_4to1" "1 4 p2_1to4" "1 1 p2_1to1" ; do
    read -r rw tw tag <<< "$step"
    apply 6000000000 "$rw" "$tw" "$tag"
    sleep 20
done
for f in $(seq 1 10); do
    if [ $((f % 2)) -eq 1 ]; then apply 12000000000 1 1 "p3_flap$f";
    else apply 6000000000 1 1 "p3_flap$f"; fi
done
echo -e "p3_settle\t6000000000\t1:1\t$(date +%s.%N)" >> "$LOG"
sleep 30
wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/rx_pol.jsonl"
ssh hpft-dpu "sudo journalctl -u hpft-txagent-e --since '-6 min' --no-pager" | grep -a "pace-escape" > "$DIR/escapes_pol.txt" || true
echo policy-done
