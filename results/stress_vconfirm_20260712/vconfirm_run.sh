#!/usr/bin/env bash
# V=4 pre-adoption confirmation. Probes the risks of doubling V (fixed bit
# quantity -> slower response to sustained overshoot): higher flow count
# and slower marking/reclaim. Four sub-tests; borrow/reclaim and A-edge
# run at BOTH V=2 and V=4 (direct A/B); 28fs and repro compare V=4 against
# existing current-stack V=2 baselines (7cb69fb, dev_r*).
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
RDMA_MESH="0,0 0,1 0,2 0,3 1,0 1,1 1,3 2,2 3,0 3,1 3,2 3,3"

distribute() {
    scp -q "$REPO/config/lab-registry.json" hpft-dpu:/tmp/lr.json
    ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/tmp/lr.json
    ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" sgpu02:hpft/lab-registry.json
}

# set V and A, single-pair 6G 1:1 policy (a=0 keeps default)
set_params() { # v a
    python3 - "$1" "$2" <<'EOF'
import json, sys
r = json.load(open("config/lab-registry.json"))
r["e_params"]["v_periods"] = float(sys.argv[1])
r["e_params"]["a_frac_linerate"] = float(sys.argv[2]) if sys.argv[2] != "0" else 0.0005
json.dump(r, open("config/lab-registry.json", "w"), indent=2)
EOF
    distribute
}

policy_pair() {
    python3 - <<'EOF'
import json
r = json.load(open("config/lab-registry.json"))
for vm, p in r["policy"]["vms"].items():
    p["max_rate_bps"] = 20000000000; p["class_weights"] = {"tcp": 1, "rdma": 1}; p["weight"] = 1
r["policy"]["vms"]["sgpu02/vf0"]["max_rate_bps"] = 6000000000
json.dump(r, open("config/lab-registry.json", "w"), indent=2)
EOF
}

fresh() {
    ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
    ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
    ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
    sleep 4
    ssh sgpu02 'pkill -f "ib_write_[b]"; true'
}

cd "$REPO"
rm -f "$DIR/runs.tsv"

# ---- borrow/reclaim (75s: RDMA whole, TCP [15,45]) at V=2 and V=4 ----
for v in 2 4; do
    policy_pair; set_params $v 0
    fresh
    tag="borrow_v$v"; p=$((22000 + v))
    ssh -f sgpu02 "$PT -d mlx5_6 -p $p --report_gbits -D 75 > /tmp/vc_srv.log 2>&1"
    sleep 1
    t0=$(date +%s.%N)
    $PT -d mlx5_6 -p $p --report_gbits -D 75 10.1.0.2 > "$DIR/${tag}_rdma.log" 2>&1 &
    ( sleep 15; iperf3 -B 10.1.0.1 -c 10.1.0.2 -p 5201 -b 0 -t 30 -J > "$DIR/${tag}_tcp.json" 2>&1 ) &
    wait
    scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${tag}_rx.jsonl"
    echo -e "$tag\t$t0" >> "$DIR/runs.tsv"
    echo "$tag done"
done

# ---- A-edge (A=0.00075) single-pair contention 60s at V=2 and V=4 ----
for v in 2 4; do
    policy_pair; set_params $v 0.00075
    fresh
    tag="aedge_v$v"; p=$((22100 + v))
    ssh -f sgpu02 "$PT -d mlx5_6 -p $p --report_gbits -D 60 > /tmp/vc_srv.log 2>&1"
    sleep 1
    t0=$(date +%s.%N)
    $PT -d mlx5_6 -p $p --report_gbits -D 60 10.1.0.2 > "$DIR/${tag}_rdma.log" 2>&1 &
    iperf3 -B 10.1.0.1 -c 10.1.0.2 -p 5201 -b 0 -t 60 -J > "$DIR/${tag}_tcp.json" 2>&1 &
    wait
    scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${tag}_rx.jsonl"
    echo -e "$tag\t$t0" >> "$DIR/runs.tsv"
    echo "$tag done"
done

# ---- 28fs mesh at V=4 (V=2 baseline = 7cb69fb) ----
python3 - <<'EOF'
import json
r = json.load(open("config/lab-registry.json"))
for vm, p in r["policy"]["vms"].items():
    p["max_rate_bps"] = 20000000000; p["class_weights"] = {"tcp": 1, "rdma": 1}; p["weight"] = 1
json.dump(r, open("config/lab-registry.json", "w"), indent=2)
EOF
set_params 4 0
fresh
tag="mesh_v4"
SRV=""
for ij in $RDMA_MESH; do i=${ij%,*}; j=${ij#*,}; pp=$((22200 + 4*i + j)); SRV+="nohup $PT -d mlx5_$((6+j)) -p $pp --report_gbits -D 100 > /tmp/vc_m$i$j.log 2>&1 & "; done
ssh sgpu02 "$SRV true"; sleep 2
t0=$(date +%s.%N)
for ij in $RDMA_MESH; do i=${ij%,*}; j=${ij#*,}; pp=$((22200 + 4*i + j)); $PT -d mlx5_$((6+i)) -p $pp --report_gbits -D 100 10.1.$j.2 > "$DIR/${tag}_r$i$j.log" 2>&1 & sleep 0.3; done
for i in 0 1 2 3; do for j in 0 1 2 3; do iperf3 -B 10.1.$i.1 -c 10.1.$j.2 -p $((5201 + 4*i + j)) -b 0 -t 95 -J > "$DIR/${tag}_t$i$j.json" 2>&1 & sleep 0.1; done; done
t_up=$(date +%s.%N)
sleep 90; wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${tag}_rx.jsonl"
echo -e "$tag\t$t0\t$t_up" >> "$DIR/runs.tsv"
echo "$tag done"

# ---- 64KB repro x2 at V=4 (V=2 baseline = dev_r1/2/3) ----
for n in 1 2; do
    policy_pair; set_params 4 0
    fresh
    tag="repro_v4_r$n"; p=$((22300 + n))
    ssh -f sgpu02 "$PT -d mlx5_6 -p $p --report_gbits -D 240 > /tmp/vc_srv.log 2>&1"
    sleep 1
    ssh hpft-dpu 'for i in $(seq 1 250); do echo "0xdeb 0" > /tmp/rp_fifo; sleep 1; done; grep -a HPFT_RSP /tmp/pcc_rp.log' > "$DIR/${tag}_rp.txt" &
    t0=$(date +%s.%N)
    $PT -d mlx5_6 -p $p --report_gbits -D 240 10.1.0.2 > "$DIR/${tag}_rdma.log" 2>&1 &
    iperf3 -B 10.1.0.1 -c 10.1.0.2 -p 5201 -b 0 -t 235 -J > "$DIR/${tag}_tcp.json" 2>&1
    wait
    scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${tag}_rx.jsonl"
    echo -e "$tag\t$t0" >> "$DIR/runs.tsv"
    echo "$tag done"
done
echo vconfirm-done
