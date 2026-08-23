#!/usr/bin/env python3
"""The A/B figure: same binary, same scenario, the two coupling arms.

(a) min(cc, level) on READ - the CC ratchets the RDMA flow-sets to zero and
    TCP takes the released share.
(b) rate = d * level on READ - the same CC, the same marking, flows held at
    the policy share.
(c) stall seconds per run across both arms and both verbs.
Colours from hpft-paper/paper/PLOT_STYLE.md.
"""
import json
from pathlib import Path
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

DIR = Path(__file__).resolve().parent / "results"
TCP, RDMA, FAIR, BAD, GRAY = "#EDB877", "#3D5A80", "#2a9d5c", "#c0392b", "#888888"
LS = ["-", "--", "-.", ":"]
C_ROOT = 92.0   # 100G downlink x (1 - headroom 0.08); 8 shares of 11.5G
flows = [("sgpu01/vf%d>sgpu02/vf%d|%s" % (n, n, c), n, c)
         for n in range(4) for c in ("rdma", "tcp")]


def load(tag):
    t0 = float((DIR / f"{tag}_t0.txt").read_text().strip())
    per = {k: {} for k, *_ in flows}
    for jl in (DIR / f"{tag}_rx.jsonl").read_text().splitlines():
        try: rec = json.loads(jl)
        except Exception: continue
        s = int(rec["ts"] - t0)
        for k, *_ in flows:
            per[k].setdefault(s, []).append(rec["r"].get(k, 0) / 1e9)
    return {k: {x: sum(v) / len(v) for x, v in d.items()} for k, d in per.items()}


def stalls(tag):
    P = load(tag)
    xs = sorted(set().union(*[set(v) for v in P.values()]))
    return len([x for x in xs if 5 <= x <= 84
                and any(P[k].get(x, 99) < 3 for k, _, c in flows if c == "rdma")])


fig = plt.figure(figsize=(12, 3.6))
gs = fig.add_gridspec(1, 3, width_ratios=[1, 1, 1.05], wspace=0.28)

for i, (tag, title) in enumerate([
        ("m_read1", "(a) rate = min(cc, level), READ"),
        ("d_read1", "(b) rate = d x level, READ")]):
    ax = fig.add_subplot(gs[0, i])
    P = load(tag)
    for k, n, c in flows:
        xs = sorted(P[k])
        ax.plot(xs, [P[k][x] for x in xs], color=RDMA if c == "rdma" else TCP,
                ls=LS[n], lw=1.1)
    ax.axhline(C_ROOT / 8, color=FAIR, ls="--", lw=0.8)
    ax.text(2, C_ROOT / 8 + 0.7, "policy share %.1fG" % (C_ROOT / 8),
            color=FAIR, fontsize=7)
    ax.set_xlabel("time (s)"); ax.set_title(title, fontsize=10)
    ax.set_ylim(-1, 26)
    if i == 0:
        ax.set_ylabel("received rate (Gb/s)")
        ax.text(30, 3.0, "RDMA at 0, QPs lost to\nrequester timeout",
                color=BAD, fontsize=8)
    ax.legend(handles=[Patch(color=RDMA, ec="black", lw=0.4, label="RDMA (4 flow-sets)"),
                       Patch(color=TCP, ec="black", lw=0.4, label="TCP (4 flow-sets)")],
              fontsize=7, frameon=False, loc="upper right")

ax = fig.add_subplot(gs[0, 2])
runs = [("write", "m_write%d", "d_write%d", 4), ("read", "m_read%d", "d_read%d", 2)]
xs, mv, dv, labels = [], [], [], []
pos = 0
for verb, mfmt, dfmt, n in runs:
    for i in range(1, n + 1):
        xs.append(pos); mv.append(stalls(mfmt % i)); dv.append(stalls(dfmt % i))
        labels.append("%s%d" % (verb[0].upper(), i)); pos += 1
w = 0.38
ax.bar([x - w / 2 for x in xs], mv, w, color=BAD, edgecolor="black", lw=0.4,
       zorder=3, label="min(cc, level)")
ax.bar([x + w / 2 for x in xs], dv, w, color=RDMA, edgecolor="black", lw=0.4,
       zorder=3, label="d x level")
ax.set_xticks(xs); ax.set_xticklabels(labels, fontsize=8)
ax.set_ylabel("seconds with an RDMA\nflow-set below 3 Gb/s")
ax.set_title("(c) stalls per run (W = write, R = read)", fontsize=10)
ax.legend(fontsize=7, frameon=False, loc="upper left", bbox_to_anchor=(0.0, 0.88))
ax.set_ylim(0, max(mv) * 1.28)
ax.text(0.0, max(mv) * 0.72, "coupled arm: 0 s in all 6 runs",
        color=RDMA, fontsize=8)

for a in fig.axes:
    a.grid(axis="y", color="#e0e0e0", lw=0.5, zorder=0)
    a.spines["top"].set_visible(False); a.spines["right"].set_visible(False)
fig.subplots_adjust(left=0.075, right=0.99, top=0.90, bottom=0.16)
fig.savefig(Path(__file__).resolve().parent / "fig_couple_ab.png", dpi=200)
fig.savefig(Path(__file__).resolve().parent / "fig_couple_ab.pdf")
print("stalls  min():", mv, " coupled:", dv)
