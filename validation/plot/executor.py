#!/usr/bin/env python3
"""fig/<base>_executor.png: the RDMA executor's own account of the bucket
(design 6.4), one panel per sender host, on the experiment clock.
  per RDMA flow-set of that host: R from the ledger (dashed), the sum of the
  CC rates of the QPs drawing tokens (solid), the sum of the paced rates the
  executor programmed (dotted), one colour per flow-set. Where the solid line
  is above the dashed one the bucket binds and the dotted line should sit on
  the dashed one; where it is below, the dotted line should follow the solid
  one. A last panel gives, per flow-set, the steady-state ratios paced/R and
  cc_live/R as bars.
Reads only data/<tag>_executor.csv and data/<tag>_executor_summary.csv
(distill.py output).

usage: executor.py <tag>[,<tag>,...] [<base>]
Several tags are averaged point by point on the 1 s clock and drawn as one
figure; the title names every tag that went in."""
import csv, os, sys, time
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tags = [t for t in sys.argv[1].split(",") if t]
base = sys.argv[2] if len(sys.argv) > 2 else tags[0].split("_")[0]
D = os.path.join(BASE, "data")

series = {}      # (host, fsid, field) -> list of (t, v) over all runs
summ = {}        # fsid -> list of (paced/R, cc_live/R) over runs
end = 0.0
for tag in tags:
    with open(os.path.join(D, f"{tag}_executor.csv")) as f:
        for r in csv.DictReader(f):
            t = float(r["t"]); end = max(end, t)
            for fld in ("R_gbps", "paced_gbps", "cc_live_gbps"):
                series.setdefault((r["host"], r["fsid"], fld), []).append((t, float(r[fld])))
    with open(os.path.join(D, f"{tag}_executor_summary.csv")) as f:
        for r in csv.DictReader(f):
            summ.setdefault(r["fsid"], []).append((float(r["mean_paced_over_R"]), float(r["mean_cc_live_over_R"])))
hosts = sorted({h for h, _, _ in series})
if not hosts:
    print("no executor samples"); sys.exit(0)
grid = np.arange(0, end + 1.0, 1.0)
# At a managed port the tenant CC sees no congestion and its per-QP rate sits
# at line rate, so the sum over a set's QPs is hundreds of Gb/s; the
# interesting part of the picture is where it dips below R. Clip for drawing.
RATIO_CLIP = 5.0


def binned(pts):
    out = np.full(len(grid), np.nan)
    if not pts:
        return out
    T = np.array([t for t, _ in pts]); V = np.array([v for _, v in pts])
    idx = np.floor(T - grid[0]).astype(int)
    for i in range(len(grid)):
        m = idx == i
        if m.any():
            out[i] = V[m].mean()
    return out


fig, ax = plt.subplots(len(hosts) + 1, 1, figsize=(12, 3.2 * len(hosts) + 3.5))
ax = np.atleast_1d(ax)
for j, h in enumerate(hosts):
    a = ax[j]
    fss = sorted({f for hh, f, _ in series if hh == h})
    rmax = max([max(v for _, v in series[(h, f, "R_gbps")]) for f in fss] + [1.0])
    CC_CLIP = 2.5 * rmax        # clip at 2.5 x the largest R of this panel
    for i, f in enumerate(fss):
        c = f"C{i % 10}"
        a.plot(grid, binned(series[(h, f, "R_gbps")]), color=c, ls="--", lw=0.9)
        a.plot(grid, np.minimum(binned(series[(h, f, "cc_live_gbps")]), CC_CLIP), color=c, ls="-", lw=1.1, label=f.split(">")[0] + ">" + f.split(">")[1].split("|")[0])
        a.plot(grid, binned(series[(h, f, "paced_gbps")]), color=c, ls=":", lw=1.4)
    a.set_ylabel(f"{h} executor (Gb/s)\n-- R   — sum CC of drawing QPs (clipped {CC_CLIP:.0f})   ·· sum paced")
    a.grid(alpha=0.3); a.set_xlim(0, end); a.set_ylim(0, CC_CLIP * 1.05)
    if fss:
        a.legend(fontsize=6, ncol=4, loc="upper right")
b = ax[-1]
names = sorted(summ)
x = np.arange(len(names))
pr = [np.mean([v[0] for v in summ[n]]) for n in names]
cr = [np.mean([v[1] for v in summ[n]]) for n in names]
b.bar(x - 0.2, pr, 0.4, label="paced / R", color="#1f77b4")
b.bar(x + 0.2, np.minimum(cr, RATIO_CLIP), 0.4, label=f"CC of drawing QPs / R (clipped at {RATIO_CLIP:.0f})", color="#ff7f0e")
for xi, v in zip(x, cr):
    if v > RATIO_CLIP:
        b.text(xi + 0.2, RATIO_CLIP + 0.05, "%.0f" % v, ha="center", fontsize=6)
b.axhline(1.0, color="k", ls=":", lw=1)
b.set_xticks(x); b.set_xticklabels([n.split("|")[0] for n in names], rotation=60, ha="right", fontsize=6)
b.set_ylabel("steady-state ratio to R"); b.set_ylim(0, RATIO_CLIP + 0.6); b.grid(alpha=0.3, axis="y"); b.legend(fontsize=8)
title = tags[0] if len(tags) == 1 else f"mean of {len(tags)}: " + ", ".join(tags)
fig.suptitle(f"{title}    RDMA executor, 1 s device readback    drawn {time.strftime('%Y-%m-%d %H:%M:%S')}", fontsize=10)
fig.tight_layout()
os.makedirs(os.path.join(BASE, "fig"), exist_ok=True)
fig.savefig(os.path.join(BASE, "fig", f"{base}_executor.png"), dpi=130); print("ok")
