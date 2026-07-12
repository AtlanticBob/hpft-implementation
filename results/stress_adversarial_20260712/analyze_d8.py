#!/usr/bin/env python3
"""D8 analysis: adversarial pump's long-run wire share vs the 3G steady
entitlement, and the compliant RDMA class's mean. Robustness holds if the
attacker's TCP wire average stays within +10% of 3G."""
import json
import sys
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
FS_R = "sgpu01/vf0>sgpu02/vf0|rdma"
FS_T = "sgpu01/vf0>sgpu02/vf0|tcp"

print("%-8s %5s %5s %8s %8s %7s %s"
      % ("run", "on", "off", "atk_tcpG", "rdmaG", "util", "verdict"))
for line in (DIR / "d8_runs.tsv").read_text().splitlines():
    name, on, off, t0, t1 = line.split("\t")
    t0, t1 = float(t0), float(t1)
    rs, ts = [], []
    for jl in (DIR / f"{name}_rx.jsonl").read_text().splitlines():
        try:
            rec = json.loads(jl)
        except json.JSONDecodeError:
            continue
        if not (t0 + 15 <= rec["ts"] < t1 - 5):
            continue
        rs.append(rec["r"].get(FS_R, 0) / 1e9)
        ts.append(rec["r"].get(FS_T, 0) / 1e9)
    if not rs:
        print("%-8s  (no data)" % name)
        continue
    mr, mt = sum(rs) / len(rs), sum(ts) / len(ts)
    verdict = "OK" if mt <= 3.3 else "ATTACKER GAINS +%.0f%%" % ((mt - 3) / 3 * 100)
    print("%-8s %5s %5s %8.2f %8.2f %7.2f %s"
          % (name, on, off, mt, mr, mr + mt, verdict))
