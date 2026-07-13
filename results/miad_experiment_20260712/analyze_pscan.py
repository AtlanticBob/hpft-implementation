#!/usr/bin/env python3
"""mi_alpha cliff scan: for each (law, mi_alpha), competition ratio [20,40],
steady rdma sd, collapse episodes. Flags where it breaks."""
import json
import sys
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
FR = "sgpu01/vf0>sgpu02/vf0|rdma"
FT = "sgpu01/vf0>sgpu02/vf0|tcp"

print("%-6s %6s  ratio  rdma_sd  eps  flag" % ("law", "mi"))
for law in ("miad", "mimd"):
    for mia in ("0.05", "0.15", "0.3", "0.6"):
        tag = f"ps_{law}_{mia}"
        try:
            t0 = float((DIR / f"{tag}_t0.txt").read_text().strip())
        except FileNotFoundError:
            print("%-6s %6s  (no data)" % (law, mia)); continue
        sec = {}
        for jl in (DIR / f"{tag}_rx.jsonl").read_text().splitlines():
            try:
                rec = json.loads(jl)
            except json.JSONDecodeError:
                continue
            s = int(rec["ts"] - t0)
            if 20 <= s < 40:
                sec.setdefault(s, {"r": [], "t": []})
                sec[s]["r"].append(rec["r"].get(FR, 0) / 1e9)
                sec[s]["t"].append(rec["r"].get(FT, 0) / 1e9)
        if not sec:
            print("%-6s %6s  (empty)" % (law, mia)); continue
        rser = [sum(d["r"]) / len(d["r"]) for _, d in sorted(sec.items())]
        tser = [sum(d["t"]) / len(d["t"]) for _, d in sorted(sec.items())]
        mr, mt = sum(rser) / len(rser), sum(tser) / len(tser)
        sd = (sum((x - mr) ** 2 for x in rser) / len(rser)) ** 0.5
        ratio = mr / mt if mt > 0.1 else 0
        eps, run = 0, 0
        for r in rser:
            run = run + 1 if r < 1.5 else 0
            if run == 2:
                eps += 1
        flags = []
        if abs(ratio - 1.0) > 0.10:
            flags.append("RATIO")
        if sd > 0.4:
            flags.append("OSC")
        if eps:
            flags.append("EPS")
        print("%-6s %6s  %.2f   %6.2f  %3d  %s"
              % (law, mia, ratio, sd, eps, ",".join(flags) or "ok"))
