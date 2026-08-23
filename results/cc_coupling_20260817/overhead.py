#!/usr/bin/env python3
"""What the coupling costs, measured rather than estimated.

Device side: dbg_hits is the RP's per-pair event counter, so its growth
rate is how many events the DPA actually processed per second. If the
coupled arm's extra work on the rate path mattered, this is where it would
show up as fewer events handled under the same offered load.

Agent side: the rx/tx agents log one record per logging tick; the gap
distribution says whether the 1 ms control period is still being met.
"""
import json, re, sys
from pathlib import Path
from statistics import median

DIR = Path(__file__).resolve().parent / "results"
RSP = re.compile(r"HPFT_RSP ft=0x([0-9a-f]+) bud=(\d+) lvl=(\d+) avg16=(\d+) "
                 r"r=(\d+) s16=(\d+) ep=(\d+) evb32=(\d+) w8=(\d+) w9=(\d+) w10=(\d+)")
SET = re.compile(r"HPFT_SET ft=0x(de[bc]) rate=(\d+) .* t0=([\d.]+)")


def dpa_events(tag):
    """events/s the DPA processed, summed over live pairs."""
    pend, seen = None, {}
    for l in (DIR / f"{tag}_rp.txt").read_text().splitlines():
        m = RSP.match(l)
        if m:
            pend = m
            continue
        m2 = SET.match(l)
        if m2 and pend is not None:
            if m2.group(1) == "dec":
                p, hits, ts = m2.group(2), int(pend.group(5)), float(m2.group(3))
                if int(pend.group(6)):          # a live pair
                    seen.setdefault(p, []).append((ts, hits))
            pend = None
    tot = 0.0
    for p, rows in seen.items():
        rows = [r for r in rows if r[1]]
        if len(rows) > 2:
            dt = rows[-1][0] - rows[0][0]
            if dt > 10:
                tot += (rows[-1][1] - rows[0][1]) / dt
    return tot


def agent_ticks(tag, side):
    """median and p99 gap between logged ticks, ms."""
    ts = []
    for jl in (DIR / f"{tag}_{side}.jsonl").read_text().splitlines():
        try:
            ts.append(json.loads(jl)["ts"])
        except Exception:
            pass
    g = sorted((b - a) * 1e3 for a, b in zip(ts, ts[1:]) if 0 < b - a < 1)
    if not g:
        return None
    return median(g), g[int(len(g) * 0.99)]


print("%-14s %14s %22s %22s" % ("run", "DPA events/s", "rx tick gap p50/p99 ms",
                                "tx tick gap p50/p99 ms"))
for tag in sys.argv[1:]:
    try:
        e = dpa_events(tag)
    except FileNotFoundError:
        e = float("nan")
    r = agent_ticks(tag, "rx")
    t = agent_ticks(tag, "tx")
    print("%-14s %14.0f %22s %22s"
          % (tag, e,
             "%.1f / %.1f" % r if r else "-",
             "%.1f / %.1f" % t if t else "-"))
