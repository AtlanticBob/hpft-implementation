#!/usr/bin/env bash
# RDMA executor step response, control loop bypassed: one RDMA pair
# sgpu01/vf0 -> sgpu02/vf0, budgets written straight into the RP FIFO on
# the sender DPU, arrival rate read from the receiver's rx_agent log
# (20 ms records). Measures what the executor alone can do.
#
# Schedule (s after t0): flow starts unbudgeted (unknown-flow cap applies);
# 8: 40G  18: 10G  28: 40G  38: 5G  48: 40G  ; flow ends at 60.
# usage: executor_step.sh <tag>
set -u
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd); cd "$REPO"
OUT="$REPO/results/regression"; TAG=$1
PT=$HOME/hyperfront/perftest-enhanced/ib_write_bw
SND=sgpu01; SDPU=hpft-dpu; RCV=sgpu02; RDPU=hpft-dpu2
FT=$(python3 -c "
import json;r=json.load(open('config/lab-registry.json'))
print(r['rdma_flowtags'].get('sgpu01/vf0>sgpu02/vf0') or next(v['flowtag'] for v in r['vnics'] if v['vnic_id']=='sgpu01/vf0'))")
units() { python3 -c "print(max(1, round($1 / 200e9 * (1<<20))))"; }
RIP=$(python3 -c "
import json;r=json.load(open('config/lab-registry.json'))
print(next(v['ip'] for v in r['vnics'] if v['vnic_id']=='sgpu02/vf0'))")
DEV_S=$(python3 -c "
import json;r=json.load(open('config/lab-registry.json'))
print(next(v['rdma_dev'] for v in r['vnics'] if v['vnic_id']=='sgpu01/vf0'))")
DEV_R=$(python3 -c "
import json;r=json.load(open('config/lab-registry.json'))
print(next(v['rdma_dev'] for v in r['vnics'] if v['vnic_id']=='sgpu02/vf0'))")
# the sender agent must not overwrite our budgets; the RP is restarted so
# no budget is installed when the flow starts
ssh -o BatchMode=yes $SDPU 'sudo systemctl stop hpft-txagent-e 2>/dev/null; true'
ssh -o BatchMode=yes $SDPU 'bash /opt/hpft/rp_service.sh start' >/dev/null 2>&1
sleep 2
# the unknown-flow cap the agent would have set (per QP; 4 QPs here)
ssh -o BatchMode=yes $SDPU "echo '0xccf $(units 5e9)' > /tmp/rp_fifo"
# receiver-DPU clock vs this host: the rx log is stamped there
OFF=$(for i in 1 2 3; do a=$(date +%s.%N); b=$(ssh -o BatchMode=yes $RDPU date +%s.%N); c=$(date +%s.%N); python3 -c "print($b-($a+$c)/2)"; done | sort -n | sed -n 2p)
echo "clock offset receiver-dpu minus local: $OFF s"
ssh -o BatchMode=yes $RCV 'pkill -f "ib_write_[b]"; true'; sleep 1
ssh -o BatchMode=yes $RCV "setsid nohup $PT -d $DEV_R -p 27000 -q 4 --report_gbits -D 60 >/tmp/exs_srv 2>&1 </dev/null &"
sleep 2
t0=$(python3 -c "import time;print(time.time()+$OFF)"); echo "$t0" > "$OUT/${TAG}_t0.txt"
run_snd() { if [ "$SND" = "$(hostname)" ]; then bash -c "$1"; else ssh -o BatchMode=yes $SND "$1"; fi; }
run_snd "setsid nohup $PT -d $DEV_S -p 27000 -q 4 --report_gbits -D 60 $RIP >/tmp/exs_cli 2>&1 </dev/null &"
: > "$OUT/${TAG}_events.txt"
step() { # step <at s> <bps>
  local now; now=$(python3 -c "import time;print(time.time()+$OFF-$t0)")
  local wait; wait=$(python3 -c "print(max(0,$1-$now))"); sleep "$wait"
  ssh -o BatchMode=yes $SDPU "echo '0xb47c0001 $FT $(units $2) 0' > /tmp/rp_fifo"
  echo "$(python3 -c "import time;print(time.time()+$OFF)") $2" >> "$OUT/${TAG}_events.txt"
}
step 8 40e9; step 18 10e9; step 28 40e9; step 38 5e9; step 48 40e9
sleep 14
scp -q "$RDPU:/tmp/hpft_rxagent_e.jsonl" "$OUT/${TAG}_rx.jsonl"
ssh -o BatchMode=yes $SDPU 'grep -a "^HPFT_SET" /tmp/pcc_rp.log | tail -20' > "$OUT/${TAG}_rp.txt"
ssh -o BatchMode=yes $RCV 'pkill -f "ib_write_[b]"; true'
python3 - "$OUT" "$TAG" <<'EOF'
import json,sys
out,tag=sys.argv[1],sys.argv[2]
t0=float(open(f"{out}/{tag}_t0.txt").read())
ev=[(float(l.split()[0]),float(l.split()[1])) for l in open(f"{out}/{tag}_events.txt")]
rows=[json.loads(l) for l in open(f"{out}/{tag}_rx.jsonl") if l.strip()]
f="sgpu01/vf0>sgpu02/vf0|rdma"
ser=[(r["ts"],r["r"].get(f,0)) for r in rows if r["ts"]>=t0]
def at(t):
    v=[x for x in ser if abs(x[0]-t)<0.011]; return v[0][1] if v else None
print("t(s)  rate(G)  -- 100 ms samples from flow start")
for i in range(0,80,5):
    t=t0+i*0.1; v=at(t); print("%4.1f %6.1f"%(i*0.1,(v or 0)/1e9), end="   " if (i//5)%4!=3 else "\n")
print()
for te,bps in ev:
    tgt=bps
    # time from FIFO write to first record within 10% of target, and to 5 consecutive
    ok=0; first=None; settled=None
    for ts,v in ser:
        if ts<te: continue
        if ts-te>5: break
        if abs(v-tgt)<=0.10*tgt:
            first=first if first is not None else ts-te
            ok+=1
            if ok>=5 and settled is None: settled=ts-te-0.08
        else: ok=0
    pre=[v for ts,v in ser if te-0.3<=ts<te]
    print("step -> %5.1fG at t+%.1fs: before %.1fG, first in band %s, settled %s"%(bps/1e9,te-t0,(sum(pre)/len(pre) if pre else 0)/1e9,
          "%.0f ms"%(first*1e3) if first is not None else "never","%.0f ms"%(settled*1e3) if settled is not None else "never"))
EOF
echo "${TAG}-executor-step-done"
