#!/usr/bin/env python3
"""Jakiro DHTB convergence over time: GBN vs SR, at a 20G / 1:1 quota.

Receiver-side hardware vport counters (vport-meter, 1 Hz) for the managed
VF: rx_ib = RDMA class delivered, rx_eth = TCP class delivered. Jakiro
CE-marks greedy RoCE (never drops it), so RDMA is reined in only when the
sender's DCQCN loop reacts to the CNPs.

Left (GBN):  the firmware DCQCN is bistable here -- across repeats RDMA
             settles at either ~10G (fair) or 28-45G (out of control);
             the shown run is an out-of-control instance.
Right (SR):  DCQCN reacts stably; RDMA converges to its 10G share every
             time (3/3 repeats).
"""
import csv, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = os.path.dirname(os.path.abspath(__file__))
TCP_C, RDMA_C = "#EDB877", "#3D5A80"

def series(run, vport=0):
    rows = [r for r in csv.reader(open(f"{BASE}/runs_jakiro/{run}/vpm_series.csv"))
            if r and r[0] != "ts" and int(r[1]) == vport]
    tns = [int(r[2]) for r in rows]
    ib = [int(r[3]) for r in rows]
    eth = [int(r[4]) for r in rows]
    t = [float(r[0]) for r in rows]
    ts, rd, tc = [], [], []
    for k in range(1, len(rows)):
        dt = (tns[k] - tns[k-1]) / 1e9
        if dt <= 0:
            continue
        ts.append(t[k])
        rd.append((ib[k] - ib[k-1]) * 8 / dt / 1e9)
        tc.append((eth[k] - eth[k-1]) * 8 / dt / 1e9)
    act = [i for i in range(len(ts)) if rd[i] + tc[i] > 2]
    lo, hi = act[0], act[-1]
    t0 = ts[lo]
    return ([ts[i]-t0 for i in range(lo, hi+1)], rd[lo:hi+1], tc[lo:hi+1])

PANELS = [("gbn_cold", "(a) Go-Back-N  (RDMA repeats: 28 / 10 / 45G — bistable)"),
          ("srseq_A", "(b) Selective Repeat  (RDMA repeats: 10 / 10 / 10G)")]

fig, axes = plt.subplots(1, 2, figsize=(9.2, 3.4), sharey=True)
for ax, (run, title) in zip(axes, PANELS):
    t, rd, tc = series(run)
    # cut the last sample (flow stop skew)
    t, rd, tc = t[:-1], rd[:-1], tc[:-1]
    ax.plot(t, rd, color=RDMA_C, linewidth=1.9, label="RDMA class (RoCE)")
    ax.plot(t, tc, color=TCP_C, linewidth=1.9, label="TCP class")
    ax.axhline(10, color="#c0392b", linestyle="--", linewidth=1.0, zorder=1)
    ax.set_xlabel("Time (s)", fontsize=9)
    ax.set_title(title, fontsize=8.5)
    ax.set_xlim(0, 57)
    ax.tick_params(labelsize=8.5)
    ax.grid(axis="y", color="#e0e0e0", linewidth=0.5, zorder=0)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
axes[0].set_ylim(0, 56)
axes[0].set_ylabel("Delivered bandwidth\nat the VM (Gb/s)", fontsize=9)
axes[0].annotate("weighted share 10G (w=1:1)", (56, 11.2), ha="right",
                 fontsize=7.5, color="#c0392b")
axes[1].legend(fontsize=8.5, frameon=False, loc="center right")
fig.tight_layout()
for ext in ("png", "pdf"):
    fig.savefig(f"{BASE}/fig_jakiro_ts.{ext}", dpi=200)
print("fig_jakiro_ts written")
