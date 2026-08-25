#!/usr/bin/env bash
# Join/leave step scenario: an incumbent sender runs four RDMA + four TCP
# pairs to the receiver for the whole run; a joiner brings the same eight
# at t+30 s and stops at t+60 s. Every incumbent flow-set therefore steps
# from C'/8 to C'/16 and back, and the joiner's flow-sets start cold on a
# saturated root - the two transients a response law is judged on.
#
# Mechanics are incast8_regression.sh's (lock, deploy check, scenario
# registry, link assertion, roles, per-(sender,vf) listeners); only the
# launch schedule differs. Artefacts go to results/regression/<tag>_*.
#
# usage: step_regression.sh <tag> [--receiver H] [--incumbent H] [--joiner H]
set -u
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd)
OUT="$REPO/results/regression"
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
TAG=$1; shift; QN=4; D=90; DJ=30; TJ=30
RECV=sgpu02; INC=sgpu01; JOIN=sgpu03
while [ $# -gt 0 ]; do
  case "$1" in
    --receiver) RECV=$2; shift 2 ;;
    --incumbent) INC=$2; shift 2 ;;
    --joiner) JOIN=$2; shift 2 ;;
    *) echo "unknown argument $1"; exit 1 ;;
  esac
done
mkdir -p "$OUT"; cd "$REPO"
LOCK=/tmp/hpft_run.lock
exec 9>"$LOCK"
flock -n 9 || { echo "ABORT: another run holds $LOCK"; exit 1; }
echo "$TAG $$" >&9
dpu_of() { python3 -c "
import json;print({n['host']:n['dpu'] for n in json.load(open('config/lab-registry.json'))['nodes']}['$1'])"; }
dev_of() { python3 -c "
import json,sys
r=json.load(open('config/lab-registry.json'))
print(next(v['rdma_dev'] for v in r['vnics'] if v['host']=='$1' and v['netdev']=='dpu1vf$2'))"; }
ip_of() { python3 -c "
import json;r=json.load(open('config/lab-registry.json'))
print(next(v['ip'] for v in r['vnics'] if v['host']=='$1' and v['netdev']=='dpu1vf$2'))"; }
on_host() { if [ "$1" = "$(hostname)" ]; then shift; bash -c "$*"; else h=$1; shift; ssh -o BatchMode=yes "$h" "$*"; fi; }

bash tools/lab-infra/deploy_check.sh >/dev/null || {
  bash tools/lab-infra/deploy_check.sh; echo "ABORT: lab not in a known state"; exit 1; }
DPUS=$(python3 -c "
import json;print(' '.join(n['dpu'] for n in json.load(open('config/lab-registry.json'))['nodes']))")
restore_standing() { for d in $DPUS; do scp -q "$REPO/config/lab-registry.json" "$d:/opt/hpft/" 2>/dev/null || true; done; }
trap restore_standing EXIT

python3 - <<'EOF'
import json
r = json.load(open("config/lab-registry.json"))
for vm, p in r["policy"]["vms"].items():
    p["max_rate_bps"] = 50000000000
    p["weight"] = 1
    p["class_weights"] = {"tcp": 1, "rdma": 1}
json.dump(r, open("/tmp/lr_step.json", "w"), indent=2)
EOF
for d in $DPUS; do
  scp -q /tmp/lr_step.json "$d:/tmp/lr.json"; ssh -o BatchMode=yes "$d" 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
done
WANT=$(python3 -c "import json;print(json.load(open('config/lab-registry.json'))['line_rate_bps']//1000000)")
for h in $RECV $INC $JOIN; do
  d=$(dpu_of $h); cur=$(ssh -o BatchMode=yes "$d" 'cat /sys/class/net/p1/speed' 2>/dev/null)
  [ "$cur" = "$WANT" ] || { echo "!! $d p1 is ${cur}Mb, expected ${WANT}Mb"; exit 1; }
done
bash tools/lab-infra/roles.sh set --receiver "$RECV" --senders "$INC,$JOIN" >/dev/null
sleep 4
on_host "$RECV" 'pkill -f "ib_write_[b]"; pkill -f "iperf3 -s"; true'
sleep 1
# perftest -D must match on both ends or the pair fails to negotiate and
# the flow-set silently never exists (ops_notes): the joiner's listeners
# carry the joiner's duration.
si=0
for s in $INC $JOIN; do
  SRV=""; SD=$D; [ $si -eq 1 ] && SD=$DJ
  for n in 0 1 2 3; do
    SRV+="setsid nohup $PT -d $(dev_of $RECV $n) -p $((26400+si*10+n)) -q $QN --report_gbits -D $SD >/tmp/st_${si}_$n 2>&1 </dev/null & "
    SRV+="setsid nohup iperf3 -s -p $((5301+si*10+n)) >/dev/null 2>&1 </dev/null & "
  done
  on_host "$RECV" "$SRV true"
  si=$((si+1))
done
sleep 2

launch() { # launch <sender> <si> <duration>
  local s=$1 si=$2 dur=$3 CLI="" i
  for i in 0 1 2 3; do
    CLI+="setsid nohup $PT -d $(dev_of $s $i) -p $((26400+si*10+i)) -q $QN --report_gbits -D $dur $(ip_of $RECV $i) >/tmp/stc_${si}_$i.log 2>&1 </dev/null & "
    CLI+="setsid nohup iperf3 -B $(ip_of $s $i)%dpu1vf$i -c $(ip_of $RECV $i) -p $((5301+si*10+i)) -P$QN -b 0 -t $((dur-2)) -J >/tmp/stt_${si}_$i.log 2>&1 </dev/null & "
  done
  on_host "$s" "$CLI true"
}
t0=$(date +%s.%N); echo "$t0" > "$OUT/${TAG}_t0.txt"
launch $INC 0 $D
sleep $TJ
echo "$(date +%s.%N)" > "$OUT/${TAG}_tjoin.txt"
launch $JOIN 1 $DJ
sleep $((D-TJ-4))
scp -q "$(dpu_of $RECV):/tmp/hpft_rxagent_e.jsonl" "$OUT/${TAG}_rx.jsonl"
for s in $INC $JOIN; do
  scp -q "$(dpu_of $s):/tmp/hpft_txagent_e.jsonl" "$OUT/${TAG}_tx_${s}.jsonl" 2>/dev/null || true
done
wait 2>/dev/null
on_host "$RECV" 'pkill -f "ib_write_[b]"; pkill -f "iperf3 -s"; true'
echo "${TAG}-step-done  receiver=$RECV incumbent=$INC joiner=$JOIN"
