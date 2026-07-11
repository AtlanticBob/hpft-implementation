#!/usr/bin/env python3
"""D3a/D3b analysis: yield/reclaim per churn cycle.

For each TCP-on event: time until the pair's RDMA wire rate first enters
the yield band (cap/2 +-10%). For each TCP-off event: time until RDMA
first recovers to >= 0.9 x cap (the historical reclaim criterion). Also:
reach-rate (cycles where the band was reached at all before the next
event), RDMA floor (pin detector), and drift (last-3 vs first-3 cycles'
reclaim plateau).

Usage: analyze_churn.py <wave.tsv> <rx.jsonl> [cap_gbps=6]
"""
import json
import sys
from collections import defaultdict

wave_f, rx_f = sys.argv[1], sys.argv[2]
CAP = float(sys.argv[3]) if len(sys.argv) > 3 else 6.0
YLD = (CAP / 2 * 0.9, CAP / 2 * 1.1)
RCL = CAP * 0.9

events = defaultdict(list)          # pair -> [(kind, t)]
for line in open(wave_f):
    kind, i, c, ts = line.split("\t")
    events[int(i)].append((kind, float(ts)))

series = defaultdict(list)          # pair -> [(ts, rdma_bps)]
for line in open(rx_f):
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        continue
    for f, v in rec["r"].items():
        if f.endswith("|rdma") and f.startswith("sgpu01/vf"):
            series[int(f[9])].append((rec["ts"], v))

def first_time(ser, t0, t1, pred):
    for ts, v in ser:
        if t0 <= ts < t1 and pred(v / 1e9):
            return ts - t0
    return None

def pct(v, q):
    s = sorted(v)
    return s[min(len(s) - 1, int(len(s) * q))] if s else float("nan")

for pair in sorted(events):
    ev = sorted(events[pair], key=lambda e: e[1])
    ser = sorted(series.get(pair, []))
    ylds, rcls, plateaus = [], [], []
    ymiss = rmiss = 0
    for k, ((kind, t0), (_, t1)) in enumerate(zip(ev, ev[1:] + [("end", ev[-1][1] + 30)])):
        if kind == "on":
            d = first_time(ser, t0, t1, lambda g: YLD[0] <= g <= YLD[1])
            ylds.append(d) if d is not None else (ymiss := ymiss + 1)
        else:
            d = first_time(ser, t0, t1, lambda g: g >= RCL)
            if d is None:
                rmiss += 1
            else:
                rcls.append(d)
                tail = [v / 1e9 for ts, v in ser if t1 - 2 <= ts < t1]
                if tail:
                    plateaus.append(sum(tail) / len(tail))
    lows = [v / 1e9 for ts, v in ser
            if ev[0][1] <= ts <= ev[-1][1]]
    drift = ""
    if len(plateaus) >= 6:
        drift = " plateau first3=%.2f last3=%.2f" % (
            sum(plateaus[:3]) / 3, sum(plateaus[-3:]) / 3)
    print("pair%d cycles=%d | yield p50=%.2fs p95=%.2fs miss=%d | "
          "reclaim p50=%.2fs p95=%.2fs miss=%d | rdma_min=%.2fG%s"
          % (pair, sum(1 for k, _ in ev if k == "on"),
             pct(ylds, .5), pct(ylds, .95), ymiss,
             pct(rcls, .5), pct(rcls, .95), rmiss,
             min(lows) if lows else float("nan"), drift))
