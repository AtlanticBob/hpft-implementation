#!/usr/bin/env python3
"""Sample an RDMA port TX counter at fixed interval; print CSV to stdout.

Usage: bw_sampler.py <ibdev> <interval_ms> <duration_s>
Counter: /sys/class/infiniband/<dev>/ports/1/counters/port_xmit_data
(IB semantics: units of 4-byte words; Gbps = delta*32/interval_ns.)
"""
import sys
import time

dev, interval_ms, duration_s = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
path = f"/sys/class/infiniband/{dev}/ports/1/counters/port_xmit_data"
interval = interval_ms / 1000.0
end = time.time() + duration_s
f = open(path, "rb")

def read():
    f.seek(0)
    return int(f.read())

print("ts_ns,gbps")
prev_v, prev_t = read(), time.time_ns()
next_t = time.time() + interval
while time.time() < end:
    dt = next_t - time.time()
    if dt > 0:
        time.sleep(dt)
    next_t += interval
    v, t = read(), time.time_ns()
    dv, dtn = v - prev_v, t - prev_t
    if dtn > 0:
        print(f"{t},{dv * 32.0 / dtn:.3f}")
    prev_v, prev_t = v, t
