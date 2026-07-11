#!/usr/bin/env python3
"""Slew A/B damping metrics from the 1Hz RP trace + rx episodes.

For each run dir: budget swing (stdev of per-second budget deltas, G),
worst level undershoot (min lvl/bud), collapse episodes and band residence
(from the rx ledger, same criteria as analyze_repro)."""
import json
import re
import sys
from pathlib import Path

FS_R = "sgpu01/vf0>sgpu02/vf0|rdma"
FS_T = "sgpu01/vf0>sgpu02/vf0|tcp"
U = 200 / 1048576


def run_metrics(d):
    d = Path(d)
    buds, ratios = [], []
    for line in (d / "repro_rp.txt").read_text().splitlines():
        if "HPFT_RSP" not in line:
            continue
        f = dict(re.findall(r"(\w+)=(\w+)", line.split("HPFT_RSP")[1]))
        bud, lvl = int(f["bud"]) * U, int(f["lvl"]) * U
        if bud > 0.1:
            buds.append(bud)
            ratios.append(lvl / bud)
    deltas = [abs(b - a) for a, b in zip(buds, buds[1:])]
    swing = (sum(x * x for x in deltas) / len(deltas)) ** 0.5

    t0 = float((d / "repro_t0.txt").read_text().strip())
    per_sec = {}
    for line in (d / "repro_rx.jsonl").read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if t0 + 15 <= rec["ts"] < t0 + 235:
            s = int(rec["ts"] - t0)
            per_sec.setdefault(s, {"r": [], "t": []})
            per_sec[s]["r"].append(rec["r"].get(FS_R, 0))
            per_sec[s]["t"].append(rec["r"].get(FS_T, 0))
    rser = [sum(v["r"]) / len(v["r"]) / 1e9 for _, v in sorted(per_sec.items())]
    tser = [sum(v["t"]) / len(v["t"]) / 1e9 for _, v in sorted(per_sec.items())]
    band = sum(1 for r, t in zip(rser, tser)
               if abs(r - 3) <= 0.3 and abs(t - 3) <= 0.3)
    eps, cur = 0, 0
    for r in rser:
        cur = cur + 1 if r < 1.5 else 0
        if cur == 2:
            eps += 1
    dipsec = sum(1 for r in rser if r < 1.5)
    return (swing, min(ratios), eps, 100 * band / len(rser),
            sum(rser) / len(rser), min(rser), dipsec)


print("%-10s %9s %9s %5s %6s %7s %6s %7s"
      % ("run", "budswing", "min l/b", "eps", "band%", "meanG", "minG", "dips"))
for d in sys.argv[1:]:
    m = run_metrics(d)
    print("%-10s %8.2fG %9.2f %5d %6.0f %7.2f %6.2f %7d" % (d, *m))
