#!/usr/bin/env bash
# Reproducibility test for the multi-pair churn oscillation. 4 pairs at
# L1b caps (steady baseline confirmed rock-stable). vf0 TCP does one class
# churn: on [0,40], off [40,60], on [60,120]. Other 3 pairs steady the
# whole time. Watch vf0's class split through the rejoin + 40s after - does
# the delayed (~16s post-rejoin) oscillation seen once in L2 reproduce?
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
mark() { echo "$(date +%s.%N) $1" >> "$DIR/c4_events.txt"; }

ssh hpft-dpu2 'ethtool p1 | grep Speed'
ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
sleep 5
ssh sgpu02 'pkill -f "ib_write_[b]"; true'; sleep 1

# RDMA servers+clients for all 4 pairs (-D 120 matched), TCP for vf1-3 steady
for n in 0 1 2 3; do ssh -f sgpu02 "$PT -d mlx5_$((6+n)) -p $((24220+n)) --report_gbits -D 120 > /tmp/c4_s$n.log 2>&1"; done
sleep 2
rm -f "$DIR/c4_events.txt"
date +%s.%N > "$DIR/c4_t0.txt"; mark start
for n in 0 1 2 3; do
    $PT -d mlx5_$((6+n)) -p $((24220+n)) --report_gbits -D 120 "10.1.$n.2" > "$DIR/c4_rdma$n.log" 2>&1 &
done
for n in 1 2 3; do
    iperf3 -B "10.1.$n.1%dpu1vf$n" -c "10.1.$n.2" -p $((5201+4*n)) -P4 -b 0 -t 120 -J > "$DIR/c4_tcp$n.json" 2>&1 &
done
# vf0 TCP: on [0,40]
iperf3 -B "10.1.0.1%dpu1vf0" -c "10.1.0.2" -p 5201 -P4 -b 0 -t 40 -J > "$DIR/c4_tcp0a.json" 2>&1 &
sleep 40
mark tcp0_off
sleep 20
mark tcp0_on
iperf3 -B "10.1.0.1%dpu1vf0" -c "10.1.0.2" -p 5201 -P4 -b 0 -t 58 -J > "$DIR/c4_tcp0b.json" 2>&1 &
sleep 58
mark end
wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/c4_rx.jsonl"
echo churn4-done
