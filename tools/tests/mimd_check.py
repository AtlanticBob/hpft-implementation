#!/usr/bin/env python3
"""Offline check of the pure-factor MIMD arm against the production
receiver pieces, with the lab's loop delay. Three gating modes:
phase (deterministic, one step per delay, flow-sets offset), random
(sparse MI, p = T/gate) and none (every record - the overreaction arm).

  M1 phase: two flow-sets from 80/20 reach 50/50 within 3 loop delays
  M2 phase: steady peak-to-peak is small (no limit cycle)
  M3 none : every-record application does not compound (assignment-type factor)
  M4 random: converges, with a longer tail than phase
"""
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "tools", "dpu"))
import rx_agent          # noqa: E402
import tx_agent_e        # noqa: E402

reg = json.load(open(os.path.join(REPO, "config", "lab-registry.json")))
EP = reg["e_params"]
T = EP["period_ms"] / 1e3
EPS = EP.get("eps", 0.02)
REPAY = EP.get("d_repay_s", 0.15)
PHI = EP.get("phi_min", 0.5)
CLIP = EP.get("d_clip_s", 0.2)
BMAX = EP.get("beta_max", 2.0)
GATE = EP.get("gate_s", 0.024)
FLOOR = reg["control"]["pace_floor_bps"]
TAU = 0.024
fails = []


def check(name, ok, detail):
    print("%-44s %s  %s" % (name, "PASS" if ok else "FAIL", detail))
    if not ok:
        fails.append(name)


def simulate(mode, R0, secs=1.5, cap=40e9):
    pol = {"vms": {"sgpu02/vf0": {"weight": 1, "max_rate_bps": cap,
                                  "class_weights": {"tcp": 1, "rdma": 1}}}}
    sched = rx_agent.Scheduler(pol, 100e9, 0.0, 0.0)
    sched.set_downlink(100e9)
    vqs = rx_agent.VirtualQueueScheduler(sched, CLIP, hold_s=0.05)
    fs = list(R0)
    R = dict(R0)
    epoch = {f: -1 for f in fs}
    phase = {f: (hash(f) & 0xffff) / 65536.0 * GATE for f in fs}
    rng = random.Random(1)
    pending, hist, t = [], [], 0.0
    for _ in range(int(secs / T)):
        rates = {f: R[f] for f in fs}
        ents, ceils, delay = vqs.step(rates, rates, T, now=t)
        pending.append((t + TAU, {f: (ceils[f], delay[f]) for f in fs}))
        while pending and pending[0][0] <= t:
            _, rec = pending.pop(0)
            for f in fs:
                if mode == "none":
                    due = True
                elif mode == "random":
                    due = rng.random() < T / GATE
                else:
                    idx = int((t + phase[f]) / GATE)
                    due = idx != epoch[f]
                    epoch[f] = idx
                if due:
                    g, d = rec[f]
                    R[f] = max(R[f] * tx_agent_e.mimd_factor(
                        R[f], g, d, EPS, BMAX, REPAY, PHI, FLOOR), FLOOR)
        hist.append((t, dict(rates)))
        t += T
    return hist


A, B = "sgpu01/vf0>sgpu02/vf0|rdma", "sgpu03/vf0>sgpu02/vf0|rdma"


def stats(h):
    tail = [x[1] for x in h if x[0] > 1.0]
    ma = sum(x[A] for x in tail) / len(tail)
    mb = sum(x[B] for x in tail) / len(tail)
    pp = (max(x[A] for x in tail) - min(x[A] for x in tail)) / 20e9
    tf = next((x[0] for x in h if x[0] > 0.01 and all(abs(x[1][f] - 20e9) < 2e9 for f in (A, B))), None)
    return ma, mb, pp, tf


ma, mb, pp, tf = stats(simulate("phase", {A: 32e9, B: 8e9}))
check("M1 phase: 80/20 -> 50/50 within 3 loop delays",
      abs(ma - mb) < 0.05 * 40e9 and tf is not None and tf < 3 * TAU + 0.01,
      "%.1fG/%.1fG, 10%% band at %s" % (ma / 1e9, mb / 1e9, "%.0f ms" % (tf * 1e3) if tf else "never"))
check("M2 phase: steady peak-to-peak small", pp < 0.15,
      "peak-to-peak %.0f%% of share" % (pp * 100))
# Finding, not a failure: a factor computed from the receiver's signal as
# a RATIO TO THE CURRENT RATE is idempotent - R*(g/R) is g however often
# it is applied - so acting on every record cannot compound. Overreaction
# needs a factor that does not cancel its own state (a constant one, or
# one applied to a reference the signal did not measure). Gating is
# therefore not what keeps this law stable; it only spreads the
# beta_max-capped big climbs over the loop delay.
_, _, pp_none, tf_none = stats(simulate("none", {A: 32e9, B: 8e9}))
check("M3 none: every-record application does not compound",
      pp_none < 0.15 and tf_none is not None and tf_none <= (tf or 9) + 0.005,
      "peak-to-peak %.0f%%, 10%% band at %s (phase-gated: %s)"
      % (pp_none * 100, "%.0f ms" % (tf_none * 1e3) if tf_none else "never",
         "%.0f ms" % (tf * 1e3) if tf else "never"))
ma, mb, pp_r, tf_r = stats(simulate("random", {A: 32e9, B: 8e9}))
check("M4 random: converges", abs(ma - mb) < 0.05 * 40e9 and tf_r is not None,
      "%.1fG/%.1fG, 10%% band at %s, peak-to-peak %.0f%%"
      % (ma / 1e9, mb / 1e9, "%.0f ms" % (tf_r * 1e3) if tf_r else "never", pp_r * 100))
print("\n%d/4 checks passed" % (4 - len(fails)))
sys.exit(1 if fails else 0)
