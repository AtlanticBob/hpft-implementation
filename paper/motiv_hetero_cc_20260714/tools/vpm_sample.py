#!/usr/bin/env python3
"""Sample the hpft-vport-meter shared memory (/dev/shm/hpft_vpm) at 1 Hz.

Runs on dpu2. Emits cumulative per-vport octet counters as CSV; rates are
derived offline. Usage: vpm_sample.py <duration_s> <out_csv>
Record layout: see tools/dpu/vport_meter.c (vpm_hdr / vpm_rec, seqlock).
"""
import struct, sys, time

dur = float(sys.argv[1]) if len(sys.argv) > 1 else 60
out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/vpm_series.csv"

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
t0 = time.time()
while time.time() - t0 < dur:
    ts = time.time() - t0
    for i, (t, a, b) in enumerate(snap()):
        w.write(f"{ts:.2f},{i},{t},{a},{b}\n")
    w.flush()
    time.sleep(1)
w.close()
