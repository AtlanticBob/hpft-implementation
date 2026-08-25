#!/usr/bin/env python3
"""Join/leave step scenario, one panel per arm: incumbent and joiner
flow-sets on dst vf0 (RDMA and TCP), receiver-measured arrival rate,
20 ms samples, 28-72 s window around the join (t~32 s) and the leaves
(TCP joiner ~63 s, RDMA joiner ~66 s)."""
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HERE, "results")
ARMS = [a.split("=", 1) for a in sys.argv[1:]] or [
    ("st_vq", "vq law (log-tracking), prior = service rate"),
    ("st_mimd_none", "pure-factor MIMD, every record"),
    ("st_mimd_phase", "pure-factor MIMD, phase-gated 24 ms"),
    ("st_mimd_rand", "pure-factor MIMD, sparse MI p=T/24ms"),
    ("st_vq_prior", "vq law, prior = share hint"),
    ("st_vq_split", "vq law, prior = share hint + sender-reported split"),
]
FS = [("sgpu01/vf0>sgpu02/vf0|rdma", "#3D5A80", "-", "incumbent RDMA"),
      ("sgpu03/vf0>sgpu02/vf0|rdma", "#A8C5DA", "-", "joiner RDMA"),
      ("sgpu01/vf0>sgpu02/vf0|tcp", "#EDB877", "-", "incumbent TCP"),
      ("sgpu03/vf0>sgpu02/vf0|tcp", "#c99a4a", "-", "joiner TCP")]
arms = [(t, l) for t, l in ARMS if os.path.exists(os.path.join(D, t + "_rx.jsonl"))]
fig, axes = plt.subplots(len(arms), 1, figsize=(11, 2.2 * len(arms)), sharex=True)
if len(arms) == 1:
    axes = [axes]
for ax, (tag, label) in zip(axes, arms):
    t0 = float(open(os.path.join(D, tag + "_t0.txt")).read())
    rows = [json.loads(l) for l in open(os.path.join(D, tag + "_rx.jsonl")) if l.strip()]
    rows = [r for r in rows if 28 <= r["ts"] - t0 <= 72]
    for f, c, ls, name in FS:
        xs = [r["ts"] - t0 for r in rows if f in r["r"]]
        ys = [r["r"][f] / 1e9 for r in rows if f in r["r"]]
        ax.plot(xs, ys, color=c, linestyle=ls, linewidth=0.7, label=name)
    for y in (23.0, 11.5):
        ax.axhline(y, color="#2a9d5c", linestyle="--", linewidth=0.7)
    ax.set_ylim(0, 40)
    ax.set_ylabel("Gbps")
    ax.set_title(label, fontsize=9, loc="left")
    ax.grid(axis="y", color="#e0e0e0", linewidth=0.5, zorder=0)
axes[0].legend(loc="upper right", fontsize=7, ncol=4)
axes[-1].set_xlabel("time (s)")
fig.suptitle("join at ~32 s (8 flow-sets from sgpu03), TCP joiner leaves ~63 s, RDMA joiner ~66 s; "
             "dashed = C'/8 and C'/16", fontsize=9)
fig.tight_layout()
fig.savefig(os.path.join(HERE, "fig_step.png"), dpi=140)
fig.savefig(os.path.join(HERE, "fig_step.pdf"))
print("wrote fig_step.png for", [t for t, _ in arms])
