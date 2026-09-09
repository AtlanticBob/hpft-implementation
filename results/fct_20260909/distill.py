#!/usr/bin/env python3
"""results/<arm>/ -> data/fct.csv, one tidy row per point.

metrics
  fct_cdf     the empirical CDF of the probe's completion times, one point per
              subsampled rank, per (arm, size)
  fct_p50, fct_p99, fct_p999   the same distribution's quantiles, per size
  goodput_gbps, ecn_marked, queue_drops   per arm

ib_write_lat's ping-pong is symmetric - both ends write the same size - so
the number it reports, half the round trip, is the one-way completion time of
a write of that size. That is the FCT and it is NOT doubled here; the queueing
bundle's round-trip figure is the one that needs doubling.
usage: distill.py
"""
import csv, os, re

BASE = os.path.dirname(os.path.abspath(__file__))
ARMS = ("cc", "hpft")
SIZES = (4096, 16384, 65536, 262144, 1048576)
POINTS = 300


def inside_load(R, size):
    """Was this size's probe finished before the load was? A probe that runs
    on past the flows measures an empty port and its distribution is two
    regimes glued together."""
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
    """-H prints '<rank>, <usec>', already sorted."""
    if not os.path.exists(path):
        return []
    return [float(l.split(",")[1]) for l in open(path)
            if re.match(r"^\s*\d+,\s*[\d.]+\s*$", l)]


rows = []
for arm in ARMS:
    R = os.path.join(BASE, "results", arm)
    for size in SIZES:
        ok, why = inside_load(R, size)
        if not ok:
            print("   DROPPED %-5s %8d B: %s" % (arm, size, why))
            continue
        s = samples(os.path.join(R, "probe_%d.txt" % size))
        if not s:
            continue
        n = len(s)
        for i in range(POINTS + 1):
            k = min(n - 1, round(i * (n - 1) / POINTS))
            rows.append((arm, size, "fct_cdf", "%.4f" % s[k], "%.4f" % (100.0 * (k + 1) / n)))
        for q, name in ((0.50, "fct_p50"), (0.99, "fct_p99"), (0.999, "fct_p999")):
            rows.append((arm, size, name, "", "%.3f" % s[min(n - 1, int(q * n))]))
        rows.append((arm, size, "samples", "", str(n)))
    g = [l for l in open(os.path.join(R, "goodput.txt"))] if os.path.exists(os.path.join(R, "goodput.txt")) else []
    tot = [l for l in g if l.startswith("TOTAL")]
    if tot:
        rows.append((arm, "", "goodput_gbps", "", tot[0].split()[1]))
    for f, name in (("sw_pre.txt", None), ("sw_post.txt", None)):
        pass
    try:
        pre = dict(kv.split("=") for kv in open(os.path.join(R, "sw_pre.txt")).read().split())
        post = dict(kv.split("=") for kv in open(os.path.join(R, "sw_post.txt")).read().split())
        rows.append((arm, "", "ecn_marked", "", str(int(post["ecn"]) - int(pre["ecn"]))))
        rows.append((arm, "", "queue_drops", "", str(int(post["drops"]) - int(pre["drops"]))))
    except Exception:
        pass

os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
with open(os.path.join(BASE, "data", "fct.csv"), "w") as f:
    w = csv.writer(f); w.writerow(["arm", "size", "metric", "x", "value"]); w.writerows(rows)
print("ok", len(rows), "rows")
for arm in ARMS:
    for size in SIZES:
        q = {m: v for a, s_, m, _, v in rows if a == arm and s_ == size and m.startswith("fct_p")}
        if q:
            print("   %-5s %8d B   p50 %9s us   p99 %9s us   p99.9 %9s us"
                  % (arm, size, q.get("fct_p50"), q.get("fct_p99"), q.get("fct_p999")))
