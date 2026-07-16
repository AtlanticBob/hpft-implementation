#!/usr/bin/env python3
"""Host-side ground-truth counter sampler, all 4 VFs (runs on sgpu02).

Per line: wall time, then per VF n=0..3 the RDMA arrival counter
(mlx5_{6+n} port_rcv_data, units of 4-byte words) and the kernel-path
arrival counter (dpu1vf{n} netdev rx_bytes = TCP+ip incl. headers; RoCE
bypasses it entirely). Both stamped at read time on this host, NTP-synced.

usage: gt_sampler4.py <interval_s> <duration_s>
"""
import sys
import time

iv = float(sys.argv[1])
dur = float(sys.argv[2])
fds = []
names = []
for n in range(4):
    names += ["rcv4_vf%d" % n, "rx_vf%d" % n]
    fds.append(open("/sys/class/infiniband/mlx5_%d/ports/1/counters"
                    "/port_rcv_data" % (6 + n)))
    fds.append(open("/sys/class/net/dpu1vf%d/statistics/rx_bytes" % n))
print("t " + " ".join(names), flush=True)
t_end = time.time() + dur
while time.time() < t_end:
    vals = []
    for fd in fds:
        fd.seek(0)
        vals.append(fd.read().strip())
    print("%.6f %s" % (time.time(), " ".join(vals)), flush=True)
    time.sleep(iv)
