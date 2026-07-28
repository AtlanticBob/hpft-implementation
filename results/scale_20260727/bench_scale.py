#!/usr/bin/env python3
"""How far does the receiver's control loop scale, and where does Python
stop fitting in the 1 ms period?

This drives the ACTUAL production classes - Scheduler, VQMarker, Telemetry
from rx_agent - with N synthetic flow-sets, so what is measured is the
code that runs on the DPU, not a model of it. The lab caps real traffic at
4x4x2 = 32 flow-sets (a flow-set is (src VM, dst VM, class)), which is far
below where the interesting behaviour is, so the scaling curve has to come
from here; the lab's job is to confirm the small-N end of it.

Reports per-tick cost broken into its three parts and the N at which a
tick no longer fits in the control period. That N is the number the
C/C++ rewrite decision should turn on - not a guess that "Python is slow".

usage: bench_scale.py [--reps 200]
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "tools", "dpu"))

import rx_agent  # noqa: E402

REPS = int(sys.argv[sys.argv.index("--reps") + 1]) if "--reps" in sys.argv else 200
reg = json.load(open(os.path.join(REPO, "config", "lab-registry.json")))
EP = reg["e_params"]
PERIOD = EP["period_ms"] / 1e3
LINE = reg["line_rate_bps"]
C_LINK = 100e9
V = EP["v_seconds"] * EP["headroom"] * C_LINK
GAMMA = EP["gamma"]
DELTA = EP["delta_demand"]
THETA = EP["backlog_theta"]


def make_policy(n_vm):
    vms = {}
    for i in range(n_vm):
        vms["h/vm%d" % i] = {"weight": 1, "max_rate_bps": 30000000000,
                             "class_weights": {"tcp": 1, "rdma": 1}}
    return {"vms": vms, "per_sender_weights": {}}


def make_flowsets(n_vm):
    """(src, dst, class) over an n_vm x n_vm mesh, both classes."""
    fs = []
    for i in range(n_vm):
        for j in range(n_vm):
            for c in ("rdma", "tcp"):
                fs.append("h/vm%d>h/vm%d|%s" % (i, j, c))
    return fs


def bench(n_vm):
    fsids = make_flowsets(n_vm)
    n = len(fsids)
    sched = rx_agent.Scheduler(make_policy(n_vm), LINE, EP["headroom"], DELTA)
    sched.set_downlink(C_LINK)
    marker = rx_agent.VQMarker(V)
    telem = rx_agent.Telemetry({}, {}, 9710)
    share = sched.c_root / n
    rates = {f: share * 0.95 for f in fsids}

    t_sched = t_mark = t_telem = 0.0
    for _ in range(REPS):
        shat = sched._fill({f: float("inf") for f in fsids})
        demand = {f: (sched.c_root if shat.get(f, 0) > 0
                      and r >= THETA * shat[f] else r * (1 + DELTA))
                  for f, r in rates.items()}
        t = time.perf_counter()
        ents, ceils = sched.entitlements(rates, demand)
        t_sched += time.perf_counter() - t
        t = time.perf_counter()
        marks = marker.step(rates, ents, ceils, PERIOD)
        t_mark += time.perf_counter() - t
        t = time.perf_counter()
        targets = {f: ceils.get(f, 0.0) * (1 - GAMMA * marks.get(f, 0.0))
                   for f in rates}
        telem._pack([(f, u, rates[f]) for f, u in targets.items()])
        t_telem += time.perf_counter() - t
    return n, (t_sched / REPS * 1e6, t_mark / REPS * 1e6, t_telem / REPS * 1e6)


print("receiver control loop, per tick, driven through the production code")
print("period = %.0f ms = %d us budget\n" % (PERIOD * 1e3, PERIOD * 1e6))
print("  flow-sets   VMs   waterfill      VQ    telemetry     TOTAL   of budget")
budget = PERIOD * 1e6
first_over = None
for n_vm in (4, 8, 12, 16, 20, 24, 28, 32):
    n, (a, b, c) = bench(n_vm)
    tot = a + b + c
    frac = 100 * tot / budget
    flag = ""
    if tot > budget and first_over is None:
        first_over = n
        flag = "  <-- exceeds the period"
    print("  %9d %5d %10.1f %7.1f %11.1f %9.1f %8.0f%%%s"
          % (n, n_vm, a, b, c, tot, frac, flag))
print()
if first_over:
    print("Python stops fitting the %d ms period at ~%d flow-sets."
          % (PERIOD * 1e3, first_over))
else:
    print("Still inside the period at the largest size measured.")
print("The lab can generate at most 4x4x2 = 32 flow-sets, so everything")
print("above that is reachable only here.")
