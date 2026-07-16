#!/usr/bin/env python3
"""1.2.3 figure: class bandwidth vs CC recovery aggressiveness.

Reads runs.csv (produced by tools/parse.py); every bar is the mean of the
3 runs for that cell. Operating point: [71%, 120%] marking band (the one
rung where DCQCN demonstrably steers both arms).

Caveat carried by the data, not the figure: share at this rung drifts on a
tens-of-seconds scale, so per-cell min-max spread is wide (see runs.csv).
The bars are for qualitative reading -- notably the total height, which
collapses for rdma-fast/GBN.
"""
import csv, os
from collections import defaultdict
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

BASE = os.path.dirname(os.path.abspath(__file__))
TCP_C, GBN_C, SR_C, INK = "#EDB877", "#3D5A80", "#A8C5DA", "#1a1a1a"

CONFIGS = ["rdma-slow", "default", "rdma-fast", "tcp-slow", "tcp-fast"]
LABELS = ["RDMA\nslow", "default", "RDMA\nfast", "TCP\nslow", "TCP\nfast"]

acc = defaultdict(list)
for r in csv.DictReader(open(os.path.join(BASE, "runs.csv"))):
    acc[(r["config"], r["arm"])].append(
        (float(r["rdma_gbps"]), float(r["tcp_gbps"])))

def mean(cfg, arm):
    v = acc[(cfg, arm)]
    return (sum(x[0] for x in v) / len(v), sum(x[1] for x in v) / len(v))

fig, ax = plt.subplots(figsize=(6.0, 3.6))
W = 0.26
for arm, dx, rc in (("gbn", -0.17, GBN_C), ("sr", 0.17, SR_C)):
    for i, cfg in enumerate(CONFIGS):
        rd, tc = mean(cfg, arm)
        ax.bar(i + dx, tc, width=W, color=TCP_C, edgecolor="black",
               linewidth=0.4, zorder=3)
        ax.bar(i + dx, rd, bottom=tc, width=W, color=rc, edgecolor="black",
               linewidth=0.4, zorder=3)
        if rd < 1.0:
            ax.annotate(f"{rd:.2f}", (i + dx, tc + rd),
                        xytext=(i + dx, tc + rd + 7.5), textcoords="data",
                        ha="center", va="bottom", fontsize=7, color=INK,
                        arrowprops=dict(arrowstyle="-", color="#888888",
                                        lw=0.6, shrinkA=0, shrinkB=1))

ax.set_xticks(range(len(CONFIGS)))
ax.set_xticklabels(LABELS, fontsize=8)
ax.set_ylim(0, 112)
ax.set_yticks([0, 20, 40, 60, 80, 100])
ax.tick_params(labelsize=8.5)
ax.set_xlabel("CC recovery aggressiveness", fontsize=9)
ax.set_ylabel("Bandwidth (Gb/s)", fontsize=9)
ax.grid(axis="y", color="#e0e0e0", linewidth=0.5, zorder=0)
for s in ("top", "right"):
    ax.spines[s].set_visible(False)
ax.legend([Patch(facecolor=TCP_C, edgecolor="black", linewidth=0.4),
           Patch(facecolor=GBN_C, edgecolor="black", linewidth=0.4),
           Patch(facecolor=SR_C, edgecolor="black", linewidth=0.4)],
          ["TCP", "RDMA-GBN", "RDMA-SR"],
          fontsize=8, ncol=3, frameon=False, loc="lower left",
          bbox_to_anchor=(0.0, 1.0))
fig.tight_layout()
for ext in ("png", "pdf"):
    fig.savefig(os.path.join(BASE, f"fig_cc_scan.{ext}"), dpi=200)
print("exp3_cc_scan/fig_cc_scan written")
