#!/usr/bin/env python3
"""L3 QP-scale analysis. Per QP-count: per-pair steady wire (rx), aggregate,
tick health (dt), RC packet_seq_err delta, and agent CPU%. Control-plane
cost should be flat (flow-set count constant at 4); the QP axis stresses
the RP/data-path."""
import json
import sys
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")

cpu = {}
for line in (DIR / "l3_cpu.txt").read_text().splitlines():
    parts = line.split()
    cpu[parts[0]] = parts

print("%-6s %6s %8s %8s %9s %8s %8s"
      % ("Q", "totQP", "aggG", "perpairG", "dt_p95ms", "pse_d", "note"))
for line in (DIR / "l3_runs.tsv").read_text().splitlines():
    Q, tot, t0, t1 = line.split("\t")
    t0, t1 = float(t0), float(t1)
    per = {}
    dts = []
    for jl in (DIR / f"l3_rx_q{Q}.jsonl").read_text().splitlines():
        try:
            rec = json.loads(jl)
        except json.JSONDecodeError:
            continue
        if not (t0 + 18 <= rec["ts"] < t1 - 3):
            continue
        dts.append(rec["dt_s"] * 1e3)
        for f, v in rec["r"].items():
            if "rdma" in f:
                per.setdefault(f, []).append(v)
    means = {f: sum(v) / len(v) / 1e9 for f, v in per.items()}
    agg = sum(means.values())
    perpair = agg / max(1, len(means))
    dts.sort()
    dt95 = dts[int(len(dts) * 0.95)] if dts else 0
    pse_b = sum(int(x) for x in (DIR / f"l3_pse_before_q{Q}.txt").read_text().split())
    pse_a = sum(int(x) for x in (DIR / f"l3_pse_after_q{Q}.txt").read_text().split())
    print("%-6s %6s %8.1f %8.2f %9.3f %8d %8s"
          % (Q, tot, agg, perpair, dt95, pse_a - pse_b,
             "%d pairs" % len(means)))

# CPU deltas across the ladder
print("\nagent CPU (jiffies/s of one core, HZ=100):")
tags = [k for k in cpu]
for i in range(1, len(tags)):
    a, b = cpu[tags[i - 1]], cpu[tags[i]]
    w = float(b[1]) - float(a[1])
    dr = int(b[2].split("=")[1]) - int(a[2].split("=")[1])
    dx = int(b[3].split("=")[1]) - int(a[3].split("=")[1])
    print("  %-10s rx=%.0f%% tx=%.0f%%" % (tags[i], dr / w, dx / w))
