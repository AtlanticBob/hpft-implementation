#!/usr/bin/env python3
"""Two questions this campaign answered, one panel each.

(a) Does the coupling work for a CC that is not DCQCN? Class totals under
    each arm, against the 46 G the four RDMA flow-sets are entitled to.
(b) What the coupling depends on: the same 60 G shaped path, allocated
    against the port nameplate and against the capacity that path can
    actually deliver.
"""
from pathlib import Path
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from analyze import rep

TCP, RDMA, FAIR, BAD = "#EDB877", "#3D5A80", "#2a9d5c", "#c0392b"
HERE = Path(__file__).resolve().parent


def cls(tags):
    r = [rep(t)[1] for t in tags]
    return sum(x["rdma"] for x in r) / len(r), sum(x["tcp"] for x in r) / len(r)


ARMS = [("DCQCN\nmin(cc, level)", ["m_write1", "m_write2", "m_write3", "m_write4"]),
        ("DCQCN\nd x level", ["d_write1", "d_write2", "d_write3", "d_write4"]),
        ("ZTR\nd x level,\nZTR's own recovery", ["ztr_write1", "ztr_write2"]),
        ("ZTR\nd x level,\ncoupled recovery", ["ztr2_write1", "ztr2_write2"])]
CORE = [("allocator told\n92 G (nameplate)", ["core1", "core2"]),
        ("...and f_core\nraised to 1/4", ["core_f4"]),
        ("allocator told\n60 G (deliverable)", ["core_cap"])]

fig, (a1, a2) = plt.subplots(1, 2, figsize=(11.5, 4.0),
                             gridspec_kw={"width_ratios": [1.32, 1]})

for ax, rows, share, title in (
        (a1, ARMS, 46.0, "(a) class totals by arm, 100 G path"),
        (a2, CORE, None, "(b) 60 G shaped path, coupled DCQCN")):
    xs = range(len(rows))
    rd = [cls(t)[0] for _, t in rows]
    tc = [cls(t)[1] for _, t in rows]
    ax.bar([x - 0.19 for x in xs], rd, 0.38, color=RDMA, edgecolor="black",
           lw=0.4, zorder=3, label="RDMA (4 flow-sets)")
    ax.bar([x + 0.19 for x in xs], tc, 0.38, color=TCP, edgecolor="black",
           lw=0.4, zorder=3, label="TCP (4 flow-sets)")
    for x, (v, w) in enumerate(zip(rd, tc)):
        ax.text(x - 0.19, v + 1.0, "%.0f" % v, ha="center", fontsize=8)
        ax.text(x + 0.19, w + 1.0, "%.0f" % w, ha="center", fontsize=8)
    ax.set_xticks(list(xs))
    ax.set_xticklabels([n for n, _ in rows], fontsize=8)
    ax.set_ylabel("class total (Gb/s)")
    ax.set_title(title, fontsize=10)
    ax.grid(axis="y", color="#e0e0e0", lw=0.5, zorder=0)
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    ax.legend(fontsize=7, frameon=False,
              loc="upper right" if ax is a2 else "upper left")

a1.axhline(46.0, color=FAIR, ls="--", lw=0.8)
a1.text(1.55, 48.0, "each class is entitled to 46 G", color=FAIR, fontsize=7.5)
a1.set_ylim(0, 78)
a2.axhline(26.8, color=FAIR, ls="--", lw=0.8)
a2.text(0.62, 22.0, "26.8 G each once the\ncapacity is known", color=FAIR, fontsize=7.5)
a2.set_ylim(0, 72)
a2.text(-0.42, 62.5, "the coupling divides what the allocator\n"
                     "believes it has; belief is the input", color=BAD, fontsize=8)
fig.subplots_adjust(left=0.07, right=0.99, top=0.91, bottom=0.17, wspace=0.22)
fig.savefig(HERE / "fig_arms.png", dpi=200)
fig.savefig(HERE / "fig_arms.pdf")
print("RDMA/TCP by arm:", [(n, "%.1f/%.1f" % cls(t)) for n, t in ARMS])
print("core:", [(n, "%.1f/%.1f" % cls(t)) for n, t in CORE])
