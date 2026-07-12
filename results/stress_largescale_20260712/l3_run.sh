#!/usr/bin/env bash
# L3 QP scale ladder (RDMA-only). 4 straight pairs, each ib_write_bw -q Q,
# total QPs = 4*Q. Ladder Q = 64/256/1024 -> 256/1024/4096 total QPs. Each
# pair capped 20G. Tests the DATA-PATH/RP scale axis: control plane still
# sees 4 flow-sets regardless of QP count. Metrics: per-pair budget hit
# (client BW vs 20G cap), rx/tx tick health + CPU (should be flat since
# flow-set count is constant), RC retransmit counters at scale.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
CTR=/sys/class/infiniband
LADDER="64 256 1024"

cpu() { # label
    rx=$(ssh hpft-dpu2 'p=$(pgrep -of rx_agent.py); awk "{print \$14+\$15}" /proc/$p/stat' 2>/dev/null)
    tx=$(ssh hpft-dpu 'p=$(pgrep -of tx_agent_e.py); awk "{print \$14+\$15}" /proc/$p/stat' 2>/dev/null)
    echo "$1 $(date +%s.%N) rx=$rx tx=$tx" >> "$DIR/l3_cpu.txt"
}

# fresh RP+tx+rx for a clean batch (RDMA-only, no attribution concern)
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
sleep 4

rm -f "$DIR/l3_cpu.txt" "$DIR/l3_runs.tsv"
for Q in $LADDER; do
    tot=$((4*Q))
    ssh sgpu02 'pkill -f "ib_write_[b]"; true'; sleep 1
    # per-pair server + client, -q Q QPs, matching -D
    for n in 0 1 2 3; do
        ssh -f sgpu02 "$PT -d mlx5_$((6+n)) -p $((24090+n)) -q $Q --report_gbits -D 45 > /tmp/l3_s${n}.log 2>&1"
    done
    sleep 2
    # snapshot sender RC counters before
    for n in 0 1 2 3; do
        ssh sgpu02 "cat $CTR/mlx5_$((6+n))/ports/1/hw_counters/packet_seq_err" 2>/dev/null
    done > "$DIR/l3_pse_before_q$Q.txt"
    t0=$(date +%s.%N)
    for n in 0 1 2 3; do
        $PT -d mlx5_$((6+n)) -p $((24090+n)) -q $Q --report_gbits -D 45 "10.1.$n.2" \
            > "$DIR/l3_q${Q}_vf${n}.log" 2>&1 &
    done
    sleep 15; cpu "q${Q}_mid"
    sleep 25
    for n in 0 1 2 3; do
        ssh sgpu02 "cat $CTR/mlx5_$((6+n))/ports/1/hw_counters/packet_seq_err" 2>/dev/null
    done > "$DIR/l3_pse_after_q$Q.txt"
    wait
    t1=$(date +%s.%N)
    scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/l3_rx_q$Q.jsonl"
    echo -e "$Q\t$tot\t$t0\t$t1" >> "$DIR/l3_runs.tsv"
    echo "q$Q ($tot QPs) done"
    sleep 3
done
echo l3-done
