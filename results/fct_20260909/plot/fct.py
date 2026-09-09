#!/usr/bin/env python3
"""fig/fct.png: how long a short flow takes to finish while the port is full.

left    the completion-time CDF at each size, both arms, each curve running
        the full 0 to 100 %
right   the median and the 99th percentile against flow size, which is the
        line a reader takes away: the shaper's curve is serialisation, the
        unshaped one is the queue

Reads data/fct.csv (distill.py output) only. The times are one-way completion
times of a write of that size: ib_write_lat's ping-pong is symmetric, so the
half round trip it reports IS the completion time and is not doubled.
"""
import csv, os
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
C = {"hpft": "#3D5A80", "cc": "#c0392b", "ink": "#1a1a1a"}
LBL = {"hpft": "HyperFront (ledger + token pool)", "cc": "tenant CC alone"}
SIZES = [4096, 16384, 65536, 262144, 1048576]
SZL = {4096: "4 KB", 16384: "16 KB", 65536: "64 KB", 262144: "256 KB", 1048576: "1 MB"}

rows = list(csv.DictReader(open(os.path.join(BASE, "data", "fct.csv"))))


def cdf(arm, size):
    p = sorted((float(r["x"]), float(r["value"])) for r in rows
               if r["arm"] == arm and r["size"] == str(size) and r["metric"] == "fct_cdf")
    return [a for a, _ in p], [b for _, b in p]


def q(arm, size, name):
    v = [r["value"] for r in rows if r["arm"] == arm and r["size"] == str(size) and r["metric"] == name]
    return float(v[0]) if v else None


fig, ax = plt.subplots(1, 2, figsize=(13.5, 4.6))
for i, size in enumerate(SIZES):
    for arm in ("cc", "hpft"):
        x, y = cdf(arm, size)
        if not x:
            continue
        ax[0].plot(x, y, color=C[arm], lw=1.6, alpha=0.35 + 0.16 * i,
                   label=(LBL[arm] if i == 0 else None))
        if y and x:
            # label each curve where it crosses 50 %, staggered by size so
            # neighbouring sizes do not print on top of one another
            ax[0].annotate(SZL[size], (x[len(x) // 2], 92 - 7 * i), fontsize=7, color=C[arm],
                           ha="center", va="center",
                           bbox=dict(fc="white", ec="none", alpha=0.75, pad=0.6))
ax[0].set_xscale("log"); ax[0].set_ylim(-2, 104); ax[0].set_yticks(range(0, 101, 20))
ax[0].set_xlabel("flow completion time (µs)")
ax[0].set_ylabel("share of flows finished by then (%)")
ax[0].set_title("Every flow, both arms, five sizes (darker = larger)", fontsize=10)
ax[0].grid(alpha=0.3, which="both"); ax[0].legend(fontsize=8, loc="lower right", framealpha=0.92)

for arm in ("cc", "hpft"):
    for name, ls, mk in (("fct_p50", "-", "o"), ("fct_p99", "--", "s")):
        xs = [s for s in SIZES if q(arm, s, name) is not None]
        ys = [q(arm, s, name) for s in xs]
        if xs:
            ax[1].plot(xs, ys, ls, color=C[arm], marker=mk, ms=5, lw=1.8,
                       label=f"{LBL[arm]}, {'median' if name=='fct_p50' else '99th pct'}")
ax[1].set_xscale("log"); ax[1].set_yscale("log")
ax[1].set_xticks(SIZES); ax[1].set_xticklabels([SZL[s] for s in SIZES])
ax[1].set_xlabel("flow size"); ax[1].set_ylabel("completion time (µs)")
ax[1].set_title("Median and tail against size", fontsize=10)
ax[1].grid(alpha=0.3, which="both"); ax[1].legend(fontsize=7.5, loc="upper left", framealpha=0.92)

g = {a: q(a, "", "goodput_gbps") for a in ("cc", "hpft")}
fig.suptitle("A short flow finishes two orders of magnitude sooner when the shaper keeps the receiver port's queue short", fontsize=12)
sub = ("Same load in both arms: 24 flow-sets saturating one 200 G receiver port.   "
       "The probe crosses the same egress queue on a spare VF pair; times are one-way completions of a write of that size.")
fig.text(0.5, 0.02, sub, ha="center", fontsize=8, color=C["ink"])
fig.tight_layout(rect=(0, 0.06, 1, 0.93))
os.makedirs(os.path.join(BASE, "fig"), exist_ok=True)
fig.savefig(os.path.join(BASE, "fig", "fct.png"), dpi=140)
print("ok")
