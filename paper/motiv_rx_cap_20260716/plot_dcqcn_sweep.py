#!/usr/bin/env python3
"""Walk 2: Jakiro's weighted fairness is decided by DCQCN recovery aggressiveness.

Fixed Jakiro (20G quota, 1:1), GBN. Per DCQCN recovery setting, 4 fresh
runs; each dot is the RDMA-class steady rate. Gentle recovery -> every run
converges to the 10G share; default -> bistable; aggressive -> RDMA runs
away to 3x the quota every time. RDMA is only CE-marked (never dropped),
so its rate is set entirely by how hard DCQCN backs off.
"""
import csv, glob, os, re
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = os.path.dirname(os.path.abspath(__file__))
RDMA_C, FAIR_C, RUN_C = "#3D5A80", "#2a9d5c", "#c0392b"

KNOBS = [("slow", "gentle\n1200/1/10"),
         ("default", "default\n300/5/50"),
         ("fast", "aggressive\n75/50/500")]

def rdma_total(D):
    tot = 0.0
    for f in glob.glob(f"{D}/rdma_v*_f*.log"):
        m = re.search(r"^\s*65536\s+\d+\s+([\d.]+)\s+([\d.]+)",
                      open(f, errors="replace").read(), re.M)
        tot += float(m.group(2)) if m else 0.0
    return tot

fig, ax = plt.subplots(figsize=(5.4, 3.6))
for x, (knob, _) in enumerate(KNOBS):
    for t in (1, 2, 3, 4):
        D = f"{BASE}/dcqcn_sweep/{knob}_{t}"
        if not os.path.isdir(D):
            continue
        r = rdma_total(D)
        c = FAIR_C if r < 15 else RUN_C
        ax.plot(x + (t - 2.5) * 0.06, r, "o", color=c, markersize=8,
                markeredgecolor="white", markeredgewidth=0.8, zorder=3)

ax.axhline(10, color="#888888", linestyle="--", linewidth=1.0, zorder=1)
ax.annotate("10G weighted share", (2.42, 11.5), ha="right", fontsize=7.5,
            color="#555555")
ax.axhline(20, color="#c0392b", linestyle=":", linewidth=1.0, zorder=1)
ax.annotate("20G VM quota", (2.42, 21.5), ha="right", fontsize=7.5,
            color=RUN_C)

ax.set_xticks(range(len(KNOBS)))
ax.set_xticklabels([k[1] for k in KNOBS], fontsize=8.5)
ax.set_xlim(-0.4, 2.4)
ax.set_ylim(0, 70)
ax.set_xlabel("DCQCN recovery aggressiveness (rpg_time_reset / ai / hai)",
              fontsize=8.5)
ax.set_ylabel("RDMA class rate at the VM (Gb/s)", fontsize=9)
ax.tick_params(labelsize=8.5)
ax.grid(axis="y", color="#e0e0e0", linewidth=0.5, zorder=0)
for s in ("top", "right"):
    ax.spines[s].set_visible(False)
ax.plot([], [], "o", color=FAIR_C, label="converged (fair, ~10G)")
ax.plot([], [], "o", color=RUN_C, label="runaway (> quota)")
ax.legend(fontsize=8, frameon=False, loc="upper left")
ax.set_title("Jakiro fairness hinges on how gently DCQCN recovers (GBN)",
             fontsize=9)
fig.tight_layout()
for ext in ("png", "pdf"):
    fig.savefig(f"{BASE}/fig_dcqcn_sweep.{ext}", dpi=200)
print("fig_dcqcn_sweep written")
