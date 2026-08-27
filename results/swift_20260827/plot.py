#!/usr/bin/env python3
"""plot.py <tag>: (a) received Gb/s per receiver VF (vport meter, 100 ms,
ib+eth), (b) per-pair Swift telemetry on each sender DPU: cwnd, rtt_s vs
target, cc_rate. Colors per paper/PLOT_STYLE.md."""
import csv, glob, os, sys
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_poll import parse
BASE = os.path.dirname(os.path.abspath(__file__)); TAG = sys.argv[1]
D = f"{BASE}/results/{TAG}"
TCP_C, RDMA_C, GRID, REF, FAIR = "#EDB877", "#3D5A80", "#e0e0e0", "#888888", "#2a9d5c"
TEN = ["#DCA9A3", "#B9CFB6", "#C3B5D9", "#ABC4DD", "#3D5A80", "#A8C5DA", "#7a9cc6", "#5b7fa6"]
WARM = 5.0; DT = 0.1
T0 = int(open(f"{D}/t0.txt").read())

def load_vpm():
    rows, last = {}, {}
    for r in csv.DictReader(open(f"{D}/vpm_series.csv")):
        if not r.get("rx_eth"): continue
        i, tn = int(r["vport_idx"]), int(r["t_ns"])
        if last.get(i) == tn: continue
        last[i] = tn
        rows.setdefault(i, []).append((float(r["ts"]), int(r["rx_ib"]), int(r["rx_eth"])))
    return rows
def rate(series, col):
    ts = np.array([s[0] for s in series]); b = np.array([s[col] for s in series], float)
    edges = np.arange(ts[0], ts[-1], DT); bb = np.interp(edges, ts, b)
    return edges[:-1] + DT / 2, np.diff(bb) * 8 / DT / 1e9
vpm = load_vpm()
# align on the sampler's own clock: the t=0 RDMA flows start at T0 (figure
# time -WARM); the first 100 ms bin with >1 Gb/s of RDMA anywhere is that edge.
tot = None
for i in sorted(vpm):
    t, ib = rate(vpm[i], 1)
    tot = ib if tot is None else tot[:len(ib)] + ib[:len(tot)]
tt, _ = rate(vpm[min(vpm)], 1)
edge = tt[:len(tot)][tot > 1.0][0] - DT / 2
off = edge + WARM          # sampler time of figure t=0
fig, axes = plt.subplots(3, 1, figsize=(8, 9), sharex=True)
ax = axes[0]
for i in sorted(vpm):
    t, ib = rate(vpm[i], 1); t2, eth = rate(vpm[i], 2)
    if ib.max() > 0.5: ax.plot(t - off, ib, color=TEN[i % 8], lw=1.1, label=f"vf{i} RDMA")
    if eth.max() > 0.5: ax.plot(t2 - off, eth, color=TCP_C, lw=1.1, label=f"vf{i} TCP")
ax.axhline(50 * 1514 / 1588, color=REF, ls="--", lw=0.8); ax.text(0.2, 48.5, "VF cap 50G", color=REF, fontsize=8)
ax.set_ylabel("received Gb/s (per receiver VF)"); ax.legend(fontsize=7, ncol=4, frameon=False)
# telemetry
tel = {}
for f in sorted(glob.glob(f"{D}/poll_*.log")):
    d = os.path.basename(f)[5:-4]
    for r in parse(f):
        if r[2] == 0: continue
        tel.setdefault((d, r[1]), []).append(r)
ci = 0
for (d, p), rs in sorted(tel.items()):
    t = np.array([r[0] for r in rs]) - (T0 + WARM)
    cw = np.array([r[3] for r in rs]) / 1024.0; rtt = np.array([r[4] for r in rs]) / 1e3; tg = np.array([r[5] for r in rs]) / 1e3
    c = TEN[ci % 8]; ci += 1
    axes[1].plot(t, rtt, color=c, lw=1, label=f"{d} p{p} rtt_s")
    axes[1].plot(t, tg, color=c, lw=0.8, ls="--")
    axes[2].plot(t, cw, color=c, lw=1, label=f"{d} p{p}")
axes[1].set_ylabel("smoothed RTT (solid) / target (dashed), µs"); axes[1].legend(fontsize=7, ncol=3, frameon=False)
axes[2].set_ylabel("cwnd, KB"); axes[2].set_xlabel("time since t=0 (s)"); axes[2].legend(fontsize=7, ncol=3, frameon=False)
for a in axes:
    a.grid(axis="y", color=GRID, lw=0.5); a.spines["top"].set_visible(False); a.spines["right"].set_visible(False)
axes[2].set_xlim(-WARM, 30)
fig.suptitle(f"Swift on the HPFT executor - {TAG}", fontsize=10); fig.tight_layout()
fig.savefig(f"{BASE}/fig_{TAG}.png", dpi=200); fig.savefig(f"{BASE}/fig_{TAG}.pdf")
# window stats
print("per-VF received Gb/s, windows 0-10/10-20/20-30 (RDMA ib bucket; eth in brackets)")
for i in sorted(vpm):
    t, ib = rate(vpm[i], 1); _, eth = rate(vpm[i], 2); t = t - off
    w = [(ib[(t >= a) & (t < b)].mean(), eth[(t >= a) & (t < b)].mean()) for a, b in ((0, 10), (10, 20), (20, 30))]
    if max(x for x, _ in w) > 0.3 or max(y for _, y in w) > 0.3:
        print(f"vf{i}: " + " | ".join(f"{x:6.2f} ({y:5.1f})" for x, y in w))
for (d, p), rs in sorted(tel.items()):
    a = np.array([[r[0] - (T0 + WARM), r[3], r[4], r[5], r[6], r[7], r[8], r[9]] for r in rs], float)
    m = a[(a[:, 0] >= 5) & (a[:, 0] < 30)]
    if len(m) == 0: continue
    print(f"{d} pair{p} ft=0x{rs[0][2]:x}: cwnd {m[:,1].mean()/1024:.1f} KB (sd {m[:,1].std()/1024:.1f}), rtt_s {m[:,2].mean()/1e3:.1f} us (sd {m[:,2].std()/1e3:.1f}), target {m[:,3].mean()/1e3:.1f} us, cc_rate {m[:,4].mean()/2**20*200:.1f} G, cuts {rs[-1][7]-rs[0][7]}, loss-cuts {rs[-1][8]-rs[0][8]}, qp {int(m[:,7].max())}")
