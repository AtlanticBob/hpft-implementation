#!/usr/bin/env python3
"""Batch A analysis: per-stage tick health vs flow-set count.

Reads timeline.txt (stage boundaries), sc_rx.jsonl (dt_s = achieved period
per sampled tick, nfs, read_ms), sc_rx_journal.txt (ticks/s prints),
sc_tx.jsonl (tus = sender tree recompute us), cpu_samples.txt.
"""
import json
import re
import sys
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")


def pct(v, q):
    if not v:
        return float("nan")
    s = sorted(v)
    return s[min(len(s) - 1, int(len(s) * q))]


stages = []          # (name, t_start)
for line in (DIR / "timeline.txt").read_text().splitlines():
    ts, name = line.split()
    if name.endswith("_start") or name in ("batchA_drain",):
        stages.append((name.replace("_start", ""), float(ts)))
windows = [(n, t0, stages[i + 1][1]) for i, (n, t0) in enumerate(stages[:-1])]

rx = [json.loads(l) for l in (DIR / "sc_rx.jsonl").read_text().splitlines()]
tx = []
for l in (DIR / "sc_tx.jsonl").read_text().splitlines():
    try:
        tx.append(json.loads(l))
    except json.JSONDecodeError:
        pass

# journal ticks/s: anchor agent-relative t= to wall clock via jsonl ts range
jt = []
for line in (DIR / "sc_rx_journal.txt").read_text().splitlines():
    m = re.search(r"t=(\d+)s ticks/s=(\d+)", line)
    if m:
        jt.append((int(m.group(1)), int(m.group(2))))

cpu = []
for line in (DIR / "cpu_samples.txt").read_text().splitlines():
    tag, ts, rxf, txf = line.split()
    cpu.append((tag, float(ts), int(rxf.split("=")[1]), int(txf.split("=")[1])))

print("%-8s %5s %6s %9s %9s %9s %8s %7s %7s"
      % ("stage", "nfs", "recs", "dt_p50ms", "dt_p95ms", "dt_maxms",
         "read_ms", "tusp50", "tusp95"))
for name, t0, t1 in windows:
    seg = [r for r in rx if t0 + 10 <= r["ts"] < t1]   # skip 10s transient
    if not seg:
        continue
    nfs = pct([r["nfs"] for r in seg], 0.5)
    dts = [r["dt_s"] * 1e3 for r in seg]
    rms = pct([r["read_ms"] for r in seg], 0.5)
    tseg = [t.get("tus") for t in tx
            if t0 + 10 <= t.get("ts", 0) < t1 and t.get("tus") is not None]
    print("%-8s %5d %6d %9.3f %9.3f %9.3f %8.2f %7s %7s"
          % (name, nfs, len(seg), pct(dts, 0.5), pct(dts, 0.95), max(dts),
             rms,
             "%.0f" % pct(tseg, 0.5) if tseg else "-",
             "%.0f" % pct(tseg, 0.95) if tseg else "-"))

# CPU% between consecutive samples (jiffies -> % of one core, HZ=100)
print("\ncpu%% (segment ending at tag):")
for (tag0, ts0, r0, x0), (tag1, ts1, r1, x1) in zip(cpu, cpu[1:]):
    w = ts1 - ts0
    print("  %-10s rx=%.1f%% tx=%.1f%%" % (tag1, (r1 - r0) / w, (x1 - x0) / w))

# per-fs mean wire rate in the last full-load window (stage4)
s4 = [r for r in rx if windows[-1][1] + 10 <= r["ts"] < windows[-1][2]]
if s4:
    acc = {}
    for r in s4:
        for f, v in r["r"].items():
            acc.setdefault(f, []).append(v)
    print("\nstage4 per-fs mean wire (G), %d fs:" % len(acc))
    for f in sorted(acc):
        print("  %-34s %6.2f" % (f, sum(acc[f]) / len(acc[f]) / 1e9))
