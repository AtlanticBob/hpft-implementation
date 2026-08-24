#!/usr/bin/env python3
"""vq-sched demo: four arms of the incast8 regression, one RDMA and one
TCP flow-set (sgpu01/vf0 -> sgpu02/vf0), receiver-measured arrival rate
over the run, plus the steady-state distribution across all 24 flow-sets.
Data: results/*.jsonl (copied from results/regression)."""
import json
import os
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HERE, "results")
ARMS = [("track_i8", "v2 tracking law (baseline)"),
        ("vq_i8", "vq: no hold, eps 5%, D_r 60 ms"),
        ("vqh_i8", "vq: hold 50 ms, eps 5%, D_r 60 ms"),
        ("vqt_i8", "vq: hold 50 ms, eps 2%, D_r 150 ms")]
FS = {"rdma": "sgpu01/vf0>sgpu02/vf0|rdma", "tcp": "sgpu01/vf0>sgpu02/vf0|tcp"}
COL = {"rdma": "#3D5A80", "tcp": "#EDB877"}
SHARE = 184 / 24


def load(tag):
    t0 = float(open(os.path.join(D, tag + "_t0.txt")).read())
    rows = [json.loads(l) for l in open(os.path.join(D, tag + "_rx.jsonl"))
            if l.strip()]
    return t0, [r for r in rows if r["ts"] >= t0]


fig, axes = plt.subplots(len(ARMS), 2, figsize=(11, 9),
                         gridspec_kw={"width_ratios": [3, 1]})
for i, (tag, label) in enumerate(ARMS):
    t0, rows = load(tag)
    ax = axes[i][0]
    for cls, f in FS.items():
        xs = [r["ts"] - t0 for r in rows if f in r["r"]]
        ys = [r["r"][f] / 1e9 for r in rows if f in r["r"]]
        ax.plot(xs, ys, color=COL[cls], linewidth=0.6, label=cls.upper())
    ax.axhline(SHARE, color="#2a9d5c", linestyle="--", linewidth=0.8,
               label="share C'/24")
    ax.set_xlim(0, 88)
    ax.set_ylim(0, 32)
    ax.set_ylabel("Gbps")
    ax.set_title(label, fontsize=10, loc="left")
    ax.grid(axis="y", color="#e0e0e0", linewidth=0.5, zorder=0)
    if i == 0:
        ax.legend(loc="upper right", fontsize=8, ncol=3)
    if i == len(ARMS) - 1:
        ax.set_xlabel("time (s)")
    # steady-state spread over ALL flow-sets, 40-85 s, per class
    st = [r for r in rows if 40 <= r["ts"] - t0 <= 85]
    axb = axes[i][1]
    data, colors = [], []
    for cls in ("tcp", "rdma"):
        vals = []
        for f in st[0]["r"]:
            if f.endswith(cls):
                vals += [r["r"][f] / 1e9 for r in st if f in r["r"]]
        data.append(vals)
        colors.append(COL[cls])
    bp = axb.boxplot(data, vert=True, widths=0.6, patch_artist=True,
                     showfliers=False, whis=(1, 99))
    for patch, c in zip(bp["boxes"], colors):
        patch.set_facecolor(c)
        patch.set_edgecolor("black")
        patch.set_linewidth(0.4)
    axb.set_xticks([1, 2])
    axb.set_xticklabels(["TCP x12", "RDMA x12"], fontsize=8)
    axb.axhline(SHARE, color="#2a9d5c", linestyle="--", linewidth=0.8)
    axb.set_ylim(0, 32)
    axb.set_title("40-85 s, 20 ms samples (1-99%)", fontsize=8, loc="left")
    axb.grid(axis="y", color="#e0e0e0", linewidth=0.5, zorder=0)
    sd = {cls: statistics.pstdev(v) for cls, v in zip(("tcp", "rdma"), data)}
    axb.text(0.05, 0.93, "sd TCP %.2f  RDMA %.2f" % (sd["tcp"], sd["rdma"]),
             transform=axb.transAxes, fontsize=8, color="#1a1a1a", va="top")
fig.suptitle("incast8, 3 senders x 4 dst VMs x {TCP, RDMA}: 24 flow-sets on a "
             "184G root", fontsize=11)
fig.tight_layout()
fig.savefig(os.path.join(HERE, "fig_arms.png"), dpi=140)
fig.savefig(os.path.join(HERE, "fig_arms.pdf"))
print("wrote fig_arms.png/pdf")
