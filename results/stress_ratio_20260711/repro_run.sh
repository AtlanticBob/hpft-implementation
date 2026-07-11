#!/usr/bin/env bash
# Targeted reproduction of the 1:1 rep2 episodic RDMA collapse, with a 1Hz
# RP introspection sampler (0xdeb) so bud/lvl/cc are captured live during
# any incident. 240s single run, vf0, 6G, both classes full demand.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw

ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
sleep 4
ssh sgpu02 'pkill -f "ib_write_[b]"; true'
ssh -f sgpu02 "$PT -d mlx5_6 -p 19500 --report_gbits -D 240 > /tmp/repro_srv.log 2>&1"
sleep 1

# 1 Hz RP sampler: pair idx 0 is the only rdma pair on a fresh table.
# The sampler outlives the traffic by ~10s, a single wait covers all.
ssh hpft-dpu 'for i in $(seq 1 250); do echo "0xdeb 0" > /tmp/rp_fifo; sleep 1; done; grep -a HPFT_RSP /tmp/pcc_rp.log' > "$DIR/repro_rp.txt" &

date +%s.%N > "$DIR/repro_t0.txt"
$PT -d mlx5_6 -p 19500 --report_gbits -D 240 10.1.0.2 > "$DIR/repro_rdma.log" 2>&1 &
iperf3 -B 10.1.0.1 -c 10.1.0.2 -p 5201 -b 0 -t 235 -J > "$DIR/repro_tcp.json" 2>&1 &
wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/repro_rx.jsonl"
ssh hpft-dpu 'cp /tmp/hpft_txagent_e.jsonl /tmp/txe_repro.jsonl' && scp -q hpft-dpu:/tmp/txe_repro.jsonl "$DIR/repro_tx.jsonl"
echo repro-done
