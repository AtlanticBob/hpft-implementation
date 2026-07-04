#!/usr/bin/env python3
"""Measure OVS offloaded-flow byte-counter freshness on the DPU.

Polls `ovs-appctl dpctl/dump-flows` at a fixed interval, tracks the
fastest-growing flow entry, and reports per-sample (ts_ns, bytes) CSV plus
poll-latency stats. Run while a known-rate flow crosses the eSwitch.

Usage: ovs_counter_probe.py <interval_ms> <duration_s>
"""
import re
import subprocess
import sys
import time

interval = float(sys.argv[1]) / 1000.0
duration = float(sys.argv[2])

BYTES_RE = re.compile(r"bytes:(\d+)")


def dump():
    t0 = time.time_ns()
    out = subprocess.run(["sudo", "ovs-appctl", "dpctl/dump-flows", "-m"],
                         capture_output=True, text=True, timeout=5).stdout
    t1 = time.time_ns()
    flows = {}
    for line in out.splitlines():
        m = BYTES_RE.search(line)
        if not m:
            continue
        key = line.split(", packets:")[0][-180:]
        flows[key] = int(m.group(1))
    return flows, t0, (t1 - t0) / 1e6


samples = []   # (ts_ns, {key: bytes})
lat = []
end = time.time() + duration
while time.time() < end:
    flows, ts, ms = dump()
    samples.append((ts, flows))
    lat.append(ms)
    time.sleep(max(0.0, interval - ms / 1000.0))

# find fastest-growing key present in first and last sample
first, last = samples[0][1], samples[-1][1]
best_key, best_delta = None, -1
for k, v in last.items():
    d = v - first.get(k, 0)
    if d > best_delta:
        best_key, best_delta = k, d

print(f"# poll_ms p50={sorted(lat)[len(lat)//2]:.1f} max={max(lat):.1f} n={len(lat)}")
print(f"# best flow delta_bytes={best_delta}")
print(f"# key={best_key}")
print("ts_ns,bytes")
prev = None
changes = 0
for ts, flows in samples:
    v = flows.get(best_key)
    if v is None:
        continue
    if prev is not None and v != prev:
        changes += 1
    prev = v
    print(f"{ts},{v}")
print(f"# value_changes={changes} of {len(samples)} samples")
