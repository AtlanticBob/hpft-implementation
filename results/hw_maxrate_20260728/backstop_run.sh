#!/usr/bin/env bash
# Does layer one actually hold when layer two would allow more?
#
# That is the whole reason the hardware cap exists: the selling principle
# is supposed to survive the software being wrong. M5d showed this under
# the v1 stack (a VF hard-capped at 8G measured 7.72G while the software
# layer handed its share to the others). Two things make it worth redoing
# under v2. The response law is different - it now tracks a receiver-
# supplied target rather than searching - and a hardware cap below that
# target is exactly the case where the loop sees its own actuation fail:
# the law commands 30G, the wire delivers 10G, and nothing in the control
# plane is told why. A search law would read that as congestion and back
# off; a tracking law should sit still. Second, the escape tripwire must
# NOT fire here (r stays below pace, so it should stay quiet), and the
# demand estimate must not latch the flow low once the cap is released.
#
#   t=0    vf0 -> vf0, 4 QPs, policy MaxRate 30G, hardware 30G
#   t=15   hardware cap alone set to 10G  (software still says 30G)
#   t=40   hardware cap restored to 30G
#
# The cap is set by hand rather than through the policy on purpose: the
# point is to make the two layers DISAGREE, which the normal path cannot
# do. Note the side effect - a hand-set cap survives until the next
# policy reload, since the agent only syncs on startup and on mtime.
set -u
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd)
OUT="$DIR/results"; PT=$HOME/hyperfront/perftest-26015/ib_write_bw
QN=4; D=60; LOW_AT=15; HIGH_AT=40
LEAF=pci/0000:03:00.1/262145      # pf1vf0, from devlink port show
mkdir -p "$OUT"; cd "$REPO"
bash tools/lab-infra/deploy_check.sh >/dev/null || {
  bash tools/lab-infra/deploy_check.sh; echo "ABORT: lab not in a known state"; exit 1; }

hwset() { ssh hpft-dpu "sudo devlink port function rate set $LEAF tx_max $1"; }
restore() {
  kill $SAMP 2>/dev/null
  hwset 30000000000 2>/dev/null || true
}
trap restore EXIT

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
ssh sgpu02 "nohup $PT -d mlx5_6 -p 26400 -q $QN --report_gbits -D $D >/tmp/bs_srv 2>&1 & true"
sleep 2

( while :; do echo "$(date +%s.%N) $(ethtool -S dpu1vf0 | awk '/tx_vport_rdma_unicast_bytes/{print $2}')"; sleep 1; done ) \
    > "$OUT/bs_txbytes.txt" &
SAMP=$!

t0=$(date +%s.%N); echo "$t0" > "$OUT/bs_t0.txt"
: > "$OUT/bs_events.txt"
$PT -d mlx5_6 -p 26400 -q $QN --report_gbits -D $D 10.1.0.2 \
    > "$OUT/bs_client.txt" 2>&1 &
CL=$!

sleep $LOW_AT
echo "$(date +%s.%N) hw-10G" >> "$OUT/bs_events.txt"
hwset 10000000000
sleep $((HIGH_AT - LOW_AT))
echo "$(date +%s.%N) hw-30G" >> "$OUT/bs_events.txt"
hwset 30000000000

wait $CL; echo "client rc=$?" >> "$OUT/bs_events.txt"
sleep 2
ssh hpft-dpu 'sudo cat /tmp/hpft_txagent_e.jsonl' > "$OUT/bs_tx.jsonl"
ssh hpft-dpu 'sudo journalctl -u hpft-txagent-e --no-pager -o cat --since "-80s" | grep -iE "escape|MaxRate"' \
    > "$OUT/bs_agent.log" 2>/dev/null
echo "bs-done"
