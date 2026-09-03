#!/usr/bin/env python3
"""fig/<base>_trust.png: what the two executors did with the tenant CC during
the run (V7 and any scenario where a CC becomes binding).
  panel 1  TCP executor: trust per pair from the BPF map, 100 ms (one line per
           pair that carried traffic), all sender hosts
  panel 2  RDMA executor: per pair, the fence (level, dashed) and the CC's own
           rate (cc, solid) and the paced rate (dotted), 1 s samples from the
           device query, one colour per pair
  panel 3  RDMA executor: trust per pair
Reads results/<tag>/trust_<host>.jsonl and rp_<host>.jsonl.

usage: trust.py <tag>[,<tag>,...] [<base>]
Several tags (the repetitions of one scenario) are averaged point by point on
the experiment clock (100 ms for the TCP map, 1 s for the device samples) and
drawn as ONE figure; the title names every tag that went in."""
import json, os, sys, warnings
warnings.filterwarnings("ignore", category=RuntimeWarning)   # all-NaN bins are expected
import time
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tags = [t for t in sys.argv[1].split(",") if t]
base = sys.argv[2] if len(sys.argv) > 2 else tags[0].split("_")[0]


def binned(T, V, grid, step):
    out = np.full(len(grid), np.nan)
    idx = np.floor((np.asarray(T) - grid[0]) / step).astype(int)
    V = np.asarray(V, dtype=float)
    for i in range(len(grid)):
        m = idx == i
        if m.any():
            out[i] = V[m].mean()
    return out


tcp, rp = {}, {}      # (host, key) -> [series per run]; (host, ft) -> [series per run]
for tag in tags:
    R = os.path.join(BASE, "results", tag)
    t0 = float(open(os.path.join(R, "t0.txt")).read()); warm = float(open(os.path.join(R, "warm.txt")).read()); z = t0 + warm
    rows = [l.split() for l in open(os.path.join(R, "flows.txt")) if l.strip() and not l.startswith("#")]
    end = max(float(r[6]) for r in rows); events = sorted({float(r[5]) for r in rows} | {float(r[6]) for r in rows})
    for fn in sorted(os.listdir(R)):
        if fn.startswith("trust_") and fn.endswith(".jsonl"):
            host = fn[6:-6]; per = {}
            for l in open(os.path.join(R, fn)):
                if l.strip():
                    x = json.loads(l); per.setdefault(x["key"], []).append((x["ts"] - z, x["trust"]))
            for k, v in per.items():
                tcp.setdefault((host, k), []).append(v)
        if fn.startswith("rp_") and fn.endswith(".jsonl"):
            host = fn[3:-6]; per = {}
            for l in open(os.path.join(R, fn)):
                if l.strip():
                    x = json.loads(l); per.setdefault(x["ft"], []).append((x["ts"] - z, x))
            for ft, v in per.items():
                rp.setdefault((host, ft), []).append(v)
g100 = np.arange(0, end + 0.05, 0.1); g1 = np.arange(0, end + 0.5, 1.0)
fig, ax = plt.subplots(3, 1, figsize=(12, 9), sharex=True)
for (host, k), runs in sorted(tcp.items()):
    y = np.nanmean([binned([a for a, _ in v], [b for _, b in v], g100, 0.1) for v in runs], axis=0)
    if np.nanmax(y) > 0 or len(tcp) <= 8:
        ax[0].plot(g100, y, lw=0.8, label=f"{host} {k[:8]}")
for i, ((host, ft), runs) in enumerate(sorted(rp.items())):
    c = f"C{i % 10}"
    def mean_of(field, cap=None):
        return np.nanmean([binned([a for a, _ in v],
                                  [min(b[field], cap) if cap else b[field] for _, b in v], g1, 1.0)
                           for v in runs], axis=0)
    ax[1].plot(g1, mean_of("level"), color=c, ls="--", lw=0.9)
    ax[1].plot(g1, mean_of("cc", 60), color=c, ls="-", lw=1.2, label=f"{host} {ft}")
    ax[1].plot(g1, mean_of("paced"), color=c, ls=":", lw=0.9)
    ax[2].plot(g1, mean_of("trust"), color=c, lw=1.0)
ax[0].set_ylabel("TCP executor trust"); ax[0].set_ylim(-0.02, 1.02); ax[0].grid(alpha=0.3)
if ax[0].lines: ax[0].legend(fontsize=6, ncol=4, loc="upper right")
ax[1].set_ylabel("RDMA executor per QP (Gb/s)\n-- fence level, — CC rate (clipped 60), ·· paced"); ax[1].grid(alpha=0.3)
if ax[1].lines: ax[1].legend(fontsize=6, ncol=4, loc="upper right")
ax[2].set_ylabel("RDMA executor trust"); ax[2].set_ylim(-0.02, 1.02); ax[2].grid(alpha=0.3); ax[2].set_xlabel("experiment time (s)")
for e in events:
    if 0 < e < end:
        for a in ax: a.axvline(e, color="gray", lw=0.6, ls="--")
title = tags[0] if len(tags) == 1 else f"mean of {len(tags)}: " + ", ".join(tags)
ax[0].set_xlim(0, end); fig.suptitle(f"{title}    executor trust and the CC next to the fence    drawn {time.strftime('%Y-%m-%d %H:%M:%S')}", fontsize=10); fig.tight_layout()
fig.savefig(os.path.join(BASE, "fig", f"{base}_trust.png"), dpi=130); print("ok")
