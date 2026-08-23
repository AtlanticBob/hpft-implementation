#!/usr/bin/env python3
"""Observer state over a run, from the 0xded device probe.

The host prints a fixed set of slots, so 0xded reuses them:
  ft=flowtag  bud=level  lvl=paced  avg16=achieved  r=d  s16=cc_rate
  ep=cc_prev  evb32=pace_limited  w8=events  w9=counted cuts
d is the deviation from the policy share in fxp20 (1048576 = the share);
cc_rate is the untouched CC's own rate, in fxp20 of line.
"""
import re, sys
from pathlib import Path
from collections import Counter

DIR = Path(__file__).resolve().parent / "results"
D_ONE = 1 << 20
RSP = re.compile(r"HPFT_RSP ft=0x([0-9a-f]+) bud=(\d+) lvl=(\d+) avg16=(\d+) "
                 r"r=(\d+) s16=(\d+) ep=(\d+) evb32=(\d+) w8=(\d+) w9=(\d+)")
SET = re.compile(r"HPFT_SET ft=0x(de[bcd]) rate=(\d+) .* t0=([\d.]+)")


def rows(tag):
    t0 = float((DIR / f"{tag}_t0.txt").read_text().strip())
    pend, out = None, []
    for l in (DIR / f"{tag}_rp.txt").read_text().splitlines():
        m = RSP.match(l)
        if m:
            pend = m
            continue
        m2 = SET.match(l)
        if m2 and pend is not None:
            if m2.group(1) == "ded" and int(pend.group(1), 16):
                out.append(dict(t=float(m2.group(3)) - t0, pair=m2.group(2),
                                level=int(pend.group(2)), paced=int(pend.group(3)),
                                got=int(pend.group(4)), d=int(pend.group(5)),
                                cc=int(pend.group(6)), ccprev=int(pend.group(7)),
                                pl=int(pend.group(8)), ev=int(pend.group(9)), cuts=int(pend.group(10))))
            pend = None
    return out


for tag in sys.argv[1:]:
    try:
        live = [r for r in rows(tag) if 8 <= r["t"] <= 86]
    except FileNotFoundError:
        print("%-16s no probe data" % tag)
        continue
    if not live:
        print("%-16s no live-pair rows" % tag)
        continue
    ds = sorted(r["d"] / D_ONE for r in live)
    ccs = sorted(r["cc"] / D_ONE for r in live)
    pl = Counter(r["pl"] for r in live)
    low = [(round(r["t"]), r["pair"], round(r["d"] / D_ONE, 3))
           for r in live if r["d"] < D_ONE * 0.9]
    per = {}
    for r in live:
        per.setdefault(r["pair"], []).append((r["t"], r["ev"]))
    evs = 0.0
    for rows_ in per.values():
        rows_ = [x for x in rows_ if x[1]]
        if len(rows_) > 2 and rows_[-1][0] - rows_[0][0] > 10:
            evs += (rows_[-1][1] - rows_[0][1]) / (rows_[-1][0] - rows_[0][0])
    # the absolute count, not the in-window delta: the decreases worth
    # seeing often land while a flow is still ramping, before the window
    cuts = sum(max(v) for v in
               ({p: [r["cuts"] for r in live if r["pair"] == p]
                 for p in {r["pair"] for r in live}}).values())
    print("%-16s d: min=%.2f p50=%.2f | CC's own rate: min=%.3f p50=%.3f | "
          "pace-limited %d/%d | cuts counted %d | DPA %.0f ev/s | d<0.9 at: %s"
          % (tag, ds[0], ds[len(ds) // 2], ccs[0], ccs[len(ccs) // 2],
             pl.get(1, 0), len(live), cuts, evs, low[:4]))
