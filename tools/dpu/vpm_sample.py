#!/usr/bin/env python3
"""Sample the hpft-vport-meter shared memory (/dev/shm/hpft_vpm).

Runs on dpu2. Emits cumulative per-vport octet counters as CSV; rates are
derived offline. Eval version (P6, 2026-07-29): sampling interval is a
parameter — baseline arms have no agent jsonl, so transient/convergence
time series come from here at 20–50 ms. The vport_meter updates the shm
at 1 ms, so any interval >= ~5 ms is honest; flushing is deferred to
close at sub-100ms intervals to keep the sampler's own cost negligible.

usage: vpm_sample.py <duration_s> <out_csv> [interval_ms]   (default 1000)
Record layout: see tools/dpu/vport_meter.c (vpm_hdr / vpm_rec, seqlock).
"""
import struct, sys, time

dur = float(sys.argv[1]) if len(sys.argv) > 1 else 60
out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/vpm_series.csv"
itv = (float(sys.argv[3]) if len(sys.argv) > 3 else 1000.0) / 1e3

f = open("/dev/shm/hpft_vpm", "rb")

def snap():
    f.seek(0)
    buf = f.read(4096)
    magic, nv, iv = struct.unpack_from("<8sII", buf, 0)
    assert magic == b"HPFTVPM1", magic
    rows = []
    for i in range(nv):
        off = 64 + i * 64
        for _ in range(3):
            seq, t, rxib, rxeth, txib, txeth, rip, rep = \
                struct.unpack_from("<8Q", buf, off)
            if seq % 2 == 0:
                break
            f.seek(0)
            buf = f.read(4096)
        rows.append((t, rxib, rxeth))
    return rows

w = open(out, "w")
w.write("ts,vport_idx,t_ns,rx_ib,rx_eth\n")
flush_every = itv >= 0.1
t0 = time.time()
next_t = t0
while True:
    now = time.time()
    if now - t0 >= dur:
        break
    ts = now - t0
    for i, (t, a, b) in enumerate(snap()):
        w.write(f"{ts:.3f},{i},{t},{a},{b}\n")
    if flush_every:
        w.flush()
    next_t += itv
    delay = next_t - time.time()
    if delay > 0:
        time.sleep(delay)
    else:
        next_t = time.time()   # fell behind: re-anchor, don't burst
w.close()
