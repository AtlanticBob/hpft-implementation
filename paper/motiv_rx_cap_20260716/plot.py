#!/usr/bin/env python3
"""1.3 figure: class allocation at a receiver bandwidth cap.

Three arms: GBN, SR with a 512-packet TX PSN window (default,
LOG_TX_PSN_WINDOW=9), SR with 1024 (LOG_TX_PSN_WINDOW=10). Stacked bar per
arm: TCP bottom, RDMA top. Every bar is the mean of 3 runs.

Colors follow the 1.2 figures: TCP soft orange, RDMA dark blue (GBN) /
light blue (SR).
"""
import csv, os
from collections import defaultdict
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

BASE = os.path.dirname(os.path.abspath(__file__))
# SR arms share one colour: the x labels (w=512 / w=1024) already separate them
TCP_C, GBN_C, SR_C = "#EDB877", "#3D5A80", "#A8C5DA"
SR2_C = SR_C

ARMS = [("gbn", "GBN", GBN_C),
        ("sr", "SR (w=512)", SR_C),
        ("sr1024", "SR (w=1024)", SR2_C)]

rows = list(csv.DictReader(open(f"{BASE}/runs.csv")))
def m(arm, k):
    v = [float(r[k]) for r in rows if r["arm"] == arm]
    return sum(v) / len(v) if v else 0.0

present = [a for a in ARMS if any(r["arm"] == a[0] for r in rows)]

fig, ax = plt.subplots(figsize=(4.6, 3.4))
for i, (arm, label, rc) in enumerate(present):
    tc, rd = m(arm, "tcp_gbps"), m(arm, "rdma_gbps")
    ax.bar(i, tc, width=0.5, color=TCP_C, edgecolor="black",
           linewidth=0.4, zorder=3)
    ax.bar(i, rd, bottom=tc, width=0.5, color=rc, edgecolor="black",
           linewidth=0.4, zorder=3)

ax.set_xticks(range(len(present)))
ax.set_xticklabels([a[1] for a in present], fontsize=8.5)
ax.set_ylim(0, 22)
ax.set_ylabel("Bandwidth at the receiver (Gb/s)", fontsize=9)
ax.tick_params(labelsize=8.5)
ax.grid(axis="y", color="#e0e0e0", linewidth=0.5, zorder=0)
for s in ("top", "right"):
    ax.spines[s].set_visible(False)

# one legend entry per colour (the SR arms share a colour; the x labels
# already say which window each bar is)
handles, labels, seen = [Patch(facecolor=TCP_C, edgecolor="black", linewidth=0.4)], ["TCP"], set()
for arm, label, rc in present:
    if rc in seen:
        continue
    seen.add(rc)
    handles.append(Patch(facecolor=rc, edgecolor="black", linewidth=0.4))
    labels.append("RDMA-GBN" if arm == "gbn" else "RDMA-SR")
ax.legend(handles, labels, fontsize=8, ncol=3, frameon=False,
          loc="upper center", bbox_to_anchor=(0.5, 1.14))

fig.tight_layout(rect=[0, 0, 1, 0.90])
for ext in ("png", "pdf"):
    fig.savefig(f"{BASE}/fig_rx_cap.{ext}", dpi=200)
print("fig_rx_cap written")
