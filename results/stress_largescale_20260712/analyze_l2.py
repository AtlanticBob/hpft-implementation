#!/usr/bin/env python3
"""L2 churn analysis. Per-second per-flow-set rates aligned to event marks.
Checks: (1) untouched pairs hold their class weights through neighbor churn;
(2) yield/reclaim times for the class-churn event (vf0 TCP out/in)."""
import json
import sys
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
t0 = float((DIR / "l2_t0.txt").read_text().strip())
ev = {}
for line in (DIR / "l2_events.txt").read_text().splitlines():
    ts, name = line.split()
    ev[name] = float(ts) - t0

per = {}   # fs -> {sec: [r]}
for jl in (DIR / "l2_rx.jsonl").read_text().splitlines():
    try:
        rec = json.loads(jl)
    except json.JSONDecodeError:
        continue
    s = rec["ts"] - t0
    for f, v in rec["r"].items():
        per.setdefault(f, {}).setdefault(round(s, 1), v / 1e9)


def win(fs, a, b):
    xs = [v for s, v in per.get(fs, {}).items() if a <= s < b]
    return sum(xs) / len(xs) if xs else 0.0


print("events:", {k: round(v) for k, v in sorted(ev.items(), key=lambda x: x[1])})
CW = [(7, 3), (6, 4), (5, 5), (3, 7)]
CAP = [40, 30, 20, 10]

# (1) untouched pairs (vf2, vf3) class ratio in three phases
print("\nclass ratio of untouched pairs across churn phases:")
phases = [("baseline", 25, 43), ("vf1_gone", 48, 73),
          ("vf1_back", 78, 103), ("vf0tcp_gone", 108, 133)]
for n in (2, 3):
    rw, tw = CW[n]
    line = "  vf%d (%d:%d=%.2f): " % (n, rw, tw, rw / tw)
    for name, a, b in phases:
        r = win("sgpu01/vf%d>sgpu02/vf%d|rdma" % (n, n), a, b)
        t = win("sgpu01/vf%d>sgpu02/vf%d|tcp" % (n, n), a, b)
        line += "%s=%.2f " % (name, r / t if t > 0.1 else 0)
    print(line)

# (2) vf0: TCP out -> RDMA fills cap; TCP in -> reclaim
print("\nvf0 class churn (cap 40G, 7:3):")
fr = "sgpu01/vf0>sgpu02/vf0|rdma"
ft = "sgpu01/vf0>sgpu02/vf0|tcp"
kt = ev["kill_vf0_tcp"]
rt = ev["rejoin_vf0_tcp"]
# fill time: after kill, rdma to >=0.9*cap (36G) sustained
fill = None
run = 0
for s in sorted(per[fr]):
    if s < kt:
        continue
    run = run + 1 if per[fr][s] >= 36 else 0
    if run >= 5 and fill is None:
        fill = s - kt
print("  pre-kill: rdma=%.1f tcp=%.1f" % (win(fr, kt - 15, kt), win(ft, kt - 15, kt)))
print("  post-kill rdma fill: %.1fG (fill_to_cap=%.1fs)"
      % (win(fr, kt + 15, rt), fill if fill else -1))
print("  after tcp rejoin: rdma=%.1f tcp=%.1f ratio=%.2f"
      % (win(fr, rt + 15, rt + 40), win(ft, rt + 15, rt + 40),
         win(fr, rt + 15, rt + 40) / max(0.1, win(ft, rt + 15, rt + 40))))
