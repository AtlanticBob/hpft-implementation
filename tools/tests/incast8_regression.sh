#!/usr/bin/env bash
# Standing regression: four RDMA pairs and four TCP pairs, straight
# vf_n -> vf_n, all eight contending for the receiver's 97G root.
#
# Fairness lives in the receiver's water-filling, so this is the check
# that says the whole loop still divides a saturated root evenly. Expect
# each flow-set near 12G and Jain at 1.000; the analyzer prints both.
#
# args: tag
set -u
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd)
OUT="$DIR/../../results/regression"
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
TAG=$1; QN=4; D=90
mkdir -p "$OUT"; cd "$REPO"
# repo == DPUs and both agents healthy, or the run is
# meaningless (a crashed sender leaves RDMA unpaced while
# every other layer still reports fine)
bash tools/lab-infra/deploy_check.sh >/dev/null || {
  bash tools/lab-infra/deploy_check.sh; echo "ABORT: lab not in a known state"; exit 1; }

# Restore the standing configuration on the way out, whatever happened.
# Runners push their own scenario registry, so without this the lab keeps
# whatever the last experiment wanted and the next one silently inherits
# it - which is the same class of mistake as a stale deployed agent, just
# quieter.
restore_standing() {
  scp -q "$REPO/config/lab-registry.json" hpft-dpu:/opt/hpft/  2>/dev/null || true
  scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/opt/hpft/ 2>/dev/null || true
}
trap restore_standing EXIT


python3 - <<'EOF'
import json
r = json.load(open("config/lab-registry.json"))
for vm, p in r["policy"]["vms"].items():
    p["max_rate_bps"] = 50000000000
    p["weight"] = 1
    p["class_weights"] = {"tcp": 1, "rdma": 1}
json.dump(r, open("/tmp/lr_i8.json", "w"), indent=2)
EOF
scp -q /tmp/lr_i8.json hpft-dpu:/tmp/lr.json;  ssh hpft-dpu  'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q /tmp/lr_i8.json hpft-dpu2:/tmp/lr.json; ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'

cur=$(ssh hpft-dpu2 "ethtool p1|awk '/Speed/{print \$2}'")
[ "$cur" = "100000Mb/s" ] || { echo "!! p1 is $cur"; exit 1; }
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
SRV=""; for n in 0 1 2 3; do SRV+="nohup $PT -d mlx5_$((6+n)) -p $((26400+n)) -q $QN --report_gbits -D $D >/tmp/i8_$n 2>&1 & "; done
ssh sgpu02 "$SRV true"
sleep 2
t0=$(date +%s.%N); echo "$t0" > "$OUT/${TAG}_t0.txt"
for n in 0 1 2 3; do $PT -d mlx5_$((6+n)) -p $((26400+n)) -q $QN --report_gbits -D $D 10.1.$n.2 >/dev/null 2>&1 & done
for n in 0 1 2 3; do iperf3 -B "10.1.$n.1%dpu1vf$n" -c 10.1.$n.2 -p $((5301+n)) -P$QN -b 0 -t $((D-5)) -J >/dev/null 2>&1 & done
sleep $((D-6))
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$OUT/${TAG}_rx.jsonl"
scp -q hpft-dpu:/tmp/hpft_txagent_e.jsonl  "$OUT/${TAG}_tx.jsonl"
wait 2>/dev/null
ssh sgpu02 'pkill -f "ib_write_[b]"; true'
echo "${TAG}-incast8-done"
