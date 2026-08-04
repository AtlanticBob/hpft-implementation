#!/usr/bin/env bash
# P7 acceptance: end-to-end dry run of one 2-1-2-M-shaped cell through
# eval_lib.sh (HPFT arm, DCQCN(PCC)xCubic, 32-flow symmetric incast, 30s).
# Verifies: registry push/restore, stack restart, traffic helpers, vpm +
# armstat sampling, counters snap, collection, quick Jain analysis.
set -eu
REPO=/home/zhaoxiang/hyperfront/hpft-implementation
. "$REPO/tools/eval/eval_lib.sh"
D=${1:-/tmp/eval_dryrun}; mkdir -p "$D"
DUR=30

trap reg_restore EXIT
reg_push ""                      # scenario == repo defaults
stack_restart
traffic_clear
counters_snap "$D/ctr_pre"
vpm_start $((DUR+8)) 50
armstat_start hpft-dpu2 $((DUR+8))

for j in 0 1 2 3; do rdma_server mlx5_$((6+j)) $((28100+j)) 4 $DUR; tcp_server $((5480+j)); done
sleep 2; stamp "$D/t0"
for i in 0 1 2 3; do
  rdma_client mlx5_$((6+i)) $((28100+i)) 4 $DUR 10.1.$i.2 "$D/rdma$i.log" --rate_limit=5 --rate_units=g &
  tcp_client $((5480+i)) 4 $DUR $i 10.1.$i.2 "$D/tcp$i.json" &
done
wait; stamp "$D/t1"

counters_snap "$D/ctr_post"
agents_grab "$D" cell
vpm_grab "$D" cell
armstat_grab "$D" hpft-dpu2 cell
traffic_clear

python3 - "$D" <<'EOF'
import json, sys, collections
D=sys.argv[1]; G=1e9
t0=float(open(f"{D}/t0").read()); t1=float(open(f"{D}/t1").read())
acc=collections.defaultdict(list)
for line in open(f"{D}/cell_rx.jsonl"):
    try: d=json.loads(line)
    except ValueError: continue
    if t0+10 <= d.get("ts",0) <= t1-3:
        for f,v in (d.get("r") or {}).items(): acc[f].append(v)
means={f:sum(v)/len(v) for f,v in acc.items() if sum(v)/len(v)>0.5*G}
vals=list(means.values()); tot=sum(vals)
jain=tot*tot/(len(vals)*sum(v*v for v in vals))
rd=sum(v for f,v in means.items() if f.endswith("rdma")); tc=tot-rd
print(f"dryrun: fs={len(vals)} agg={tot/G:.1f}G Jain={jain:.4f} rdma:tcp={rd/G:.1f}:{tc/G:.1f}")
import csv, os
vpm=sum(1 for _ in open(f"{D}/cell_vpm.csv")) if os.path.exists(f"{D}/cell_vpm.csv") else 0
arm=sum(1 for _ in open(f"{D}/cell_armstat_hpft-dpu2.csv")) if os.path.exists(f"{D}/cell_armstat_hpft-dpu2.csv") else 0
print(f"artifacts: vpm_rows={vpm} armstat_rows={arm} "
      f"ctr={'ok' if os.path.getsize(f'{D}/ctr_post')>0 else 'MISSING'}")
EOF
echo "dryrun done -> $D"
