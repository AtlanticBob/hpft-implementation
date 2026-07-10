#!/usr/bin/env bash
# Scale ladder: 2 -> 8 -> 20 flow-sets, 60s per stage; samples agent CPU.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw

cpu_sample() {
    local tag=$1
    local rx tx
    rx=$(ssh hpft-dpu2 'p=$(pgrep -of "rx_agent.py"); awk "{print \$14+\$15}" /proc/$p/stat' 2>/dev/null)
    tx=$(ssh hpft-dpu 'p=$(pgrep -of "tx_agent_e.py"); awk "{print \$14+\$15}" /proc/$p/stat' 2>/dev/null)
    echo "$tag $(date +%s) rx=$rx tx=$tx" >> "$DIR/cpu_samples.txt"
}

ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e python3 /opt/hpft/rx_agent.py' >/dev/null
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e python3 /opt/hpft/tx_agent_e.py' >/dev/null
ssh sgpu02 'pkill -f ib_write_b 2>/dev/null; echo servers-kept'
for i in 0 1 2 3; do
    p=$((18515+i))
    ssh -f sgpu02 "$PT -d mlx5_$((6+i)) -p $p --report_gbits -D 200 > /tmp/sc_server$i.log 2>&1"
done
sleep 2
rm -f "$DIR/cpu_samples.txt"
cpu_sample stage0

# stage 1: 2 fs (vf0 tcp + vf0 rdma)
$PT -d mlx5_6 -p 18515 --report_gbits --rate_limit=2.5 -D 200 10.1.0.2 > "$DIR/sc_rdma0.log" 2>&1 &
iperf3 -B 10.1.0.1 -c 10.1.0.2 -p 5201 -b 300M -t 195 > /dev/null 2>&1 &
sleep 60
cpu_sample stage1_2fs

# stage 2: +6 fs (diag tcp/rdma on vf1-3) = 8
for i in 1 2 3; do
    p=$((18515+i))
    $PT -d mlx5_$((6+i)) -p $p --report_gbits --rate_limit=2.5 -D 135 10.1.$i.2 > "$DIR/sc_rdma$i.log" 2>&1 &
    iperf3 -B 10.1.$i.1 -c 10.1.$i.2 -p $((5201+5*i)) -b 300M -t 130 > /dev/null 2>&1 &
done
sleep 60
cpu_sample stage2_8fs

# stage 3: +12 cross tcp = 20
for i in 0 1 2 3; do
    for j in 0 1 2 3; do
        [ $i -ne $j ] && iperf3 -B 10.1.$i.1 -c 10.1.$j.2 -p $((5201+4*i+j)) -b 300M -t 65 > /dev/null 2>&1 &
    done
done
sleep 60
cpu_sample stage3_20fs
wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/sc_rx.jsonl"
scp -q hpft-dpu:/tmp/hpft_txagent_e.jsonl "$DIR/sc_tx.jsonl"
echo ladder-done
