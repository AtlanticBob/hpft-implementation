#!/usr/bin/env python3
"""fig/pairjitter_*.png: why the steady state ripples, TCP against RDMA.

The two runs are the same shape - two senders competing for one destination
VM's 50 G cap, ten flows each, 30 s, no physical congestion - so the only
difference between the columns is which executor carries the fence.

  pairjitter_compare.png   per column: attributed rate (20 ms), the sender's
                           fence R, the virtual queue

The forward-path delay is NOT measurable from these runs: in closed loop the
pace and the arrival are linked through the whole loop, so cross-correlating
them returns the oscillation's phase lag, not a transport delay. step_delay.py
measures it open-loop instead.

usage: pair_jitter.py <tag> [<tag> ...]"""
import json, os, sys, time
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from distill import bin_mean
W0, W1 = 5.0, 29.0                      # steady window inside the 30 s run
BIN_S = 0.1                             # drawing granularity, matching the wire panel


def load(tag):
    R = os.path.join(BASE, "results", tag)
    z = float(open(os.path.join(R, "t0.txt")).read()) + float(open(os.path.join(R, "warm.txt")).read())
    rx = [json.loads(l) for l in open(os.path.join(R, "rx.jsonl")) if l.strip()]
    rx = [(x["ts"] - z, x) for x in rx if W0 <= x["ts"] - z <= W1]
    fs = sorted({k for _, x in rx for k in x["r"]})
    T = np.array([t for t, _ in rx])
    A = np.array([[x["r"].get(f, 0) / 1e9 for _, x in rx] for f in fs])
    Q = np.array([[x.get("d", {}).get(f, 0.0) for _, x in rx] for f in fs])
    fence = {}
    for h in sorted({f.split("/")[0] for f in fs}):
        p = os.path.join(R, f"tx_{h}.jsonl")
        if not os.path.exists(p):
            continue
        rows = [json.loads(l) for l in open(p) if l.strip()]
        for f in fs:
            xs = [(x["ts"] - z, x["R"] / 1e9, x["pace"] / 1e9)
                  for x in rows if x.get("fs") == f and W0 <= x["ts"] - z <= W1]
            if len(xs) > 30:
                fence[f] = (np.array([a for a, _, _ in xs]), np.array([b for _, b, _ in xs]),
                            np.array([c for _, _, c in xs]))
    return dict(tag=tag, fs=fs, T=T, A=A, Q=Q, fence=fence, cls=fs[0].rsplit("|", 1)[1])


runs = [load(t) for t in sys.argv[1:]]
n = len(runs)
COL = {"rdma": "#d62728", "tcp": "#1f77b4"}
stamp = time.strftime("%Y-%m-%d %H:%M:%S")

# ---- figure 1: three rows per run ----------------------------------------
fig, ax = plt.subplots(3, n, figsize=(6.2 * n, 9.0), squeeze=False)
for j, r in enumerate(runs):
    c = COL[r["cls"]]
    for i, f in enumerate(r["fs"]):
        ls = "-" if i == 0 else "--"

        # The 20 ms trace is one 10 ms rate window per point, so most of what
        # it shows is measurement granularity rather than delivered rate: it
        # averages down as 1/sqrt(window), the signature of independent noise,
        # whereas a loop that is hunting does not. The bold line is the same
        # data over SMOOTH_MS - a drawing choice, nothing in the control path
        # is averaged.
        bt, bv = bin_mean(r["T"], r["A"][i], BIN_S)
        ax[0][j].plot(bt, bv, color=c, lw=1.0, ls=ls, alpha=0.9, label=f.split(">")[0])
        qt, qv = bin_mean(r["T"], r["Q"][i], BIN_S)
        ax[2][j].plot(qt, qv, color=c, lw=0.8, ls=ls, alpha=0.85)
        if f in r["fence"]:
            ft, fR, _ = r["fence"][f]
            ax[1][j].plot(ft, fR, color=c, lw=0.8, ls=ls, alpha=0.85)
    sd = " / ".join(f"{r['A'][i].std():.2f}" for i in range(len(r["fs"])))
    sd100 = " / ".join(f"{bin_mean(r['T'], r['A'][i], BIN_S)[1].std():.2f}"
                       for i in range(len(r["fs"])))
    ax[0][j].axhline(25.0, color="k", ls=":", lw=1.0)
    ax[0][j].set_title(f"{r['tag']}\nattributed rate sd = {sd100} G at 100 ms ({sd} G at the 20 ms log)", fontsize=9)
    ax[0][j].set_ylim(10, 40); ax[0][j].legend(fontsize=7, ncol=2); ax[0][j].grid(alpha=0.3)
    fsd = " / ".join(f"{r['fence'][f][1].std():.2f}" for f in r["fs"] if f in r["fence"])
    ax[1][j].axhline(25.0, color="k", ls=":", lw=1.0)
    ax[1][j].set_title(f"fence R sd = {fsd} G", fontsize=9)
    ax[1][j].set_ylim(10, 40); ax[1][j].grid(alpha=0.3)
    qm = " / ".join(f"{r['Q'][i].mean():.2f}" for i in range(len(r["fs"])))
    ax[2][j].set_title(f"virtual queue mean = {qm} ms (criterion <= 1 ms)", fontsize=9)
    ax[2][j].set_ylim(0, 20); ax[2][j].grid(alpha=0.3); ax[2][j].set_xlabel("experiment time (s)")
ax[0][0].set_ylabel("attributed rate per flow-set (Gb/s), 100 ms")
ax[1][0].set_ylabel("sender fence R (Gb/s)")
ax[2][0].set_ylabel("virtual queue (ms), 100 ms mean")
fig.suptitle(f"two tenants competing for one 50 G destination VM, 10 flows each, no physical congestion    drawn {stamp}", fontsize=11)
fig.tight_layout()
os.makedirs(os.path.join(BASE, "fig"), exist_ok=True)
fig.savefig(os.path.join(BASE, "fig", "pairjitter_compare.png"), dpi=130)

print("ok")
