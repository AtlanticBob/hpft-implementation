#!/usr/bin/env python3
"""D7 W2 analysis: root-regime aggregate + synchronization for each param
point. D4 baseline: aggregate 53.6G (55%), per-flow sd 10.2, cross-flow
correlation high (synchronized). Reports aggregate, mean per-flow sd, and
a synchronization index = mean pairwise correlation of the 4 flows'
per-second series (1.0 = perfectly in phase)."""
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


print("%-28s %8s %7s %7s %s"
      % ("point", "aggG", "sd_flow", "sync", "flag"))
for line in (DIR / "w2_runs.tsv").read_text().splitlines():
    tag, param, value, t0, t1 = line.split("\t")
    t0, t1 = float(t0), float(t1)
    per = {i: {} for i in range(4)}
    for jl in (DIR / f"{tag}_rx.jsonl").read_text().splitlines():
        try:
            rec = json.loads(jl)
        except json.JSONDecodeError:
            continue
        if not (t0 + 15 <= rec["ts"] < t1 - 5):
            continue
        s = int(rec["ts"] - t0)
        for i in range(4):
            f = "sgpu01/vf%d>sgpu02/vf%d|rdma" % (i, i)
            per[i].setdefault(s, []).append(rec["r"].get(f, 0))
    ser = {i: [sum(v) / len(v) / 1e9 for _, v in sorted(d.items())]
           for i, d in per.items() if d}
    if len(ser) < 4:
        print("%-28s  (incomplete)" % tag)
        continue
    means = {i: sum(s) / len(s) for i, s in ser.items()}
    agg = sum(means.values())
    sds = [(sum((x - means[i]) ** 2 for x in ser[i]) / len(ser[i])) ** 0.5
           for i in ser]
    sd = sum(sds) / len(sds)
    sync = sum(corr(ser[a], ser[b]) for a, b in combinations(range(4), 2)) / 6
    flag = "DAMPED" if sync < 0.5 and agg > 65 else ("better" if agg > 60 else "")
    print("%-28s %7.1fG %7.2f %7.2f %s" % (tag, agg, sd, sync, flag))
