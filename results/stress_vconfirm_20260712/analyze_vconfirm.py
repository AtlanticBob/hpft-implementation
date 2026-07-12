#!/usr/bin/env python3
"""V=4 confirmation analysis. borrow/reclaim: competition ratio, reclaim
time (TCP exits at t=45 -> RDMA back to >=5.7G) and yield time (TCP enters
at t=15 -> RDMA down to ~3G); A-edge: ratio/sd/episodes (cliff check);
repro: episodes + min lvl/bud."""
import json
import re
import sys
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
FS_R = "sgpu01/vf0>sgpu02/vf0|rdma"
FS_T = "sgpu01/vf0>sgpu02/vf0|tcp"
U = 200 / 1048576

t0s = {}
for line in (DIR / "runs.tsv").read_text().splitlines():
    parts = line.split("\t")
    t0s[parts[0]] = float(parts[1])


def load(tag):
    per = {}
    for jl in (DIR / f"{tag}_rx.jsonl").read_text().splitlines():
        try:
            rec = json.loads(jl)
        except json.JSONDecodeError:
            continue
        per.setdefault(round(rec["ts"] - t0s[tag], 1), rec)
    return per


def borrow(tag):
    per = load(tag)
    comp = [(v["r"].get(FS_R, 0) / 1e9, v["r"].get(FS_T, 0) / 1e9)
            for t, v in per.items() if 20 <= t < 40]
    mr = sum(r for r, _ in comp) / len(comp)
    mt = sum(t for _, t in comp) / len(comp)
    # reclaim: after t=45, first t where rdma >= 5.7 sustained 1s
    recl = None
    run = 0
    for t in sorted(per):
        if t < 45:
            continue
        r = per[t]["r"].get(FS_R, 0) / 1e9
        run = run + 1 if r >= 5.7 else 0
        if run >= 5 and recl is None:
            recl = t - 45
    # yield: after t=15, first t where rdma <= 3.5 sustained
    yld = None
    run = 0
    for t in sorted(per):
        if t < 15:
            continue
        r = per[t]["r"].get(FS_R, 0) / 1e9
        run = run + 1 if r <= 3.5 else 0
        if run >= 5 and yld is None:
            yld = t - 15
    print("  %-12s comp ratio=%.2f (rdma %.2f/tcp %.2f)  reclaim=%.1fs  yield=%.1fs"
          % (tag, mr / mt if mt > 0.1 else 0, mr, mt,
             recl if recl else -1, yld if yld else -1))


def contention(tag, w0, w1):
    per = load(tag)
    rs = [v["r"].get(FS_R, 0) / 1e9 for t, v in per.items() if w0 <= t < w1]
    ts = [v["r"].get(FS_T, 0) / 1e9 for t, v in per.items() if w0 <= t < w1]
    mr, mt = sum(rs) / len(rs), sum(ts) / len(ts)
    sd = (sum((x - mr) ** 2 for x in rs) / len(rs)) ** 0.5
    eps, run = 0, 0
    for r in rs:
        run = run + 1 if r < 1.5 else 0
        if run == 2:
            eps += 1
    print("  %-12s ratio=%.2f rdma=%.2f tcp=%.2f sd=%.2f eps=%d util=%.2f"
          % (tag, mr / mt if mt > 0.1 else 0, mr, mt, sd, eps, mr + mt))


def repro(tag):
    per = load(tag)
    rs = [v["r"].get(FS_R, 0) / 1e9 for t, v in per.items() if 15 <= t < 235]
    eps, run = 0, 0
    for r in rs:
        run = run + 1 if r < 1.5 else 0
        if run == 2:
            eps += 1
    band = sum(1 for r in rs if 2.7 <= r <= 3.3)
    ratios = []
    for line in (DIR / f"{tag}_rp.txt").read_text().splitlines():
        if "HPFT_RSP" not in line:
            continue
        f = dict(re.findall(r"(\w+)=(\w+)", line.split("HPFT_RSP")[1]))
        bud, lvl = int(f["bud"]) * U, int(f["lvl"]) * U
        if bud > 0.1:
            ratios.append(lvl / bud)
    print("  %-14s eps=%d band=%.0f%% mean=%.2fG min_lvl/bud=%.2f"
          % (tag, eps, 100 * band / len(rs), sum(rs) / len(rs),
             min(ratios) if ratios else -1))


print("borrow/reclaim (V=2 vs V=4):")
for v in (2, 4):
    borrow(f"borrow_v{v}")
print("A-edge A=0.00075 (V=2 vs V=4; cliff check):")
for v in (2, 4):
    contention(f"aedge_v{v}", 15, 55)
print("28fs mesh V=4 (vs V=2 baseline 7cb69fb):")
# handled by separate call to analyze_fair on mesh_v4_rx.jsonl if needed
print("repro V=4 (vs V=2 dev_r*: 0/2/0 episodes):")
for n in (1, 2):
    repro(f"repro_v4_r{n}")
