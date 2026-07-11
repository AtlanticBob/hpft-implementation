#!/usr/bin/env python3
"""Offline scheduler bench: cost of one rx control step vs flow-set count.

Runs ON the receiver DPU (same Arm core class as production) against the
production code in /opt/hpft, no data path involved. One step = demand
estimate + 3-layer water-filling entitlements (fastfill ceilings) + VQ
marker step. Telemetry pack/send excluded (linear, socket-bound).

Topology mimics the lab mesh scaled up: each dst VM has 8 flow-sets
(4 senders x 2 classes), so N flow-sets = N/8 dst VMs.
"""
import random
import sys
import time

sys.path.insert(0, "/opt/hpft")
from rx_agent import Scheduler, VQMarker   # noqa: E402

LINE = 200e9
V_FULL = 2 * 0.050 * LINE * 0.03           # registry: v_periods*ref*line*headroom


def build(nfs):
    nvm = max(1, nfs // 8)
    policy = {"vms": {}, "per_sender_weights": {}}
    fsids = []
    for d in range(nvm):
        dst = "h2/vf%d" % d
        policy["vms"][dst] = {"weight": 1, "max_rate_bps": 20e9,
                              "class_weights": {"tcp": 1, "rdma": 1}}
        for s in range(4):
            src = "h1/vf%d" % s
            if src not in policy["vms"]:
                policy["vms"][src] = {"weight": 1, "max_rate_bps": 20e9,
                                      "class_weights": {"tcp": 1, "rdma": 1}}
            for cls in ("tcp", "rdma"):
                if len(fsids) < nfs:
                    fsids.append("%s>%s|%s" % (src, dst, cls))
    return policy, fsids


def main():
    print("nfs nvm iters step_p50_us step_p95_us step_max_us")
    for nfs in (8, 16, 28, 32, 64, 128, 256, 512):
        policy, fsids = build(nfs)
        sched = Scheduler(policy, LINE, 0.03, 0.15)
        marker = VQMarker(V_FULL, V_FULL)
        iters = 300 if nfs <= 128 else 100
        times = []
        for _ in range(iters):
            rates = {f: random.uniform(0.1e9, 6e9) for f in fsids}
            t0 = time.perf_counter()
            demand = {f: r * 1.15 for f, r in rates.items()}
            ents, ceils = sched.entitlements(rates, demand)
            marker.step(rates, ents, ceils, 0.001)
            times.append(time.perf_counter() - t0)
        times.sort()
        p50 = times[len(times) // 2] * 1e6
        p95 = times[int(len(times) * 0.95)] * 1e6
        print("%d %d %d %.0f %.0f %.0f"
              % (nfs, max(1, nfs // 8), iters, p50, p95, times[-1] * 1e6))


if __name__ == "__main__":
    main()
