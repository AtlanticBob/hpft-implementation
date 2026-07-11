#!/usr/bin/env bash
# Stress D1 batch A: overhead ladder 0 -> 2 -> 8 -> 20 -> 28 flow-sets at
# period_ms=1. Fixed per-flow load (RDMA hw gear 2.5G, TCP 300M) so the only
# variable across stages is N. Cross-pair RDMA uses the (src,dst) flowtags
# probed 2026-07-11; the mesh is the 12-pair collision-free set (vf1/vf2
# flowtag collision - see registry rdma_flowtags comment).
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
CROSS_RDMA="0,1 0,2 0,3 1,0 1,3 3,0 3,1 3,2"

log() { echo "$(date +%s.%N) $*" >> "$DIR/timeline.txt"; }

cpu_sample() {
    local rx tx
    rx=$(ssh hpft-dpu2 'p=$(pgrep -of "rx_agent.py"); awk "{print \$14+\$15}" /proc/$p/stat' 2>/dev/null)
    tx=$(ssh hpft-dpu 'p=$(pgrep -of "tx_agent_e.py"); awk "{print \$14+\$15}" /proc/$p/stat' 2>/dev/null)
    echo "$1 $(date +%s.%N) rx=$rx tx=$tx" >> "$DIR/cpu_samples.txt"
}

rdma_pair() { # i j duration
    local p=$((18800 + 4 * $1 + $2))
    ssh -f sgpu02 "$PT -d mlx5_$((6 + $2)) -p $p --report_gbits -D $3 > /tmp/sc_rsrv_$1$2.log 2>&1"
    sleep 1
    $PT -d mlx5_$((6 + $1)) -p $p --report_gbits --rate_limit=2.5 -D "$3" \
        "10.1.$2.2" > "$DIR/rdma_$1$2.log" 2>&1 &
}

tcp_pair() { # i j duration
    iperf3 -B "10.1.$1.1" -c "10.1.$2.2" -p $((5201 + 4 * $1 + $2)) \
        -b 300M -t "$3" -J > "$DIR/tcp_$1$2.json" 2>&1 &
}

# ---- fresh control plane: RP first, then agents (batch hygiene) ----
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; true'
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; true'
ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
ssh hpft-dpu2 'sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
ssh hpft-dpu 'sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
pkill -f "ib_write_[b]" 2>/dev/null
ssh sgpu02 'pkill -f "ib_write_[b]"; true'
rm -f "$DIR/timeline.txt" "$DIR/cpu_samples.txt"
sleep 5
T0=$(date +%s)
log batchA_t0

log stage0_start        # idle
sleep 25; cpu_sample stage0; sleep 5

log stage1_start        # 2 fs: vf0 straight
rdma_pair 0 0 265
tcp_pair 0 0 262
sleep 53; cpu_sample stage1; sleep 5

log stage2_start        # 8 fs: all straight pairs
for i in 1 2 3; do rdma_pair $i $i 200; tcp_pair $i $i 199; done
sleep 50; cpu_sample stage2; sleep 5

log stage3_start        # 20 fs: + 12 cross TCP
for i in 0 1 2 3; do for j in 0 1 2 3; do
    [ "$i" -ne "$j" ] && tcp_pair "$i" "$j" 140
done; done
sleep 55; cpu_sample stage3; sleep 5

log stage4_start        # 28 fs: + 8 cross RDMA (collision-free set)
for ij in $CROSS_RDMA; do rdma_pair "${ij%,*}" "${ij#*,}" 75; done
sleep 45; cpu_sample stage4a; sleep 15; cpu_sample stage4b

log batchA_drain
wait
log batchA_end

scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/sc_rx.jsonl"
scp -q hpft-dpu:/tmp/hpft_txagent_e.jsonl "$DIR/sc_tx.jsonl"
ssh hpft-dpu2 "sudo journalctl -u hpft-rxagent-e --since '@$T0' --no-pager" > "$DIR/sc_rx_journal.txt"
ssh hpft-dpu "sudo journalctl -u hpft-txagent-e --since '@$T0' --no-pager" > "$DIR/sc_tx_journal.txt"
echo batchA-done
