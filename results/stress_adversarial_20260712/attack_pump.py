#!/usr/bin/env python3
"""Adversarial on-off TCP pump (stress D8).

Bursts at full speed for on_ms, idles for off_ms, repeating for dur_s.
The pump does not try to defeat the pacer - it exploits the ledger
dynamics: an idle gap clears the flow-set's virtual queue (idle flows
drain at V per period) and lets AI/FR grow R, so each burst rides an
elevated allowance before marking catches up. The question D8 answers is
whether the long-run average of this pattern beats the steady-state
entitlement (fairness robustness) or not.

Usage: attack_pump.py <dst_ip> <port> <on_ms> <off_ms> <dur_s> [bind_ip]
"""
import socket
import sys
import time

dst, port, on_ms, off_ms, dur_s = (sys.argv[1], int(sys.argv[2]),
                                   float(sys.argv[3]), float(sys.argv[4]),
                                   float(sys.argv[5]))
bind_ip = sys.argv[6] if len(sys.argv) > 6 else None

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
if bind_ip:
    s.bind((bind_ip, 0))
s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 0)
s.settimeout(10)
s.connect((dst, port))
s.setblocking(True)
buf = b"\x5a" * (1 << 20)

t_end = time.monotonic() + dur_s
sent = 0
cycles = 0
while time.monotonic() < t_end:
    t_burst_end = time.monotonic() + on_ms / 1e3
    while time.monotonic() < t_burst_end:
        try:
            sent += s.send(buf)
        except (BrokenPipeError, ConnectionResetError):
            print("pump: connection lost", flush=True)
            sys.exit(1)
    if off_ms > 0:
        time.sleep(off_ms / 1e3)
    cycles += 1

el = dur_s
print("pump done: cycles=%d sent=%.2fGB avg_goodput=%.2fG duty=%.0f%%"
      % (cycles, sent / 1e9, sent * 8 / el / 1e9,
         100 * on_ms / (on_ms + off_ms)), flush=True)
s.close()
