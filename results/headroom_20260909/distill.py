#!/usr/bin/env python3
"""results/h<pct>/ -> data/headroom.csv, one tidy row per point.

metrics
  goodput_gbps            application goodput of all 24 flow-sets, per h
  fct_p50 / p99 / p999    short-flow completion time, per (h, size)
  fct_cdf                 the whole distribution, per (h, size)

The times are one-way completion times: ib_write_lat's ping-pong is symmetric,
so the half round trip it reports IS the completion time and is not doubled.
A size whose probe outlived the load is refused, not averaged in.
usage: distill.py
"""
import csv, os, re

BASE = os.path.dirname(os.path.abspath(__file__))
HS = (4, 6, 8, 10, 12)
SIZES = (4096, 65536)
POINTS = 200


def inside_load(R, size):
    w = os.path.join(R, "windows.txt")
    if not os.path.exists(w):
        return True, ""
    for l in open(w):
        f = l.split()
        if f and f[0] == str(size):
            d = dict(kv.split("=") for kv in f[1:])
            over = int(d["end"]) - int(d["load_end"])
            return over <= 0, ("ended %d s after the load" % over if over > 0 else "")
    return True, ""


def samples(path):
    if not os.path.exists(path):
        return []
    return [float(l.split(",")[1]) for l in open(path)
            if re.match(r"^\s*\d+,\s*[\d.]+\s*$", l)]


rows = []
for h in HS:
    R = os.path.join(BASE, "results", "h%d" % h)
    if not os.path.isdir(R):
        continue
    g = [l for l in open(os.path.join(R, "goodput.txt"))] if os.path.exists(os.path.join(R, "goodput.txt")) else []
    tot = [l for l in g if l.startswith("TOTAL")]
    if tot:
        rows.append((h, "", "goodput_gbps", "", tot[0].split()[1]))
    for size in SIZES:
        ok, why = inside_load(R, size)
        if not ok:
            print("   DROPPED h=%d %d B: %s" % (h, size, why))
            continue
        s = samples(os.path.join(R, "probe_%d.txt" % size))
        if not s:
            continue
        n = len(s)
        for i in range(POINTS + 1):
            k = min(n - 1, round(i * (n - 1) / POINTS))
            rows.append((h, size, "fct_cdf", "%.4f" % s[k], "%.4f" % (100.0 * (k + 1) / n)))
        for q, name in ((0.50, "fct_p50"), (0.99, "fct_p99"), (0.999, "fct_p999")):
            rows.append((h, size, name, "", "%.3f" % s[min(n - 1, int(q * n))]))
        rows.append((h, size, "samples", "", str(n)))

os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
with open(os.path.join(BASE, "data", "headroom.csv"), "w") as f:
    w = csv.writer(f); w.writerow(["h_pct", "size", "metric", "x", "value"]); w.writerows(rows)
print("ok", len(rows), "rows")
for h in HS:
    g = [v for hh, s_, m, _, v in rows if hh == h and m == "goodput_gbps"]
    q = {(s_, m): v for hh, s_, m, _, v in rows if hh == h and m.startswith("fct_p")}
    if g:
        print("   h=%2d%%  goodput %7s G   4 KB p50 %8s us p99 %8s   64 KB p50 %8s us p99 %8s"
              % (h, g[0], q.get((4096, "fct_p50")), q.get((4096, "fct_p99")),
                 q.get((65536, "fct_p50")), q.get((65536, "fct_p99"))))
