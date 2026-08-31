#!/usr/bin/env python3
"""Minimal SNTP (RFC 4330) responder so the DPUs can sync their clocks to
their host over tmfifo. The hosts are NTP-synced (site server, ~1 ms); the
DPUs have no time source at all (no chrony, no route to the site server),
which left them hundreds of ms apart and drifting - every cross-DPU
timestamp comparison in the agent logs was contaminated (found 2026-08-29).
systemd-timesyncd on the DPU is a plain SNTP client and is happy with this.

usage: sudo sntp_server.py [bind_ip] [port=123]
"""
import socket, struct, sys, time

NTP_EPOCH = 2208988800          # 1900 -> 1970
bind = sys.argv[1] if len(sys.argv) > 1 else "0.0.0.0"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 123


def ts(t):
    sec = int(t) + NTP_EPOCH
    frac = int((t - int(t)) * (1 << 32))
    return struct.pack("!II", sec, frac)


s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind((bind, port))
print("sntp_server: %s:%d" % (bind, port), flush=True)
while True:
    data, addr = s.recvfrom(1024)
    rx = time.time()
    if len(data) < 48:
        continue
    vn = (data[0] >> 3) & 7
    origin = data[40:48]                       # client's transmit timestamp
    li_vn_mode = (0 << 6) | (vn << 3) | 4      # server mode
    hdr = struct.pack("!BBBb", li_vn_mode, 2, 4, -20)   # stratum 2, poll, precision
    root = struct.pack("!II", 0, 0)             # root delay, root dispersion
    refid = b"LOCL"
    reply = hdr + root + refid + ts(rx) + origin + ts(rx) + ts(time.time())
    s.sendto(reply, addr)
