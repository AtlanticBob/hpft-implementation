#!/usr/bin/env python3
"""fig/queueing.png: what the shaper does to the receiver port's queue, as CDFs.

Left  - how deep the egress queue at swp37s0 was, from the ASIC's own
        occupancy histogram: one sample every 1024 ns, so the curve reads
        "the queue was no deeper than x for this share of the time".
Right - how long a packet leaving that port waited, from the ASIC's
        egress-latency histogram over the same window: one sample per packet,
        so the curve reads "this share of packets waited no longer than x".

Each curve runs the full 0 to 100 %.  The switch bins the data itself, so a
curve is a step function whose risers sit on the bin edges, and the two ends
of the range need care:

  - the lowest bin is [0, first edge], so nothing can be said about the
    distribution inside it; the curve stays at 0 % until that edge and jumps
    there.
  - the highest bin is open ("everything above the last edge"), so its mass
    cannot be placed either.  It is drawn as a dashed riser reaching 100 % at
    the physical ceiling: a queue cannot exceed the switch's shared-buffer
    limit for one congested port (alpha_1 = 35.2 MB), and a packet cannot wait
    longer than that queue takes to drain at line rate (35.2 MB / 200 Gb/s =
    1.41 ms).  Dashed means "somewhere in this interval", not "at this point".

Reads data/queueing.csv (distill.py output) only.
"""
import csv, os
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
C = {"hpft": "#3D5A80", "cc": "#c0392b", "ref": "#888888", "ink": "#1a1a1a"}
LBL = {"hpft": "HyperFront (ledger + token bucket)", "cc": "tenant CC alone"}
ARMS = ("cc", "hpft")
SHARED_BUFFER_B = 35.2e6                       # alpha_1: one congested queue's share of the pool
CEIL = {"buffer": SHARED_BUFFER_B, "latency": SHARED_BUFFER_B * 8 / 200e9 * 1e9}

rows = list(csv.DictReader(open(os.path.join(BASE, "data", "queueing.csv"))))
val = lambda m, a: float(next(r["value"] for r in rows if r["metric"] == m and r["arm"] == a))


def cdf(metric, arm, scale):
    """(steps at the finite bin edges, share left in the open top bin).
    A step is (x in display units, cumulative %)."""
    sel = [r for r in rows if r["metric"] == metric and r["arm"] == arm]
    bins = sorted((float(r["bin_lo"]), r["bin"], float(r["count"])) for r in sel)
    tot = sum(b[2] for b in bins) or 1.0
    pts, cum = [], 0.0
    for _, name, cnt in bins:
        hi = name.split(":")[1]
        if hi == "*":
            break
        cum += cnt
        pts.append((int(hi) * scale, 100.0 * cum / tot))
    return pts, 100.0 - pts[-1][1]


PANELS = [
    ("buffer", 1 / 1e3, 0.05, "queue occupancy (KB)",
     "How deep the receiver port's egress queue was",
     "share of time the queue was no deeper (%)",
     [(800, "ECN $K_{min}$"), (3200, "ECN $K_{max}$"), (35200, "buffer limit")], "3.1 MB", "lower right"),
    ("latency", 1 / 1e3, 0.05, "queueing delay (µs)",
     "How long a packet waited at that port",
     "share of packets that waited no longer (%)",
     [(1410, "buffer / 200 G")], "133 µs", "center left"),
]
fig, ax = plt.subplots(1, 2, figsize=(11.5, 4.4))
for a, (metric, scale, x0, xlab, title, ylab, marks, top_edge, legend_loc) in enumerate(PANELS):
    ceil = CEIL[metric] * scale
    for arm in ARMS:
        pts, rest = cdf(metric, arm, scale)
        xs = [x0] + [p[0] for p in pts]        # flat at 0 % through the lowest bin
        ys = [0.0] + [p[1] for p in pts]
        ax[a].step(xs, ys, where="post", color=C[arm], lw=2.2, marker="o", ms=3.5,
                   label=f"{LBL[arm]}\n{rest:.1f}% above {top_edge}")
        ax[a].step([pts[-1][0], ceil], [pts[-1][1], 100.0], where="post",
                   color=C[arm], lw=2.0, ls="--")
    for v, t in marks:
        ax[a].axvline(v, color=C["ref"], ls=":", lw=1.1)
        ax[a].text(v * 1.07, 97, t, fontsize=7.5, color=C["ink"], rotation=90, va="top")
    ax[a].set_xscale("log")
    ax[a].set_xlim(x0, ceil * 1.7)
    ax[a].set_ylim(-2, 104); ax[a].set_yticks(range(0, 101, 20))
    ax[a].set_xlabel(xlab); ax[a].set_ylabel(ylab)
    ax[a].set_title(title, fontsize=10)
    ax[a].grid(alpha=0.3, which="both")
    ax[a].legend(fontsize=7.5, loc=legend_loc, framealpha=0.92)
fig.suptitle("HyperFront holds the receiver port's queue down; the tenant CC alone keeps it full", fontsize=11)
sub1 = ("Same load in both arms: 24 flow-sets saturating one 200 G receiver port for 12 s.  Curves are the switch ASIC's "
        "own histograms; the dashed riser is its open top bin, drawn to the physical ceiling.")
sub2 = (f"2 B RDMA round-trip probe across the same queue: {val('probe_rtt_us','hpft'):.1f} µs vs "
        f"{val('probe_rtt_us','cc'):.0f} µs (2.8 µs on an idle port).   "
        f"Application goodput {val('goodput_gbps','hpft'):.1f} G vs {val('goodput_gbps','cc'):.1f} G.   "
        f"ECN-marked frames {val('ecn_marked','hpft')/1e3:.0f} k vs {val('ecn_marked','cc')/1e6:.2f} M.")
fig.text(0.5, 0.055, sub1, ha="center", fontsize=8, color=C["ink"])
fig.text(0.5, 0.013, sub2, ha="center", fontsize=8, color=C["ink"])
fig.tight_layout(rect=(0, 0.10, 1, 0.93))
os.makedirs(os.path.join(BASE, "fig"), exist_ok=True)
fig.savefig(os.path.join(BASE, "fig", "queueing.png"), dpi=140)
print("ok")
