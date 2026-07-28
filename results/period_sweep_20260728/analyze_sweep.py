#!/usr/bin/env python3
"""What the control period actually buys and costs.

Four quantities per T, chosen so that three of them are PREDICTED FLAT and
only one is predicted to move. A sweep where everything moves tells you
nothing; a sweep where the controls stay put and the one variable behaves
as derived is evidence.

  convergence   single-flow up-step, law-only. k is a wall-clock constant
                and the law discretises on the measured interval, so this
                is predicted FLAT. If it moves, the T-invariance claim is
                wrong and everything else here is moot.
  noise         steady-state sd of r. The measurement window is a fixed
                number of samples, nkeep = rate_window/T + 1, so larger T
                means fewer samples per window - the one thing that could
                legitimately get worse.
  fairness      incast8 Jain and aggregate. Predicted FLAT.
  headroom      receiver tick cost against the period. This is what T buys,
                and it should scale linearly.

usage: analyze_sweep.py [T ...]
"""
import json
import os
import re
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HERE, "results")
TS = [int(x) for x in (sys.argv[1:] or ["1", "2", "4", "8"])]
FS = "sgpu01/vf0>sgpu02/vf0|rdma"
RDMA = [(n, n) for n in range(4)]


def load(path):
    out = []
    try:
        fh = open(path)
    except OSError:
        return out
    for l in fh:
        l = l.strip()
        if not l:
            continue
        try:
            out.append(json.loads(l))
        except ValueError:
            pass
    return out


def upstep(T):
    """law-only settling of the single-flow up-step, via the analyser the
    acceptance runs already use - one implementation of a settling-time
    definition is enough, and it is the one whose numbers are on record."""
    import subprocess
    out = subprocess.run(
        ["python3", os.path.join(HERE, "..", "acceptance_20260727",
                                 "analyze_step.py"),
         "%s/T%d_step_tx.jsonl" % (D, T)],
        capture_output=True, text=True).stdout
    m = re.search(r"UP .*law-only\s+(\d+) ms", out)
    return float(m.group(1)) if m else None


def noise(T):
    """steady sd of r on the single flow, before the competitor joins."""
    rx = load("%s/T%d_step_rx.jsonl" % (D, T))
    try:
        t0 = float(open("%s/T%d_step_t0.txt" % (D, T)).read().strip())
    except OSError:
        return None
    v = [x["r"][FS] / 1e9 for x in rx
         if FS in x.get("r", {}) and 5 <= x["ts"] - t0 <= 14]
    if len(v) < 20:
        return None
    return st.mean(v), st.pstdev(v), 100 * st.pstdev(v) / max(st.mean(v), 1e-9)


def incast(T):
    rx = load("%s/T%d_i8_rx.jsonl" % (D, T))
    try:
        t0 = float(open("%s/T%d_i8_t0.txt" % (D, T)).read().strip())
    except OSError:
        return None
    win = [x for x in rx if 40 <= x["ts"] - t0 <= 85]
    if not win:
        return None
    per = {}
    for i, j in RDMA:
        for c in ("rdma", "tcp"):
            f = "sgpu01/vf%d>sgpu02/vf%d|%s" % (i, j, c)
            vals = [x["r"].get(f, 0) / 1e9 for x in win]
            per[f] = sum(vals) / len(vals)
    v = list(per.values())
    agg = sum(v)
    jain = agg ** 2 / (8 * sum(x * x for x in v)) if any(v) else 0
    # Startup overshoot is the thing the staleness budget actually
    # protects: every flow-set climbs at once on targets computed under
    # "siblings hold still", and k*tau_eff bounds how far they can
    # jointly overshoot before the stale window closes. Steady-state
    # Jain would not show it, so take the peak of the aggregate over the
    # first seconds against the 97G ledger capacity.
    early = [x for x in rx if 0 <= x["ts"] - t0 <= 6]
    peak = max((sum(x.get("r", {}).values()) / 1e9 for x in early),
               default=0.0)
    return jain, agg, peak


def tick(T):
    try:
        s = open("%s/T%d_tick.txt" % (D, T)).read()
    except OSError:
        return None
    m = re.search(r"fast n=(\d+) p50=(\d+) p90=(\d+) p99=(\d+) p999=(\d+) "
                  r"max=(\d+) \| slow n=(\d+) p50=(\d+) max=(\d+) \| "
                  r"over_period=(\d+)", s)
    if not m:
        return None
    g = [int(x) for x in m.groups()]
    return {"n": g[0], "p50": g[1], "p99": g[3], "max": g[5],
            "slow_p50": g[7], "over": g[9]}


print("control period sweep\n")
print("  T     up-step law-only   steady r (mean/sd)      incast8       "
      "fast-tick p50   of T   startup peak")
print("  " + "-" * 96)
for T in TS:
    us = upstep(T)
    nz = noise(T)
    ic = incast(T)
    tk = tick(T)
    print("  %2d ms %13s   %20s  %14s  %11s %6s %8s"
          % (T,
             ("%.0f ms" % us) if us else "-",
             ("%.2fG sd %.3f (%.1f%%)" % nz) if nz else "-",
             ("Jain %.3f %2.0fG" % ic[:2]) if ic else "-",
             ("%d us" % tk["p50"]) if tk else "-",
             ("%.0f%%" % (100.0 * tk["p50"] / (T * 1000))) if tk else "-",
             ("%.0fG" % ic[2]) if ic else "-"))
print()
print("predicted: up-step FLAT (k is wall-clock, law discretises on the")
print("measured interval); incast8 FLAT; 'of T' should fall ~linearly,")
print("which is the capacity T buys. Only the noise column is at risk,")
print("because the measurement window holds rate_window/T + 1 samples.")
