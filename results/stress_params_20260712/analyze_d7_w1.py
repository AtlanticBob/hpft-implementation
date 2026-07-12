#!/usr/bin/env python3
"""D7 W1 sweep analysis: per-point cliff metrics on the single-pair
two-class protocol. Flags: ratio error beyond 10%, any collapse episode
(rdma < 1.5G for >= 2s), utilization below 5.0G."""
import json
import sys
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
FS_R = "sgpu01/vf0>sgpu02/vf0|rdma"
FS_T = "sgpu01/vf0>sgpu02/vf0|tcp"

print("%-30s %6s %6s %6s %6s %6s %5s %5s %s"
      % ("point", "rdmaG", "tcpG", "ratio", "sd_r", "wkmin", "util", "eps", "flag"))
for line in (DIR / "w1_runs.tsv").read_text().splitlines():
    tag, param, value, t0, t1 = line.split("\t")
    t0, t1 = float(t0), float(t1)
    per = {}
    for jl in (DIR / f"{tag}_rx.jsonl").read_text().splitlines():
        try:
            rec = json.loads(jl)
        except json.JSONDecodeError:
            continue
        if not (t0 + 15 <= rec["ts"] < t1 - 5):
            continue
        s = int(rec["ts"] - t0)
        d = per.setdefault(s, {"r": [], "t": []})
        d["r"].append(rec["r"].get(FS_R, 0))
        d["t"].append(rec["r"].get(FS_T, 0))
    if not per:
        print("%-30s  (no data)" % tag)
        continue
    rser = [sum(v["r"]) / len(v["r"]) / 1e9 for _, v in sorted(per.items())]
    tser = [sum(v["t"]) / len(v["t"]) / 1e9 for _, v in sorted(per.items())]
    mr, mt = sum(rser) / len(rser), sum(tser) / len(tser)
    sd = (sum((x - mr) ** 2 for x in rser) / len(rser)) ** 0.5
    ratio = mr / mt if mt > 0.05 else float("inf")
    eps, cur = 0, 0
    for r in rser:
        cur = cur + 1 if r < 1.5 else 0
        if cur == 2:
            eps += 1
    util = mr + mt
    flags = []
    if abs(ratio - 1.0) > 0.10:
        flags.append("RATIO")
    if eps:
        flags.append("EPS")
    if util < 5.0:
        flags.append("UTIL")
    if sd > 0.5:
        flags.append("OSC")
    print("%-30s %6.2f %6.2f %6.2f %6.2f %6.2f %5.2f %5d %s"
          % (tag, mr, mt, ratio, sd, min(rser), util, eps,
             ",".join(flags) or "ok"))
