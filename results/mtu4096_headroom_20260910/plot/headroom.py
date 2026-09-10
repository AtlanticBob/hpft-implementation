#!/usr/bin/env python3
"""fig/headroom.png: what giving away capacity buys.

left    the trade itself - goodput against short-flow completion time, one
        point per h, the design point marked. A point that is below AND to
        the left of another is strictly better.
right   both quantities against h, so the knee is visible directly

Reads data/headroom.csv (distill.py output) only. Times are one-way
completion times of a write of that size (ib_write_lat's ping-pong is
symmetric, so its half round trip IS the completion time).
"""
import csv, os
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
rows = list(csv.DictReader(open(os.path.join(BASE, "data", "headroom.csv"))))
HS = sorted({int(r["h_pct"]) for r in rows})
C4, C64, DES = "#3D5A80", "#c0392b", "#e8b33c"


def val(h, size, metric):
    v = [r["value"] for r in rows if int(r["h_pct"]) == h and r["size"] == str(size) and r["metric"] == metric]
    return float(v[0]) if v else None


g = {h: val(h, "", "goodput_gbps") for h in HS}
p50 = {s: {h: val(h, s, "fct_p50") for h in HS} for s in (4096, 65536)}
p99 = {s: {h: val(h, s, "fct_p99") for h in HS} for s in (4096, 65536)}

fig, ax = plt.subplots(1, 2, figsize=(13.5, 4.6))

for size, c, nm in ((4096, C4, "4 KB"), (65536, C64, "64 KB")):
    xs = [p50[size][h] for h in HS]; ys = [g[h] for h in HS]
    ax[0].plot(xs, ys, "-o", color=c, ms=6, lw=1.6, label=f"{nm} flow, median")
    for h, x, y in zip(HS, xs, ys):
        ax[0].annotate(f"h={h}%", (x, y), fontsize=7.5, color=c,
                       xytext=(4, 4), textcoords="offset points")
ax[0].scatter([p50[4096][8]], [g[8]], s=190, facecolors="none", edgecolors=DES, lw=2.2, zorder=5)
ax[0].scatter([p50[65536][8]], [g[8]], s=190, facecolors="none", edgecolors=DES, lw=2.2, zorder=5,
              label="the design point, h = 8 %")
ax[0].set_xscale("log")
ax[0].set_xlabel("short-flow completion time, median (µs)   ← better")
ax[0].set_ylabel("application goodput (G)   better ↑")
ax[0].set_title("The trade: every point is one value of h", fontsize=10)
ax[0].grid(alpha=0.3, which="both"); ax[0].legend(fontsize=8, loc="lower right", framealpha=0.92)

a2 = ax[1]; a3 = a2.twinx()
a2.plot(HS, [g[h] for h in HS], "-o", color="#444444", ms=6, lw=2, label="goodput (left)")
for size, c, nm in ((4096, C4, "4 KB"), (65536, C64, "64 KB")):
    a3.plot(HS, [p50[size][h] for h in HS], "-o", color=c, ms=5, lw=1.6, label=f"{nm} median (right)")
    a3.plot(HS, [p99[size][h] for h in HS], "--s", color=c, ms=4, lw=1.2, alpha=0.75,
            label=f"{nm} 99th pct (right)")
a2.axvline(8, color=DES, ls=":", lw=2)
a2.annotate("design point", (8, min(g.values())), fontsize=8, color="#8a6a12",
            xytext=(6, 6), textcoords="offset points")
a2.set_xlabel("headroom h (%)"); a2.set_ylabel("application goodput (G)")
a3.set_yscale("log"); a3.set_ylabel("completion time (µs)")
a2.set_xticks(HS); a2.set_title("Both against h: the knee is at 8 %", fontsize=10)
a2.grid(alpha=0.3)
h1, l1 = a2.get_legend_handles_labels(); h2, l2 = a3.get_legend_handles_labels()
a2.legend(h1 + h2, l1 + l2, fontsize=7, loc="center left", framealpha=0.92)

fig.suptitle("Headroom is the one knob that trades throughput for latency directly", fontsize=12)
fig.text(0.5, 0.02,
         "Same load at every h: 24 flow-sets saturating one 200 G receiver port. The ledger hands out (1-h)C, "
         "so h is capacity deliberately left unallocated.", ha="center", fontsize=8, color="#1a1a1a")
fig.tight_layout(rect=(0, 0.06, 1, 0.93))
os.makedirs(os.path.join(BASE, "fig"), exist_ok=True)
fig.savefig(os.path.join(BASE, "fig", "headroom.png"), dpi=140)
print("ok")
