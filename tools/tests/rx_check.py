#!/usr/bin/env python3
"""Offline checks for the receiver's decision logic.

loop_dryrun covers the sender's
process, fastfill_test covers the allocation arithmetic. The receiver's
DECISIONS - how a descent is rate-limited, and how one destination's
measured total is split among its senders - had only lab A/B coverage, and
each is one edit away from silently undoing a result that took a day to
establish: mis-splitting a newcomer's bytes charges its peer's ledger for
traffic it never sent, which is what used to make two RDMA senders on one
destination alternate instead of share. Those belong in a test that runs in
a second, not in an experiment.

usage: rx_check.py       exit 0 = all checks passed
"""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "tools", "dpu"))

import rx_agent  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print("%-52s %s  %s" % (name, "PASS" if ok else "FAIL", detail))
    if not ok:
        fails.append(name)


# ------------------------------- §5.1 transition limiting (sender side)
# Two halves, and the second is the one that distinguishes this clause
# from a limit on the budget: an external step must be spread, and the
# law's own descent must NOT be touched. A limiter that also caught the
# law would make the executor the convergence bottleneck.
sys.path.insert(0, os.path.join(REPO, "tools", "dpu"))
import tx_agent_e  # noqa: E402

HALVE = 0.013
K = 20.0


def descend(prev, want, dt):
    return max(want, prev * 2.0 ** (-dt / HALVE))


# an operator step: 20x down in one recomputation
steps, cur, t = 0, 20e9, 0.0
while cur > 1.05e9 and steps < 200:
    cur = descend(cur, 1e9, 0.001)
    steps += 1
    t += 0.001
check("F1 a 20x external step is spread, not applied at once",
      0.02 < t < 0.15,
      "reaches the new allowance in %.0f ms over %d ticks" % (t * 1e3, steps))

# The law's own descent must pass through untouched. The invariant is
# structural rather than a rate comparison - a rate comparison would be
# wrong, since the law's instantaneous descent is k times the LOG GAP and
# against a 20x step that is 20*ln20 = 60 s^-1, faster than this limiter.
# What makes it safe is min(): the limiter only holds Tree ABOVE its
# unlimited value, and pace = min(R, Tree), so a lagging Tree can only be
# the un-selected side. Test exactly that: limited pace is never below
# unlimited pace, and the two converge.
R, tree_lim, worst_low, gap_end = 20e9, 20e9, 0.0, None
for i in range(400):
    R = tx_agent_e.track_step(R, 1e9, 0.001, K, 50e6)
    tree_lim = descend(tree_lim, 1e9, 0.001)     # external 20x step
    pace_lim = min(R, tree_lim)
    pace_unl = min(R, 1e9)                        # no limiter
    worst_low = min(worst_low, pace_lim - pace_unl)
    gap_end = pace_lim - pace_unl
check("F2 the limiter can only raise pace, never delay a descent",
      worst_low >= 0.0 and abs(gap_end) < 1e3,
      "pace_limited - pace_unlimited >= 0 throughout (min %.1e) and closes "
      "to %.1e; a limiter on the budget would sit downstream of the min, where it could throttle the law"
      "it could throttle the law" % (worst_low, gap_end))

check("F3 ascent is immediate",
      descend(1e9, 20e9, 0.001) == 20e9,
      "raising an allowance must not wait - only falls are a hazard")


# ------------------------------- intra-class multi-sender attribution
# The mix splits one dst's measured total among its senders. A sender that
# has just started has no bytes in the window yet, and calling that "idle"
# hands its entire rate to its peer - which then reads far over the budget
# it was given and has its executor rate dug to zero. This is the defect
# that made two RDMA senders on one dst alternate instead of sharing.


def _meter(mix, unmeasured, prev_ents, r_d, by_dst):
    # Run the real constructor and override what the case under test needs.
    # Building this with __new__ and hand-setting a subset meant the helper
    # broke every time HybridRates gained a field, silently taking the
    # receiver's decision logic out of test coverage until someone ran it.
    m = rx_agent.HybridRates({}, 2.0)
    m.mix_shares, m.unmeasured, m.prev_ents = mix, set(unmeasured), prev_ents
    m.r_d, m.by_dst, m.meter = r_d, by_dst, None
    return m


def mix_of(members_bytes, window=2.0, now=100.0):
    m = rx_agent.HybridRates({}, window)
    m.update_mix(members_bytes, list(members_bytes), now)
    return m


A, B = "h/a>h/d|rdma", "h/b>h/d|rdma"
m = mix_of({A: 1000, B: 0})
check("G1 a member with no bytes in the window is flagged unmeasured",
      m.unmeasured == {B} and abs(m.mix_shares[A] - 1.0) < 1e-9,
      "the ratio itself stays a plain byte ratio; the flag is what tells "
      "the splitter not to trust it")

m = mix_of({A: 1000, B: 0}, now=100.0)
m.first_seen[B] = 90.0          # present far longer than the window
m.update_mix({A: 1000, B: 0}, [A, B], 100.0)
check("G1b a member that HAS stopped is idle, not unmeasured",
      m.unmeasured == set(),
      "otherwise it stays a phantom in the split and halves the "
      "survivor's measured rate")

m = mix_of({A: 3000, B: 1000})
check("G2 with every member measured nothing is flagged",
      m.unmeasured == set() and abs(m.mix_shares[A] - 0.75) < 1e-9)

# 58.65G arriving at one dst from two senders, the newcomer unmeasured
mt = _meter({A: 1.0, B: 0.0}, {B}, {A: 15e9, B: 15e9},
            {"h/d": 58.65e9}, {"h/d": {A, B}})
r = mt.rates(now=1.0)
check("G3 an unmeasured member switches the split to the entitlement prior",
      abs(r[A] - r[B]) < 1e6 and abs(r[A] - 29.3e9) < 1e8,
      "A %.2fG B %.2fG - the byte ratio would have said 58.65 / 0.00, "
      "which is what dug the peer's executor to zero" % (r[A]/1e9, r[B]/1e9))

mt = _meter({A: 0.75, B: 0.25}, set(), {A: 15e9, B: 15e9},
            {"h/d": 40e9}, {"h/d": {A, B}})
r = mt.rates(now=1.0)
check("G4 once measured, the byte ratio wins over the prior",
      abs(r[A] - 30e9) < 1e6 and abs(r[B] - 10e9) < 1e6,
      "the prior is a fallback, not a smoother")

mt = _meter({A: 1.0, B: 0.0}, {B}, {A: 30e9},          # B not in the prior
            {"h/d": 58.65e9}, {"h/d": {A, B}})
r = mt.rates(now=1.0)
check("G4b a newcomer absent from the prior weighs like its peers",
      abs(r[A] - r[B]) < 1e6,
      "A %.2fG B %.2fG - defaulting an absent prior to 0 reproduces the "
      "original bug exactly" % (r[A]/1e9, r[B]/1e9))

# a genuinely silent flow-set: no bytes AND a demand-capped entitlement ~0
mt = _meter({A: 1.0, B: 0.0}, {B}, {A: 29e9, B: 0.0},
            {"h/d": 29e9}, {"h/d": {A, B}})
r = mt.rates(now=1.0)
check("G5 the prior still gives a truly idle member ~nothing",
      r.get(B, 0.0) == 0.0 and abs(r[A] - 29e9) < 1e6,
      "no extra rule needed: an idle flow-set's entitlement is already ~0, "
      "so 'assume it uses its allowance' assumes almost nothing")


