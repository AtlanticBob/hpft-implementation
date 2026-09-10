#!/usr/bin/env bash
# The headroom knob: what does giving away capacity buy?  run.sh <h percent>
#
# The ledger hands out (1-h)C, so h is the only place in the design where
# latency is traded for throughput directly. Everything else about the two is
# a consequence. This sweep measures both ends of that trade at once, under
# the load the receiver port actually saturates at:
#   throughput  the application goodput of all 24 flow-sets
#   latency     the completion time of a short flow crossing the same queue,
#               at 4 KB (all queueing) and 64 KB (queueing plus a little
#               serialisation)
#
# h lives in the standing registry (e_params.headroom), so the sweep edits it,
# pushes it to the DPUs and restarts the agents, which is the only way the
# receiver picks it up.
#
# This copy re-runs the sweep at the lab's MTU of 4096 (the 2026-09-09 original
# ran at 1024). The MTU is the reason to re-run rather than reuse: h has to
# cover the gap between what the ledger counts (inner wire bytes) and what the
# port carries (outer, VxLAN-encapsulated bytes), and that gap shrinks from
# about 5% of the frame at MTU 1024 to about 1.2% at 4096. Whatever is left of
# h above that gap is the slack that keeps the switch queue empty, so the
# design point moves and has to be measured again, not scaled.
set -u
HPCT=${1:?headroom, in percent: 3 4 5 6 8}
DIR=$(cd "$(dirname "$0")/.." && pwd); REPO=$(cd "$DIR/../.." && pwd); cd "$REPO"
OUT="$DIR/results/h${HPCT}"; rm -rf "$OUT"; mkdir -p "$OUT"
PROBE_VF=4; SRC=sgpu01; DST=sgpu02
PT=$HOME/hyperfront/perftest-enhanced/ib_write_lat
NAME=hr_${HPCT}
SIZES="4096 65536"
LOAD_S=60
declare -A N=( [4096]=100000 [65536]=25000 )

reg() { python3 -c "import json;r=json.load(open('config/lab-registry.json'));print($1)"; }
dev_of() { reg "next(v['rdma_dev'] for v in r['vnics'] if v['host']=='$1' and v['netdev']=='dpu1vf$2')"; }

set_h() {   # $1 = headroom as a fraction, e.g. 0.08
  python3 - "$1" <<'PY'
import json, re, sys
p = "config/lab-registry.json"
s = open(p).read()
s = re.sub(r'"headroom": [0-9.]+', '"headroom": %s' % sys.argv[1], s, count=1)
json.loads(s)
open(p, "w").write(s)
PY
  bash tools/lab-infra/deploy_check.sh --deploy >/dev/null 2>&1
  bash tools/lab-infra/roles.sh all >/dev/null 2>&1
}


# The switch's own view of the same window: how many frames the bottleneck port
# marked, and whether it ever had to drop. This is the evidence for what h is
# actually buying - the probe says how long a short flow waited, these say
# whether the ledger kept the port below the point where it queues at all.
snap_switch() { timeout 60 ssh -o BatchMode=yes sn5600 "nv show interface swp37s0 counters qos -o json 2>/dev/null" 2>/dev/null | python3 -c "
import json,sys
try: d=json.load(sys.stdin).get('egress-queue-stats',{})
except Exception: sys.exit(0)
print('# tc tx-frames tx-bytes tx-uc-buffer-discards ecn-marked-frames')
for tc in sorted(d, key=int):
    v=d[tc]
    print(tc, v.get('tx-frames',0), v.get('tx-bytes',0), v.get('tx-uc-buffer-discards',0), v.get('ecn-marked-frames',0))
"; }
H=$(python3 -c "print($HPCT/100)")
echo "== headroom $HPCT % (h = $H)"
set_h "$H"
got=$(reg "r['e_params']['headroom']")
[ "$got" = "$H" ] || { echo "ABORT: registry says headroom=$got, wanted $H"; exit 1; }
echo "headroom=$H" > "$OUT/arm.txt"
# what the RECEIVER is actually running on, not what this script asked for
ssh -o BatchMode=yes hpft-dpu2 "grep -o '\"headroom\": [0-9.]*' /opt/hpft/lab-registry.json" >> "$OUT/arm.txt" 2>/dev/null

rm -rf /tmp/quick_$NAME
( bash validation/run/quick.sh $NAME $LOAD_S validation/scenarios/q_v1.spec > "$OUT/load.txt" 2>&1 ) &
LOAD=$!
while [ ! -s /tmp/quick_$NAME/t0.txt ]; do sleep 1; done
T0=$(cat /tmp/quick_$NAME/t0.txt)
while [ "$(date +%s)" -lt $((T0+2)) ]; do sleep 0.5; done
snap_switch > "$OUT/switch_pre.txt"

: > "$OUT/windows.txt"
for S in $SIZES; do
  n=${N[$S]}
  echo "   size $S bytes, $n exchanges"
  P_START=$(date +%s)
  ssh -n -o BatchMode=yes $DST "setsid nohup $PT -d $(dev_of $DST $PROBE_VF) -s $S -m 4096 -p 27500 -n $n -H >/tmp/lat_srv.log 2>&1 </dev/null &"
  sleep 1
  timeout 90 $PT -d $(dev_of $SRC $PROBE_VF) -s $S -m 4096 -p 27500 -n $n -H $DST > "$OUT/probe_$S.txt" 2>&1
  ssh -n -o BatchMode=yes $DST "pkill -f 'ib_write_la[t]' 2>/dev/null; true" </dev/null
  # a probe that outlives the load measures an EMPTY port and the numbers look
  # wonderful; the distiller refuses the size rather than mixing two regimes
  echo "$S start=$P_START end=$(date +%s) load_end=$((T0+LOAD_S))" >> "$OUT/windows.txt"
  sleep 1
done

snap_switch > "$OUT/switch_post.txt"
wait $LOAD
cp /tmp/quick_$NAME/result.txt "$OUT/goodput.txt" 2>/dev/null
echo "== h $HPCT % done -> $OUT"
