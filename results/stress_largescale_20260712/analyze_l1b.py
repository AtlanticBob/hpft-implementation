#!/usr/bin/env python3
"""L1b analysis: class-weight enforcement at a policy bottleneck (per-pair
caps, no physical congestion). Each pair should fill its cap and split it
by class weight. Steady [t0+25, t0+85]."""
import json
import sys
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
CAP = [40, 30, 20, 10]
CW = [(7, 3), (6, 4), (5, 5), (3, 7)]

t0 = float((DIR / "l1b_t0.txt").read_text().strip())
acc = {}
for jl in (DIR / "l1b_rx.jsonl").read_text().splitlines():
    try:
        rec = json.loads(jl)
    except json.JSONDecodeError:
        continue
    if not (t0 + 25 <= rec["ts"] < t0 + 85):
        continue
    for f, v in rec["r"].items():
        acc.setdefault(f, []).append(v)
mean = {f: sum(v) / len(v) / 1e9 for f, v in acc.items()}

print("pair  cap  rdmaG  tcpG  fill%%  ratio  target  err")
for n in range(4):
    r = mean.get("sgpu01/vf%d>sgpu02/vf%d|rdma" % (n, n), 0)
    t = mean.get("sgpu01/vf%d>sgpu02/vf%d|tcp" % (n, n), 0)
    rw, tw = CW[n]
    tgt = rw / tw
    got = r / t if t > 0.1 else 0
    print("  vf%d  %2d  %6.2f %6.2f  %4.0f%%  %5.2f  %5.2f  %+.1f%%"
          % (n, CAP[n], r, t, 100 * (r + t) / CAP[n], got, tgt,
             (got - tgt) / tgt * 100 if tgt else 0))
