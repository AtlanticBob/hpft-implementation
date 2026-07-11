#!/usr/bin/env python3
"""Batch B analysis: per-fs fairness at 28 concurrent flow-sets.

Steady window = [clients_up + 20s, fair_end - 10s]. For each fs: mean wire
rate (rx account), per-second stdev, mean grant e, r-vs-e gap. Reference
equilibrium (hierarchical max-min over both trees, demand-cap semantics,
derived in summary.md):
  tcp any:            2.50 G  (sender and receiver bind together)
  rdma vf0->*, vf3->*: 2.50 G  (sender class 10G over 4 dsts)
  rdma vf1->{0,1,3}:  3.33 G  (sender class 10G over 3 dsts)
  rdma vf2->vf2:      4.25 G  (receiver residual after demand-capped peers)
Targets are budget-units; RP-bound flows realize ~0.92 of budget on the wire
(known 20G->18.3G wire ceiling), so judge RDMA against 0.92x target.
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")

REF = {}
for i in range(4):
    for j in range(4):
        REF["sgpu01/vf%d>sgpu02/vf%d|tcp" % (i, j)] = 2.5
for (i, j), t in {(0, 0): 2.5, (0, 1): 2.5, (0, 2): 2.5, (0, 3): 2.5,
                  (1, 0): 10 / 3, (1, 1): 10 / 3, (1, 3): 10 / 3,
                  (2, 2): 4.25,
                  (3, 0): 2.5, (3, 1): 2.5, (3, 2): 2.5, (3, 3): 2.5}.items():
    REF["sgpu01/vf%d>sgpu02/vf%d|rdma" % (i, j)] = t

tl = dict((name, float(ts)) for ts, name in
          (l.split() for l in (DIR / "fair_timeline.txt").read_text().splitlines()))
w0, w1 = tl["clients_up"] + 20, tl["fair_end"] - 10

per_sec = defaultdict(lambda: defaultdict(list))   # fs -> sec -> [r]
grants = defaultdict(list)
for line in (DIR / "fair_rx.jsonl").read_text().splitlines():
    rec = json.loads(line)
    if not (w0 <= rec["ts"] < w1):
        continue
    sec = int(rec["ts"])
    for f, v in rec["r"].items():
        per_sec[f][sec].append(v)
    for f, v in rec["e"].items():
        grants[f].append(v)

print("%-34s %6s %6s %6s %6s %7s" % ("fs", "meanG", "sdevG", "e_G", "refG", "vs_ref"))
tot = defaultdict(float)
for f in sorted(per_sec):
    secs = [sum(v) / len(v) for _, v in sorted(per_sec[f].items())]
    mean = sum(secs) / len(secs) / 1e9
    sdev = (sum((x / 1e9 - mean) ** 2 for x in secs) / len(secs)) ** 0.5
    e = sum(grants[f]) / max(1, len(grants[f])) / 1e9
    ref = REF.get(f, float("nan"))
    cls = f.rsplit("|", 1)[1]
    eff = ref * 0.92 if cls == "rdma" else ref
    dev = (mean - eff) / eff * 100 if eff else float("nan")
    tot[cls] += mean
    tot["dst" + f.split(">")[1].split("|")[0][-1]] += mean
    print("%-34s %6.2f %6.2f %6.2f %6.2f %+6.1f%%" % (f, mean, sdev, e, eff, dev))

print("\nclass totals: tcp=%.1fG rdma=%.1fG  aggregate=%.1fG"
      % (tot["tcp"], tot["rdma"], tot["tcp"] + tot["rdma"]))
for d in "0123":
    print("  dst vf%s total=%.2fG" % (d, tot["dst" + d]))
