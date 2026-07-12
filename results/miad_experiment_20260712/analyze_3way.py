#!/usr/bin/env python3
"""Three-way AIMD/MIAD/MIMD comparison: T2 fairness, T3@40G both-cap
(fill+yield), collapse repro (episodes + steady noise)."""
import json
import sys
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
FR = "sgpu01/vf0>sgpu02/vf0|rdma"
FT = "sgpu01/vf0>sgpu02/vf0|tcp"
LAWS = ("aimd", "miad", "mimd")


def load(tag):
    t0 = float((DIR / f"{tag}_t0.txt").read_text().strip())
    per = {}
    for jl in (DIR / f"{tag}_rx.jsonl").read_text().splitlines():
        try:
            per.setdefault(round(json.loads(jl)["ts"] - t0, 2), json.loads(jl))
        except (json.JSONDecodeError, KeyError):
            pass
    return t0, per


def win(per, fs, a, b):
    xs = [v["r"].get(fs, 0) / 1e9 for t, v in per.items() if a <= t < b]
    return sum(xs) / len(xs) if xs else 0


print("=== T2 class fairness @6G ===")
print("%-5s  1:1_ratio  1:1_borrow  3:1_ratio" % "law")
for law in LAWS:
    row = "%-5s  " % law
    try:
        _, p = load(f"{law}_t21x1")
        mr, mt = win(p, FR, 20, 40), win(p, FT, 20, 40)
        recl = next((t - 45 for t in sorted(p) if t >= 45
                     and p[t]["r"].get(FR, 0) / 1e9 >= 5.7), -1)
        row += "%9.2f  %9.1fs  " % (mr / mt if mt > 0.1 else 0, recl)
    except FileNotFoundError:
        row += "%9s  %10s  " % ("-", "-")
    try:
        _, p = load(f"{law}_t23x1")
        mr, mt = win(p, FR, 20, 40), win(p, FT, 20, 40)
        row += "%9.2f" % (mr / mt if mt > 0.1 else 0)
    except FileNotFoundError:
        row += "%9s" % "-"
    print(row)

print("\n=== T3 churn @40G both-cap (7:3=2.33): fill(TCP off) + yield/reconverge(TCP on) ===")
print("%-5s  fill_s  reconv_s  tcp_peak  rdma_min" % "law")
for law in LAWS:
    tag = f"{law}_t3b"
    try:
        t0, per = load(tag)
        ev = dict((n, float(t) - t0) for t, n in
                  (l.split() for l in (DIR / f"{tag}_events.txt").read_text().splitlines()))
    except FileNotFoundError:
        print("%-5s  (no data)" % law); continue
    off, on = ev["tcp_off"], ev["tcp_on"]
    fill = next((t - off for t in sorted(per) if t >= off
                 and per[t]["r"].get(FR, 0) / 1e9 >= 36), -1)
    recon, run = -1, 0
    for t in sorted(per):
        if t < on + 2:
            continue
        rr, tt = per[t]["r"].get(FR, 0) / 1e9, per[t]["r"].get(FT, 0) / 1e9
        ra = rr / tt if tt > 0.1 else 0
        run = run + 1 if abs(ra - 2.33) / 2.33 <= 0.15 else 0
        if run >= 30 and recon < 0:
            recon = t - on
    peak = max((per[t]["r"].get(FT, 0) / 1e9 for t in per if on <= t < on + 30), default=0)
    minr = min((per[t]["r"].get(FR, 0) / 1e9 for t in per if on + 1 <= t < on + 30), default=0)
    print("%-5s  %5.1f  %7.1f  %7.1f  %7.1f" % (law, fill, recon, peak, minr))

print("\n=== collapse repro (240s @6G 1:1): episodes + steady stability ===")
print("%-5s  episodes  band%%  rdma_sd  rdma_min  util" % "law")
for law in LAWS:
    try:
        t0, per = load(f"{law}_repro")
    except FileNotFoundError:
        print("%-5s  (no data)" % law); continue
    sec = {}
    for t, v in per.items():
        if 15 <= t < 235:
            s = int(t)
            sec.setdefault(s, {"r": [], "t": []})
            sec[s]["r"].append(v["r"].get(FR, 0) / 1e9)
            sec[s]["t"].append(v["r"].get(FT, 0) / 1e9)
    rser = [sum(d["r"]) / len(d["r"]) for _, d in sorted(sec.items())]
    tser = [sum(d["t"]) / len(d["t"]) for _, d in sorted(sec.items())]
    mr, mt = sum(rser) / len(rser), sum(tser) / len(tser)
    sd = (sum((x - mr) ** 2 for x in rser) / len(rser)) ** 0.5
    eps, run = 0, 0
    for r in rser:
        run = run + 1 if r < 1.5 else 0
        if run == 2:
            eps += 1
    band = sum(1 for r, t in zip(rser, tser) if abs(r - 3) <= 0.3 and abs(t - 3) <= 0.3)
    print("%-5s  %8d  %4.0f%%  %7.2f  %8.2f  %.1fG"
          % (law, eps, 100 * band / len(rser), sd, min(rser), mr + mt))
