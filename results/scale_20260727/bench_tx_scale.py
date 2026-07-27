#!/usr/bin/env python3
"""The sender side of the same question, which the first scaling pass
never asked.

rx was measured; tx was not. That was a gap, not a judgement: tx
recomputes its whole sender tree on EVERY telemetry datagram - at a 1 ms
period that is 1000 water-fillings per second over all local flow-sets -
and then runs the law and the actuation per flow-set. If tx is the
tighter constraint then the C work should have gone there first.

Drives the production objects (SenderTree, track_step, the telemetry
parser) with N synthetic flow-sets.

usage: bench_tx_scale.py [--reps 200]
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "tools", "dpu"))

import rx_agent      # noqa: E402  (Telemetry, to build realistic datagrams)
import tx_agent_e    # noqa: E402

REPS = int(sys.argv[sys.argv.index("--reps") + 1]) if "--reps" in sys.argv else 200
reg = json.load(open(os.path.join(REPO, "config", "lab-registry.json")))
EP = reg["e_params"]
PERIOD = EP["period_ms"] / 1e3
LINE = reg["line_rate_bps"]
K = EP["k"]
FLOOR = reg["control"]["pace_floor_bps"]


def make(n_vm):
    """n_vm x n_vm mesh x 2 classes, all sourced from this host."""
    policy = {"vms": {}, "per_sender_weights": {}}
    for i in range(n_vm):
        policy["vms"]["h/vm%d" % i] = {
            "weight": 1, "max_rate_bps": 30000000000,
            "class_weights": {"tcp": 1, "rdma": 1}}
    fsids = ["h/vm%d>h/vm%d|%s" % (i, j, c)
             for i in range(n_vm) for j in range(n_vm)
             for c in ("rdma", "tcp")]
    return policy, fsids


def bench(n_vm):
    policy, fsids = make(n_vm)
    n = len(fsids)
    stree = tx_agent_e.SenderTree(policy, LINE, EP["headroom"],
                                  EP["delta_demand"], FLOOR)
    now = time.monotonic()
    flows = {f: tx_agent_e.FlowState(1e9, now) for f in fsids}
    share = LINE * 0.97 / n
    for f, st in flows.items():
        st.r = share * 0.9
        st.pace = share

    # a realistic datagram: what rx would actually put on the wire
    telem = rx_agent.Telemetry({}, {}, 9710)
    blob = telem._pack([(f, int(share), int(share * 0.9)) for f in fsids])

    t_parse = t_tree = t_law = 0.0
    for _ in range(REPS):
        t = time.perf_counter()
        _, recs = tx_agent_e.parse_telemetry(blob)
        t_parse += time.perf_counter() - t

        t = time.perf_counter()
        trees = stree.trees(flows)
        t_tree += time.perf_counter() - t

        t = time.perf_counter()
        now = time.monotonic()
        for f, rec in recs.items():
            st = flows[f]
            st.last_rx = now
            dt = PERIOD
            st.R = tx_agent_e.track_step(st.R, rec["u"], dt, K, FLOOR)
            pace = max(min(st.R, trees.get(f, 5e10)), FLOOR)
            st.pace = pace
        t_law += time.perf_counter() - t
    return n, (t_parse / REPS * 1e6, t_tree / REPS * 1e6, t_law / REPS * 1e6)


print("sender agent, per telemetry datagram (one per control period)")
print("period = %.0f ms = %d us budget\n" % (PERIOD * 1e3, PERIOD * 1e6))
print("  flow-sets   VMs   parse   sender-tree     law+pace     TOTAL   of budget")
first_over = None
for n_vm in (4, 8, 12, 16, 20, 24, 32):
    n, (a, b, c) = bench(n_vm)
    tot = a + b + c
    flag = ""
    if tot > PERIOD * 1e6 and first_over is None:
        first_over = n
        flag = "  <-- exceeds the period"
    print("  %9d %5d %7.1f %13.1f %12.1f %9.1f %8.0f%%%s"
          % (n, n_vm, a, b, c, tot, 100 * tot / (PERIOD * 1e6), flag))
print()
print("sender-tree is a water-filling cascade, so it already runs through")
print("the C path; parse and law+pace are Python.")
if first_over:
    print("tx stops fitting the period at ~%d flow-sets." % first_over)
else:
    print("tx still inside the period at the largest size measured.")
