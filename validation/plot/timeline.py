#!/usr/bin/env python3
"""fig/<base>_timeline.png: what the run looked like, on the experiment clock.
  panel 1  attributed rate of every flow-set (receiver agent, drawn at 100 ms
           to match panel 2; the agent logs at 20 ms and the criteria in
           distill.py read that raw series, not this one), RDMA red /
           TCP blue, solid = first sender host in the table, dashed = others;
           black dotted = the expected rate of each group (README §四)
  panel 2  wire rate per VM per class from the receiver NIC counters (100 ms),
           stacked, plus the C' line - one such panel per receiving host
  last     virtual queue per flow-set (ms)
Reads only data/ (distill.py output) plus results/<tag>/flows.txt for the
expected-rate steps.

usage: timeline.py <tag>[,<tag>,...] [<base>]
Several tags (the repetitions of one scenario) are averaged point by point on
the 100 ms experiment clock and drawn as ONE figure - a decision-basis run is
three repetitions, and three separate pictures of the same scenario say less
than their mean. The title names every tag that went in. <base> is the
figure-name prefix (default: the first tag's scenario id); two-arm scenarios
use it to keep the arms apart."""
import csv, json, os, sys, time
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from distill import load_flows, expected_at, receivers, C_ROOT
tags = [t for t in sys.argv[1].split(",") if t]
base = sys.argv[2] if len(sys.argv) > 2 else tags[0].split("_")[0]
D = os.path.join(BASE, "data")
BIN_S = 0.1        # drawing granularity, matching the wire panel


def binned(T, V, grid):
    """Mean of V over each grid cell [g, g+BIN_S); NaN where empty."""
    out = np.full(len(grid), np.nan)
    idx = np.floor((np.asarray(T) - grid[0]) / BIN_S).astype(int)
    V = np.asarray(V, dtype=float)
    for i in range(len(grid)):
        m = idx == i
        if m.any():
            out[i] = V[m].mean()
    return out


runs = []
for tag in tags:
    R = os.path.join(BASE, "results", tag)
    rows = load_flows(os.path.join(R, "flows.txt"))
    t0 = float(open(os.path.join(R, "t0.txt")).read()); warm = float(open(os.path.join(R, "warm.txt")).read())
    z = t0 + warm; end = max(r["end"] for r in rows)
    # one agent log per receiving host (rx_<host>.jsonl); a run from before
    # 2026-09-04 has a single receiver and a single rx.jsonl
    rxh = {}
    for dh in receivers(rows):
        pth = os.path.join(R, f"rx_{dh}.jsonl")
        if not os.path.exists(pth):
            pth = os.path.join(R, "rx.jsonl")
        rx = [json.loads(l) for l in open(pth) if l.strip()]
        rxh[dh] = [(x["ts"] - z, x) for x in rx if -1 <= x["ts"] - z <= end + 1]
    with open(os.path.join(D, f"{tag}_vmclass.csv")) as f:
        vm = list(csv.DictReader(f))
    runs.append((rows, end, rxh, vm))
rows, end = runs[0][0], runs[0][1]
recv = receivers(rows)
grid = np.arange(0, end + BIN_S / 2, BIN_S)
hosts = []
for r in rows:
    if r["host"] != "-" and r["host"] not in hosts:
        hosts.append(r["host"])
col = {"rdma": "#d62728", "tcp": "#1f77b4"}
npan = 2 + len(recv)
fig, ax = plt.subplots(npan, 1, figsize=(12, 6.5 + 3 * len(recv)), sharex=True)
axq = ax[-1]
for r in [r for r in rows if r["cls"] in col]:
    f = r["fsid"]; dh = r["dhost"]
    rate = np.nanmean([binned([t for t, _ in rxh[dh]], [x["r"].get(f, 0) / 1e9 for _, x in rxh[dh]], grid)
                       for _, _, rxh, _ in runs], axis=0)
    q = np.nanmean([binned([t for t, _ in rxh[dh]], [x.get("d", {}).get(f, 0) for _, x in rxh[dh]], grid)
                    for _, _, rxh, _ in runs], axis=0)
    ax[0].plot(grid, rate, color=col[r["cls"]], lw=0.8,
               ls="-" if r["host"] == hosts[0] else "--", alpha=0.85)
    axq.plot(grid, q, color=col[r["cls"]], lw=0.7, alpha=0.7)
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
# wire per VM per class: distill writes every run on the same 100 ms grid,
# so the columns can be averaged row by row after aligning on t. Columns are
# <host>_vf<i>_<class> (one block per receiver); a data file from before
# 2026-09-04 has vf<i>_<class>, i.e. the one receiver of that run.
cols = [c for c in runs[0][3][0] if c != "t"]
vm_series = {}
for _, _, _, vm in runs:
    for row in vm:
        k = round(float(row["t"]), 2)
        vm_series.setdefault(k, []).append([float(row[c]) for c in cols])
vt = np.array(sorted(vm_series))
stack = np.array([[np.nanmean([v[i] for v in vm_series[t]]) for t in vt] for i in range(len(cols))])
stack = np.nan_to_num(stack)
for j, dh in enumerate(recv):
    idx = [i for i, c in enumerate(cols) if c.startswith(dh + "_") or "_vf" not in c]
    colors = [("#d62728" if cols[i].endswith("rdma") else "#1f77b4") for i in idx]
    a = ax[1 + j]
    a.stackplot(vt, stack[idx], colors=colors, alpha=0.7, lw=0.2, edgecolor="white")
    a.axhline(C_ROOT / 1e9, color="k", ls=":", lw=1); a.text(0.5, C_ROOT / 1e9 + 2, "C' = 184 G", fontsize=8)
    a.set_ylabel(f"wire at {dh} per VM per class (Gb/s)" if len(recv) > 1 else "wire per VM per class, stacked (Gb/s)")
    a.grid(alpha=0.3); a.set_ylim(0, 215)
axq.set_ylabel("virtual queue (ms), 100 ms mean"); axq.set_xlabel("experiment time (s)"); axq.grid(alpha=0.3)
for e in sorted({r["start"] for r in rows} | {r["end"] for r in rows}):
    if 0 < e < end:
        for a in ax:
            a.axvline(e, color="gray", lw=0.6, ls="--")
ax[0].set_xlim(0, end)
# The file name is per-scenario and is overwritten by every run, so the
# figure has to carry its own identity: every tag that went in, and the
# wall-clock second it was drawn.
title = tags[0] if len(tags) == 1 else f"mean of {len(tags)}: " + ", ".join(tags)
fig.suptitle(f"{title}    {len(rows)} flow-sets    drawn {time.strftime('%Y-%m-%d %H:%M:%S')}", fontsize=10)
fig.tight_layout()
os.makedirs(os.path.join(BASE, "fig"), exist_ok=True)
fig.savefig(os.path.join(BASE, "fig", f"{base}_timeline.png"), dpi=130); print("ok")
