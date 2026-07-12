#!/usr/bin/env bash
# Instrumented single-pair (vf0, 40G, 7:3) class-churn diagnostic. RDMA runs
# the whole 95s; TCP on [10,40], off [40,60], on [60,95]. Capture rx (r,e,s)
# and tx (R,pace,mode) jsonl + event marks to pin the two mechanisms:
#   fill  (TCP off@40): does RDMA grant e jump to ~40G or ratchet with r?
#   rejoin(TCP on@60):  does TCP mode = fail_open + overshoot?
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
mark() { echo "$(date +%s.%N) $1" >> "$DIR/cd_events.txt"; }

ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
sleep 5
ssh sgpu02 'pkill -f "ib_write_[b]"; true'; sleep 1

ssh -f sgpu02 "$PT -d mlx5_6 -p 24200 --report_gbits -D 95 > /tmp/cd_s.log 2>&1"
sleep 1
rm -f "$DIR/cd_events.txt"
date +%s.%N > "$DIR/cd_t0.txt"; mark start
$PT -d mlx5_6 -p 24200 --report_gbits -D 95 10.1.0.2 > "$DIR/cd_rdma.log" 2>&1 &
sleep 10
mark tcp_on_1
iperf3 -B "10.1.0.1%dpu1vf0" -c 10.1.0.2 -p 5201 -P4 -b 0 -t 30 -J > "$DIR/cd_tcp1.json" 2>&1 &
sleep 30
mark tcp_off       # tcp1 exits (t=40)
sleep 20
mark tcp_on_2      # rejoin (t=60)
iperf3 -B "10.1.0.1%dpu1vf0" -c 10.1.0.2 -p 5201 -P4 -b 0 -t 33 -J > "$DIR/cd_tcp2.json" 2>&1 &
sleep 35
mark end
wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/cd_rx.jsonl"
ssh hpft-dpu 'cp /tmp/hpft_txagent_e.jsonl /tmp/cdtx.jsonl' && scp -q hpft-dpu:/tmp/cdtx.jsonl "$DIR/cd_tx.jsonl"
echo churn-diag-done
