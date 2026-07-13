#!/usr/bin/env python3
"""Incast root-regime 3-way: aggregate utilization (vs 97G), per-flow
mean/sd, cross-flow synchronization (mean pairwise correlation)."""
import json
import sys
from itertools import combinations
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")


def corr(a, b):
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a) ** 0.5
    vb = sum((x - mb) ** 2 for x in b) ** 0.5
    if va == 0 or vb == 0:
        return 0.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb)


print("%-5s  aggG  util%%  perflowG  flow_sd  sync  min_flow" % "law")
for law, tag in (("aimd", "ic_aimd"), ("miad", "ic_miad"), ("mimd", "ic_mimd")):
    try:
        t0 = float((DIR / f"{tag}_t0.txt").read_text().strip())
    except FileNotFoundError:
        print("%-5s (no data)" % law); continue
    per = {i: {} for i in range(4)}
    for jl in (DIR / f"{tag}_rx.jsonl").read_text().splitlines():
        try:
            rec = json.loads(jl)
        except json.JSONDecodeError:
            continue
        s = int(rec["ts"] - t0)
        if not (20 <= s < 85):
            continue
        for i in range(4):
            f = "sgpu01/vf%d>sgpu02/vf%d|rdma" % (i, i)
            per[i].setdefault(s, []).append(rec["r"].get(f, 0))
    ser = {i: [sum(v) / len(v) / 1e9 for _, v in sorted(d.items())]
           for i, d in per.items() if d}
    if len(ser) < 4:
        print("%-5s (incomplete)" % law); continue
    means = {i: sum(s) / len(s) for i, s in ser.items()}
    agg = sum(means.values())
    sds = [(sum((x - means[i]) ** 2 for x in ser[i]) / len(ser[i])) ** 0.5 for i in ser]
    sync = sum(corr(ser[a], ser[b]) for a, b in combinations(range(4), 2)) / 6
    minf = min(min(ser[i]) for i in ser)
    print("%-5s  %.0f  %4.0f%%  %7.2f  %7.2f  %.2f  %7.2f"
          % (law, agg, 100 * agg / 97, agg / 4, sum(sds) / len(sds), sync, minf))
