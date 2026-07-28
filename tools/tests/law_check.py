#!/usr/bin/env python3
"""Offline acceptance for the response law. No lab, no DPU.

Three checks, all against the PRODUCTION code (imported, not re-typed):

  A. wire  - rx_agent.Telemetry packs {u, r}, tx_agent_e.parse_telemetry
             unpacks it byte-identically; truncation and 64-byte fsids
             degrade instead of crashing. The two agents are a
             synchronized change, so a mismatch here is a dead control
             loop in the lab.
  B. law   - the three convergence closed-forms of design_theory.md §2.4
             (up-step 2.6/k, deep recovery 4.1/k, down-step symmetric),
             simulated with tx_agent_e.track_step at the registry's k
             and period.
  C. audit - closed loop of the real VQMarker against the real
             track_step: proposition 3 (residual-ledger self-healing with
             time constant V/(gamma*ceil)) and the §2.5 damping table
             (overdamped at a typical share -> no oscillation).

Usage: python3 law_check.py [path/to/lab-registry.json]
Exit status 0 = every check passed. Writes results/law_check.json.
"""
import json
import math
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
GAMMA = EP["gamma"]
T = EP["period_ms"] / 1e3
LINE = reg["line_rate_bps"]
# V is anchored on the downlink bottleneck capacity C, not the port line
# rate (design.md §3.3). C is not a registry field - rx_agent reads it
# from the uplink at runtime - so the lab's standing 100G bottleneck is
# stated here explicitly. 0.2 s * 3% * 100G = 600 Mbit, the deployed value.
C_DOWNLINK = 100e9
V = EP["v_seconds"] * EP["headroom"] * C_DOWNLINK
FLOOR = reg["control"]["pace_floor_bps"]

out = {"registry": REG, "k": K, "gamma": GAMMA, "period_s": T,
       "V_bits": V, "checks": []}
fails = []


