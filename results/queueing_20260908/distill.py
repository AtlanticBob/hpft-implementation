#!/usr/bin/env python3
"""results/<arm>_<range>/ -> data/queueing.csv, one tidy row per point.

The switch bins into a fixed number of buckets, so one run resolves only the
decade its range covers.  The same load is therefore run once per decade (low,
mid, high) per arm and the CDFs are joined at their shared bin edges; the join
is checked, not assumed - `overlap_max_gap_pct` in data/stitch_check.csv is the
largest disagreement between two runs at an edge they both measure.

metrics
  buffer      cumulative % of TIME the egress queue was no deeper than bin_lo
  latency     cumulative % of PACKETS that waited no longer than bin_lo
  probe_cdf   the application probe's round-trip CDF, from every sample
              (ib_write_lat -H), subsampled to PROBE_POINTS ranks
  probe_rtt_us, probe_iters, goodput_gbps, ecn_marked, queue_drops

ib_write_lat reports HALF the ping-pong, so every probe figure here is
doubled to a round trip.  That is not an assumption: with the CC alone the
switch's own histograms put the queue at 53-57 MB and the wait at 2.0-2.2 ms,
while the tool reported 1.06 ms - exactly half - and the reverse path is
uncongested, so the round trip must be the 2.1 ms the switch measured.
usage: distill.py
"""
import csv, json, os, re

BASE = os.path.dirname(os.path.abspath(__file__))
ARMS = ("hpft", "cc")
RANGES = ("low", "mid", "high", "top", "top2", "top3")
PROBE_POINTS = 400
# The snapshot window recorded by run.sh starts with the flows but ends when
# quick.sh has finished tearing down, so it runs on past the 12 s of load; the
# idle seconds would all land in the empty-queue bucket and swell it (both arms
# read ~60 % there before this was trimmed). Snapshots are one second each, so
# the load is the first LOAD_SECONDS of them, in time order.
LOAD_SECONDS = 11
OPEN_TOP = "open"          # marks the row carrying the open top bin's mass


def read_hist(d, key):
    """{upper edge (int) or OPEN_TOP: count} for one snapshot file's histogram."""
    out = {}
    for name, v in d[key]["swp37s0"]["0"]["data"].items():
        hi = name.split(":")[1]
        out[OPEN_TOP if hi == "*" else int(hi)] = v
    return out


rows, checks = [], []
for arm in ARMS:
    merged = {"buffer": {}, "latency": {}}     # edge -> cumulative %
    seen = {"buffer": {}, "latency": {}}       # edge -> [cum % per range], for the overlap check
    for rng in RANGES:
        R = os.path.join(BASE, "results", f"{arm}_{rng}")
        if not os.path.isdir(R):
            continue
        snaps = json.load(open(os.path.join(R, "histogram_window.json")))
        snaps = sorted(snaps, key=lambda d: d["timestamp_info"]["start_datetime"])[:LOAD_SECONDS]
        for metric, key in (("buffer", "histogram_tc_info"), ("latency", "histogram_latency_info")):
            agg = {}
            for s in snaps:
                for edge, v in read_hist(s, key).items():
                    agg[edge] = agg.get(edge, 0) + v
            tot = sum(agg.values()) or 1
            cum = 0
            for edge in sorted(e for e in agg if e != OPEN_TOP):
                cum += agg[edge]
                pct = 100.0 * cum / tot
                merged[metric][edge] = pct
                seen[metric].setdefault(edge, []).append(pct)
        # per-run scalars come from the mid pass, the one whose range spans both arms
        if rng == "mid":
            txt = open(os.path.join(R, "probe.txt")).read()
            m = re.search(r"^\s*2\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)", txt, re.M)
            if m:
                rows.append((arm, "", "probe_iters", "", m.group(1)))
                rows.append((arm, "", "probe_rtt_us", "", "%.2f" % (2 * float(m.group(4)))))
            g = [l for l in open(os.path.join(R, "goodput.txt")) if l.startswith("TOTAL")]
            if g:
                rows.append((arm, "", "goodput_gbps", "", g[0].split()[1]))
            pre = dict(kv.split("=") for kv in open(os.path.join(R, "sw_pre.txt")).read().split())
            post = dict(kv.split("=") for kv in open(os.path.join(R, "sw_post.txt")).read().split())
            rows.append((arm, "", "ecn_marked", "", str(int(post["ecn"]) - int(pre["ecn"]))))
            rows.append((arm, "", "queue_drops", "", str(int(post["drops"]) - int(pre["drops"]))))
        # every sample of the probe: -H prints "<rank>, <usec>" already sorted
        smp = [float(l.split(",")[1]) for l in open(os.path.join(R, "probe.txt"))
               if re.match(r"^\s*\d+,\s*[\d.]+\s*$", l)]
        if smp and rng == "mid":
            n = len(smp)
            for i in range(PROBE_POINTS + 1):
                k = min(n - 1, round(i * (n - 1) / PROBE_POINTS))
                rows.append((arm, "", "probe_cdf", "%.4f" % (2 * smp[k]), "%.4f" % (100.0 * (k + 1) / n)))
            rows.append((arm, "", "probe_samples", "", str(n)))
    # the join: enforce monotonicity and record the worst disagreement at a shared edge
    for metric in ("buffer", "latency"):
        gap = max((max(v) - min(v) for v in seen[metric].values() if len(v) > 1), default=0.0)
        checks.append((arm, metric, "%.2f" % gap,
                       str(sum(1 for v in seen[metric].values() if len(v) > 1)),
                       str(len(merged[metric]))))
        run = 0.0
        for edge in sorted(merged[metric]):
            run = max(run, merged[metric][edge])
            rows.append((arm, "", metric, str(edge), "%.4f" % run))

os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
with open(os.path.join(BASE, "data", "queueing.csv"), "w") as f:
    w = csv.writer(f); w.writerow(["arm", "unused", "metric", "x", "value"]); w.writerows(rows)
with open(os.path.join(BASE, "data", "stitch_check.csv"), "w") as f:
    w = csv.writer(f); w.writerow(["arm", "metric", "overlap_max_gap_pct", "shared_edges", "total_edges"])
    w.writerows(checks)
print("ok", len(rows), "rows")
for c in checks:
    print("   overlap check %-5s %-8s worst gap %5s %% over %s shared edges, %s edges total" % c)
