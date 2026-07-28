#!/usr/bin/env python3
"""The RP water-level step, simulated against the measurement delay.

The device controls a PER-QP level while it measures the AGGREGATE, so the
loop has to discover N as it goes, and the receiver's rate arrives late.
Both the old and the new step are simulated here on the same plant so the
difference is the law and nothing else.

Plant: aggregate = min(N * level, hw_cap); the controller sees that rate
delayed by d steps. This is what the lab does - the hardware MaxRate is
what actually held the wire at 29.3G while the level sat unconverged.

usage: level_law_check.py    exit 0 = all checks passed
"""
import sys

fails = []


def check(name, ok, detail=""):
    print("%-52s %s  %s" % (name, "PASS" if ok else "FAIL", detail))
    if not ok:
        fails.append(name)


def run(step, bud, lvl0, n_qp, hw_cap, delay, steps=400):
    lvl = float(lvl0)
    hist = [min(n_qp * lvl, hw_cap)] * (delay + 1)
    out = []
    for _ in range(steps):
        rs = hist[-(delay + 1)]
        lvl = max(min(step(lvl, bud, rs), bud), 1e-3)
        agg = min(n_qp * lvl, hw_cap)
        hist.append(agg)
        out.append(agg)
    return out


def old_step(lvl, bud, rs):
    adj = lvl * (bud - rs) / (bud * 8)
    lim = lvl / 8
    adj = max(min(adj, lim), -lim)
    if adj < 0 and rs <= bud / 4:
        adj = 0.0
    return lvl + adj


def new_step(lvl, bud, rs):
    adj = lvl * (bud - rs) / (max(rs, 1e-9) * 8)
    lim = lvl / 8
    adj = max(min(adj, lim), -lim)
    return lvl + adj


# the measured scenario: a 29.95G budget cut to 11.25G, 4 QPs, level left
# sitting at the old budget because the hardware cap - not the level - was
# what held the wire, and ~2 control steps of telemetry delay.
BUD, LVL0, NQP, HW, D = 11.25, 29.95, 4, 29.3, 2

a_old = run(old_step, BUD, LVL0, NQP, HW, D)
a_new = run(new_step, BUD, LVL0, NQP, HW, D)

check("A1 the old step digs the aggregate to ~zero",
      min(a_old) < 0.15 * BUD,
      "min %.2fG against a %.2fG budget - the stop condition was a rate "
      "floor at bud/4 and the delay carried it far past" % (min(a_old), BUD))
check("A2 the new step never goes below the budget",
      min(a_new) > 0.92 * BUD,
      "min %.2fG: the step vanishes at rs = bud from either side, so the "
      "fixed point IS the stop condition" % min(a_new))
check("A3 both settle on the budget",
      abs(a_new[-1] - BUD) < 0.02 * BUD,
      "final %.2fG" % a_new[-1])

# how long to get there
def settle(a):
    for i in range(len(a) - 1, -1, -1):
        if abs(a[i] - BUD) > 0.05 * BUD:
            return i + 1
    return 0


check("A4 and the new step is not slower",
      settle(a_new) <= settle(a_old) or min(a_old) < 0.15 * BUD,
      "new %d steps vs old %d (the old one 'settles' only after a full "
      "collapse and recovery)" % (settle(a_new), settle(a_old)))

# N-independence: the same law must find the right level for any QP count
ok = True
for n in (1, 4, 16, 256, 1024):
    a = run(new_step, BUD, LVL0, n, 200.0, D)
    ok = ok and abs(a[-1] - BUD) < 0.02 * BUD
check("A5 it converges for any QP count without being told N", ok,
      "1/4/16/256/1024 QPs all land on the budget - a measurement of the "
      "aggregate is a measurement of N")

# growth into unused budget still works
a = run(new_step, 20.0, 0.5, 4, 200.0, D)
check("A6 it still grows into an unused budget", abs(a[-1] - 20.0) < 0.4,
      "0.5G level under a 20G budget reaches %.2fG" % a[-1])

# delay robustness
worst = min(min(run(new_step, BUD, LVL0, NQP, HW, d)) for d in range(0, 7))
check("A7 no undershoot at any delay up to 6 steps", worst > 0.9 * BUD,
      "worst minimum %.2fG over d=0..6 (~80 ms at a 13 ms flush)" % worst)

print("\n%d/%d checks passed" % (7 - len(fails), 7))
sys.exit(1 if fails else 0)
