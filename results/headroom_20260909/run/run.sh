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
# receiver picks it up. It is put back to the design point at the end.
set -u
HPCT=${1:?headroom, in percent: 4 6 8 10 12}
DIR=$(cd "$(dirname "$0")/.." && pwd); REPO=$(cd "$DIR/../.." && pwd); cd "$REPO"
OUT="$DIR/results/h${HPCT}"; rm -rf "$OUT"; mkdir -p "$OUT"
PROBE_VF=4; SRC=sgpu01; DST=sgpu02
PT=$HOME/hyperfront/perftest-enhanced/ib_write_lat
NAME=hr_${HPCT}
SIZES="4096 65536"
LOAD_S=100
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

: > "$OUT/windows.txt"
for S in $SIZES; do
  n=${N[$S]}
  echo "   size $S bytes, $n exchanges"
  P_START=$(date +%s)
  ssh -n -o BatchMode=yes $DST "setsid nohup $PT -d $(dev_of $DST $PROBE_VF) -s $S -m 1024 -p 27500 -n $n -H >/tmp/lat_srv.log 2>&1 </dev/null &"
  sleep 1
  timeout 90 $PT -d $(dev_of $SRC $PROBE_VF) -s $S -m 1024 -p 27500 -n $n -H $DST > "$OUT/probe_$S.txt" 2>&1
  ssh -n -o BatchMode=yes $DST "pkill -f 'ib_write_la[t]' 2>/dev/null; true" </dev/null
  # a probe that outlives the load measures an EMPTY port and the numbers look
  # wonderful; the distiller refuses the size rather than mixing two regimes
  echo "$S start=$P_START end=$(date +%s) load_end=$((T0+LOAD_S))" >> "$OUT/windows.txt"
  sleep 1
done

wait $LOAD
cp /tmp/quick_$NAME/result.txt "$OUT/goodput.txt" 2>/dev/null
echo "== h $HPCT % done -> $OUT"
