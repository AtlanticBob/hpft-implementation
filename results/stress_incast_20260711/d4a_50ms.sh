#!/usr/bin/env bash
# A/B: d4a50 (100G root, 4 flows) with budget slew DISABLED (1.0). Suspect:
# slow-down/fast-up slew asymmetry under sustained contention creates a
# 2-3s enforcement-lag sawtooth (sd 9G, agg 57%); the device dig floor
# now covers what the slew was protecting against, so it may be retired.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw

ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
sleep 4
ssh sgpu02 'pkill -f "ib_write_[b]"; true'
CMD=""
for i in 0 1 2 3; do
    CMD+="nohup $PT -d mlx5_$((6 + i)) -p $((20400 + i)) --report_gbits -D 120 > /tmp/ns_srv$i.log 2>&1 & "
done
ssh sgpu02 "$CMD true"
sleep 2
( ping -i 0.2 -c 600 10.1.1.2 > "$DIR/d4a50_ns_ping.txt" 2>&1 ) &
date +%s.%N > "$DIR/d4a50_ns_t0.txt"
for i in 0 1 2 3; do
    $PT -d mlx5_$((6 + i)) -p $((20400 + i)) --report_gbits -D 120 \
        "10.1.$i.2" > "$DIR/d4a50_ns_vf$i.log" 2>&1 &
done
wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/d4a50_ns_rx.jsonl"
echo d4a50-noslew-done
