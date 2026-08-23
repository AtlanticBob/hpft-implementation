#!/usr/bin/env python3
"""(a) what the arms deliver; (b) why.

Panel (b) is one READ run of the observer arm. The tenant's DCQCN, which
the executor never writes to, free-runs: no longer the actuator, it sits
near line rate and dips - to 0.000 at the extreme - as congestion signals
reach it. Under min(cc, level) each of those dips IS the flow's rate, and
the deep ones are what took the READ arm's QPs. Here they are readings,
and the flow holds its share across all of them.
"""
from pathlib import Path
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
import statistics as st
from analyze import rep, load, flows
from probe_series import rows

TCP, RDMA, FAIR, BAD, GRAY, INK = ("#EDB877", "#3D5A80", "#2a9d5c",
                                   "#c0392b", "#888888", "#1a1a1a")
HERE = Path(__file__).resolve().parent
D_ONE = 1 << 20

ARMS = [("observer\nWRITE", ["o_write%d" % i for i in (1, 2, 3, 4)]),
        ("observer\nREAD", ["o_read1", "o_read2"]),
        ("observer\nboth classes", ["t_write1", "t_write2"]),
        ("min(cc, level)\nWRITE", ["m_write%d" % i for i in (1, 2, 3, 4)]),
        ("min(cc, level)\nREAD", ["m_read1", "m_read2"])]

fig, (a1, a2) = plt.subplots(1, 2, figsize=(11.5, 3.9),
                             gridspec_kw={"width_ratios": [1, 1.3]})

xs = range(len(ARMS))
rd = [st.mean(rep(t)[1]["rdma"] for t in ts) for _, ts in ARMS]
tc = [st.mean(rep(t)[1]["tcp"] for t in ts) for _, ts in ARMS]
a1.bar([x - 0.19 for x in xs], rd, 0.38, color=RDMA, edgecolor="black", lw=0.4,
       zorder=3, label="RDMA (4 flow-sets)")
a1.bar([x + 0.19 for x in xs], tc, 0.38, color=TCP, edgecolor="black", lw=0.4,
       zorder=3, label="TCP (4 flow-sets)")
for x, (v, w) in enumerate(zip(rd, tc)):
    a1.text(x - 0.19, v + 1.0, "%.0f" % v, ha="center", fontsize=8)
    a1.text(x + 0.19, w + 1.0, "%.0f" % w, ha="center", fontsize=8)
a1.axhline(46.0, color=FAIR, ls="--", lw=0.8)
a1.text(1.2, 58, "each class is entitled to 46 G", color=FAIR, fontsize=7.5)
a1.set_xticks(list(xs))
a1.set_xticklabels([n for n, _ in ARMS], fontsize=7.5)
a1.set_ylabel("class total (Gb/s)")
a1.set_ylim(0, 66)
a1.set_title("(a) the CC is untouched in every arm", fontsize=10)
a1.legend(fontsize=7, frameon=False, loc="lower left")

TAG = "o_read2"
P = load(TAG)
k = [f for f, *_ in flows if f.endswith("rdma")][0]
ts = sorted(P[k])
a2.plot(ts, [P[k][x] for x in ts], color=RDMA, lw=1.4,
        label="what the flow got (one RDMA flow-set)")
a2.axhline(11.5, color=FAIR, ls="--", lw=0.8)
a2.text(48, 13.2, "its policy share, 11.5 G", color=FAIR, fontsize=7.5)
a2.set_xlabel("time (s)")
a2.set_ylabel("rate (Gb/s)", color=RDMA)
a2.set_ylim(0, 15.5)

b = a2.twinx()
pr = [r for r in rows(TAG) if r["pair"] == "0"]
b.plot([r["t"] for r in pr], [r["cc"] / D_ONE for r in pr], color=BAD, lw=1.2,
       label="the tenant CC's own rate, untouched")
b.plot([r["t"] for r in pr], [r["d"] / D_ONE for r in pr], color=INK, lw=1.2,
       ls="--", label="what the executor asked for (d)")
b.set_ylabel("fraction", color=INK)
b.set_ylim(-0.03, 1.28)
b.spines["top"].set_visible(False)
a2.set_title("(b) the CC dips; under min() each dip is the rate", fontsize=10)
h1, l1 = a2.get_legend_handles_labels()
h2, l2 = b.get_legend_handles_labels()
a2.legend(h1 + h2, l1 + l2, fontsize=7, frameon=False, loc="lower left",
          ncol=1)
for ax in (a1, a2):
    ax.grid(axis="y", color="#e0e0e0", lw=0.5, zorder=0)
    ax.spines["top"].set_visible(False)
a1.spines["right"].set_visible(False)
fig.subplots_adjust(left=0.07, right=0.93, top=0.9, bottom=0.19, wspace=0.30)
fig.savefig(HERE / "fig_observer.png", dpi=200)
fig.savefig(HERE / "fig_observer.pdf")
print("arms:", [(n, "%.1f/%.1f" % (r, t)) for (n, _), r, t in zip(ARMS, rd, tc)])
