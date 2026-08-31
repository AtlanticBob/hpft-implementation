#!/usr/bin/env python3
"""fig/qpcount_fix.png: the executor's own budget overshoot, and its removal.

The RDMA executor paces every QP of a flow-set at level = budget / N, so the
pair puts N_sending x level on the wire and N has to be right. N is counted
from the event stream, and a QP that is being paced hard raises events
sparsely enough to fall out of the activity window now and then; N then reads
low and the pair runs over its budget by N_real / N_read.

Measured with the budget frozen at 24 G and ten QPs sending flat out, reading
the device's level back through mailbox 0xded. Reads data/qpcount_*.csv.

usage: qp_count.py"""
import csv, os, time
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
rows = list(csv.DictReader(open(os.path.join(BASE, "data", "qpcount_20260831.csv"))))
arms = ["before", "after"]
label = {"before": "before: N taken as read",
         "after": "after: N rises at once, falls only after 64 epochs"}
tot = {a: sum(int(r["samples"]) for r in rows if r["arm"] == a) for a in arms}

fig, ax = plt.subplots(1, 2, figsize=(11.5, 4.3))
ns = [10, 9, 8]
w = 0.36
for k, a in enumerate(arms):
    got = {int(r["n_read"]): int(r["samples"]) for r in rows if r["arm"] == a}
    frac = [100.0 * got.get(n, 0) / tot[a] for n in ns]
    b = ax[0].bar(np.arange(len(ns)) + (k - 0.5) * w, frac, w,
                  color=("#d62728" if a == "before" else "#2ca02c"), alpha=0.85,
                  label=label[a])
    for r, f in zip(b, frac):
        if f > 0:
            ax[0].text(r.get_x() + r.get_width() / 2, f + 1.5, "%.1f%%" % f,
                       ha="center", fontsize=8)
ax[0].set_xticks(range(len(ns)))
ax[0].set_xticklabels(["N = 10\n(correct)", "N = 9\nbudget x 1.11", "N = 8\nbudget x 1.25"])
ax[0].set_ylabel("share of readings (%)")
ax[0].set_ylim(0, 132)
ax[0].set_title("what the device reads for N,\nbudget frozen at 24 G with ten QPs sending", fontsize=10)
ax[0].legend(fontsize=8, loc="upper right", framealpha=0.95); ax[0].grid(alpha=0.3, axis="y")

over = {}
for a in arms:
    got = {int(r["n_read"]): int(r["samples"]) for r in rows if r["arm"] == a}
    over[a] = sum(c * (10.0 / n - 1.0) for n, c in got.items()) / tot[a] * 100
b = ax[1].bar([0, 1], [over[a] for a in arms], 0.5,
              color=["#d62728", "#2ca02c"], alpha=0.85)
for r, a in zip(b, arms):
    ax[1].text(r.get_x() + r.get_width() / 2, r.get_height() + 0.03,
               "%.2f%%" % over[a], ha="center", fontsize=10)
ax[1].set_xticks([0, 1]); ax[1].set_xticklabels(["before", "after"])
ax[1].set_ylabel("mean overshoot of the budget (%)")
ax[1].set_ylim(0, 1.45)
ax[1].set_title("the fence being exceeded by our own executor\n(design property one says this cannot happen)", fontsize=10)
ax[1].grid(alpha=0.3, axis="y")

fig.suptitle("RDMA executor: budget / N, and what a low reading of N costs    drawn "
             + time.strftime("%Y-%m-%d %H:%M:%S"), fontsize=11)
fig.tight_layout()
fig.savefig(os.path.join(BASE, "fig", "qpcount_fix.png"), dpi=130)
print("ok")
