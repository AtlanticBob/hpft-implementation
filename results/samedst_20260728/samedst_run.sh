#!/usr/bin/env bash
# Does a second RDMA flow to the same dst still crush the incumbent?
#
# incumbent from 44G to ~2G, and after it left the incumbent recovered
# only to 12.5G in 5 s. That observation was filed as a control-domain
# leftover and never retested. It matters now because it would silently
# corrupt any experiment with several senders aimed at one receiver -
# and it would look like the fairness algorithm failing rather than the
# executor.
#
# Sources are vf0 and vf3 deliberately: vf1 and vf2 hash to the SAME PCC
# flowtag for every dst, so a vf1+vf2 pair would share one budget and
# produce exactly this symptom for a completely different reason. vf0>vf0
# (0x74249a41) and vf3>vf0 (0xb61f934b) are distinct.
#
#   t=0    RDMA vf0 -> vf0 alone            expect ~29G (dst VM cap 30G)
#   t=25   RDMA vf3 -> vf0 joins            expect both ~14.6G
#   t=45   it leaves                        expect vf0 back to ~29G
#
# No TCP, unlike the original run - the question here is RDMA against
# RDMA, and a TCP flow-set in the same class tree only blurs the split.
# Each source is its own VF, so the host's per-VF RoCE counters separate
# the two flows without relying on the control plane's own numbers.
set -u
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd)
OUT="$DIR/results"; PT=$HOME/hyperfront/perftest-26015/ib_write_bw
D=70; JOIN=25; LEAVE=45; QN=4
# Optional arg: the dst VM's MaxRate. At 30G the two flows must share one
# cap; at 60G each can hold a ~29G ceiling and they never contend for the
# allocator's second layer. That splits "contention for a shared cap" from
# "two flows down one receive path" - the two live hypotheses.
DSTCAP=${1:-30000000000}; TAG=${2:-sd}; THETA=${3:-}
# theta=0 makes every active flow-set count as backlogged, which freezes
# the backlog criterion entirely: the split becomes a fixed 15/15 from
# policy alone. If the oscillation survives that, the criterion is not
# what is driving it and the executor is.
mkdir -p "$OUT"; cd "$REPO"
THETA="$THETA" python3 - "$DSTCAP" <<EOF
import json, sys
r = json.load(open("config/lab-registry.json"))
for vm, p in r["policy"]["vms"].items():
    p["max_rate_bps"] = 30000000000
r["policy"]["vms"]["sgpu02/vf0"]["max_rate_bps"] = int(sys.argv[1])
import os
if os.environ.get("THETA"):
    r["e_params"]["backlog_theta"] = float(os.environ["THETA"])
json.dump(r, open("/tmp/lr_sd.json", "w"), indent=2)
EOF
scp -q /tmp/lr_sd.json hpft-dpu:/tmp/lr.json;  ssh hpft-dpu  'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q /tmp/lr_sd.json hpft-dpu2:/tmp/lr.json; ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
restore_standing() {
  scp -q "$REPO/config/lab-registry.json" hpft-dpu:/tmp/lrr.json  2>/dev/null; ssh hpft-dpu  'sudo cp /tmp/lrr.json /opt/hpft/lab-registry.json' 2>/dev/null
  scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/tmp/lrr.json 2>/dev/null; ssh hpft-dpu2 'sudo cp /tmp/lrr.json /opt/hpft/lab-registry.json' 2>/dev/null
}
bash tools/lab-infra/deploy_check.sh >/dev/null || {
  bash tools/lab-infra/deploy_check.sh; echo "ABORT: lab not in a known state"; exit 1; }

ssh hpft-dpu 'bash /tmp/rp_service.sh start' >/dev/null 2>&1
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; true'
ssh hpft-dpu  'sudo systemctl stop hpft-txagent-e 2>/dev/null; true'
sleep 1
ssh hpft-dpu2 'sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' >/dev/null
sleep 3
ssh hpft-dpu  'sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' >/dev/null
sleep 4

ssh sgpu02 'pkill -f "ib_write_[b]"; true'
sleep 1
ssh sgpu02 "nohup $PT -d mlx5_6 -p 26420 -q $QN --report_gbits -D $D    >/tmp/${TAG}_srv0 2>&1 & true"
sleep 2

( while :; do
    echo "$(date +%s.%N) $(ethtool -S dpu1vf0 | awk '/tx_vport_rdma_unicast_bytes/{print $2}')" \
         "$(ethtool -S dpu1vf3 | awk '/tx_vport_rdma_unicast_bytes/{print $2}')"
    sleep 1
  done ) > "$OUT/${TAG}_txbytes.txt" &
SAMP=$!
trap 'kill $SAMP 2>/dev/null; restore_standing' EXIT

# Device-side probe. 0xdeb gives {flowtag,budget,level,remote_rx}, 0xdec
# gives {.,.,qp_count,cc_rate,.,flowtag}. Each response is printed just
# before the HPFT_SET line that carries the probe type and pair index, so
# the two can be paired unambiguously in a log the agent is also writing.
if [ "${PROBE:-0}" = "1" ]; then
  ssh hpft-dpu 'echo "PROBE_START $(date +%s.%N)" > /tmp/rp_probe_mark;
    ( for i in $(seq 1 300); do
        for p in 0 1 2 3; do
          echo "0xdeb $p" > /tmp/rp_fifo; echo "0xdec $p" > /tmp/rp_fifo
        done
        sleep 1
      done ) >/dev/null 2>&1 &' &
fi

t0=$(date +%s.%N); echo "$t0" > "$OUT/${TAG}_t0.txt"; : > "$OUT/${TAG}_events.txt"
$PT -d mlx5_6 -p 26420 -q $QN --report_gbits -D $D 10.1.0.2 > "$OUT/${TAG}_A.txt" 2>&1 &
A=$!

sleep $JOIN
echo "$(date +%s.%N) join" >> "$OUT/${TAG}_events.txt"
ssh sgpu02 "nohup $PT -d mlx5_6 -p 26421 -q $QN --report_gbits -D $((LEAVE-JOIN-2)) >/tmp/${TAG}_srv1 2>&1 & true"
sleep 2
$PT -d mlx5_9 -p 26421 -q $QN --report_gbits -D $((LEAVE-JOIN-2)) 10.1.0.2 > "$OUT/${TAG}_B.txt" 2>&1
echo "$(date +%s.%N) leave" >> "$OUT/${TAG}_events.txt"

wait $A; echo "A rc=$?" >> "$OUT/${TAG}_events.txt"
sleep 2
ssh hpft-dpu 'sudo cat /tmp/hpft_txagent_e.jsonl' > "$OUT/${TAG}_tx.jsonl"
if [ "${PROBE:-0}" = "1" ]; then
  ssh hpft-dpu 'pkill -f "0xdeb" 2>/dev/null; grep -a -A1 HPFT_RSP /tmp/pcc_rp.log | grep -aE "HPFT_RSP|ft=0xde" | tail -4000' > "$OUT/${TAG}_rp.txt" 2>/dev/null
fi
echo "sd-done"
