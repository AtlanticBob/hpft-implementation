#!/usr/bin/env python3
"""Offline checks for the receiver's decision logic.

law_check covers the sender's law, loop_dryrun covers the sender's
process, fastfill_test covers the allocation arithmetic. The receiver's
DECISIONS - which flow-sets count as backlogged, what target comes out,
who appears in telemetry - had only lab A/B coverage, and each of them is
one edit away from silently undoing a result that took a day to
establish: dropping the congestion entry returns incast fairness to Jain
0.68, and dropping the membership rule cuts the sender's effective k by
6x. Those belong in a test that runs in a second, not in an experiment.

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


# ---------------------------------------------------------------- backlog
# Backlog discrimination answers "is this flow-set held back by us, or does
# it simply have nothing to send". Every entry is now STRUCTURAL: a node of
# the policy tree is saturated, so everything below it is contending. The
# share test that used to sit alongside them is gone - it thresholded the
# flow-set's own measured rate, which is the one quantity the receiver
# cannot measure per sender, and with every VM carrying a sold MaxRate
# there is no node it covered that a saturation test does not.
BIG = 97e9
DELTA = 0.15
ACT = {"h/a>h/d|rdma": 8e9, "h/b>h/d|rdma": 2e9}

d = rx_agent.demand_estimate(ACT, DELTA, BIG, congested=False)
check("A1 no saturated node anywhere: the plain ratchet",
      abs(d["h/a>h/d|rdma"] - 8e9 * 1.15) < 1
      and abs(d["h/b>h/d|rdma"] - 2e9 * 1.15) < 1,
      "spare capacity everywhere, so a rate below the share is a rate "
      "nobody is holding down")

d = rx_agent.demand_estimate(ACT, DELTA, BIG, congested=True)
check("A2 root saturated: both claim their share",
      d["h/a>h/d|rdma"] == BIG and d["h/b>h/d|rdma"] == BIG,
      "neither is near its share, but there is no spare capacity to have "
      "declined")

d = rx_agent.demand_estimate(ACT, DELTA, BIG, congested=False,
                             saturated_dsts={"h/d"})
check("A3 their dst VM at its MaxRate does the same one layer down",
      d["h/a>h/d|rdma"] == BIG and d["h/b>h/d|rdma"] == BIG,
      "root is at 10 percent here; without this entry each ceiling is "
      "computed as though the flow-set were alone under the cap")

d = rx_agent.demand_estimate({"h/a>h/d|rdma": 0.0, "h/b>h/d|rdma": 8e9},
                             DELTA, BIG, congested=True,
                             saturated_dsts={"h/d"})
check("A4 an idle flow-set is never backlogged, whatever is saturated",
      d["h/a>h/d|rdma"] == 0.0, "r=0 outranks every entry; borrowing must "
      "still work")

d = rx_agent.demand_estimate(ACT, DELTA, BIG, congested=False,
                             saturated_dsts={"h/other"})
check("A5 saturation elsewhere in the tree changes nothing here",
      abs(d["h/a>h/d|rdma"] - 8e9 * 1.15) < 1,
      "the entry is per node, on the flow-set's own path")

# ------------------------------------------------------------- hysteresis
rc = rx_agent.RootCongestion()
C = 97e9
seq = [(0.5, False), (0.94, False), (0.96, True), (0.90, True),
       (0.86, True), (0.84, False), (0.99, True)]
ok = True
for frac, want in seq:
    got = rc.update(frac * C, C)
    ok = ok and got == want
check("B1 congestion state is hysteretic (enter .95, leave .85)", ok,
      "a single threshold would chatter: entering closes borrowing, which "
      "drops utilisation, which looks like 'not congested'")

# --------------------------------------------------------------- target
GAMMA = 0.25
ceils = {"a": 20e9}
for s_val in (0.0, 0.3, 1.0):
    u = ceils["a"] * (1.0 - GAMMA * s_val)
    lo, hi = (1 - GAMMA) * ceils["a"], ceils["a"]
    if not (lo - 1 <= u <= hi + 1):
        check("C1 target stays inside the discount band", False,
              "s=%.1f -> u=%.3fG" % (s_val, u / 1e9))
        break
else:
    check("C1 target stays inside the discount band", True,
          "u in [0.75, 1.00] x ceil for s in [0,1]")

# ------------------------------------------------------- V anchored on C
ep = {"v_seconds": 0.2, "headroom": 0.03}


def v_of(cap):
    return ep["v_seconds"] * ep["headroom"] * cap


m = rx_agent.VQMarker(v_of(100e9))
check("D1 V = v_seconds x headroom x C", abs(m.v_full - 600e6) < 1e3,
      "C=100G -> V=%.0f Mbit" % (m.v_full / 1e6))
m.vq["f"] = 500e6
m.set_v(v_of(25e9))
check("D2 V follows a link-speed change", abs(m.v_full - 150e6) < 1e3,
      "C=25G -> V=%.0f Mbit; zeta = 0.5*sqrt(kV/(gamma*ehat)) has ehat "
      "scaling with C, so V must too or damping drifts with link rate"
      % (m.v_full / 1e6))
marks = m.step({"f": 30e9}, {"f": 1e9}, {"f": 1e9}, 0.001)
check("D3 a ledger above the new V is clipped, not rescaled",
      m.vq["f"] <= m.v_full + 1,
      "vq %.0f Mbit <= V %.0f Mbit" % (m.vq["f"] / 1e6, m.v_full / 1e6))

# ------------------------------------------------- telemetry membership
sched = rx_agent.Scheduler(
    {"vms": {"h/b": {"weight": 1, "max_rate_bps": 20000000000,
                     "class_weights": {"tcp": 1, "rdma": 1}}},
     "per_sender_weights": {}}, 200e9, 0.03, 0.15)
sched.set_downlink(100e9)
active = {"h/a>h/b|rdma": 5e9, "h/a>h/b|tcp": 0.0}   # tcp present but at 0
demand = {f: r * 1.15 for f, r in active.items()}
ents, ceil = sched.entitlements(active, demand)
marker = rx_agent.VQMarker(600e6)
marks = marker.step(active, ents, ceil, 0.001)
targets = {f: ceil.get(f, ents.get(f, 0.0)) * (1.0 - GAMMA * marks.get(f, 0.0))
           for f in active}
check("E1 a flow-set at r=0 is still reported",
      "h/a>h/b|tcp" in targets,
      "membership is the flow-table key, not the instantaneous rate "
      "(§3.4); dropping it cost the sender 6x its effective k")
check("E2 and its target is its ceiling, not zero",
      targets["h/a>h/b|tcp"] > 0,
      "u=%.2fG" % (targets["h/a>h/b|tcp"] / 1e9))
check("E3 the marker alone would have dropped it",
      "h/a>h/b|tcp" not in marks,
      "VQMarker pops an idle drained flow-set - which is why targets are "
      "built from the active set and not from marks")

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


# --------------------------- node saturation (the structural entry)
ns = rx_agent.NodeSaturation()
CAPS = {"h/d": 30e9}
act = {"h/a>h/d|rdma": 29.3e9, "h/b>h/d|rdma": 29.3e9}
check("H1 a dst VM at its cap makes every sender to it backlogged",
      ns.update(act, CAPS) == {"h/d"},
      "the SUM of the per-sender estimates is the exact vport total, so "
      "this test needs no split - which is the point")

d = rx_agent.demand_estimate(act, DELTA, BIG, congested=False,
                             saturated_dsts={"h/d"})
check("H2   ... so both ceilings collapse to the joint share",
      d["h/a>h/d|rdma"] == BIG and d["h/b>h/d|rdma"] == BIG,
      "without it each e-hat is computed with its OWN demand infinite and "
      "the two sum to 2x the cap; gamma=0.25 cannot claw that back")

d = rx_agent.demand_estimate({"h/a>h/d|rdma": 29.3e9, "h/b>h/d|rdma": 0.0},
                             DELTA, BIG, congested=False,
                             saturated_dsts={"h/d"})
check("H3 an idle sender under a saturated node is still idle",
      d["h/b>h/d|rdma"] == 0.0, "r=0 outranks every backlog entry")

ns2 = rx_agent.NodeSaturation()
ns2.update({"h/a>h/d|rdma": 29.3e9}, CAPS)
check("H4 the node test is hysteretic like the root test",
      ns2.update({"h/a>h/d|rdma": 27.0e9}, CAPS) == {"h/d"}
      and ns2.update({"h/a>h/d|rdma": 20.0e9}, CAPS) == set(),
      "0.90 stays saturated, 0.67 leaves - entering closes borrowing, "
      "which lowers utilisation, which must not immediately read as free")

check("H5 a dst with no MaxRate is never saturated",
      rx_agent.NodeSaturation().update(act, {}) == set(),
      "an unsold VM has no node capacity to saturate")


print("\n%d/%d checks passed" % (26 - len(fails), 26))
sys.exit(1 if fails else 0)

