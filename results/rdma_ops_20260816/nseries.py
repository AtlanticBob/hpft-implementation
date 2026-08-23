#!/usr/bin/env python3
"""qp_count (N) time series per pair from the 0xdec probe: histogram of N
values seen over the run, per tag. Confirms whether N ever collapses."""
import re, sys
from pathlib import Path
from collections import Counter
DIR = Path(__file__).resolve().parent / "results"
RSP = re.compile(r"HPFT_RSP ft=0x([0-9a-f]+) bud=(\d+) lvl=(\d+) avg16=(\d+) r=(\d+) s16=(\d+)")
SET = re.compile(r"HPFT_SET ft=0x(de[bc]) rate=(\d+) .* t0=([\d.]+)")
for tag in sys.argv[1:]:
    t0 = float((DIR / f"{tag}_t0.txt").read_text().strip())
    pend = None; hist = Counter(); low = []
    for l in (DIR / f"{tag}_rp.txt").read_text().splitlines():
        m = RSP.match(l)
        if m: pend = m; continue
        m2 = SET.match(l)
        if m2 and pend is not None:
            if m2.group(1) == "dec":
                ft = int(pend.group(6)); n = int(pend.group(3)); ts = float(m2.group(3)) - t0
                if ft and 8 <= ts <= 86:          # a live pair, steady window
                    hist[n] += 1
                    if n < 4: low.append((round(ts), m2.group(2), n))
            pend = None
    print("%-14s N histogram (live pairs, t in [8,86]): %s   N<4 at: %s" % (tag, dict(sorted(hist.items())), low[:10]))
