#!/usr/bin/env bash
# One M1b run (single RDMA vf0->vf0, 6G cap) + dynamics analysis from the
# rx agent jsonl. Usage: run_m1b_once.sh <label> [duration_s]
# Prints one summary line and appends it to $OUT (env, default rw_sweep log).
set -u
LABEL=${1:?label}
DUR=${2:-30}
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
OUT=${OUT:-/home/zhaoxiang/hyperfront/hpft-implementation/results/rw_sweep_20260711/runs.tsv}
mkdir -p "$(dirname "$OUT")"

# pkill and server start in SEPARATE ssh commands (ops_notes pkill trap)
ssh -o BatchMode=yes sgpu02 "pkill -f 'ib_write_b[w].*18520' 2>/dev/null; true"
ssh -o BatchMode=yes -f sgpu02 "\$HOME/hyperfront/perftest-26015/ib_write_bw -d mlx5_6 -p 18520 --report_gbits -D $DUR > /tmp/exp_server.log 2>&1"
sleep 3
T0=$(date +%s.%N)
CLIENT=$($PT -d mlx5_6 -p 18520 --report_gbits -D "$DUR" 10.1.0.2 2>&1)
T1=$(date +%s.%N)
GBPS=$(echo "$CLIENT" | awk '/^ 65536/ {print $4}')

ssh -o BatchMode=yes hpft-dpu2 "python3 - $T0 $T1 <<'EOF'
import json, sys, statistics as S
t0, t1 = float(sys.argv[1]), float(sys.argv[2])
key = 'sgpu01/vf0>sgpu02/vf0|rdma'
rows = []
for line in open('/tmp/hpft_rxagent_e.jsonl'):
    try: d = json.loads(line)
    except: continue
    if t0 <= d['ts'] <= t1 and d.get('r'):
        rows.append(d)
if not rows:
    print('NO_DATA'); sys.exit(0)
rs = [d['r'].get(key,0)/1e9 for d in rows]
ss = [d['s'].get(key,0) for d in rows]
n = len(rows)
half = rs[n//2:]
# 500ms-bucket smoothed r for convergence metrics
b = {}
for d in rows:
    b.setdefault(int((d['ts']-t0)/0.5), []).append(d['r'].get(key,0)/1e9)
bk = [S.mean(v) for k, v in sorted(b.items())]
lo, hi = 6.0*0.95, 6.0*1.05
first = next((i*0.5 for i, x in enumerate(bk) if x >= lo*0.9), -1)  # ~5.1G
# settle: last bucket OUTSIDE [5.1, inf) -> after this it stays converged
last_bad = max((i for i, x in enumerate(bk) if x < lo*0.9), default=-1)
settle = (last_bad+1)*0.5 if last_bad >= 0 else 0.0
# mark storm: fraction of ticks with s>0.1, and last time s>0.1 seen
storm = sum(1 for x in ss if x > 0.1)/n
ts_storm = [d['ts']-t0 for d in rows if d['s'].get(key,0) > 0.1]
last_storm = ts_storm[-1] if ts_storm else 0.0
print('steady_mean=%.2f steady_p5=%.2f steady_p95=%.2f rmax=%.2f '
      'first_entry_s=%.1f settle_s=%.1f smax=%.3f storm_frac=%.4f last_storm_s=%.1f nticks=%d'
      % (S.mean(half), sorted(half)[int(len(half)*.05)],
         sorted(half)[int(len(half)*.95)], max(rs), first, settle,
         max(ss), storm, last_storm, n))
EOF" > /tmp/m1b_rx_analysis.txt
RX=$(cat /tmp/m1b_rx_analysis.txt)
LINE="$LABEL	client_gbps=$GBPS	$RX	t0=$T0"
echo "$LINE" | tee -a "$OUT"
