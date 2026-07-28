#!/usr/bin/env bash
# 20x MaxRate down-step with BOTH §4.3 layers live.
#
# The §5.1 transition clause spreads an external step over the software
# tree so the controlled transport does not read it as a fault, and rests
# its "this does not weaken the selling principle" argument on layer one
# enforcing the new allowance immediately. Until today layer one was not
# wired, so that half was never exercised: the earlier 3/3 survival was
# measured with the software limiter alone.
#
# Wiring it raises a question the earlier run could not ask. The hardware
# cap does NOT descend gradually - it cannot, that is the point of it - so
# a 20x policy cut now reaches the NIC as a single instantaneous throttle.
# If an abrupt hardware step is itself enough to kill an RC connection,
# then the clause's premise defeats the clause's purpose. M5d only ever
# stepped this primitive by <=4x under load (10G/5G/20G), so the answer is
# not on record.
#
# One RDMA pair alone on the root, so the VM cap is the only thing binding
# and the step is unambiguous:
#   t=0    vf0 -> vf0, 4 QPs, MaxRate 30G
#   t=20   registry pushed with MaxRate 1.5G   (20x down)
#   t=45   registry pushed back to 30G          (20x up)
# Survival = ib_write_bw finishing without a completion error, which for
# RC is terminal - a killed QP never comes back.
# args: trial
set -u
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd)
OUT="$DIR/results"; PT=$HOME/hyperfront/perftest-26015/ib_write_bw
TRIAL=$1; QN=4; D=70; DOWN_AT=20; UP_AT=45; LOW=1500000000; HIGH=30000000000
mkdir -p "$OUT"; cd "$REPO"
bash tools/lab-infra/deploy_check.sh >/dev/null || {
  bash tools/lab-infra/deploy_check.sh; echo "ABORT: lab not in a known state"; exit 1; }

restore_standing() {
  scp -q "$REPO/config/lab-registry.json" hpft-dpu:/tmp/lr_restore.json 2>/dev/null || true
  ssh hpft-dpu 'sudo cp /tmp/lr_restore.json /opt/hpft/lab-registry.json' 2>/dev/null || true
  scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/tmp/lr_restore.json 2>/dev/null || true
  ssh hpft-dpu2 'sudo cp /tmp/lr_restore.json /opt/hpft/lab-registry.json' 2>/dev/null || true
}
trap restore_standing EXIT

mk() { # mk <vf0 max_rate_bps> <outfile>
  python3 - "$1" "$2" <<'EOF'
import json, sys
cap = int(sys.argv[1])
r = json.load(open("config/lab-registry.json"))
for vm, p in r["policy"]["vms"].items():
    p["max_rate_bps"] = 30000000000; p["weight"] = 1
    p["class_weights"] = {"tcp": 1, "rdma": 1}
r["policy"]["vms"]["sgpu01/vf0"]["max_rate_bps"] = cap
json.dump(r, open(sys.argv[2], "w"), indent=2)
EOF
}
push() { # push <file>  -- one write, both DPUs; mtime change is the trigger
  scp -q "$1" hpft-dpu:/tmp/lr.json;  ssh hpft-dpu  'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
  scp -q "$1" hpft-dpu2:/tmp/lr.json; ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
}

mk $HIGH /tmp/lr_high.json
mk $LOW  /tmp/lr_low.json
push /tmp/lr_high.json

ssh hpft-dpu 'bash /tmp/rp_service.sh start' >/dev/null 2>&1
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; true'
ssh hpft-dpu  'sudo systemctl stop hpft-txagent-e 2>/dev/null; true'
sleep 1
ssh hpft-dpu2 'sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' >/dev/null
sleep 3
ssh hpft-dpu  'sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' >/dev/null
sleep 4

bash tools/lab-infra/flow_preflight.sh >/dev/null 2>&1 || true
ssh sgpu02 'pkill -f "ib_write_[b]"; true'
sleep 1
ssh sgpu02 "nohup $PT -d mlx5_6 -p 26400 -q $QN --report_gbits -D $D >/tmp/ds_srv 2>&1 & true"
sleep 2

# Independent arbiter. perftest's own BW average came back at 7.27G on
# trial 1 while both the control plane and the receiver's vport counter
# read ~29G, and its "BW peak 0.00" says the report path is degraded in
# this mode. The host NIC's own byte counter belongs to neither party, so
# it settles which number describes the wire.
# RoCE writes never touch the netdev's software counters - the first
# attempt sampled statistics/tx_bytes and read a flat zero through a 30G
# transfer. The vport RDMA counter is the one the traffic actually moves.
( while :; do echo "$(date +%s.%N) $(ethtool -S dpu1vf0 | awk '/tx_vport_rdma_unicast_bytes/{print $2}')"; sleep 1; done ) \
    > "$OUT/ds${TRIAL}_txbytes.txt" &
SAMP=$!
trap 'kill $SAMP 2>/dev/null; restore_standing' EXIT

t0=$(date +%s.%N); echo "$t0" > "$OUT/ds${TRIAL}_t0.txt"
$PT -d mlx5_6 -p 26400 -q $QN --report_gbits -D $D 10.1.0.2 \
    > "$OUT/ds${TRIAL}_client.txt" 2>&1 &
CL=$!

sleep $DOWN_AT
echo "$(date +%s.%N) down" >> "$OUT/ds${TRIAL}_events.txt"
push /tmp/lr_low.json
sleep $((UP_AT - DOWN_AT))
echo "$(date +%s.%N) up" >> "$OUT/ds${TRIAL}_events.txt"
push /tmp/lr_high.json

wait $CL; echo "client rc=$?" >> "$OUT/ds${TRIAL}_events.txt"
sleep 2
ssh hpft-dpu 'sudo cat /tmp/hpft_txagent_e.jsonl' > "$OUT/ds${TRIAL}_tx.jsonl"
ssh hpft-dpu 'sudo journalctl -u hpft-txagent-e --no-pager -o short-precise --since "-90s" | grep -E "hw MaxRate|policy reloaded"' \
    > "$OUT/ds${TRIAL}_hw.log" 2>/dev/null
ssh sgpu02 'cat /tmp/ds_srv' > "$OUT/ds${TRIAL}_srv.txt" 2>/dev/null
echo "ds${TRIAL}-done"
