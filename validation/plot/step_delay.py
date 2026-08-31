#!/usr/bin/env python3
"""fig/pairjitter_stepdelay.png: how long the executor takes to put a new rate
on the wire, measured OPEN LOOP.

The sender agent is stopped and the rate is driven as a square wave from a
known instant, so nothing feeds back and the number is a transport delay
rather than a loop property. Left: the wire's response averaged over the
twenty steps, aligned on the command. Right: the per-step latency.

The RDMA panel also marks when doca_pcc_mailbox_send returns. It returns
AFTER the wire has already moved, which is why the mailbox's 13.5 ms is not
in the control path.

usage: step_delay.py"""
import csv, os, re, time
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LO, HI, MID = 8.0, 24.0, 16.0
COL = {"rdma": "#d62728", "tcp": "#1f77b4"}


def load(tag, field):
    R = os.path.join(BASE, "results", tag)
    steps = [(float(r["wall"]), float(r["level"])) for r in csv.DictReader(open(R + "/steps.csv"))]
    seen, off = {}, []
    for r in csv.DictReader(open(R + "/finewire.csv")):
        tn = int(r["t_ns"]); off.append(float(r["wall"]) - tn / 1e9)
        seen.setdefault(tn, int(r[field]))
    o = float(np.median(off))
    W = sorted((tn / 1e9 + o, b) for tn, b in seen.items())
    mb = []
    if os.path.exists(R + "/mailbox.log"):
        for l in open(R + "/mailbox.log"):
            m = re.search(r"t0=(\d+)\.(\d+) send_ns=(\d+)", l)
            if m:
                mb.append((int(m.group(1)) + int(m.group(2)) / 1e9, int(m.group(3)) / 1e9))
        mb.sort()
    return steps, np.array([a for a, _ in W]), np.array([float(b) for _, b in W]), mb


def rate(wt, wb, t, win=0.008):
    i1 = np.searchsorted(wt, t); i0 = np.searchsorted(wt, t - win)
    return (wb[i1] - wb[i0]) * 8 / (wt[i1] - wt[i0]) / 1e9 if 0 <= i0 < i1 < len(wt) else np.nan


fig, ax = plt.subplots(1, 2, figsize=(12.5, 4.6),
                       gridspec_kw={"width_ratios": [1.6, 1]})
lat_all, mb_all = {}, {}
grid = np.arange(-0.02, 0.06, 0.002)
for tag, field, cls, name in [("step_tcp_20260831", "rx_eth", "tcp", "TCP (host fq+EDT)"),
                              ("step_rdma_20260831", "rx_ib", "rdma", "RDMA (DPU PCC)")]:
    steps, wt, wb, mb = load(tag, field)
    up_curves, lat = [], []
    for tc, lvl in steps:
        up = lvl > (1.6e10 if cls == "tcp" else 60000)
        cur = np.array([rate(wt, wb, tc + g) for g in grid])
        up_curves.append(cur if up else (LO + HI) - cur)   # fold falls onto rises
        tw = None
        for t in np.arange(tc, tc + 0.6, 0.002):
            r = rate(wt, wb, t)
            if np.isnan(r):
                continue
            if (r > MID) == up and all((rate(wt, wb, t + d) > MID) == up
                                       for d in (0.004, 0.008, 0.012)
                                       if not np.isnan(rate(wt, wb, t + d))):
                tw = t; break
        if tw is not None:
            lat.append((tw - tc) * 1e3)
    med = np.nanmedian(np.array(up_curves), axis=0)
    ax[0].plot(grid * 1e3, med, color=COL[cls], lw=1.8, label=name)
    lat_all[cls] = np.array(lat)
    if mb:
        d = [dur for tc, _ in steps for t, dur in mb if abs(t - tc) < 0.002]
        mb_all[cls] = np.median(d) * 1e3 if d else None

ax[0].axvline(0, color="k", lw=1, ls="--")
ax[0].text(0.6, LO + 1.2, "command issued", fontsize=8, rotation=90, va="bottom")
ax[0].axhline(MID, color="gray", lw=0.7, ls=":")
if mb_all.get("rdma"):
    ax[0].axvline(mb_all["rdma"], color=COL["rdma"], lw=1, ls=":")
    ax[0].text(mb_all["rdma"] + 0.5, HI - 1.5,
               "mailbox_send returns\n(%.1f ms)" % mb_all["rdma"], fontsize=8, color=COL["rdma"])
ax[0].set_xlabel("time since the rate command (ms)")
ax[0].set_ylabel("wire rate at the receiver (Gb/s)")
ax[0].set_title("wire response to a commanded 8 -> 24 G step\n(median of 20 steps, falls folded onto rises)", fontsize=10)
ax[0].legend(fontsize=8); ax[0].grid(alpha=0.3)

pos = [1, 2]
for i, cls in enumerate(["tcp", "rdma"]):
    v = lat_all[cls]
    ax[1].scatter(np.full(len(v), pos[i]) + np.random.uniform(-.08, .08, len(v)), v,
                  s=22, color=COL[cls], alpha=0.75)
    ax[1].hlines(np.median(v), pos[i] - .22, pos[i] + .22, color="k", lw=2)
    ax[1].text(pos[i], np.median(v) + 1.4, "%.1f ms" % np.median(v), ha="center", fontsize=9)
ax[1].set_xticks(pos); ax[1].set_xticklabels(["TCP", "RDMA"])
ax[1].set_ylabel("command -> wire (ms)")
ax[1].set_title("per-step latency", fontsize=10); ax[1].grid(alpha=0.3, axis="y")

fig.suptitle("executor actuation delay, open loop (sender agent stopped)    drawn "
             + time.strftime("%Y-%m-%d %H:%M:%S"), fontsize=11)
fig.tight_layout()
fig.savefig(os.path.join(BASE, "fig", "pairjitter_stepdelay.png"), dpi=130)
print("ok")
