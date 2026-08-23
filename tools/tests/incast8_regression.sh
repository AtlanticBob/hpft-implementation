#!/usr/bin/env bash
# Standing regression: every sender runs four RDMA pairs and four TCP pairs,
# straight vf_n -> vf_n, all of them contending for the receiver's root.
#
# Fairness lives in the receiver's water-filling, so this is the check that
# says the whole loop still divides a saturated root evenly. The analyzer
# prints the per-flow-set rates and Jain; expect Jain at 1.000 and each
# flow-set near C' / (8 * senders).
#
# usage: incast8_regression.sh <tag> [--receiver H] [--senders h1,h2,...]
#        default receiver/senders come from the registry, which keeps the
#        historical single-sender run reproducible; --senders is how the
#        four-node incast is asked for.
set -u
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd)
OUT="$REPO/results/regression"
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
TAG=$1; shift; QN=4; D=90
RECV=""; SENDERS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --receiver) RECV=$2; shift 2 ;;
    --senders)  SENDERS=$(echo "$2" | tr ',' ' '); shift 2 ;;
    *) echo "unknown argument $1"; exit 1 ;;
  esac
done
mkdir -p "$OUT"; cd "$REPO"
RECV=${RECV:-$(python3 -c "import json;print(json.load(open('config/lab-registry.json'))['receiver_host'])")}
SENDERS=${SENDERS:-$(python3 -c "import json;print(json.load(open('config/lab-registry.json'))['sender_host'])")}
RECV_DPU=$(python3 -c "
import json;print({n['host']:n['dpu'] for n in json.load(open('config/lab-registry.json'))['nodes']}['$RECV'])")
# rdma device index of vf_n differs per host (how many other cards probed
# first), so it is read from the registry rather than assumed to be 6+n.
dev_of() { python3 -c "
import json,sys
r=json.load(open('config/lab-registry.json'))
print(next(v['rdma_dev'] for v in r['vnics'] if v['host']=='$1' and v['netdev']=='dpu1vf$2'))"; }
on_host() { if [ "$1" = "$(hostname)" ]; then shift; bash -c "$*"; else h=$1; shift; ssh -o BatchMode=yes "$h" "$*"; fi; }

# repo == DPUs and the enabled agents healthy, or the run is meaningless (a
# crashed sender leaves RDMA unpaced while every other layer still reports
# fine)
bash tools/lab-infra/deploy_check.sh >/dev/null || {
  bash tools/lab-infra/deploy_check.sh; echo "ABORT: lab not in a known state"; exit 1; }

# Restore the standing configuration on the way out, whatever happened.
# Runners push their own scenario registry, so without this the lab keeps
# whatever the last experiment wanted and the next one silently inherits
# it - which is the same class of mistake as a stale deployed agent, just
# quieter.
DPUS=$(python3 -c "
import json;print(' '.join(n['dpu'] for n in json.load(open('config/lab-registry.json'))['nodes']))")
restore_standing() { for d in $DPUS; do scp -q "$REPO/config/lab-registry.json" "$d:/opt/hpft/" 2>/dev/null || true; done; }
trap restore_standing EXIT

# Scenario registry: equal weights, one class ratio, and a VM cap high enough
# that the ROOT is what binds - the whole point of the check.
python3 - <<'EOF'
import json
r = json.load(open("config/lab-registry.json"))
for vm, p in r["policy"]["vms"].items():
    p["max_rate_bps"] = 50000000000
    p["weight"] = 1
    p["class_weights"] = {"tcp": 1, "rdma": 1}
json.dump(r, open("/tmp/lr_i8.json", "w"), indent=2)
EOF
for d in $DPUS; do
  scp -q /tmp/lr_i8.json "$d:/tmp/lr.json"; ssh -o BatchMode=yes "$d" 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
done

# Link speed is a premise of the expected numbers, so it is asserted, not
# hoped for. It comes from the registry so this file does not carry a second
# opinion about how fast the lab is.
WANT=$(python3 -c "import json;print(json.load(open('config/lab-registry.json'))['line_rate_bps']//1000000)")
for h in $RECV $SENDERS; do
  d=$(python3 -c "
import json;print({n['host']:n['dpu'] for n in json.load(open('config/lab-registry.json'))['nodes']}['$h'])")
  cur=$(ssh -o BatchMode=yes "$d" 'cat /sys/class/net/p1/speed' 2>/dev/null)
  [ "$cur" = "$WANT" ] || { echo "!! $d p1 is ${cur}Mb, expected ${WANT}Mb (tools/reboot_recover.sh asserts it)"; exit 1; }
done

# roles.sh restarts each sender's RP before its agent; an executor carried
# over from a previous run keeps taking budgets and stops pacing.
# roles.sh truncates the agent logs BEFORE it starts each agent. Removing
# them again afterwards leaves every agent writing to an unlinked inode, so
# the run produces no jsonl at all and the analyzer has nothing to read.
bash tools/lab-infra/roles.sh set --receiver "$RECV" --senders "$(echo $SENDERS | tr ' ' ',')" >/dev/null
sleep 4

on_host "$RECV" 'pkill -f "ib_write_[b]"; true'
sleep 1
# one RDMA server and one iperf3 server per (sender, vf): flow-sets are
# per-(src,dst,class), so senders must not share a listener.
si=0
for s in $SENDERS; do
  SRV=""
  for n in 0 1 2 3; do
    SRV+="setsid nohup $PT -d $(dev_of $RECV $n) -p $((26400+si*10+n)) -q $QN --report_gbits -D $D >/tmp/i8_${si}_$n 2>&1 </dev/null & "
    SRV+="setsid nohup iperf3 -s -p $((5301+si*10+n)) -1 >/dev/null 2>&1 </dev/null & "
  done
  on_host "$RECV" "$SRV true"
  si=$((si+1))
done
sleep 2

t0=$(date +%s.%N); echo "$t0" > "$OUT/${TAG}_t0.txt"
RIP=$(python3 -c "
import json;r=json.load(open('config/lab-registry.json'))
print(' '.join(next(v['ip'] for v in r['vnics'] if v['host']=='$RECV' and v['netdev']=='dpu1vf%d'%i) for i in range(4)))")
# Remote launches need setsid+nohup and a detached stdin. A trailing `&`
# inside an ssh command is not enough: the session closes as soon as the
# command returns and takes the job with it, which showed up as a run that
# reported cleanly with a third of its flow-sets simply absent.
si=0
for s in $SENDERS; do
  i=0
  for ip in $RIP; do
    sip=$(python3 -c "
import json;r=json.load(open('config/lab-registry.json'))
print(next(v['ip'] for v in r['vnics'] if v['host']=='$s' and v['netdev']=='dpu1vf$i'))")
    on_host "$s" "setsid nohup $PT -d $(dev_of $s $i) -p $((26400+si*10+i)) -q $QN --report_gbits -D $D $ip >/tmp/i8c_${si}_$i.log 2>&1 </dev/null &"
    on_host "$s" "setsid nohup iperf3 -B $sip%dpu1vf$i -c $ip -p $((5301+si*10+i)) -P$QN -b 0 -t $((D-5)) -J >/tmp/i8t_${si}_$i.log 2>&1 </dev/null &"
    i=$((i+1))
  done
  si=$((si+1))
done
sleep $((D-4))
scp -q "$RECV_DPU:/tmp/hpft_rxagent_e.jsonl" "$OUT/${TAG}_rx.jsonl"
si=0
for s in $SENDERS; do
  d=$(python3 -c "
import json;print({n['host']:n['dpu'] for n in json.load(open('config/lab-registry.json'))['nodes']}['$s'])")
  scp -q "$d:/tmp/hpft_txagent_e.jsonl" "$OUT/${TAG}_tx_${s}.jsonl" 2>/dev/null || true
  si=$((si+1))
done
wait 2>/dev/null
on_host "$RECV" 'pkill -f "ib_write_[b]"; pkill -f "iperf3 -s"; true'
echo "${TAG}-incast8-done  receiver=$RECV senders=$SENDERS"
