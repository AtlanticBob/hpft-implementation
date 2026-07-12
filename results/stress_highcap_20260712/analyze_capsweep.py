#!/usr/bin/env python3
"""Cap-sweep analysis: does the class ratio and stability hold as the cap
(pacing bandwidth) rises? Reports per-class mean, ratio, per-second sd
(normalized as CoV since absolute sd scales with the cap), episodes."""
import json
import sys
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
FS_R = "sgpu01/vf0>sgpu02/vf0|rdma"
FS_T = "sgpu01/vf0>sgpu02/vf0|tcp"

print("%-8s %5s %7s %7s %6s %7s %7s %5s %6s"
      % ("cap", "tgt", "rdmaG", "tcpG", "ratio", "cov_r", "util%", "eps", "flag"))
for line in (DIR / "runs.tsv").read_text().splitlines():
    tag, cap, t0 = line.split("\t")
    cap, t0 = float(cap), float(t0)
    per = {}
    for jl in (DIR / f"{tag}_rx.jsonl").read_text().splitlines():
        try:
            rec = json.loads(jl)
        except json.JSONDecodeError:
            continue
        if not (t0 + 15 <= rec["ts"] < t0 + 58):
            continue
        s = int(rec["ts"] - t0)
        d = per.setdefault(s, {"r": [], "t": []})
        d["r"].append(rec["r"].get(FS_R, 0))
        d["t"].append(rec["r"].get(FS_T, 0))
    rser = [sum(v["r"]) / len(v["r"]) / 1e9 for _, v in sorted(per.items())]
    tser = [sum(v["t"]) / len(v["t"]) / 1e9 for _, v in sorted(per.items())]
    mr, mt = sum(rser) / len(rser), sum(tser) / len(tser)
    sd = (sum((x - mr) ** 2 for x in rser) / len(rser)) ** 0.5
    cov = sd / mr if mr > 0.1 else 0
    tgt = cap / 2
    eps, run = 0, 0
    for r in rser:
        run = run + 1 if r < tgt * 0.5 else 0
        if run == 2:
            eps += 1
    util = (mr + mt) / cap * 100
    ratio = mr / mt if mt > 0.1 else 0
    flags = []
    if abs(ratio - 1.0) > 0.10:
        flags.append("RATIO")
    if cov > 0.15:
        flags.append("OSC")
    if eps:
        flags.append("EPS")
    if util < 85:
        flags.append("UTIL")
    print("%-8s %5.0f %7.2f %7.2f %6.2f %7.2f %6.0f%% %5d %s"
          % (tag, tgt, mr, mt, ratio, cov, util, eps, ",".join(flags) or "ok"))
