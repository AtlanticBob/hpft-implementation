#!/usr/bin/env python3
"""L4 control-plane scale bench. Runs ON the receiver DPU Arm against the
production scheduler (/opt/hpft). One control step = demand est + 3-layer
water-filling entitlements (fastfill) + VQ marker. Sweeps flow-set count
far past the hardware max (32 real fs on 4x4 VFs) to locate the 1ms wall
and break down where the time goes. Also reports the parallelizable
fraction: the per-VM class/flow-set fills are independent across VMs, so
the dominant water-filling term is embarrassingly parallel across dst VMs.
"""
import random
import sys
import time

sys.path.insert(0, "/opt/hpft")
from rx_agent import Scheduler, VQMarker   # noqa: E402

LINE = 200e9
V_FULL = 4 * 0.050 * LINE * 0.03           # v_periods=4


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


def timeit(fn, iters):
    ts = []
    for _ in range(iters):
        t0 = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t0)
    ts.sort()
    return ts[len(ts) // 2] * 1e6, ts[int(len(ts) * 0.95)] * 1e6


def main():
    print("nfs nvm step_p50us step_p95us sched_us marker_us over_1ms")
    for nfs in (8, 16, 28, 32, 64, 128, 256, 512, 1024, 2048, 4096):
        policy, fsids = build(nfs)
        sched = Scheduler(policy, LINE, 0.03, 0.15)
        marker = VQMarker(V_FULL, V_FULL)
        it = 300 if nfs <= 128 else (100 if nfs <= 1024 else 30)

        def rates():
            return {f: random.uniform(0.1e9, 6e9) for f in fsids}

        # full step
        def step():
            r = rates()
            d = {f: v * 1.15 for f, v in r.items()}
            e, c = sched.entitlements(r, d)
            marker.step(r, e, c, 0.001)
        # sched only
        r0 = rates()
        d0 = {f: v * 1.15 for f, v in r0.items()}

        def sched_only():
            sched.entitlements(r0, d0)
        e0, c0 = sched.entitlements(r0, d0)

        def marker_only():
            marker.step(r0, e0, c0, 0.001)

        p50, p95 = timeit(step, it)
        s50, _ = timeit(sched_only, it)
        m50, _ = timeit(marker_only, it)
        print("%d %d %.0f %.0f %.0f %.0f %s"
              % (nfs, max(1, nfs // 8), p50, p95, s50, m50,
                 "OVER" if p50 > 1000 else ""))


if __name__ == "__main__":
    main()
