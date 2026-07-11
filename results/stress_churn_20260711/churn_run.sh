#!/usr/bin/env bash
# Stress D3a/D3b: class enter/exit churn at 1ms.
#
# D3a (single pair, two intensities): vf0, dst 6G, RDMA continuous, TCP
# square wave. a1 = 5s on / 5s off x 18 (both phases longer than the known
# yield/reclaim times ~1.5s/1s); a2 = 2s on / 2s off x 30 (system lives in
# permanent transient). Judged on: yield/reclaim time distributions across
# cycles, drift of the RDMA steady mean (first vs last cycles), no wedge
# (pace-escape alarm quiet, RDMA never pinned).
#
# D3b (four pairs, dephased waves): all straight pairs, dst caps 6G, RDMA
# continuous on all four, per-pair TCP waves with different periods so the
# phases drift. Judged on: per-pair yield/reclaim staying healthy while
# NEIGHBOUR pairs churn (control-plane isolation), no cross-pair coupling.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw

set_caps() { # cap_bps for the listed dst vms
    python3 - "$@" <<'EOF'
import json, sys
cap = int(sys.argv[1])
r = json.load(open("config/lab-registry.json"))
for vm in sys.argv[2:]:
    r["policy"]["vms"][vm]["max_rate_bps"] = cap
    r["policy"]["vms"][vm]["class_weights"] = {"tcp": 1, "rdma": 1}
json.dump(r, open("config/lab-registry.json", "w"), indent=2)
EOF
    scp -q "$REPO/config/lab-registry.json" hpft-dpu:/tmp/lr.json
    ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/tmp/lr.json
    ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" sgpu02:hpft/lab-registry.json
}

fresh_stack() {
    ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
    ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
    ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
    sleep 4
}

tcp_wave() { # i on off cycles logfile
    local i=$1 on=$2 off=$3 n=$4 log=$5
    for c in $(seq 1 "$n"); do
        echo -e "on\t$i\t$c\t$(date +%s.%N)" >> "$log"
        iperf3 -B "10.1.$i.1" -c "10.1.$i.2" -p $((5201 + 5 * i)) \
            -b 0 -t "$on" > /dev/null 2>&1
        echo -e "off\t$i\t$c\t$(date +%s.%N)" >> "$log"
        sleep "$off"
    done
}

cd "$REPO"

# ---------- D3a ----------
set_caps 6000000000 sgpu02/vf0
fresh_stack
for phase in "a1 5 5 18" "a2 2 2 30"; do
    read -r tag on off n <<< "$phase"
    dur=$(( (on + off) * n + 20 ))
    ssh sgpu02 'pkill -f "ib_write_[b]"; true'
    ssh -f sgpu02 "$PT -d mlx5_6 -p 19200 --report_gbits -D $dur > /tmp/churn_srv.log 2>&1"
    sleep 1
    $PT -d mlx5_6 -p 19200 --report_gbits -D "$dur" 10.1.0.2 \
        > "$DIR/rdma_$tag.log" 2>&1 &
    sleep 10
    tcp_wave 0 "$on" "$off" "$n" "$DIR/wave_$tag.tsv"
    wait
    scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/rx_$tag.jsonl"
    echo "$tag done"
done

# ---------- D3b ----------
set_caps 6000000000 sgpu02/vf0 sgpu02/vf1 sgpu02/vf2 sgpu02/vf3
fresh_stack
ssh sgpu02 'pkill -f "ib_write_[b]"; true'
sleep 1
SRV=""
for i in 0 1 2 3; do
    SRV+="nohup $PT -d mlx5_$((6 + i)) -p $((19300 + i)) --report_gbits -D 200 > /tmp/churn_srv$i.log 2>&1 & "
done
ssh sgpu02 "$SRV true"
sleep 1
for i in 0 1 2 3; do
    $PT -d "mlx5_$((6 + i))" -p $((19300 + i)) --report_gbits -D 200 \
        "10.1.$i.2" > "$DIR/rdma_b_$i.log" 2>&1 &
done
sleep 10
tcp_wave 0 6 4 18 "$DIR/wave_b.tsv" &
tcp_wave 1 8 4 15 "$DIR/wave_b.tsv" &
tcp_wave 2 10 5 12 "$DIR/wave_b.tsv" &
tcp_wave 3 12 6 10 "$DIR/wave_b.tsv" &
wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/rx_b.jsonl"
ssh hpft-dpu "sudo journalctl -u hpft-txagent-e --since '-15 min' --no-pager" | grep -a "pace-escape" > "$DIR/escapes.txt" || true
echo churn-done
