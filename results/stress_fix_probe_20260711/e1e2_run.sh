#!/usr/bin/env bash
# E1: 64KB repro + 1Hz RC hardware counters on both ends. If sender
#     local_ack_timeout_err and receiver duplicate_request spike at episode
#     onsets, the retransmit-bypass theory is confirmed.
# E2: same repro with qp ack timeout raised 3 notches (-u 17: 67ms -> 537ms
#     x8). Quantitative prediction: stall threshold pace = depth*msg*8/T =
#     128*64KB*8/67ms ~= 1.0G (matches the observed stall region); at -u 17
#     it drops to ~0.125G, below where the MD floor lets pace go ->
#     episodes should vanish.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
CTR=/sys/class/infiniband/mlx5_6/ports/1/hw_counters

set_policy() {
    python3 - <<'EOF'
import json
r = json.load(open("config/lab-registry.json"))
for vm in ("sgpu01/vf0", "sgpu02/vf0"):
    r["policy"]["vms"][vm]["class_weights"] = {"tcp": 1, "rdma": 1}
r["policy"]["vms"]["sgpu02/vf0"]["max_rate_bps"] = 6000000000
json.dump(r, open("config/lab-registry.json", "w"), indent=2)
EOF
    scp -q "$REPO/config/lab-registry.json" hpft-dpu:/tmp/lr.json
    ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/tmp/lr.json
    ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" sgpu02:hpft/lab-registry.json
}

one_run() { # outdir port extra_args...
    local out=$1 p=$2; shift 2
    ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
    ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
    ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
    sleep 4
    ssh sgpu02 'pkill -f "ib_write_[b]"; true'
    ssh -f sgpu02 "$PT -d mlx5_6 -p $p --report_gbits -D 240 $* > /tmp/e_srv.log 2>&1"
    sleep 1
    # samplers: RP introspection + RC counters both ends, all 1Hz
    ssh hpft-dpu 'for i in $(seq 1 250); do echo "0xdeb 0" > /tmp/rp_fifo; sleep 1; done; grep -a HPFT_RSP /tmp/pcc_rp.log' > "$out/repro_rp.txt" &
    ( for i in $(seq 1 250); do
        echo "$(date +%s.%N) tmo=$(cat $CTR/local_ack_timeout_err) rtr=$(cat $CTR/req_transport_retries_exceeded)"
        sleep 1
      done ) > "$out/snd_ctr.txt" &
    ssh sgpu02 "for i in \$(seq 1 250); do echo \"\$(date +%s.%N) dup=\$(cat $CTR/duplicate_request) oos=\$(cat $CTR/out_of_sequence) pse=\$(cat $CTR/packet_seq_err)\"; sleep 1; done" > "$out/rcv_ctr.txt" &
    date +%s.%N > "$out/repro_t0.txt"
    $PT -d mlx5_6 -p $p --report_gbits -D 240 $* 10.1.0.2 > "$out/repro_rdma.log" 2>&1 &
    iperf3 -B 10.1.0.1 -c 10.1.0.2 -p 5201 -b 0 -t 235 -J > "$out/repro_tcp.json" 2>&1
    wait
    scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$out/repro_rx.jsonl"
    echo "run-$(basename "$out")-done"
}

cd "$REPO"
set_policy
one_run "$DIR/e1" 19700
sleep 5
one_run "$DIR/e2" 19701 -u 17
echo e1e2-done
