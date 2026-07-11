#!/usr/bin/env python3
"""D2 analysis: wire ratio tracking per (point, rep), steady window
[t0+20, t1-5]. Reports rdma/tcp means, achieved:target ratio error,
per-second stdev, weak-class minimum (lock-in detector), utilization."""
import json
import sys
from collections import defaultdict
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
FS_R = "sgpu01/vf0>sgpu02/vf0|rdma"
FS_T = "sgpu01/vf0>sgpu02/vf0|tcp"

runs = []
for line in (DIR / "runs.tsv").read_text().splitlines():
    pt, rep, t0, t1 = line.split("\t")
    runs.append((pt, int(rep), float(t0), float(t1)))

rx = []
for line in (DIR / "ratio_rx.jsonl").read_text().splitlines():
    try:
        rx.append(json.loads(line))
    except json.JSONDecodeError:
        pass

print("%-6s %3s %7s %7s %7s %7s %8s %7s %7s"
      % ("point", "rep", "rdmaG", "tcpG", "ratio", "err%", "weakmin",
         "sd_r", "sd_t"))
for pt, rep, t0, t1 in runs:
    w0, w1 = t0 + 20, t1 - 5
    sec = defaultdict(lambda: [[], []])
    for rec in rx:
        if not (w0 <= rec["ts"] < w1):
            continue
        s = int(rec["ts"])
        sec[s][0].append(rec["r"].get(FS_R, 0))
        sec[s][1].append(rec["r"].get(FS_T, 0))
    rs = [sum(v[0]) / max(1, len(v[0])) / 1e9 for v in sec.values()]
    ts = [sum(v[1]) / max(1, len(v[1])) / 1e9 for v in sec.values()]
    if not rs:
        print("%-6s %3d  (no data)" % (pt, rep))
        continue
    mr, mt = sum(rs) / len(rs), sum(ts) / len(ts)
    sd = lambda v, m: (sum((x - m) ** 2 for x in v) / len(v)) ** 0.5
    tgt_r, tgt_t = (float(x) for x in pt.split(":"))
    tgt = tgt_r / tgt_t
    ratio = mr / mt if mt > 0 else float("inf")
    err = (ratio - tgt) / tgt * 100
    weak = min(rs) if tgt_r <= tgt_t else min(ts)
    print("%-6s %3d %7.2f %7.2f %7.2f %+7.1f %8.2f %7.2f %7.2f"
          % (pt, rep, mr, mt, ratio, err, weak, sd(rs, mr), sd(ts, mt)))
