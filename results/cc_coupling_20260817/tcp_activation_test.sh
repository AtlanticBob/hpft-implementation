#!/usr/bin/env bash
# Does the TCP observer's ACTIVE branch work? In incast8 every TCP flow-set
# is held by the shaper the whole time, so its cuts are correctly not
# counted and d never leaves 1 - which leaves the branch that reads a cut
# unexercised.
#
# Here one TCP flow-set runs alone with a share well above what the path
# can deliver. The EDT stamp is then never in the future, the flow is never
# pace-limited, and the kernel CC's cuts must be counted: d has to fall,
# and once d*share drops to what the flow can actually send the shaper
# becomes the constraint again and d has to recover. That is the whole loop.
#
# usage: tcp_activation_test.sh <tag> [share_gbps] [seconds]
set -u
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd)
OUT="$DIR/results"; mkdir -p "$OUT"
TAG=$1; SHARE=${2:-90}; D=${3:-40}
cd "$REPO"
bash tools/lab-infra/deploy_check.sh >/dev/null || { echo "ABORT: lab not in a known state"; exit 1; }
restore_standing() {
  scp -q "$REPO/config/lab-registry.json" hpft-dpu:/opt/hpft/  2>/dev/null || true
  scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/opt/hpft/ 2>/dev/null || true
}
trap restore_standing EXIT

python3 - "$SHARE" <<'PY'
import json, sys
g = int(sys.argv[1]) * 1000000000
r = json.load(open("config/lab-registry.json"))
for vm, p in r["policy"]["vms"].items():
    p["max_rate_bps"] = g
    p["weight"] = 1
    p["class_weights"] = {"tcp": 1, "rdma": 1}
json.dump(r, open("/tmp/lr_act.json", "w"), indent=2)
PY
scp -q /tmp/lr_act.json hpft-dpu:/tmp/lr.json;  ssh hpft-dpu  'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q /tmp/lr_act.json hpft-dpu2:/tmp/lr.json; ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
ssh hpft-dpu2 'sudo systemctl restart hpft-rxagent-e'; sleep 3
ssh hpft-dpu  'sudo systemctl restart hpft-txagent-e'; sleep 4

# the servers are started from a file, not an inline remote string: the
# quoting needed to nest a shell loop inside ssh has silently eaten the
# loop before, and a missing server reads as "the CC never cut"
cat > /tmp/iperf_srv.sh <<'EOS'
for p in 5301 5302 5303 5304; do
  ss -ltn | grep -q ":$p " || (setsid nohup iperf3 -s -p $p >/dev/null 2>&1 </dev/null &)
done
sleep 1; ss -ltn | grep -cE ":530[1-4] "
EOS
scp -q /tmp/iperf_srv.sh sgpu02:/tmp/
[ "$(ssh sgpu02 'bash /tmp/iperf_srv.sh')" = "4" ] || { echo "ABORT: iperf servers not up"; exit 1; }
t0=$(date +%s.%N); echo "$t0" > "$OUT/${TAG}_t0.txt"
iperf3 -B "10.1.0.1%dpu1vf0" -c 10.1.0.2 -p 5301 -P4 -b 0 -t $D -J > "$OUT/${TAG}_iperf.json" 2>&1 &
bash "$DIR/dump_tcp_d.sh" "$TAG" $((D / 2))
wait 2>/dev/null
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$OUT/${TAG}_rx.jsonl" 2>/dev/null || true
echo "${TAG}-activation-done"
