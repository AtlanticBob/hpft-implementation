#!/usr/bin/env python3
"""fig/V2_punctual.png: the join, before and after the TCP generator was made
punctual.

iperf3 has no absolute start, so the runner could only sleep to the event and
then exec it; its 0.5-1.1 s of startup landed AFTER the event and the receiver
saw no bytes at all for that long. tcp_blast connects two seconds ahead and
puts its first byte on the event. Left: the old way. Right: three runs of the
new way, to show the start time is reproducible and not a lucky sample.
usage: punctual.py"""
import json, os, sys, time
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from distill import bin_mean

NT = "sgpu03/vf0>sgpu02/vf0|tcp"
NR = "sgpu03/vf0>sgpu02/vf0|rdma"
COL = {NT: "#1f77b4", NR: "#d62728"}
PANELS = [("iperf3: sleep to the event, then exec",
           [("V2_now_20260831_rep1", 10.0)]),
          ("tcp_blast: connect ahead, first byte ON the event",
           [(f"V2_punct_20260831_{r}", 10.0) for r in ("rep1", "rep2", "rep3")])]

fig, ax = plt.subplots(1, 2, figsize=(12.5, 4.6), sharey=True)
for j, (title, runs) in enumerate(PANELS):
    firsts = {NT: [], NR: []}
    for tag, ev in runs:
        R = os.path.join(BASE, "results", tag)
        z = float(open(R + "/t0.txt").read()) + float(open(R + "/warm.txt").read())
        rx = [(json.loads(l)["ts"] - z - ev, json.loads(l))
              for l in open(R + "/rx.jsonl") if l.strip()]
        w = [(t, x) for t, x in rx if -0.4 <= t <= 2.6]
        T = np.array([t for t, _ in w])
        for fs in (NR, NT):
            bt, bv = bin_mean(T, [x["r"].get(fs, 0) / 1e9 for _, x in w], 0.1)
            ax[j].plot(bt, bv, color=COL[fs], lw=1.2, alpha=0.8)
            f = next((t for t, x in w if t > -0.05 and x["r"].get(fs, 0) / 1e9 > 0.3), np.nan)
            firsts[fs].append(f * 1000)
    ax[j].axvline(0, color="k", lw=1, ls="--")
    ax[j].axhline(11.5, color="k", ls=":", lw=0.9)
    ax[j].set_xlabel("time since the join (s)")
    ax[j].grid(alpha=0.3)
    lab = "  ".join("%s first byte %s ms" % (n, "/".join(f"{x:.0f}" for x in firsts[fs]))
                    for fs, n in ((NR, "RDMA"), (NT, "TCP")))
    ax[j].set_title(f"{title}\n{lab}", fontsize=10)
ax[0].set_ylabel("newcomer's attributed rate (Gb/s), 100 ms")
ax[0].plot([], [], color=COL[NR], label="RDMA newcomer")
ax[0].plot([], [], color=COL[NT], label="TCP newcomer")
ax[0].plot([], [], color="k", ls=":", label="expected 11.5 G")
ax[0].legend(fontsize=8, loc="lower right")
fig.suptitle("V2 join: making the TCP generator punctual    drawn "
             + time.strftime("%Y-%m-%d %H:%M:%S"), fontsize=12)
fig.tight_layout()
fig.savefig(os.path.join(BASE, "fig", "V2_punctual.png"), dpi=130)
print("ok")
