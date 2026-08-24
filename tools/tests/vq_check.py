#!/usr/bin/env python3
"""Offline closed-loop check of the vq law arm (branch vq-sched). No lab.

Runs the PRODUCTION receiver pieces (Scheduler + VirtualQueueScheduler)
against the PRODUCTION sender law (vq_step) in a fluid simulation with
the lab's loop delay, and checks the properties the redesign proposal
claims:

  V1 two backlogged equal-weight flow-sets that start at 80/20 of a
     shared VM cap end up at 50/50 (the queue is what fixes the
     equilibrium: the sender only ever aims eps above the hint)
  V2 the steady state is a bounded sawtooth (amplitude ~eps) with a
     small standing virtual delay - the signal is live at equilibrium
  V3 borrowing: a demand-limited sibling leaves capacity, the backlogged
     one takes it, and gives it back when the sibling's demand returns,
     within max(2.6/k, ~3*d_repay) plus the loop delay
  V4 the drain loop is stable at the lab's tau_eff with the registry's
     d_repay_s (delay-ODE bound), and would not be at d_repay_s ~ tau_eff/2
  V5 a hint that is WRONG HIGH (2x, e.g. an attribution error) does not
     let the flow-set keep 2x: the queue pulls it back to its share

Usage: python3 vq_check.py [path/to/lab-registry.json]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "tools", "dpu"))

import rx_agent          # noqa: E402
import tx_agent_e        # noqa: E402

REG = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    REPO, "config", "lab-registry.json")
reg = json.load(open(REG))
EP = reg["e_params"]
K = EP["k"]
T = EP["period_ms"] / 1e3
EPS = EP.get("eps", 0.05)
REPAY = EP.get("d_repay_s", 0.06)
PHI = EP.get("phi_min", 0.5)
CLIP = EP.get("d_clip_s", 0.2)
FLOOR = reg["control"]["pace_floor_bps"]
TAU = 0.024        # measured loop delay (design_theory §3.8)
LINE = 100e9

fails = []


def check(name, ok, detail):
    print("%-40s %s  %s" % (name, "PASS" if ok else "FAIL", detail))
    if not ok:
        fails.append(name)


def policy(cap):
    return {"vms": {"sgpu02/vf0": {"weight": 1, "max_rate_bps": cap,
                                   "class_weights": {"tcp": 1, "rdma": 1}}}}


def simulate(fs, R0, demand, secs, cap=40e9, tau=TAU, repay=REPAY,
             hint_scale=None, hooks=None):
    """fs: list of fsids; R0: initial sender caps; demand(t, f): the
    tenant's offered load (bps) for f; wire rate = min(R, demand).
    hint_scale: {f: factor} applied to g on the wire (V5).
    Telemetry is delayed by tau. Returns per-tick history."""
    sched = rx_agent.Scheduler(policy(cap), LINE, 0.0, 0.0)
    sched.set_downlink(LINE)
    vqs = rx_agent.VirtualQueueScheduler(sched, CLIP)
    R = dict(R0)
    hist = []
    pending = []      # (deliver_at, {f: (g, d)})
    t = 0.0
    n = int(secs / T)
    for i in range(n):
        if hooks:
            for h in hooks:
                h(t, R)
        rates = {f: min(R[f], demand(t, f)) for f in fs}
        ents, ceils, delay = vqs.step({f: rates[f] for f in fs}, rates, T)
        rec = {f: (ceils[f] * (hint_scale or {}).get(f, 1.0), delay[f])
               for f in fs}
        pending.append((t + tau, rec))
        while pending and pending[0][0] <= t:
            _, rec_d = pending.pop(0)
            for f in fs:
                g, d = rec_d[f]
                R[f] = tx_agent_e.vq_step(R[f], g, d, T, K, EPS, repay,
                                          PHI, FLOOR)
        hist.append((t, dict(rates), dict(delay), dict(R)))
        t += T
    return hist


A, B = "sgpu01/vf0>sgpu02/vf0|rdma", "sgpu03/vf0>sgpu02/vf0|rdma"
BIG = 200e9

# ---- V1: fairness from the queue, not the hint ----------------------------
h = simulate([A, B], {A: 32e9, B: 8e9}, lambda t, f: BIG, 1.5)
tail = h[int(1.0 / T):]
ma = sum(x[1][A] for x in tail) / len(tail)
mb = sum(x[1][B] for x in tail) / len(tail)
check("V1 80/20 start -> 50/50 of the cap",
      abs(ma - mb) < 0.04 * (ma + mb) and abs(ma + mb - 40e9) < 0.05 * 40e9,
      "steady %.2fG / %.2fG (sum %.1fG of 40G cap)" % (ma / 1e9, mb / 1e9,
                                                       (ma + mb) / 1e9))
# time to fairness: both inside 10% of 20G
tf = next((x[0] for x in h if all(abs(x[1][f] - 20e9) < 2e9 for f in (A, B))
           and x[0] > 0.02), None)
print("   time to 10%% band: %s" % ("%.0f ms" % (tf * 1e3) if tf else "never"))

# ---- V2: bounded sawtooth, live signal ------------------------------------
ra = [x[1][A] for x in tail]
da = [x[2][A] for x in tail]
amp = (max(ra) - min(ra)) / 20e9
live = sum(1 for d in da if d > 0) / len(da)
check("V2 steady state is a small sawtooth",
      amp < 3 * EPS + 0.05,
      "peak-to-peak %.1f%% of share (eps=%.0f%%), queue non-empty %.0f%% of "
      "ticks, mean virtual delay %.2f ms" % (amp * 100, EPS * 100,
                                             live * 100,
                                             sum(da) / len(da) * 1e3))

# ---- V3: borrow and reclaim ----------------------------------------------
def dem(t, f):
    if f == B:
        return 4e9 if 0.5 <= t < 1.5 else BIG   # B goes demand-limited
    return BIG


h = simulate([A, B], {A: 20e9, B: 20e9}, dem, 2.5)
seg = lambda a, b: [x for x in h if a <= x[0] < b]
borrow = seg(1.2, 1.5)
ma_b = sum(x[1][A] for x in borrow) / len(borrow)
check("V3a A borrows what B leaves", abs(ma_b - 36e9) < 2e9,
      "A at %.1fG while B offers 4G (want ~36G)" % (ma_b / 1e9))
back = next((x[0] - 1.5 for x in seg(1.5, 2.5)
             if abs(x[1][B] - 20e9) < 2e9 and abs(x[1][A] - 20e9) < 2e9), None)
bound = max(2.6 / K, 3 * REPAY) + TAU + 0.05
check("V3b reclaim within max(2.6/k, 3*d_repay)+tau",
      back is not None and back < bound,
      "%s to both within 10%% of 20G (bound %.0f ms)"
      % ("%.0f ms" % (back * 1e3) if back is not None else "never",
         bound * 1e3))

# ---- V4: drain-loop stability vs d_repay ----------------------------------
def ringing(repay):
    hh = simulate([A, B], {A: 32e9, B: 8e9}, lambda t, f: BIG, 2.0,
                  repay=repay)
    tl = [x[1][A] for x in hh[int(1.2 / T):]]
    return (max(tl) - min(tl)) / 20e9


amp_ok = ringing(REPAY)
amp_bad = ringing(TAU * 0.4)
check("V4 d_repay above the delay bound is calm",
      amp_ok < 0.25 and amp_bad > amp_ok,
      "peak-to-peak %.0f%% at d_repay=%.0f ms vs %.0f%% at %.0f ms "
      "(bound 0.64*tau=%.0f ms)" % (amp_ok * 100, REPAY * 1e3,
                                     amp_bad * 100, TAU * 0.4 * 1e3,
                                     0.64 * TAU * 1e3))

# ---- V5: a wrong-high hint does not buy a bigger share --------------------
h = simulate([A, B], {A: 20e9, B: 20e9}, lambda t, f: BIG, 2.0,
             hint_scale={A: 2.0})
tl = h[int(1.2 / T):]
ma = sum(x[1][A] for x in tl) / len(tl)
mb = sum(x[1][B] for x in tl) / len(tl)
check("V5 2x-wrong hint is corrected by the queue",
      ma < 1.15 * mb,
      "A %.1fG vs B %.1fG with A's hint doubled" % (ma / 1e9, mb / 1e9))

print("\n%d/%d checks passed" % (7 - len(fails), 7))
sys.exit(1 if fails else 0)
