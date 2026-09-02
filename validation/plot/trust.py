#!/usr/bin/env python3
"""fig/<tag>_trust.png: what the two executors did with the tenant CC during
the run (V7 and any scenario where a CC becomes binding).
  panel 1  TCP executor: trust per pair from the BPF map, 100 ms (one line per
           pair that carried traffic), all sender hosts
  panel 2  RDMA executor: per pair, the fence (level, dashed) and the CC's own
           rate (cc, solid) and the paced rate (dotted), 1 s samples from the
           device query, one colour per pair
  panel 3  RDMA executor: trust per pair
Reads results/<tag>/trust_<host>.jsonl and rp_<host>.jsonl. usage: trust.py <tag>"""
import json, os, sys
import time
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tag = sys.argv[1]
base = sys.argv[2] if len(sys.argv) > 2 else tag.split("_")[0]  # 图名前缀；双臂场景用它区分臂
R = os.path.join(BASE, "results", tag)
t0 = float(open(os.path.join(R, "t0.txt")).read()); warm = float(open(os.path.join(R, "warm.txt")).read()); z = t0 + warm
rows = [l.split() for l in open(os.path.join(R, "flows.txt")) if l.strip() and not l.startswith("#")]
end = max(float(r[6]) for r in rows); events = sorted({float(r[5]) for r in rows} | {float(r[6]) for r in rows})
fig, ax = plt.subplots(3, 1, figsize=(12, 9), sharex=True)
for fn in sorted(os.listdir(R)):
    if fn.startswith("trust_") and fn.endswith(".jsonl"):
        host = fn[6:-6]; per = {}
        for l in open(os.path.join(R, fn)):
            if l.strip():
                x = json.loads(l); per.setdefault(x["key"], []).append((x["ts"] - z, x["trust"]))
        for k, v in per.items():
            if max(t for _, t in v) > 0 or len(per) <= 8:
                ax[0].plot([a for a, _ in v], [b for _, b in v], lw=0.8, label=f"{host} {k[:8]}")
    if fn.startswith("rp_") and fn.endswith(".jsonl"):
        host = fn[3:-6]; per = {}
        for l in open(os.path.join(R, fn)):
            if l.strip():
                x = json.loads(l); per.setdefault(x["ft"], []).append((x["ts"] - z, x))
        for i, (ft, v) in enumerate(sorted(per.items())):
            c = f"C{i % 10}"; T = [a for a, _ in v]
            ax[1].plot(T, [b["level"] for _, b in v], color=c, ls="--", lw=0.9)
            ax[1].plot(T, [min(b["cc"], 60) for _, b in v], color=c, ls="-", lw=1.2, label=f"{host} {ft}")
            ax[1].plot(T, [b["paced"] for _, b in v], color=c, ls=":", lw=0.9)
            ax[2].plot(T, [b["trust"] for _, b in v], color=c, lw=1.0)
ax[0].set_ylabel("TCP executor trust"); ax[0].set_ylim(-0.02, 1.02); ax[0].grid(alpha=0.3)
if ax[0].lines: ax[0].legend(fontsize=6, ncol=4, loc="upper right")
ax[1].set_ylabel("RDMA executor per QP (Gb/s)\n-- fence level, — CC rate (clipped 60), ·· paced"); ax[1].grid(alpha=0.3)
if ax[1].lines: ax[1].legend(fontsize=6, ncol=4, loc="upper right")
ax[2].set_ylabel("RDMA executor trust"); ax[2].set_ylim(-0.02, 1.02); ax[2].grid(alpha=0.3); ax[2].set_xlabel("experiment time (s)")
for e in events:
    if 0 < e < end:
        for a in ax: a.axvline(e, color="gray", lw=0.6, ls="--")
ax[0].set_xlim(0, end); fig.suptitle(f"{tag}: executor trust and the CC next to the fence    drawn {time.strftime('%Y-%m-%d %H:%M:%S')}"); fig.tight_layout()
fig.savefig(os.path.join(BASE, "fig", f"{base}_trust.png"), dpi=130); print("ok")
