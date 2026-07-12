#!/usr/bin/env python3
"""MIAD vs AIMD A/B analysis. T2: competition ratio + borrow/reclaim.
T3: fill time (TCP off), reconverge time + overshoot (TCP on)."""
import json
import sys
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
FR = "sgpu01/vf0>sgpu02/vf0|rdma"
FT = "sgpu01/vf0>sgpu02/vf0|tcp"


def load(tag):
    t0 = float((DIR / f"{tag}_t0.txt").read_text().strip())
    per = {}
    for jl in (DIR / f"{tag}_rx.jsonl").read_text().splitlines():
        try:
            rec = json.loads(jl)
        except json.JSONDecodeError:
            continue
        per.setdefault(round(rec["ts"] - t0, 2), rec)
    return t0, per


def win(per, fs, a, b):
    xs = [v["r"].get(fs, 0) / 1e9 for t, v in per.items() if a <= t < b]
    return sum(xs) / len(xs) if xs else 0


def t2(law, tag, target):
    try:
        _, per = load(f"{law}_t2{tag}")
    except FileNotFoundError:
        print("  %-5s t2%s: (no data)" % (law, tag)); return
    mr, mt = win(per, FR, 20, 40), win(per, FT, 20, 40)
    # borrow: TCP exits at 45, RDMA back to ~6G
    recl = None
    ks = sorted(per)
    for t in ks:
        if t < 45:
            continue
        if per[t]["r"].get(FR, 0) / 1e9 >= 5.7:
            recl = t - 45; break
    # steady RDMA-only after TCP fully gone
    solo = win(per, FR, 55, 70)
    print("  %-5s t2%s ratio=%.2f (rdma %.2f/tcp %.2f, tgt %.1f) borrow->%.1fG(%.1fs) "
          % (law, tag, mr / mt if mt > 0.1 else 0, mr, mt, target, solo,
             recl if recl else -1))


def t3(law):
    try:
        t0, per = load(f"{law}_t3")
    except FileNotFoundError:
        print("  %-5s t3: (no data)" % law); return
    ev = {}
    for line in (DIR / f"{law}_t3_events.txt").read_text().splitlines():
        ts, n = line.split()
        ev[n] = float(ts) - t0
    off, on = ev["tcp_off"], ev["tcp_on"]
    # fill: after TCP off, RDMA to >=0.9*40=36G
    fill = None
    for t in sorted(per):
        if t < off:
            continue
        if per[t]["r"].get(FR, 0) / 1e9 >= 36:
            fill = t - off; break
    # reconverge after rejoin: ratio back within 15% of 2.33, sustained
    recon = None
    run = 0
    for t in sorted(per):
        if t < on + 2:
            continue
        rr = per[t]["r"].get(FR, 0) / 1e9
        tt = per[t]["r"].get(FT, 0) / 1e9
        ra = rr / tt if tt > 0.1 else 0
        run = run + 1 if abs(ra - 2.33) / 2.33 <= 0.15 else 0
        if run >= 30 and recon is None:
            recon = t - on
    # overshoot peak of tcp after rejoin
    peak = max((per[t]["r"].get(FT, 0) / 1e9 for t in per if on <= t < on + 30), default=0)
    minr = min((per[t]["r"].get(FR, 0) / 1e9 for t in per if on + 1 <= t < on + 30), default=0)
    print("  %-5s t3 fill(TCP off)=%.1fs  reconverge(TCP on)=%.1fs  tcp_overshoot_peak=%.1fG rdma_min=%.1fG"
          % (law, fill if fill else -1, recon if recon else -1, peak, minr))


print("=== T2 class fairness (6G cap) ===")
for law in ("aimd", "miad"):
    t2(law, "1x1", 1.0)
    t2(law, "3x1", 3.0)
print("=== T3 high-rate churn (40G cap, 7:3=2.33) ===")
for law in ("aimd", "miad"):
    t3(law)
