#!/usr/bin/env python3
"""results/<arm>/ -> data/queueing.csv, one tidy row per (arm, metric, bin).

metrics: buffer   (egress buffer occupancy histogram, bytes, per 1024 ns sample)
         latency  (egress queueing delay histogram, ns, same sampling)
         probe_rtt_us, goodput_gbps, ecn_marked, queue_drops  (single values)
usage: distill.py
"""
import csv, json, os, re

BASE = os.path.dirname(os.path.abspath(__file__))
out = []
for arm in ("hpft", "cc"):
    R = os.path.join(BASE, "results", arm)
    snaps = json.load(open(os.path.join(R, "histogram_window.json")))
    agg = {"buffer": {}, "latency": {}}
    for s in snaps:
        for metric, key in (("buffer", "histogram_tc_info"), ("latency", "histogram_latency_info")):
            for b, v in s[key]["swp37s0"]["0"]["data"].items():
                agg[metric][b] = agg[metric].get(b, 0) + v
    for metric, h in agg.items():
        for b, v in h.items():
            lo = int(b.split(":")[0])
            out.append((arm, metric, lo, b, v, ""))
    txt = open(os.path.join(R, "probe.txt")).read()
    m = re.search(r"^\s*2\s+(\d+)\s+([\d.]+)\s", txt, re.M)
    if m:
        out.append((arm, "probe_rtt_us", "", "", "", m.group(2)))
        out.append((arm, "probe_iters", "", "", "", m.group(1)))
    g = [l for l in open(os.path.join(R, "goodput.txt")) if l.startswith("TOTAL")]
    if g:
        out.append((arm, "goodput_gbps", "", "", "", g[0].split()[1]))
    pre = dict(kv.split("=") for kv in open(os.path.join(R, "sw_pre.txt")).read().split())
    post = dict(kv.split("=") for kv in open(os.path.join(R, "sw_post.txt")).read().split())
    out.append((arm, "ecn_marked", "", "", "", str(int(post["ecn"]) - int(pre["ecn"]))))
    out.append((arm, "queue_drops", "", "", "", str(int(post["drops"]) - int(pre["drops"]))))
    out.append((arm, "snapshots", "", "", "", str(len(snaps))))
os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
with open(os.path.join(BASE, "data", "queueing.csv"), "w") as f:
    w = csv.writer(f)
    w.writerow(["arm", "metric", "bin_lo", "bin", "count", "value"])
    w.writerows(out)
print("ok", len(out), "rows")
