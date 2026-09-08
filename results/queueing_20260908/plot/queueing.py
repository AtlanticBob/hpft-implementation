#!/usr/bin/env python3
"""fig/queueing.png: what the shaper does to the receiver port's queue.

Three CDFs, each running the full 0 to 100 %:
  left    how deep the egress queue at swp37s0 was (switch ASIC's occupancy
          histogram, one sample per 128 ns) - "no deeper than x for this share
          of the time"
  middle  how long a packet leaving that port waited (the ASIC's egress-latency
          histogram, same window) - "this share of packets waited no longer"
  right   what an application saw: every round trip of a 2 B RDMA ping-pong on
          a spare VF pair crossing the same queue (ib_write_lat -H), so this
          curve is the empirical CDF of the samples themselves, not binned

The ASIC bins into a fixed number of buckets and only the range is settable,
so the two switch curves are step functions joined from several runs of the
same load, one per decade, down to 49 KB and 2 us per bucket where the
distribution actually lives; the join is checked in data/stitch_check.csv.
The lowest bucket is [0, first edge] and nothing can be said inside it, so a
curve stays at 0 % until that edge.  The ranges were extended until the top
bucket was empty, so no ceiling has to be assumed; if any mass were left the
legend would say so.

The probe curve is doubled from what ib_write_lat reports, which is half a
ping-pong (data/queueing.csv documents the check).

Reads data/queueing.csv (distill.py output) only.
"""
import csv, os
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
C = {"hpft": "#3D5A80", "cc": "#c0392b", "ref": "#888888", "ink": "#1a1a1a"}
LBL = {"hpft": "HyperFront (ledger + token bucket)", "cc": "tenant CC alone"}
ARMS = ("cc", "hpft")

rows = list(csv.DictReader(open(os.path.join(BASE, "data", "queueing.csv"))))
val = lambda m, a: float(next(r["value"] for r in rows if r["metric"] == m and r["arm"] == a))


def curve(metric, arm, scale=1.0):
    pts = sorted((float(r["x"]) * scale, float(r["value"]))
                 for r in rows if r["metric"] == metric and r["arm"] == arm)
    return [p[0] for p in pts], [p[1] for p in pts]


PANELS = [
    ("buffer", 1 / 1e3, 0.05, "queue occupancy (KB)",
     "Switch: how deep the egress queue was",
     "share of time the queue was no deeper (%)",
     [(800, "ECN $K_{min}$"), (3200, "ECN $K_{max}$")], "lower right", True),
    ("latency", 1 / 1e3, 0.05, "queueing delay (µs)",
     "Switch: how long a packet waited there",
     "share of packets that waited no longer (%)", [], "center left", True),
    ("probe_cdf", 1.0, None, "round-trip time (µs)",
     "Application: 2 B RDMA ping-pong on the same queue",
     "share of round trips no slower (%)", [(5.3, "idle port")], "center left", False),
]
fig, ax = plt.subplots(1, 3, figsize=(15.5, 4.4))
for a, (metric, scale, x0, xlab, title, ylab, marks, legend_loc, stepped) in enumerate(PANELS):
    for arm in ARMS:
        xs, ys = curve(metric, arm, scale)
        if stepped:
            lab = LBL[arm] if ys[-1] >= 99.95 else f"{LBL[arm]}  ({100 - ys[-1]:.1f}% beyond the range measured)"
            ax[a].step([x0] + xs, [0.0] + ys, where="post", color=C[arm], lw=2.0,
                       marker="o", ms=2.6, label=lab)
        else:
            ax[a].plot(xs, ys, color=C[arm], lw=2.0, label=LBL[arm])
    for v, t in marks:
        ax[a].axvline(v, color=C["ref"], ls=":", lw=1.1)
        ax[a].text(v * 1.09, 46, t, fontsize=7.5, color=C["ink"], rotation=90, va="center")
    ax[a].set_xscale("log")
    ax[a].set_ylim(-2, 104); ax[a].set_yticks(range(0, 101, 20))
    ax[a].set_xlabel(xlab); ax[a].set_ylabel(ylab)
    ax[a].set_title(title, fontsize=10)
    ax[a].grid(alpha=0.3, which="both")
    ax[a].legend(fontsize=7.5, loc=legend_loc, framealpha=0.92)
fig.suptitle("HyperFront holds the receiver port's queue down; the tenant CC alone keeps it full", fontsize=12)
sub = ("Same load in both arms: 24 flow-sets saturating one 200 G receiver port for 12 s.   "
       f"Application goodput {val('goodput_gbps','hpft'):.1f} G vs {val('goodput_gbps','cc'):.1f} G.   "
       f"ECN-marked frames {val('ecn_marked','hpft')/1e3:.0f} k vs {val('ecn_marked','cc')/1e6:.2f} M.   "
       "Switch curves are joined from one run per decade; the application curve is every sample.")
fig.text(0.5, 0.02, sub, ha="center", fontsize=8, color=C["ink"])
fig.tight_layout(rect=(0, 0.06, 1, 0.93))
os.makedirs(os.path.join(BASE, "fig"), exist_ok=True)
fig.savefig(os.path.join(BASE, "fig", "queueing.png"), dpi=140)
print("ok")
