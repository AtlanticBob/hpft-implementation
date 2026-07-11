#!/usr/bin/env bash
# Stress D1 batch B: fairness precision at 28 concurrent flow-sets, 1ms.
# All demands unlimited: 16 TCP (-b 0) + 12 RDMA (no rate_limit, the
# collision-free pair set). Every dst VM: 20G MaxRate contested by 7
# flow-sets (4 tcp + 3 rdma), classes 1:1 -> 10G/10G, per-sender equal.
# Sender side binds too (vf0: 8 fs in 20G) - min-invariant at scale.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
RDMA_PAIRS="0,0 0,1 0,2 0,3 1,0 1,1 1,3 2,2 3,0 3,1 3,2 3,3"
D=130

log() { echo "$(date +%s.%N) $*" >> "$DIR/fair_timeline.txt"; }

# ---- fresh control plane (batch hygiene: RP restart clears pair table) ----
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; true'
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; true'
ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
ssh hpft-dpu2 'sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
ssh hpft-dpu 'sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
pkill -f "ib_write_[b]" 2>/dev/null
ssh sgpu02 'pkill -f "ib_write_[b]"; true'
rm -f "$DIR/fair_timeline.txt"
sleep 5
T0=$(date +%s)
log fair_t0

# all 12 RDMA servers in one remote command (no pkill in the same ssh)
SRV_CMD=""
for ij in $RDMA_PAIRS; do
    i=${ij%,*}; j=${ij#*,}
    p=$((18800 + 4 * i + j))
    SRV_CMD+="nohup $PT -d mlx5_$((6 + j)) -p $p --report_gbits -D $D > /tmp/fair_rsrv_$i$j.log 2>&1 & "
done
ssh sgpu02 "$SRV_CMD true"
sleep 2

log clients_start
for ij in $RDMA_PAIRS; do
    i=${ij%,*}; j=${ij#*,}
    p=$((18800 + 4 * i + j))
    $PT -d "mlx5_$((6 + i))" -p $p --report_gbits -D $D "10.1.$j.2" \
        > "$DIR/fair_rdma_$i$j.log" 2>&1 &
    sleep 0.3
done
for i in 0 1 2 3; do for j in 0 1 2 3; do
    iperf3 -B "10.1.$i.1" -c "10.1.$j.2" -p $((5201 + 4 * i + j)) \
        -b 0 -t $((D - 5)) -J > "$DIR/fair_tcp_$i$j.json" 2>&1 &
    sleep 0.1
done; done
log clients_up

sleep 60
rx=$(ssh hpft-dpu2 'p=$(pgrep -of "rx_agent.py"); awk "{print \$14+\$15}" /proc/$p/stat' 2>/dev/null)
tx=$(ssh hpft-dpu 'p=$(pgrep -of "tx_agent_e.py"); awk "{print \$14+\$15}" /proc/$p/stat' 2>/dev/null)
echo "fair_mid $(date +%s.%N) rx=$rx tx=$tx" >> "$DIR/cpu_samples.txt"

wait
log fair_end

scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/fair_rx.jsonl"
scp -q hpft-dpu:/tmp/hpft_txagent_e.jsonl "$DIR/fair_tx.jsonl"
ssh hpft-dpu2 "sudo journalctl -u hpft-rxagent-e --since '@$T0' --no-pager" > "$DIR/fair_rx_journal.txt"
ssh hpft-dpu "sudo journalctl -u hpft-txagent-e --since '@$T0' --no-pager" > "$DIR/fair_tx_journal.txt"
echo batchB-done
