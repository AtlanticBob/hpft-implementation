#!/usr/bin/env python3
"""Large-scale 3-way: weighted-matrix per-pair class ratio (steady, before
churn) + vf0 churn reconverge + neighbor (vf2) disturbance during vf0 off."""
import json
import sys
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
CW = [(7, 3), (6, 4), (5, 5), (3, 7)]


def load(tag):
    t0 = float((DIR / f"{tag}_t0.txt").read_text().strip())
    per = {}
    for jl in (DIR / f"{tag}_rx.jsonl").read_text().splitlines():
        try:
            per.setdefault(round(json.loads(jl)["ts"] - t0, 2), json.loads(jl))
        except (json.JSONDecodeError, KeyError):
            pass
    ev = dict((n, float(t) - t0) for t, n in
              (l.split() for l in (DIR / f"{tag}_events.txt").read_text().splitlines()))
    return per, ev


def win(per, fs, a, b):
    xs = [v["r"].get(fs, 0) / 1e9 for t, v in per.items() if a <= t < b]
    return sum(xs) / len(xs) if xs else 0


for law, tag in (("aimd", "ls_aimd"), ("miad", "ls_miad"), ("mimd", "ls_mimd")):
    try:
        per, ev = load(tag)
    except FileNotFoundError:
        print("%s: (no data)" % law); continue
    off, on = ev["vf0tcp_off"], ev["vf0tcp_on"]
    print("\n=== %s ===" % law)
    # steady weighted matrix (before churn, [20,40])
    print("  weighted matrix [20,40s] per-pair class ratio (targets 2.33/1.5/1.0/0.43):")
    line = "   "
    for n in range(4):
        r = win(per, "sgpu01/vf%d>sgpu02/vf%d|rdma" % (n, n), 20, 40)
        t = win(per, "sgpu01/vf%d>sgpu02/vf%d|tcp" % (n, n), 20, 40)
        line += "vf%d=%.2f " % (n, r / t if t > 0.1 else 0)
    print(line)
    # vf0 churn reconverge after rejoin (ratio back to 2.33 within 15%)
    FR = "sgpu01/vf0>sgpu02/vf0|rdma"
    FT = "sgpu01/vf0>sgpu02/vf0|tcp"
    recon, run = -1, 0
    for t in sorted(per):
        if t < on + 2:
            continue
        rr, tt = per[t]["r"].get(FR, 0) / 1e9, per[t]["r"].get(FT, 0) / 1e9
        ra = rr / tt if tt > 0.1 else 0
        run = run + 1 if abs(ra - 2.33) / 2.33 <= 0.15 else 0
        if run >= 30 and recon < 0:
            recon = t - on
    # ringing: count ratio zero-crossings of (ratio-2.33) in [on, on+25]
    seq = []
    for t in sorted(per):
        if on <= t < on + 25:
            rr, tt = per[t]["r"].get(FR, 0) / 1e9, per[t]["r"].get(FT, 0) / 1e9
            if tt > 0.1:
                seq.append(rr / tt - 2.33)
    cross = sum(1 for a, b in zip(seq, seq[1:]) if a * b < 0)
    # neighbor vf2 disturbance during vf0 off
    v2r = win(per, "sgpu01/vf2>sgpu02/vf2|rdma", off + 5, on)
    v2t = win(per, "sgpu01/vf2>sgpu02/vf2|tcp", off + 5, on)
    print("  vf0 rejoin: reconverge=%.1fs  ringing(zero-crossings/25s)=%d" % (recon, cross))
    print("  neighbor vf2 during vf0-off: ratio=%.2f (tgt 1.0)" % (v2r / v2t if v2t > 0.1 else 0))
