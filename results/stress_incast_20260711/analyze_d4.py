#!/usr/bin/env python3
"""D4 analysis. Per phase: per-flow steady means vs weighted root shares,
spread, ping distribution (physical queueing), and for d4d the join
re-convergence time."""
import json
import re
import sys
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")


def series(tag, flows, w0, w1):
    t0 = float((DIR / f"{tag}_t0.txt").read_text().strip())
    per = {i: {} for i in flows}
    for line in (DIR / f"{tag}_rx.jsonl").read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not (t0 + w0 <= rec["ts"] < t0 + w1):
            continue
        s = int(rec["ts"] - t0)
        for i in flows:
            f = "sgpu01/vf%d>sgpu02/vf%d|rdma" % (i, i)
            per[i].setdefault(s, []).append(rec["r"].get(f, 0))
    return {i: [sum(v) / len(v) / 1e9 for _, v in sorted(d.items())]
            for i, d in per.items() if d}


def ping_stats(tag):
    txt = (DIR / f"{tag}_ping.txt").read_text()
    m = re.search(r"min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)", txt)
    loss = re.search(r"([\d.]+)% packet loss", txt)
    return ("avg=%sms max=%sms loss=%s%%"
            % (m.group(2), m.group(3), loss.group(1)) if m else "n/a")


def phase(tag, flows, weights, cap_g):
    ser = series(tag, flows, 20, 115)
    tot_w = sum(weights[i] for i in ser)
    print("== %s (root %.1fG, ping %s)" % (tag, cap_g, ping_stats(tag)))
    means = {}
    for i in sorted(ser):
        m = sum(ser[i]) / len(ser[i])
        sd = (sum((x - m) ** 2 for x in ser[i]) / len(ser[i])) ** 0.5
        share = cap_g * weights[i] / tot_w
        means[i] = m
        print("  vf%d mean=%.2fG sd=%.2f share=%.2fG err=%+.1f%%"
              % (i, m, sd, share, (m - share) / share * 100))
    if len(means) > 1 and len(set(weights[i] for i in means)) == 1:
        vals = list(means.values())
        print("  spread=%.1f%%  aggregate=%.1fG"
              % ((max(vals) - min(vals)) / max(vals) * 100, sum(vals)))
    return means


P = 0.97
phase("d4a", [0, 1, 2, 3], {i: 1 for i in range(4)}, 100 * P)
phase("d4b", [0, 1, 2, 3], {i: 1 for i in range(4)}, 25 * P)
phase("d4c5", [0, 1], {0: 5, 1: 1}, 25 * P)
phase("d4c10", [0, 1], {0: 10, 1: 1}, 25 * P)

# d4d: join convergence
t0 = float((DIR / "d4d_t0.txt").read_text().strip())
tj = float((DIR / "d4d_join.txt").read_text().strip())
per = {}
for line in (DIR / "d4d_rx.jsonl").read_text().splitlines():
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        continue
    s = round(rec["ts"] - tj, 1)
    if -5 <= s <= 30:
        for i in range(4):
            f = "sgpu01/vf%d>sgpu02/vf%d|rdma" % (i, i)
            per.setdefault(s, [0] * 4)
        for i in range(4):
            f = "sgpu01/vf%d>sgpu02/vf%d|rdma" % (i, i)
            per[s][i] = rec["r"].get(f, 0) / 1e9
share = 100 * P / 4
conv = None
run = 0
for s in sorted(per):
    if s < 0.5:
        continue
    vals = per[s]
    if all(abs(v - share) / share <= 0.10 for v in vals):
        run += 1
        if run >= 10 and conv is None:
            conv = s
    else:
        run = 0
print("== d4d join: share=%.2fG converged(all within 10%% for 1s) at t+%.1fs (ping %s)"
      % (share, conv if conv else -1, ping_stats("d4d")))
