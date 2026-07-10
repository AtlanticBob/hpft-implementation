#!/usr/bin/env python3
"""M4 sensitivity sweep analysis: one row per run."""
import json
import statistics

FS = "sgpu01/vf0>sgpu02/vf0|rdma"
RUNS = [("base", "A=0.1G b=.3 V=2"), ("a_lo", "A=0.05G"), ("a_hi", "A=0.25G"),
        ("a_xhi", "A=0.5G"), ("b_lo", "beta=.15"), ("b_hi", "beta=.5"),
        ("v_lo", "V=1T"), ("v_hi", "V=4T")]

print("%-8s %-14s %7s %6s %6s %7s | %6s %7s %7s" %
      ("run", "delta", "mean6G", "in5%", "sawG", "p5", "cross", "settle", "under"))
for name, label in RUNS:
    rx = [json.loads(l) for l in open("%s_rx.jsonl" % name)]
    t0 = float(open("%s_t0.txt" % name).read())
    ts_ = float(open("%s_step.txt" % name).read())
    t1 = float(open("%s_t1.txt" % name).read())
    run = [r for r in rx if t0 <= r["ts"] <= t1 and FS in r.get("r", {})]
    if not run:
        print("%-8s NO DATA" % name)
        continue
    start = next(r["ts"] for r in run if r["r"][FS] > 1e8)
    ss = [r["r"][FS] for r in run if start + 10 <= r["ts"] <= ts_ - 1]
    mean = statistics.mean(ss)
    saw = statistics.pstdev(ss)
    inb = 100 * sum(1 for v in ss if abs(v - 6e9) <= .3e9) / len(ss)
    p5 = sorted(ss)[int(len(ss) * .05)]
    # step: engagement = first e<=4.5G
    e4 = next((r["ts"] for r in run if r["ts"] >= ts_ and
               0 < r["e"].get(FS, 1e18) <= 4.5e9), None)
    cross = settle = under = None
    if e4:
        post = [(r["ts"], r["r"][FS]) for r in run if r["ts"] >= e4]
        cross = next((ts for ts, v in post if v <= 4.2e9), None)
        under = min((v for ts, v in post if ts <= e4 + 3), default=None)
        # settle: first t in band with the following 10 ticks median in band
        for i, (ts, v) in enumerate(post):
            if ts <= (cross or e4):
                continue
            win = [x for _, x in post[i:i + 10]]
            if win and abs(statistics.median(win) - 4e9) <= .2e9:
                settle = ts
                break
    fmt = lambda x, s=1: ("%.1f" % ((x - e4) / .05)) if x else "-"
    print("%-8s %-14s %6.2fG %5.0f%% %5.2fG %6.2fG | %6s %7s %6.2fG" %
          (name, label, mean / 1e9, inb, saw / 1e9, p5 / 1e9,
           fmt(cross), fmt(settle), (under or 0) / 1e9))
