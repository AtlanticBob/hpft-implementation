#!/usr/bin/env bash
# Does HyperFront reduce queueing at the switch?  run.sh <arm>
#
#   arm = hpft   the shaper is on (ledger + token bucket), the default lab state
#   arm = cc     the tenant CC alone (HPFT_CC_ONLY: the RDMA executor ignores
#                its budgets, the sender agents are stopped, the TCP rate table
#                is cleared) - the same flows with nothing shaping them
#
# Load: q_v1, the 24 flow-sets of validation V1 (12 RDMA of 4 QPs, 12 Cubic of
# 4 connections, three senders into sgpu02), which saturates the 200 G receiver
# port.  Same load in both arms; only the shaper differs.
#
# What is measured at the receiver port swp37s0 while the load runs:
#   - the switch's own egress-buffer occupancy histogram (bytes) and egress
#     latency histogram (ns), sampled by the ASIC every 1024 ns and written
#     once a second to /var/run/cumulus/histogram_stats_<n>.  This is the
#     direct measurement: how deep the queue is, and how long a packet waits.
#   - ECN-marked frames and queue drops over the run (nv counters).
#   - an application-visible probe: ib_write_lat, 2 B ping-pong on a spare VF
#     pair (sgpu01/vf4 -> sgpu02/vf4) crossing the same egress queue, run for
#     8 s inside the load window.
#   - per-flow-set goodput, so that a latency win is not just a throughput loss.
#
# The switch histograms must be enabled first (once, and turned off after):
#   nv set interface swp37s0 telemetry histogram egress-buffer traffic-class 0
#   nv set interface swp37s0 telemetry histogram latency traffic-class 0
#   nv set system telemetry histogram egress-buffer bin-min-boundary 4096
#   nv set system telemetry histogram egress-buffer histogram-size 4194304
#   nv set system telemetry histogram latency bin-min-boundary 2048
#   nv set system telemetry histogram latency histogram-size 163840
#   nv set system telemetry snapshot-interval 1 ; nv set system telemetry enable on
set -u
ARM=${1:?arm: hpft | cc}
DIR=$(cd "$(dirname "$0")/.." && pwd); REPO=$(cd "$DIR/../.." && pwd); cd "$REPO"
OUT="$DIR/results/$ARM"; mkdir -p "$OUT"
PORT=swp37s0; PROBE_VF=4; SRC=sgpu01; DST=sgpu02
PT=$HOME/hyperfront/perftest-enhanced/ib_write_lat
NAME=q_$ARM

reg() { python3 -c "import json;r=json.load(open('config/lab-registry.json'));print($1)"; }
dev_of() { reg "next(v['rdma_dev'] for v in r['vnics'] if v['host']=='$1' and v['netdev']=='dpu1vf$2')"; }
sw_counters() {
  ssh -o BatchMode=yes sn5600 "nv show interface $PORT counters 2>/dev/null" 2>/dev/null | python3 -c "
import sys,re
t=sys.stdin.read()
def g(p):
    m=re.search(p+r'\s+n/a\s+(\d+)',t); return int(m.group(1)) if m else -1
print('ecn=%d drops=%d'%(g('ECN Marked Packets'), g('Queue Drops')))"
}

case "$ARM" in
  hpft) ENVV=() ;;
  cc)   ENVV=(CC_ONLY=1) ;;
  *) echo "arm must be hpft or cc"; exit 1 ;;
esac

echo "== $ARM: load q_v1 (24 flow-sets) for 12 s, probe 8 s, port $PORT"
rm -rf /tmp/quick_$NAME
T_START=$(date +%s)
( env "${ENVV[@]}" bash validation/run/quick.sh $NAME 12 validation/scenarios/q_v1.spec > "$OUT/load.txt" 2>&1 ) &
LOAD=$!

# The probe: -D is SYMMETRIC in perftest, so both ends run -D 8; the server is
# started one second before the client.  The launch line must not contain the
# string a later pkill would match, or the pkill kills the launching shell -
# the same trap as ib_write_bw in the validation runner, so cleanup is a
# separate command and uses a bracketed pattern.
while [ ! -s /tmp/quick_$NAME/t0.txt ]; do sleep 1; done
T0=$(cat /tmp/quick_$NAME/t0.txt)
A=$(sw_counters)
# the histogram window starts with the flows, not when quick.sh began arming:
# the arming takes tens of seconds and its idle samples would all land in the
# empty-queue bin and dilute the distribution
while [ "$(date +%s)" -lt $((T0+1)) ]; do sleep 0.5; done
SW_A=$(ssh -o BatchMode=yes sn5600 'date +%s' 2>/dev/null | tr -dc 0-9)
ssh -n -o BatchMode=yes $DST "setsid nohup $PT -d $(dev_of $DST $PROBE_VF) -s 2 -m 1024 -p 27500 -D 8 >/tmp/lat_srv.log 2>&1 </dev/null &"
while [ "$(date +%s)" -lt $((T0+2)) ]; do sleep 0.5; done
timeout 30 $PT -d $(dev_of $SRC $PROBE_VF) -s 2 -m 1024 -p 27500 -D 8 $DST > "$OUT/probe.txt" 2>&1
wait $LOAD
B=$(sw_counters); SW_B=$(ssh -o BatchMode=yes sn5600 'date +%s' 2>/dev/null | tr -dc 0-9)
T_END=$(date +%s)
ssh -n -o BatchMode=yes $DST "pkill -f 'ib_write_la[t]' 2>/dev/null; true" </dev/null

# The histogram snapshots inside the load window.  The switch keeps local time
# (HKT) while the hosts are on UTC, so the filtering is done ON THE SWITCH,
# where the snapshot's own datetime string and the window bounds share a clock.
ssh -o BatchMode=yes sn5600 "python3 - $SW_A $SW_B <<'PY'
import json,sys,glob,datetime
a,b=int(sys.argv[1]),int(sys.argv[2]); out=[]
for f in glob.glob('/var/run/cumulus/histogram_stats_*'):
    try: d=json.load(open(f))
    except Exception: continue
    t=d['timestamp_info']['start_datetime'][:19]
    e=datetime.datetime.strptime(t,'%Y-%m-%d %H:%M:%S').timestamp()
    if a<=e<=b: out.append(d)
print(json.dumps(out))
PY" 2>/dev/null > "$OUT/histogram_window.json"
echo "$A" > "$OUT/sw_pre.txt"; echo "$B" > "$OUT/sw_post.txt"
echo "t0=$T0 start=$T_START end=$T_END sw_a=$SW_A sw_b=$SW_B arm=$ARM" > "$OUT/window.txt"
cp /tmp/quick_$NAME/result.txt "$OUT/goodput.txt" 2>/dev/null
cp /tmp/quick_$NAME/arm.txt "$OUT/arm.txt" 2>/dev/null
echo "== $ARM done -> $OUT"