def check(name, ok, detail):
    out["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
    print("%-34s %s  %s" % (name, "PASS" if ok else "FAIL", detail))
    if not ok:
        fails.append(name)


# ---------------------------------------------------------------- A. wire
tel = rx_agent.Telemetry({}, {}, 9710)
cases = [("sgpu01/vf0>sgpu02/vf0|rdma", 6_000_000_000, 5_900_000_000),
         ("sgpu01/vf3>sgpu02/vf1|tcp", 0, 0),
         ("a" * 64, 199_000_000_000, 198_000_000_000)]
blob = tel._pack([(f, u, r) for f, u, r in cases])
seq, recs = tx_agent_e.parse_telemetry(blob)
ok = len(recs) == len(cases)
for f, u, r in cases:
    got = recs.get(f)
    ok = ok and got is not None and got["u"] == u and got["r"] == r
check("A1 pack/parse round-trip", ok,
      "%d records, %d B/record, seq=%s" % (len(recs), tel.REC.size, seq))

check("A2 rec size matches sender", rx_agent.Telemetry.REC.size ==
      tx_agent_e._TREC.size and
      rx_agent.Telemetry.HDR.size == tx_agent_e._THDR.size,
      "rx=%dB tx=%dB" % (rx_agent.Telemetry.REC.size, tx_agent_e._TREC.size))

_, part = tx_agent_e.parse_telemetry(blob[:len(blob) - 7])
check("A3 truncated datagram degrades", len(part) == len(cases) - 1,
      "%d of %d records recovered, no exception" % (len(part), len(cases)))

_, empty = tx_agent_e.parse_telemetry(b"\x00")
check("A4 runt datagram ignored", empty == {}, "returns {}")


# ----------------------------------------------------------------- B. law
def settle_s(R0, u, band=0.05, tmax=5.0):
    """Wall-clock seconds for track_step to bring R0 inside `band` of u."""
    R, t = R0, 0.0
    while t < tmax:
        R = tx_agent_e.track_step(R, u, T, K, FLOOR)
        t += T
        if abs(R - u) <= band * u:
            return t
    return float("inf")


EHAT = 10e9
up = settle_s(EHAT, 2 * EHAT)
pred_up = math.log(math.log(2) / 0.049) / K
check("B1 up-step (ehat x2) -> 5% band", abs(up - pred_up) < 0.015,
      "%.0f ms measured vs %.0f ms predicted (2.6/k)"
      % (up * 1e3, pred_up * 1e3))

deep = settle_s(0.05 * EHAT, EHAT)
pred_deep = math.log(math.log(1 / 0.05) / 0.049) / K
check("B2 deep recovery (0.05 ehat)", abs(deep - pred_deep) < 0.02,
      "%.0f ms measured vs %.0f ms predicted (4.1/k)"
      % (deep * 1e3, pred_deep * 1e3))

down = settle_s(EHAT, 0.5 * EHAT)
check("B3 down-step symmetric with up", abs(down - up) < 0.010,
      "%.0f ms down vs %.0f ms up "
      % (down * 1e3, up * 1e3))

# no overshoot in either direction, ever (asymptotic approach)
R, over = 0.05 * EHAT, 0.0
for _ in range(2000):
    R = tx_agent_e.track_step(R, EHAT, T, K, FLOOR)
    over = max(over, R - EHAT)
R2, under = 2 * EHAT, 0.0
for _ in range(2000):
    R2 = tx_agent_e.track_step(R2, EHAT, T, K, FLOOR)
    under = max(under, EHAT - R2)
check("B4 no overshoot either direction", over <= 0 and under <= 0,
      "max over %.3e, max under %.3e" % (over, under))

# a coalesced/late update must not shoot past the target (the reason for
# 1-exp(-k*dt) instead of the literal (u/R)**(k*T))
late = tx_agent_e.track_step(0.05 * EHAT, EHAT, 1.0, K, FLOOR)
naive = 0.05 * EHAT * (EHAT / (0.05 * EHAT)) ** (K * 1.0)
check("B5 1 s stall does not overshoot", late <= EHAT and naive > EHAT,
      "exact -> %.3fG (<= target); literal k*dt -> %.3e G" %
      (late / 1e9, naive / 1e9))

check("B6 u=0 clamps to pace floor",
      tx_agent_e.track_step(6e9, 0.0, T, K, FLOOR) > 0,
      "R stays finite and positive (ln domain guarded by the floor)")

# ------------------------------------------------------------- C. audit
# Single flow-set, backlogged, ceiling EHAT, ledger pre-charged to full
# scale: proposition 3 says the discount alone reopens the drain and the
# residual heals with time constant V/(gamma*ehat), with NO reliance on
# executor under-realisation.
marker = rx_agent.VQMarker(V)
fs = "sgpu01/vf0>sgpu02/vf0|rdma"
marker.vq[fs] = V
R = EHAT
tau_pred = V / (GAMMA * EHAT)
t, t_heal, peak_s, hist = 0.0, None, 0.0, []
while t < 2.0:
    s = min(marker.vq.get(fs, 0.0) / V, 1.0)
    peak_s = max(peak_s, s)
    u = EHAT * (1.0 - GAMMA * s)
    R = tx_agent_e.track_step(R, u, T, K, FLOOR)
    # wire follows the pace exactly (executor realisation 1.0: the
    # pessimistic case - the drain dead-zone is the pessimistic term here
    # because realisation was < 1)
    marker.step({fs: R}, {fs: EHAT}, {fs: EHAT}, T)
    hist.append((t, s, R))
    if t_heal is None and marker.vq.get(fs, 0.0) <= V * math.exp(-1):
        t_heal = t
    t += T

check("C1 residual ledger self-heals (prop 3)",
      t_heal is not None and abs(t_heal - tau_pred) < 0.35 * tau_pred,
      "1/e at %.0f ms vs %.0f ms predicted (V/(gamma*ehat)), "
      "realisation=1.0" % ((t_heal or float('nan')) * 1e3, tau_pred * 1e3))

check("C2 discount stays inside its band",
      all(R_ >= (1 - GAMMA) * EHAT * 0.999 and R_ <= EHAT * 1.001
          for _, _, R_ in hist),
      "R in [%.2fG, %.2fG], band is [%.2fG, %.2fG]"
      % (min(R_ for _, _, R_ in hist) / 1e9,
         max(R_ for _, _, R_ in hist) / 1e9,
         (1 - GAMMA) * EHAT / 1e9, EHAT / 1e9))

# §2.5: zeta = 0.5*sqrt(k*V/(gamma*ehat)) -> overdamped at 10G means the
# rate must approach its final value monotonically (no ringing).
zeta = 0.5 * math.sqrt(K * V / (GAMMA * EHAT))
tail = [R_ for t_, _, R_ in hist if t_ > 0.5]
turns = sum(1 for i in range(1, len(tail) - 1)
            if (tail[i] - tail[i - 1]) * (tail[i + 1] - tail[i]) < 0
            and abs(tail[i + 1] - tail[i]) > 1e6)
check("C3 overdamped at 10G share, no ringing", zeta > 1.0 and turns == 0,
      "zeta=%.2f (design_theory table: 1.10), %d direction changes in "
      "the tail" % (zeta, turns))

os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
with open(os.path.join(HERE, "results", "law_check.json"), "w") as fh:
    json.dump(out, fh, indent=2)

print("\n%d/%d checks passed" % (len(out["checks"]) - len(fails),
                                 len(out["checks"])))
sys.exit(1 if fails else 0)
