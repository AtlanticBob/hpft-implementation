#!/usr/bin/env bash
# Lab probe for the per-QP executor (2026-09-07). Run it right after
# launching quick.sh in the background:
#   ( bash validation/run/quick.sh NAME 12 SPEC & ); bash validation/run/probe.sh NAME hpft-dpu hpft-dpu3
# It waits for the run's start instant (quick.sh writes /tmp/quick_NAME/t0.txt),
# then reads each sender executor (0xdef counters, 0xded per flow set, 0xdee per
# QP) and the last law record of each of its flow sets. Rates in Gb/s.
# probe.sh <quick-name> <dpu>... : wait for the run's T0 + 6 s, then
# read every executor's sets and first 24 QP slots, plus the agents' last
# law records; print a compact table.
NAME=$1; shift
while [ ! -s /tmp/quick_$NAME/t0.txt ]; do sleep 1; done
T0=$(cat /tmp/quick_$NAME/t0.txt); while [ $(date +%s) -lt $((T0+6)) ]; do sleep 1; done
for d in "$@"; do
  echo "=== $d t=+$(( $(date +%s) - T0 ))s"
  ssh -n -o BatchMode=yes $d 'N=$(wc -l < /tmp/pcc_rp.log); timeout 5 bash -c "echo \"0xdef 0\" > /tmp/rp_fifo"; for s in 0 1 2 3; do timeout 5 bash -c "echo \"0xded $s\" > /tmp/rp_fifo"; done; for s in $(seq 0 23); do timeout 5 bash -c "echo \"0xdee $s\" > /tmp/rp_fifo"; done; sleep 2.5; tail -n +$((N+1)) /tmp/pcc_rp.log | grep -a HPFT_RSP | sed "s/HPFT_RSP //" | python3 -c "
import sys,re
L=[dict(re.findall(r\"(\w+)=(\S+)\",l)) for l in sys.stdin]
L=[{k:int(v,0) for k,v in d.items()} for d in L]
G=1e9/(1<<20)*200  # fxp20 -> Gb/s: 2^20 = 200 G
u=lambda x: x*200/(1<<20)
d=L[0]; print(\"events tx=%d cnp=%d nack=%d rtt=%d alloc=%d bound=%d algo=%d cc_only=%d\"%(d[\"ft\"],d[\"bud\"],d[\"lvl\"],d[\"avg16\"],d[\"r\"],d[\"s16\"],d[\"ep\"],d[\"evb32\"]))
for d in L[1:5]:
    if d[\"ft\"]: print(\"set id=0x%x R=%.2fG sum_paced=%.2fG sum_cc=%.2fG sum_trend=%d nlive=%d nq=%d\"%(d[\"ft\"],u(d[\"bud\"]),u(d[\"lvl\"]),u(d[\"avg16\"]),d[\"r\"],d[\"s16\"],d[\"ep\"]))
for d in L[5:]:
    if d[\"ft\"]: print(\"qp=%d set=%d cc=%.2fG trend=%d paced=%.2fG rtt=%d cnp=%d vhca=%d\"%(d[\"ft\"],d[\"bud\"],u(d[\"lvl\"]),d[\"avg16\"],u(d[\"r\"]),d[\"s16\"],d[\"ep\"],d[\"evb32\"]))
"; echo "-- agent"; tail -400 /tmp/hpft_txagent_e.jsonl | python3 -c "
import sys,json
last={}
for l in sys.stdin:
    try: d=json.loads(l)
    except Exception: continue
    if \"R\" in d and \"pace\" in d: last[d[\"fs\"]]=d
for f,d in sorted(last.items()):
    print(\"%-32s R=%6.2f pace=%6.2f tree=%6.2f r=%6.2f As=%6.2f q=%5.2f %s\"%(f,d[\"R\"]/1e9,d[\"pace\"]/1e9,d.get(\"tree\",0)/1e9,d.get(\"r\",0)/1e9,d.get(\"As\",0)/1e9,d.get(\"q\",0),d.get(\"mode\")))
"'
done
