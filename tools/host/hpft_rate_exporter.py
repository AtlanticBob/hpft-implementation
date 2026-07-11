#!/usr/bin/env python3
"""Receiver-host rate exporter (runs on a receiver host, resident).

Pushes per-VF netdev rx_bytes counter snapshots to the local DPU's
rx_agent over tmfifo UDP. RoCE bypasses the kernel netdev counters
entirely (measured 2026-07-11: 16.4GB of pure RDMA moved rx_bytes by
2386 bytes; pure TCP tracks iperf3 at ratio 1.059 = headers+retrans),
so this counter is a fresh kernel-path(TCP+ip)-only byte source with
no polling-cache floor. The rx_agent subtracts it from the vport total
to split classes without the 1s-cached megaflow mix.

Snapshots carry (rx_bytes, t_host_monotonic_ns) stamped together at
read time, so byte counts can never decouple from their timestamps
(the +-60% measurement-bug family); the rx_agent does the division.

Wire format (see rx_agent.RATE_HDR/RATE_REC):
  header '<HH'   = (seq_lo16, n_records)
  record '<16sQQ' = (vnic_id[16], rx_bytes, t_host_monotonic_ns)
"""
import argparse
import json
import socket
import struct
import time

HDR = struct.Struct("<HH")
REC = struct.Struct("<16sQQ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--local-host", required=True)
    ap.add_argument("--interval-ms", type=float, default=None)
    args = ap.parse_args()
    reg = json.load(open(args.registry))
    ctl = reg["control"]
    ip, port = ctl["rate_export"][args.local_host].rsplit(":", 1)
    interval = (args.interval_ms or ctl.get("rate_export_ms", 5)) / 1e3
    vfs = [(v["vnic_id"], v["netdev"]) for v in reg["vnics"]
           if v["host"] == args.local_host]
    fds = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print("rate_exporter: %d vfs -> %s:%s every %.0fms"
          % (len(vfs), ip, port, interval * 1e3), flush=True)
    seq, sent, errs = 0, 0, 0
    last_log = time.monotonic()
    next_t = time.monotonic()
    while True:
        recs = []
        for vid, dev in vfs:
            fd = fds.get(vid)
            if fd is None:
                try:
                    fd = fds[vid] = open(
                        "/sys/class/net/%s/statistics/rx_bytes" % dev)
                except OSError:
                    continue
            try:
                fd.seek(0)
                b = int(fd.read())
            except (OSError, ValueError):
                fds.pop(vid, None)
                continue
            recs.append((vid, b, time.monotonic_ns()))
        if recs:
            out = [HDR.pack(seq & 0xffff, len(recs))]
            for vid, b, t in recs:
                out.append(REC.pack(vid.encode()[:16], b, t))
            try:
                sock.sendto(b"".join(out), (ip, int(port)))
                sent += 1
            except OSError:
                errs += 1
            seq += 1
        if time.monotonic() - last_log >= 60:
            print("rate_exporter: sent=%d errs=%d" % (sent, errs), flush=True)
            last_log = time.monotonic()
        next_t += interval
        d = next_t - time.monotonic()
        if d > 0:
            time.sleep(d)
        else:
            next_t = time.monotonic()   # overran: don't try to catch up


if __name__ == "__main__":
    main()
