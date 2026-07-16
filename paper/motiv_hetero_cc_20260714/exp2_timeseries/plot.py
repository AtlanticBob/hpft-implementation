#!/usr/bin/env python3
"""1.2.2 figure: per-class delivered bandwidth over time.

No runs of its own -- it re-reads the 1 Hz vport-meter series recorded by
exp1's [96%, 143%] rung (../exp1_ecn_band/runs/band96-143_{gbn,sr}_run3).

Source semantics: receiver-side hardware vport octets summed over the 4
VFs, i.e. what arrived on the wire (headers included; for GBN, discarded
out-of-order arrivals included). Trimmed at 57 s: the last seconds hold
the stop skew (perftest exits before iperf3, TCP briefly takes the link).
"""
import collections, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(BASE, "..", "exp1_ecn_band", "runs")
TCP_C, GBN_C, SR_C = "#EDB877", "#3D5A80", "#A8C5DA"


def series(tag):
    pts = collections.defaultdict(list)
    for ln in open(os.path.join(RUNS, tag, "vpm_series.csv")):
        if ln.startswith("ts"):
            continue
        _, vi, tn, ib, eth = ln.strip().split(",")
        pts[int(vi)].append((int(tn), int(ib), int(eth)))
    n = min(len(v) for v in pts.values())
    t, rib, reth = [], [], []
    for k in range(1, n):
        dt = sum(pts[v][k][0] - pts[v][k-1][0] for v in pts) / len(pts) / 1e9
        t.append(k)
        rib.append(sum(pts[v][k][1] - pts[v][k-1][1] for v in pts) * 8 / dt / 1e9)
        reth.append(sum(pts[v][k][2] - pts[v][k-1][2] for v in pts) * 8 / dt / 1e9)
    act = [i for i in range(len(t)) if rib[i] + reth[i] > 5]
    t0 = t[act[0]]
    keep = [k for k in range(act[0], act[-1] + 1) if t[k] - t0 <= 57]
    return ([t[k] - t0 for k in keep], [rib[k] for k in keep],
            [reth[k] for k in keep])


PANELS = [("band96-143_gbn_run3", "(a) Go-Back-N", GBN_C, "RDMA-GBN"),
          ("band96-143_sr_run3", "(b) Selective Repeat", SR_C, "RDMA-SR")]

fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.9), sharey=True)
for ax, (tag, title, rc, rlabel) in zip(axes, PANELS):
    t, rib, reth = series(tag)
    ax.plot(t, reth, color=TCP_C, linewidth=1.8, label="TCP")
    ax.plot(t, rib, color=rc, linewidth=1.8, label=rlabel)
    ax.set_title(title, fontsize=9)
    ax.set_xlabel("Time (s)", fontsize=8.5)
    ax.set_ylim(0, 100)
    ax.set_xlim(0, 57)
    ax.tick_params(labelsize=8)
    ax.grid(axis="y", color="#e0e0e0", linewidth=0.5, zorder=0)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.legend(fontsize=7.5, frameon=False, loc="center right")
axes[0].set_ylabel("Delivered bandwidth\nat receiver (Gb/s)", fontsize=8.5)
fig.tight_layout()
for ext in ("png", "pdf"):
    fig.savefig(os.path.join(BASE, f"fig_timeseries.{ext}"), dpi=200)
print("exp2_timeseries/fig_timeseries written")
