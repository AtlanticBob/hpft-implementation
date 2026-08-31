#!/usr/bin/env python3
"""V8 analysis: how much of a flapping neighbour reaches the stable tenants on
the same VM. Reads results/<tag>/rx.jsonl. Prints, for each stable flow-set,
the mean rate while the neighbour is ON and OFF (phase taken from the
neighbour's own attributed rate), the standard deviation, and the neighbour's
mean; writes fig/V8_flap.png. usage: flap.py <tag> [window_start window_end]"""
import json, os, sys, numpy as np
import time
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tag = sys.argv[1]; a = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0; b = float(sys.argv[3]) if len(sys.argv) > 3 else 45.0
R = os.path.join(BASE, "results", tag); z = float(open(os.path.join(R, "t0.txt")).read()) + float(open(os.path.join(R, "warm.txt")).read())
rx = [json.loads(l) for l in open(os.path.join(R, "rx.jsonl")) if l.strip()]
w = [(x["ts"] - z, x) for x in rx if a <= x["ts"] - z <= b]
nb = "sgpu03/vf0>sgpu02/vf0|tcp"; victims = ["sgpu01/vf0>sgpu02/vf0|tcp", "sgpu04/vf0>sgpu02/vf0|rdma"]
nbr = np.array([x["r"].get(nb, 0) / 1e9 for _, x in w]); on = nbr > 0.3 * max(nbr.max(), 1e-9)
print("%s  window %.0f-%.0f s: neighbour mean %.2f G, on %.0f%% of samples" % (tag, a, b, nbr.mean(), 100 * on.mean()))
for f in victims:
    v = np.array([x["r"].get(f, 0) / 1e9 for _, x in w]); e = np.array([x["e"].get(f, 0) / 1e9 for _, x in w]); q = np.array([x.get("d", {}).get(f, 0) for _, x in w])
    print("  %-30s mean %.2f  sd %.2f  | neighbour ON: %.2f  OFF: %.2f  (swing %.2f G, %.0f%% of mean) | E on/off %.1f/%.1f | q mean %.2f ms"
          % (f, v.mean(), v.std(), v[on].mean() if on.any() else 0, v[~on].mean() if (~on).any() else 0, (v[~on].mean() - v[on].mean()) if on.any() and (~on).any() else 0,
             100 * abs(v[~on].mean() - v[on].mean()) / v.mean() if on.any() and (~on).any() else 0, e[on].mean() if on.any() else 0, e[~on].mean() if (~on).any() else 0, q.mean()))
T = [t for t, _ in w]
fig, ax = plt.subplots(2, 1, figsize=(12, 6), sharex=True)
ax[0].plot(T, nbr, color="gray", lw=0.8, label="neighbour (sgpu03 tcp bursts)")
for f, c in zip(victims, ("#1f77b4", "#d62728")):
    ax[0].plot(T, [x["r"].get(f, 0) / 1e9 for _, x in w], color=c, lw=0.9, label=f)
    ax[1].plot(T, [x.get("d", {}).get(f, 0) for _, x in w], color=c, lw=0.7)
ax[0].set_ylabel("attributed rate (Gb/s)"); ax[0].legend(fontsize=8); ax[0].grid(alpha=0.3); ax[0].set_xlim(a, min(b, a + 6))
ax[1].set_ylabel("victims' virtual queue (ms)"); ax[1].set_xlabel("experiment time (s)"); ax[1].grid(alpha=0.3)
fig.suptitle(f"{tag}    drawn {time.strftime('%Y-%m-%d %H:%M:%S')}"); fig.tight_layout(); fig.savefig(os.path.join(BASE, "fig", "V8_flap_%s.png" % tag[3:].rsplit("_", 2)[0]), dpi=130)
