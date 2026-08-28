#!/usr/bin/env python3
"""Offline closed loop of the fence design v4 (law=conf): production
receiver allocation (Scheduler) + production sender step (conf_step),
lab loop delay, two CC models: aggressive (always line rate) and
timid (sits at a fixed fraction of the share, i.e. core-limited)."""
import json, math, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "tools", "dpu"))
import rx_agent, tx_agent_e
reg = json.load(open(os.path.join(REPO, "config", "lab-registry.json"))); EP = reg["e_params"]
T_P = EP["period_ms"] / 1e3; K = EP["k"]; DR = EP["d_repay_s"]; TR = EP["trust_recover_s"]; DELTA = EP["delta_demand"]
FLOOR = reg["control"]["pace_floor_bps"]; L = 200e9; TAU = 0.020
fails = []
def check(name, ok, detail):
    print("%-46s %s  %s" % (name, "PASS" if ok else "FAIL", detail)); fails.append(name) if not ok else None
def sim(fs, R0, cc, secs=2.0, cap=40e9, events=()):
    pol = {"vms": {"sgpu02/vf0": {"weight": 1, "max_rate_bps": cap, "class_weights": {"tcp": 1, "rdma": 1}}}}
    sched = rx_agent.Scheduler(pol, 100e9, 0.0, 0.0); sched.set_downlink(100e9)
    R = dict(R0); T = {f: 0.0 for f in fs}; Q = {f: 0.0 for f in fs}; Ehat = dict(R0)
    pend = []; hist = []; t = 0.0; active = set(fs)
    for i in range(int(secs / T_P)):
        for (te, f, on) in events:
            if abs(t - te) < T_P / 2: (active.add if on else active.discard)(f)
        rates = {}
        for f in active:
            c = cc(f, t); r = T[f] * c + (1 - T[f]) * R[f]; rates[f] = r
        demand = {f: rates[f] * (1 + DELTA) for f in active}
        ents, _ = sched.entitlements(rates, demand)
        rec = {}
        for f in active:
            A, E = rates[f], ents.get(f, 0.0)
            if E <= 0: rec[f] = (0.0, 0.0); continue
            Q[f] = min(max(Q[f] + (A - E) * T_P, 0.0), DR * E)
            rec[f] = (Q[f] / E, (A - E) / E, A)   # the receiver's A rides along, as on the wire
        pend.append((t + TAU, rec))
        while pend and pend[0][0] <= t:
            _, rc = pend.pop(0)
            for f in rc:
                if f not in active: continue
                q, g, A = rc[f]
                if A > 0 and 1 + g > 0: Ehat[f] = A / (1 + g)
                R[f], T[f] = tx_agent_e.conf_step(R[f], T[f], Ehat[f], q, T_P, K, DR, TR, FLOOR, gamma=g, delta=DELTA)
        hist.append((t, dict(rates), dict(Q), dict(T))); t += T_P
    return hist
A, B = "sgpu01/vf0>sgpu02/vf0|rdma", "sgpu03/vf0>sgpu02/vf0|rdma"
# C1: two aggressive CCs from 32/8 -> 20/20, queue drains, trust stays ~0
h = sim([A, B], {A: 32e9, B: 8e9}, lambda f, t: L)
tail = h[int(1.2 / T_P):]
ma = sum(x[1][A] for x in tail) / len(tail); mb = sum(x[1][B] for x in tail) / len(tail)
qa = sum(x[2][A] for x in tail) / len(tail); ta = sum(x[3][A] for x in tail) / len(tail)
tf = next((x[0] for x in h if x[0] > 0.02 and all(abs(x[1][f] - 20e9) < 2e9 for f in (A, B))), None)
check("C1 aggressive CCs share 50/50, queue ~0, trust ~0", abs(ma - mb) < 2e9 and qa < 0.002 * 20e9 and ta < 0.05,
      "%.1fG/%.1fG, 10%% band at %s, mean Q %.0f kbit, trust %.3f" % (ma / 1e9, mb / 1e9, "%.0f ms" % (tf * 1e3) if tf else "never", qa / 1e3, ta))
# C2: timid CC on A (core-limited at 8G), aggressive B: A keeps 8G with trust -> 1, B borrows
h = sim([A, B], {A: 8e9, B: 8e9}, lambda f, t: 8e9 if f == A else L, secs=5.0)
tail = h[int(4.0 / T_P):]
ma = sum(x[1][A] for x in tail) / len(tail); mb = sum(x[1][B] for x in tail) / len(tail); ta = sum(x[3][A] for x in tail) / len(tail)
check("C2 timid CC keeps its own rate, trust -> 1, sibling borrows", abs(ma - 8e9) < 1.0e9 and ta > 0.9 and mb > 28e9,
      "A %.1fG (CC says 8G) trust %.2f, B %.1fG" % (ma / 1e9, ta, mb / 1e9))
# C3: join/leave: B joins at 0.5 s, leaves at 1.5 s; A's settle times and overshoot
h = sim([A, B], {A: 40e9, B: 8e9}, lambda f, t: L, secs=2.5, events=[(0.5, B, True), (1.5, B, False)])
def settle(t0, tgt):
    ok = 0
    for x in h:
        if x[0] < t0: continue
        if abs(x[1].get(A, 0) - tgt) < 0.1 * tgt: ok += 1
        else: ok = 0
        if ok >= 20: return x[0] - t0 - 0.02
    return None
s1 = settle(0.5, 20e9); s2 = settle(1.5, 40e9)
peak = max(x[1].get(A, 0) + x[1].get(B, 0) for x in h if 0.5 <= x[0] < 1.0)
tpk = max((x[0] for x in h if 0.5 <= x[0] < 1.0), key=lambda t0: next(x[1].get(A, 0) + x[1].get(B, 0) for x in h if x[0] == t0))
print("   join trace (t, A, B, qA ms, TA):", [(round(x[0],3), round(x[1].get(A,0)/1e9,1), round(x[1].get(B,0)/1e9,1), round(1e3*x[2].get(A,0)/20e9,1), round(x[3].get(A,0),3)) for x in h if 0.5 <= x[0] < 0.7][::5])
check("C3 join/leave settles, aggregate overshoot bounded", s1 is not None and s2 is not None and peak < 1.25 * 40e9,
      "down %s, up %s, peak aggregate %.0f%% of cap" % ("%.0f ms" % (s1 * 1e3) if s1 else "never", "%.0f ms" % (s2 * 1e3) if s2 else "never", 100 * peak / 40e9))
print("\n%d/3 checks passed" % (3 - len(fails))); sys.exit(1 if fails else 0)
