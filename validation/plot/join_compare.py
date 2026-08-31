#!/usr/bin/env python3
"""fig/V2_join_compare.png: the join event before and after the attribution cap.

Three columns because the defect was intermittent: before the fix the receiver
charged the incumbent for the newcomer's bytes in one run out of three, so a
fair picture needs a typical pre-fix run beside the pathological one. All three
are aligned on the join and drawn at 100 ms. usage: join_compare.py"""
import json, os, sys, time
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from distill import bin_mean

RUNS = [("V2_fix_20260831_rep1", 30.0, "before: a run where it did NOT fire\n(2 of 3 looked like this)"),
        ("V2_fix_20260831_rep3", 30.0, "before: the run where it DID fire\n(1 of 3)"),
        ("V2_now_20260831_rep1", 10.0, "after the attribution cap")]
COL = {"rdma": "#d62728", "tcp": "#1f77b4"}
W0, W1 = -1.5, 4.0

fig, ax = plt.subplots(2, 3, figsize=(15, 7), sharex=True,
                       gridspec_kw={"height_ratios": [1.35, 1]})
for j, (tag, ev, title) in enumerate(RUNS):
    R = os.path.join(BASE, "results", tag)
    z = float(open(R + "/t0.txt").read()) + float(open(R + "/warm.txt").read())
    rx = [json.loads(l) for l in open(R + "/rx.jsonl") if l.strip()]
    rx = [(x["ts"] - z - ev, x) for x in rx if W0 <= x["ts"] - z - ev <= W1]
    T = np.array([t for t, _ in rx])
    fs = sorted({k for _, x in rx for k in x["r"]})
    for f in fs:
        cls = f.rsplit("|", 1)[1]
        if cls not in COL:
            continue
        ls = "-" if f.startswith("sgpu01") else "--"
        bt, bv = bin_mean(T, [x["r"].get(f, 0) / 1e9 for _, x in rx], 0.1)
        ax[0][j].plot(bt, bv, color=COL[cls], lw=1.0, ls=ls, alpha=0.85)
        qt, qv = bin_mean(T, [x.get("d", {}).get(f, 0) for _, x in rx], 0.1)
        ax[1][j].plot(qt, qv, color=COL[cls], lw=1.0, ls=ls, alpha=0.85)
    for a in (ax[0][j], ax[1][j]):
        a.axvline(0, color="k", lw=1, ls="--")
        a.grid(alpha=0.3)
    ax[0][j].axhline(23, color="k", ls=":", lw=0.9)
    ax[0][j].axhline(11.5, color="k", ls=":", lw=0.9)
    peak = max(max(x["r"].get(f, 0) for f in fs if f.endswith("|tcp")) for _, x in rx) / 1e9
    qmax = max(max(x.get("d", {}).get(f, 0) for f in fs) for _, x in rx)
    ax[0][j].set_title(f"{title}\nincumbent peak {peak:.1f} G   queue peak {qmax:.0f} ms", fontsize=10)
    ax[0][j].set_ylim(0, 52); ax[1][j].set_ylim(0, 205)
    ax[1][j].set_xlabel("time since the join (s)")
ax[0][0].set_ylabel("attributed rate per flow-set (Gb/s), 100 ms")
ax[1][0].set_ylabel("virtual queue (ms), 100 ms mean")
ax[0][0].plot([], [], color=COL["rdma"], label="RDMA")
ax[0][0].plot([], [], color=COL["tcp"], label="TCP")
ax[0][0].plot([], [], color="k", ls="-", label="incumbent (sgpu01)")
ax[0][0].plot([], [], color="k", ls="--", label="newcomer (sgpu03)")
ax[0][0].plot([], [], color="k", ls=":", label="expected 23 / 11.5 G")
ax[0][0].legend(fontsize=8, loc="upper right", ncol=2)
fig.suptitle("V2 join: what the attribution cap changed    drawn "
             + time.strftime("%Y-%m-%d %H:%M:%S"), fontsize=12)
fig.tight_layout()
fig.savefig(os.path.join(BASE, "fig", "V2_join_compare.png"), dpi=130)
print("ok")
