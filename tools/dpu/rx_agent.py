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
CAPS_FILE = "/tmp/hpft_caps.conf"   # lines: "<dst_ip> <cap_units>", re-read per tick
# dst_ip -> representor dev
DEVS = {
    "10.1.0.2": "pf1vf0",
    "10.1.0.4": "pf1vf1",
    "10.1.2.2": "pf1vf2",
    "10.1.3.2": "pf1vf3",
}


def read_caps():
    caps = {}
    try:
        for line in open(CAPS_FILE):
            toks = line.split()
            if len(toks) == 2:
                caps[toks[0]] = int(toks[1])
    except FileNotFoundError:
        pass
    return caps


def vport_tx_bytes(dev):
    out = subprocess.run(["ethtool", "-S", dev], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if " vport_tx_bytes:" in line:
            return int(line.split(":")[1])
    return None


sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
prev = {}
print(f"rx_agent: {len(DEVS)} dsts -> {SENDER} @{HZ}Hz caps={CAPS_FILE}", flush=True)
while True:
    t = time.time_ns()
    caps = read_caps()
    lines = []
    for ip, dev in DEVS.items():
        cap = caps.get(ip, 0)
        v = vport_tx_bytes(dev)
        if v is None:
            continue
        pv, pt = prev.get(ip, (v, t))
        prev[ip] = (v, t)
        dt = t - pt
        rate = int((v - pv) * 8 * (1 << 20) // (dt * 200)) if dt > 0 else 0
        lines.append(f"{ip} {cap} {rate}")
    if lines:
        sock.sendto("\n".join(lines).encode(), SENDER)
    time.sleep(max(0.0, 1.0 / HZ - (time.time_ns() - t) / 1e9))
