#!/usr/bin/env python3
"""fig/<tag>_timeline.png: what the run looked like, on the experiment clock.
  panel 1  attributed rate of every flow-set (receiver agent, drawn at 100 ms
           to match panel 2; the agent logs at 20 ms and the criteria in
           distill.py read that raw series, not this one), RDMA red /
           TCP blue, solid = first sender host in the table, dashed = others;
           black dotted = the expected rate of each group (README §四)
  panel 2  wire rate per VM per class from the receiver NIC counters (100 ms),
           stacked, plus the C' line
  panel 3  virtual queue per flow-set (ms)
Reads only data/ (distill.py output) plus results/<tag>/flows.txt for the
expected-rate steps. usage: timeline.py <tag>"""
import csv, json, os, sys, time
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from distill import load_flows, expected_at, C_ROOT, bin_mean
tag = sys.argv[1]
R = os.path.join(BASE, "results", tag); D = os.path.join(BASE, "data")
rows = load_flows(os.path.join(R, "flows.txt"))
t0 = float(open(os.path.join(R, "t0.txt")).read()); warm = float(open(os.path.join(R, "warm.txt")).read())
z = t0 + warm; end = max(r["end"] for r in rows)
rx = [json.loads(l) for l in open(os.path.join(R, "rx.jsonl")) if l.strip()]
rx = [(x["ts"] - z, x) for x in rx if -1 <= x["ts"] - z <= end + 1]
T = [t for t, _ in rx]
hosts = []
for r in rows:
    if r["host"] not in hosts:
        hosts.append(r["host"])
col = {"rdma": "#d62728", "tcp": "#1f77b4"}
fig, ax = plt.subplots(3, 1, figsize=(12, 9.5), sharex=True)
BIN_S = 0.1        # drawing granularity, matching the wire panel
for r in [r for r in rows if r["cls"] in col]:
    f = r["fsid"]
    bt, bv = bin_mean(T, [x["r"].get(f, 0) / 1e9 for _, x in rx], BIN_S)
    ax[0].plot(bt, bv, color=col[r["cls"]], lw=0.8,
               ls="-" if r["host"] == hosts[0] else "--", alpha=0.85)
    qt, qv = bin_mean(T, [x.get("d", {}).get(f, 0) for _, x in rx], BIN_S)
    ax[2].plot(qt, qv, color=col[r["cls"]], lw=0.7, alpha=0.7)
# expected steps: one dotted line per distinct trajectory
tt = np.arange(0, end, 0.25); traj = {}
for f in {r["fsid"] for r in rows if r["cls"] in col}:
    key = tuple(round(expected_at(rows, t).get(f, 0.0), 2) for t in tt)
    traj[key] = f
for key in traj:
    ax[0].step(tt, key, where="post", color="k", ls=":", lw=1.0)
ax[0].plot([], [], color=col["rdma"], label="RDMA"); ax[0].plot([], [], color=col["tcp"], label="TCP")
ax[0].plot([], [], color="k", ls="-", label=f"from {hosts[0]}")
if len(hosts) > 1:
    ax[0].plot([], [], color="k", ls="--", label="from " + "/".join(hosts[1:]))
ax[0].plot([], [], color="k", ls=":", label="expected")
ax[0].set_ylabel("attributed rate per flow-set (Gb/s), 100 ms"); ax[0].legend(ncol=5, fontsize=8); ax[0].grid(alpha=0.3)
with open(os.path.join(D, f"{tag}_vmclass.csv")) as f:
    rd = list(csv.DictReader(f))
vt = np.array([float(r["t"]) for r in rd]); cols = [c for c in rd[0] if c != "t"]
stack = np.array([[float(r[c]) for r in rd] for c in cols]); stack = np.nan_to_num(stack)
colors = [("#d62728" if c.endswith("rdma") else "#1f77b4") for c in cols]
alphas = np.linspace(0.35, 0.95, max(len(cols) // 2, 1))
ax[1].stackplot(vt, stack, colors=colors, alpha=0.7, lw=0.2, edgecolor="white")
ax[1].axhline(C_ROOT / 1e9, color="k", ls=":", lw=1); ax[1].text(0.5, C_ROOT / 1e9 + 2, "C' = 184 G", fontsize=8)
ax[1].set_ylabel("wire per VM per class, stacked (Gb/s)"); ax[1].grid(alpha=0.3); ax[1].set_ylim(0, 215)
ax[2].set_ylabel("virtual queue (ms), 100 ms mean"); ax[2].set_xlabel("experiment time (s)"); ax[2].grid(alpha=0.3)
for e in sorted({r["start"] for r in rows} | {r["end"] for r in rows}):
    if 0 < e < end:
        for a in ax:
            a.axvline(e, color="gray", lw=0.6, ls="--")
ax[0].set_xlim(0, end)
# The file name is per-scenario and is overwritten by every run, so the
# figure has to carry its own identity: stamp the wall-clock second it
# was drawn, otherwise there is no way to tell which version of the
# design a saved image belongs to.
fig.suptitle(f"{tag}: {len(rows)} flow-sets    drawn {time.strftime('%Y-%m-%d %H:%M:%S')}")
fig.tight_layout()
os.makedirs(os.path.join(BASE, "fig"), exist_ok=True)
fig.savefig(os.path.join(BASE, "fig", f"{tag.split('_')[0]}_timeline.png"), dpi=130); print("ok")
