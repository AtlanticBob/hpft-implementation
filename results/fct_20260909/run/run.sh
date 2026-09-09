#!/usr/bin/env bash
# Flow completion time under a saturated receiver port.  run.sh <arm>
#
#   arm = hpft   the shaper is on (ledger + token pool), the default lab state
#   arm = cc     the tenant CC alone (the RDMA executor ignores its budgets,
#                the sender agents are stopped, the TCP rate table is cleared)
#
# The queueing bundle showed the shaper cuts the receiver port's queue by
# orders of magnitude.  That is the mechanism; this is the outcome a reader
# cares about: how long a SHORT FLOW takes to finish while the port is full.
#
# Load: q_v1, the 24 flow-sets of validation V1 (12 RDMA of 4 QPs, 12 Cubic of
# 4 connections, three senders into sgpu02), which saturates the 200 G port.
# Same load in both arms; only the shaper differs.
#
# Probe: ib_write_lat on a spare VF pair crossing the same egress queue, one
# size at a time, -H so every sample is dumped (that option works only in -n
# mode).  Its ping-pong is SYMMETRIC - both ends write the same size - so the
# number it reports, which is half the round trip, is the one-way completion
# time of a write of that size.  That is the FCT, and unlike the queueing
# figure it must NOT be doubled.
#
# Sizes span one packet to a megabyte, so the curve covers both ends: at 4 KB
# the time is almost all queueing, at 1 MB it is almost all serialisation.
set -u
ARM=${1:?arm: hpft | cc}
DIR=$(cd "$(dirname "$0")/.." && pwd); REPO=$(cd "$DIR/../.." && pwd); cd "$REPO"
OUT="$DIR/results/$ARM"; rm -rf "$OUT"; mkdir -p "$OUT"
PORT=swp37s0; PROBE_VF=4; SRC=sgpu01; DST=sgpu02
PT=$HOME/hyperfront/perftest-enhanced/ib_write_lat
NAME=fct_${ARM}
SIZES="4096 16384 65536 262144 1048576"
LOAD_S=130

reg() { python3 -c "import json;r=json.load(open('config/lab-registry.json'));print($1)"; }
dev_of() { reg "next(v['rdma_dev'] for v in r['vnics'] if v['host']=='$1' and v['netdev']=='dpu1vf$2')"; }
dpu_of() { reg "{n['host']:n['dpu'] for n in r['nodes']}['$1']"; }
sw_counters() {
  ssh -o BatchMode=yes sn5600 "nv show interface $PORT counters 2>/dev/null" 2>/dev/null | python3 -c "
import sys,re
t=sys.stdin.read()
def g(p):
    m=re.search(p+r'\s+n/a\s+(\d+)',t); return int(m.group(1)) if m else -1
print('ecn=%d drops=%d'%(g('ECN Marked Packets'), g('Queue Drops')))"
}

case "$ARM" in
  # the probe count per size is chosen so each size gets roughly ten seconds
  # of the load window in either arm; the two arms differ by two orders of
  # magnitude in latency, so what has to match between them is the window,
  # not the count.
  hpft) ENVV=(); declare -A N=( [4096]=200000 [16384]=120000 [65536]=50000 [262144]=15000 [1048576]=4000 ) ;;
  cc)   ENVV=(CC_ONLY=1); declare -A N=( [4096]=4000 [16384]=4000 [65536]=3000 [262144]=1200 [1048576]=250 ) ;;
  *) echo "arm must be hpft or cc"; exit 1 ;;
esac

echo "== $ARM: load q_v1 (24 flow-sets) ${LOAD_S}s, probe sizes $SIZES"
rm -rf /tmp/quick_$NAME
( env "${ENVV[@]}" bash validation/run/quick.sh $NAME $LOAD_S validation/scenarios/q_v1.spec > "$OUT/load.txt" 2>&1 ) &
LOAD=$!
while [ ! -s /tmp/quick_$NAME/t0.txt ]; do sleep 1; done
T0=$(cat /tmp/quick_$NAME/t0.txt)
A=$(sw_counters)
while [ "$(date +%s)" -lt $((T0+2)) ]; do sleep 0.5; done

: > "$OUT/windows.txt"
for S in $SIZES; do
  n=${N[$S]}
  echo "   size $S bytes, $n exchanges"
  P_START=$(date +%s)
  # the launch line must not contain the string a later pkill would match, or
  # the pkill kills the launching shell
  ssh -n -o BatchMode=yes $DST "setsid nohup $PT -d $(dev_of $DST $PROBE_VF) -s $S -m 1024 -p 27500 -n $n -H >/tmp/lat_srv.log 2>&1 </dev/null &"
  sleep 1
  timeout 90 $PT -d $(dev_of $SRC $PROBE_VF) -s $S -m 1024 -p 27500 -n $n -H $DST > "$OUT/probe_$S.txt" 2>&1
  ssh -n -o BatchMode=yes $DST "pkill -f 'ib_write_la[t]' 2>/dev/null; true" </dev/null
  # A probe that outlives the load measures an EMPTY port, and the numbers
  # then look wonderful: on the cc arm the 256 KB size once reported a median
  # of 33 us against a 99th percentile of 13.8 ms, because half its samples
  # were taken after the flows had stopped. Record the window so the distiller
  # can refuse the size rather than average the two regimes together.
  echo "$S start=$P_START end=$(date +%s) load_end=$((T0+LOAD_S))" >> "$OUT/windows.txt"
  sleep 1
done

wait $LOAD
B=$(sw_counters)
echo "$A" > "$OUT/sw_pre.txt"; echo "$B" > "$OUT/sw_post.txt"
cp /tmp/quick_$NAME/result.txt "$OUT/goodput.txt" 2>/dev/null
cp /tmp/quick_$NAME/arm.txt "$OUT/arm.txt" 2>/dev/null
# Restore what this run changed: the cc arm leaves the executor ignoring its
# budgets and the sender agents stopped.  Nothing here is self-restoring.
for h in $(awk '{print $2}' validation/scenarios/q_v1.spec | sort -u); do
  d=$(dpu_of "$h")
  ssh -n -o BatchMode=yes "$d" "timeout 5 bash -c 'echo \"0xccd 2\" > /tmp/rp_fifo'
    timeout 5 bash -c 'echo \"0xcce 0 22\" > /tmp/rp_fifo'
    timeout 5 bash -c 'echo \"0xcce 0 12\" > /tmp/rp_fifo'" </dev/null >/dev/null 2>&1
done
[ "$ARM" = cc ] && bash tools/lab-infra/roles.sh all >/dev/null 2>&1
echo "== $ARM done -> $OUT  (executor arm restored)"
