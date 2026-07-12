#!/usr/bin/env python3
"""L1 analysis: two-level weighted fairness. Verify (1) each tenant (VF
pair) gets its 4:3:2:1 share of the root, and (2) within each pair the
rdma:tcp split follows its class weight. Steady window [t0+25, t0+85]."""
import json
import sys
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
WT = [4, 3, 2, 1]
CW = [(7, 3), (6, 4), (5, 5), (3, 7)]   # (rdma, tcp) per pair
ROOT = 97.0

t0 = float((DIR / "l1_t0.txt").read_text().strip())
acc = {}
for jl in (DIR / "l1_rx.jsonl").read_text().splitlines():
    try:
        rec = json.loads(jl)
    except json.JSONDecodeError:
        continue
    if not (t0 + 25 <= rec["ts"] < t0 + 85):
        continue
    for f, v in rec["r"].items():
        acc.setdefault(f, []).append(v)

mean = {f: sum(v) / len(v) / 1e9 for f, v in acc.items()}
pair = {}
for n in range(4):
    r = mean.get("sgpu01/vf%d>sgpu02/vf%d|rdma" % (n, n), 0)
    t = mean.get("sgpu01/vf%d>sgpu02/vf%d|tcp" % (n, n), 0)
    pair[n] = (r, t)

tot = sum(r + t for r, t in pair.values())
print("== tenant (VF pair) shares vs 4:3:2:1 of root ==")
print("pair  W  got_total  share%%  target(4:3:2:1 of %.0f)  err" % tot)
wsum = sum(WT)
for n in range(4):
    r, t = pair[n]
    got = r + t
    tgt = tot * WT[n] / wsum
    print("  vf%d  %d  %7.2fG  %5.1f%%  %7.2fG  %+.1f%%"
          % (n, WT[n], got, 100 * got / tot, tgt,
             (got - tgt) / tgt * 100 if tgt else 0))

print("\n== per-pair class split vs weight ==")
print("pair  weight  rdmaG  tcpG  got_ratio  target  err")
for n in range(4):
    r, t = pair[n]
    rw, tw = CW[n]
    tgt = rw / tw
    got = r / t if t > 0.1 else 0
    print("  vf%d  %d:%d   %6.2f %6.2f  %6.2f  %6.2f  %+.1f%%"
          % (n, rw, tw, r, t, got, tgt, (got - tgt) / tgt * 100 if tgt else 0))
print("\naggregate=%.1fG (root ~%.0fG)" % (tot, ROOT))
