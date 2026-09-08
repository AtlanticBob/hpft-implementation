#!/usr/bin/env python3
"""fig/<base>_executor.png: the RDMA executor's own account of the bucket
(design 6.4), one panel per sender host, on the experiment clock.
  per RDMA flow-set of that host: R from the ledger (dashed), the sum of the
  CC rates of the QPs drawing tokens (solid), the sum of the paced rates the
  executor programmed (dotted), one colour per flow-set. Where the solid line
  is above the dashed one the bucket binds and the dotted line should sit on
  the dashed one; where it is below, the dotted line should follow the solid
  one. The last panel is criterion 6 itself: per flow-set, the mean and the
  95th percentile of paced/R over the samples in which every QP of the set was
  drawing, against the thresholds the criterion uses.

A line breaks only where the flow-set was genuinely absent from the readback
for longer than GAP_S; a single missing sample (the sampler's own pass now and
then overrunning its one second period) is drawn through, not gapped.

Reads only data/<tag>_executor.csv and data/<tag>_executor_summary.csv
(distill.py output).

usage: executor.py <tag>[,<tag>,...] [<base>]
Several tags are averaged point by point on the 1 s clock and drawn as one
figure; the title names every tag that went in."""
import csv, os, sys, time
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tags = [t for t in sys.argv[1].split(",") if t]
base = sys.argv[2] if len(sys.argv) > 2 else tags[0].split("_")[0]
D = os.path.join(BASE, "data")
GAP_S = 3.0                 # longer than this and the flow-set really was gone
EX_OVER_MEAN, EX_OVER_P95 = 1.03, 1.10      # criterion 6, same as distill.py

series = {}      # (host, fsid, field) -> list of (t, v) over all runs
summ = {}        # fsid -> list of (mean paced/R, p95 paced/R) over runs
end = 0.0
for tag in tags:
    with open(os.path.join(D, f"{tag}_executor.csv")) as f:
        for r in csv.DictReader(f):
            t = float(r["t"]); end = max(end, t)
            for fld in ("R_gbps", "paced_gbps", "cc_live_gbps"):
                series.setdefault((r["host"], r["fsid"], fld), []).append((t, float(r[fld])))
    with open(os.path.join(D, f"{tag}_executor_summary.csv")) as f:
        for r in csv.DictReader(f):
            if r["all_live_mean_paced_over_R"]:
                summ.setdefault(r["fsid"], []).append((float(r["all_live_mean_paced_over_R"]),
                                                       float(r["all_live_p95_paced_over_R"])))
hosts = sorted({h for h, _, _ in series})
if not hosts:
    print("no executor samples"); sys.exit(0)


def xy(key):
    """(t, v) for one line. Several runs are averaged on a 1 s grid so they can
    be overlaid; a single run is drawn on its own sample times, which is what
    keeps an over-long sampling pass from opening a hole in the line."""
    pts = sorted(series.get(key, []))
    if not pts:
        return np.array([]), np.array([])
    if len(tags) > 1:
        acc = {}
        for t, v in pts:
            acc.setdefault(int(np.floor(t)), []).append(v)
        pts = [(k + 0.5, float(np.mean(v))) for k, v in sorted(acc.items())]
    T, V = [], []
    for i, (t, v) in enumerate(pts):
        if i and t - pts[i - 1][0] > GAP_S:
            T.append(np.nan); V.append(np.nan)
        T.append(t); V.append(v)
    return np.array(T), np.array(V)


fig, ax = plt.subplots(len(hosts) + 1, 1, figsize=(12, 3.0 * len(hosts) + 3.2))
ax = np.atleast_1d(ax)
for j, h in enumerate(hosts):
    a = ax[j]
    fss = sorted({f for hh, f, _ in series if hh == h})
    rmax = max([max(v for _, v in series[(h, f, "R_gbps")]) for f in fss] + [1.0])
    # at a managed port the tenant CC sees no congestion and its per-QP rate
    # sits at line rate, so the sum over a set's QPs is hundreds of Gb/s; the
    # part of the picture that matters is where it dips below R
    CC_CLIP = 2.5 * rmax
    for i, f in enumerate(fss):
        c = f"C{i % 10}"
        a.plot(*xy((h, f, "R_gbps")), color=c, ls="--", lw=0.9)
        T, V = xy((h, f, "cc_live_gbps"))
        a.plot(T, np.minimum(V, CC_CLIP), color=c, ls="-", lw=1.1,
               label=f.split(">")[0] + ">" + f.split(">")[1].split("|")[0])
        a.plot(*xy((h, f, "paced_gbps")), color=c, ls=":", lw=1.4)
    a.set_ylabel("Gb/s"); a.set_title(f"{h}: R (--), sum CC of drawing QPs (—, clipped at {CC_CLIP:.0f}), sum paced (··)", fontsize=9)
    a.grid(alpha=0.3); a.set_xlim(0, end); a.set_ylim(0, CC_CLIP * 1.05)
    if fss:
        a.legend(fontsize=6, ncol=4, loc="upper right")
    if j == len(hosts) - 1:
        a.set_xlabel("time in the run (s)")
b = ax[-1]
names = sorted(summ)
x = np.arange(len(names))
mn = [np.mean([v[0] for v in summ[n]]) for n in names]
p95 = [np.mean([v[1] for v in summ[n]]) for n in names]
b.bar(x - 0.2, mn, 0.4, label="mean paced / R", color="#1f77b4")
b.bar(x + 0.2, p95, 0.4, label="95th percentile paced / R", color="#ff7f0e")
b.axhline(1.0, color="k", ls=":", lw=1)
b.axhline(EX_OVER_MEAN, color="#1f77b4", ls="--", lw=1)
b.axhline(EX_OVER_P95, color="#ff7f0e", ls="--", lw=1)
b.text(len(names) - 0.4, EX_OVER_P95, " criterion 6 limits", fontsize=7, va="bottom", ha="right")
b.set_xticks(x); b.set_xticklabels([n.split("|")[0] for n in names], rotation=60, ha="right", fontsize=6)
b.set_ylabel("paced / R"); b.set_title("Criterion 6: over the samples in which every QP of the set drew tokens", fontsize=9)
b.set_ylim(0, max([1.2] + p95) * 1.15); b.grid(alpha=0.3, axis="y"); b.legend(fontsize=8)
title = tags[0] if len(tags) == 1 else f"mean of {len(tags)}: " + ", ".join(tags)
fig.suptitle(f"{title}    RDMA executor, 1 s device readback    drawn {time.strftime('%Y-%m-%d %H:%M:%S')}", fontsize=10)
fig.tight_layout(rect=(0, 0, 1, 0.97))
os.makedirs(os.path.join(BASE, "fig"), exist_ok=True)
fig.savefig(os.path.join(BASE, "fig", f"{base}_executor.png"), dpi=130); print("ok")
