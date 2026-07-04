#!/usr/bin/env python3
"""Receiver-side agent (runs on the receiver DPU Arm).

Measures per-dst-vNIC RX rate from the representor's vport hardware counter
(fresh, no event loss) and sends {dst_ip, cap, rx_rate} to the sender-side
agent over UDP at a fixed tick. cap policy is static config for now - this
is the 'receiver tells the sender a cap' authority.

Units: 2^20 = 200G line rate.
"""
import socket
import subprocess
import sys
import time

SENDER = ("10.0.4.201", 9709)
HZ = 20
# dst_ip -> {dev: representor, cap: units}
CONFIG = {
    "10.1.0.2": {"dev": "pf1vf0", "cap": 41943},
    "10.1.1.2": {"dev": "pf1vf1", "cap": 0},
}


def vport_tx_bytes(dev):
    out = subprocess.run(["ethtool", "-S", dev], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if " vport_tx_bytes:" in line:
            return int(line.split(":")[1])
    return None


sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
prev = {}
print(f"rx_agent: {len(CONFIG)} dsts -> {SENDER} @{HZ}Hz", flush=True)
while True:
    t = time.time_ns()
    lines = []
    for ip, c in CONFIG.items():
        v = vport_tx_bytes(c["dev"])
        if v is None:
            continue
        pv, pt = prev.get(ip, (v, t))
        prev[ip] = (v, t)
        dt = t - pt
        rate = int((v - pv) * 8 * (1 << 20) // (dt * 200)) if dt > 0 else 0
        lines.append(f"{ip} {c['cap']} {rate}")
    if lines:
        sock.sendto("\n".join(lines).encode(), SENDER)
    time.sleep(max(0.0, 1.0 / HZ - (time.time_ns() - t) / 1e9))
