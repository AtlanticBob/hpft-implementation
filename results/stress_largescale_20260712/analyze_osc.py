#!/usr/bin/env python3
"""Characterize the multi-pair class-split oscillation. For each pair, the
per-second class ratio over the steady run: mean, stdev, and the fraction
of time the ratio deviates >25% from its target. Flags oscillation."""
import json
import sys
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
TAG = sys.argv[2] if len(sys.argv) > 2 else "v4"
CW = [(7, 3), (6, 4), (5, 5), (3, 7)]

t0 = float((DIR / f"osc_{TAG}_t0.txt").read_text().strip())
per = {}
for jl in (DIR / f"osc_{TAG}_rx.jsonl").read_text().splitlines():
    try:
        rec = json.loads(jl)
    except json.JSONDecodeError:
        continue
    s = int(rec["ts"] - t0)
    if not (15 <= s < 118):
        continue
    for f, v in rec["r"].items():
        per.setdefault(f, {}).setdefault(s, []).append(v)

print("pair  target  ratio_mean  ratio_sd  frac>25%%off  worst")
for n in range(4):
    rw, tw = CW[n]
    tgt = rw / tw
    fr = "sgpu01/vf%d>sgpu02/vf%d|rdma" % (n, n)
    ft = "sgpu01/vf%d>sgpu02/vf%d|tcp" % (n, n)
    ratios = []
    for s in sorted(per.get(fr, {})):
        r = sum(per[fr][s]) / len(per[fr][s]) / 1e9
        tv = per.get(ft, {}).get(s, [0])
        t = sum(tv) / len(tv) / 1e9
        if t > 0.1:
            ratios.append(r / t)
    if not ratios:
        print("  vf%d  (no data)" % n)
        continue
    m = sum(ratios) / len(ratios)
    sd = (sum((x - m) ** 2 for x in ratios) / len(ratios)) ** 0.5
    off = sum(1 for x in ratios if abs(x - tgt) / tgt > 0.25) / len(ratios)
    worst = max(ratios, key=lambda x: abs(x - tgt))
    flag = "  <-- OSC" if off > 0.15 else ""
    print("  vf%d  %5.2f   %8.2f  %8.2f   %6.0f%%   %5.2f%s"
          % (n, tgt, m, sd, 100 * off, worst, flag))

# time trace of the worst pair to see the oscillation shape
worstn = 0
worst_off = -1
for n in range(4):
    fr = "sgpu01/vf%d>sgpu02/vf%d|rdma" % (n, n)
    ft = "sgpu01/vf%d>sgpu02/vf%d|tcp" % (n, n)
    rt = []
    for s in sorted(per.get(fr, {})):
        r = sum(per[fr][s]) / len(per[fr][s]) / 1e9
        tv = per.get(ft, {}).get(s, [0])
        t = sum(tv) / len(tv) / 1e9
        if t > 0.1:
            rt.append(abs(r / t - CW[n][0] / CW[n][1]))
    o = sum(rt) / len(rt) if rt else 0
    if o > worst_off:
        worst_off, worstn = o, n
print("\nworst pair vf%d rdma/tcp over time (every 5s):" % worstn)
fr = "sgpu01/vf%d>sgpu02/vf%d|rdma" % (worstn, worstn)
ft = "sgpu01/vf%d>sgpu02/vf%d|tcp" % (worstn, worstn)
for s in sorted(per.get(fr, {})):
    if s % 5:
        continue
    r = sum(per[fr][s]) / len(per[fr][s]) / 1e9
    tv = per.get(ft, {}).get(s, [0])
    t = sum(tv) / len(tv) / 1e9
    print("  t=%3ds rdma=%.1f tcp=%.1f" % (s, r, t))
