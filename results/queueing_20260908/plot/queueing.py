#!/usr/bin/env python3
"""fig/queueing.png: what the shaper does to the receiver port's queue.

Left  - how often the egress buffer at swp37s0 is at or above a threshold,
        from the ASIC's own occupancy histogram (one sample per 1024 ns).
Right - how often a packet leaving that port waited at or above a threshold,
        from the ASIC's egress-latency histogram over the same window.
Both are read as "share of samples at or above x", so lower is better, and
the thresholds are the bin edges the switch itself uses.
Reads data/queueing.csv (distill.py output) only.
"""
import csv, os
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
C = {"hpft": "#3D5A80", "cc": "#c0392b", "ink": "#1a1a1a"}
LBL = {"hpft": "HyperFront (ledger + token bucket)", "cc": "tenant CC alone"}
ARMS = ("cc", "hpft")

rows = list(csv.DictReader(open(os.path.join(BASE, "data", "queueing.csv"))))
val = lambda m, a: float(next(r["value"] for r in rows if r["metric"] == m and r["arm"] == a))


def share_at_or_above(metric, arm, lo):
    sel = [r for r in rows if r["metric"] == metric and r["arm"] == arm]
    tot = sum(float(r["count"]) for r in sel) or 1.0
    return 100.0 * sum(float(r["count"]) for r in sel if float(r["bin_lo"]) >= lo) / tot


PANELS = [
    ("buffer", "egress buffer occupancy", [(4032, "4 KB"), (397248, "400 KB"), (790464, "800 KB\n(ECN $K_{min}$)"),
                                           (1576896, "1.6 MB"), (3149760, "3.1 MB\n(≈ $K_{max}$)")]),
    ("latency", "egress queueing delay", [(2048, "2 µs"), (18432, "18 µs"), (34816, "35 µs"),
                                          (67584, "68 µs"), (133120, "133 µs")]),
]
fig, ax = plt.subplots(1, 2, figsize=(11, 4.0))
for a, (metric, title, ticks) in enumerate(PANELS):
    x = np.arange(len(ticks)); w = 0.38
    for i, arm in enumerate(ARMS):
        y = [max(share_at_or_above(metric, arm, lo), 1e-3) for lo, _ in ticks]
        b = ax[a].bar(x + (i - 0.5) * w, y, w, color=C[arm], label=LBL[arm])
        ax[a].bar_label(b, labels=[("%.0f%%" % v) if v >= 10 else ("%.1f%%" % v) if v >= 0.1 else "<0.1%"
                                   for v in y], fontsize=7, padding=2)
    ax[a].set_yscale("log"); ax[a].set_ylim(1e-3, 400)
    ax[a].set_xticks(x); ax[a].set_xticklabels([t for _, t in ticks], fontsize=8)
    ax[a].set_ylabel("share of samples at or above (%)")
    ax[a].set_title(f"{title} at the receiver port", fontsize=10)
    ax[a].grid(alpha=0.3, axis="y", which="major")
    ax[a].legend(fontsize=8, loc="lower left")
fig.suptitle("HyperFront holds the receiver port's queue down; the tenant CC alone fills it", fontsize=11)
sub1 = "Same load in both arms: 24 flow-sets (12 RDMA, 12 Cubic, three senders) saturating one 200 G receiver port, 12 s."
sub2 = (f"2 B RDMA round-trip probe across the same queue: {val('probe_rtt_us','hpft'):.1f} µs vs "
        f"{val('probe_rtt_us','cc'):.0f} µs (2.8 µs idle).   "
        f"Goodput {val('goodput_gbps','hpft'):.1f} G vs {val('goodput_gbps','cc'):.1f} G.   "
        f"ECN-marked frames {val('ecn_marked','hpft')/1e3:.0f}k vs {val('ecn_marked','cc')/1e6:.1f}M.")
fig.text(0.5, 0.055, sub1, ha="center", fontsize=8, color=C["ink"])
fig.text(0.5, 0.015, sub2, ha="center", fontsize=8, color=C["ink"])
fig.tight_layout(rect=(0, 0.10, 1, 0.93))
os.makedirs(os.path.join(BASE, "fig"), exist_ok=True)
fig.savefig(os.path.join(BASE, "fig", "queueing.png"), dpi=140)
print("ok")
