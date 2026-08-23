#!/usr/bin/env python3
"""Per-run summary across the ECN A/B: stall seconds, Jain, CNP volume.
CNP volume comes from the device probe (0xdec: field ft= is g_hpft_cnp_any,
a global counter of CNP events reaching the PCC callback); we report the
run total and the largest 2 s increment (burstiness)."""
import re, sys, json
from pathlib import Path
from analyze import rep
DIR = Path(__file__).resolve().parent / "results"
RSP = re.compile(r"HPFT_RSP ft=0x([0-9a-f]+) bud=(\d+) lvl=(\d+) avg16=(\d+) r=(\d+) s16=(\d+)")
SET = re.compile(r"HPFT_SET ft=0x(de[bc]) rate=(\d+) .* t0=([\d.]+)")
def cnp_stats(tag):
    lines = (DIR / f"{tag}_rp.txt").read_text().splitlines()
    pend = None; series = {}
    for l in lines:
        m = RSP.match(l)
        if m: pend = m; continue
        m2 = SET.match(l)
        if m2 and pend is not None:
            if m2.group(1) == "dec":
                ts = float(m2.group(3)); cnp_any = int(pend.group(1), 16)
                series.setdefault(round(ts), cnp_any)
            pend = None
    ts = sorted(series)
    if len(ts) < 2: return None, None
    vals = [series[t] for t in ts]
    inc = [(vals[i+1] - vals[i]) & 0xffffffff for i in range(len(vals) - 1)]
    return vals[-1] - vals[0], max(inc)
def stalls(tag):
    P, r = rep(tag)
    return r["stall_at"], r
if __name__ == "__main__":
    print("%-16s %-8s %6s %6s %6s %8s %8s  %s" % ("run", "ecn", "stall_s", "jain", "agg", "cnp_tot", "cnp_2s", "stall_at"))
    for tag, ecn in [x.split(":") for x in sys.argv[1:]]:
        st, r = stalls(tag); tot, burst = cnp_stats(tag)
        print("%-16s %-8s %6d %6.3f %6.1f %8s %8s  %s" % (tag, ecn, len(st), r["jain"], r["agg"], tot, burst, st[:8]))
