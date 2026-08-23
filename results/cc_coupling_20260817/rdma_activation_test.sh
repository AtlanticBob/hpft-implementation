#!/usr/bin/env bash
# Does the RDMA observer's ACTIVE branch work? In incast8 every TCP flow-set
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

PT=$HOME/hyperfront/perftest-26015/ib_write_bw
cat > /tmp/pt_srv.sh <<EOS
pkill -f 'ib_[a-z]*_bw'; sleep 0.5
setsid nohup $PT -F -d mlx5_6 -p 26600 -q 1 --report_gbits -D $D >/tmp/act_srv 2>&1 </dev/null &
sleep 1; pgrep -c ib_write_bw
EOS
scp -q /tmp/pt_srv.sh sgpu02:/tmp/
[ "$(ssh sgpu02 'bash /tmp/pt_srv.sh')" = "1" ] || { echo "ABORT: perftest server not up"; exit 1; }
t0=$(date +%s.%N); echo "$t0" > "$OUT/${TAG}_t0.txt"
ssh hpft-dpu 'wc -l < /tmp/pcc_rp.log > /tmp/act_mark;
  ( for i in $(seq 1 45); do echo "0xded 0" > /tmp/rp_fifo; sleep 1; done ) >/dev/null 2>&1 &' &
$PT -F -d mlx5_6 -p 26600 -q 1 --report_gbits -D $D 10.1.0.2 > "$OUT/${TAG}_pt.log" 2>&1
sleep 2
# two calls on purpose: a pkill whose pattern also matches the shell doing
# the capture kills that shell, and the capture silently produces nothing
ssh hpft-dpu 'pkill -f "seq 1 45" 2>/dev/null; true' >/dev/null 2>&1
ssh hpft-dpu 'M=$(cat /tmp/act_mark); tail -n +$((M+1)) /tmp/pcc_rp.log | grep -aE "HPFT_RSP|ft=0xde"' > "$OUT/${TAG}_rp.txt" 2>/dev/null
ssh sgpu02 "pkill -f 'ib_[a-z]*_bw'; true"
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$OUT/${TAG}_rx.jsonl" 2>/dev/null || true
echo "${TAG}-activation-done"
