#!/usr/bin/env python3
"""Sender-side agent v2 (runs on the sender DPU Arm).

R input: local representor vport counter (vport_rx_bytes = traffic FROM the
src vNIC toward the wire) - the enforceable sender wire rate, fresh and
loss-proof. Caps: latest values received from the receiver agent over UDP
(receiver-driven policy). Pushes one batched mailbox line per tick.
"""
import os
import select
import socket
import subprocess
import time

FIFO = "/tmp/rp_fifo"
LISTEN = ("10.0.4.201", 9709)
HZ = 20
# src vNIC: flowtag + local representor dev; dst_ip keys the receiver caps
PAIRS = {
    "10.1.0.2": {"ft": 0x74249a41, "dev": "pf1vf0"},
    "10.1.1.2": {"ft": 0x11f4386b, "dev": "pf1vf1"},
}

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(LISTEN)
sock.setblocking(False)
fifo_fd = -1
fifo_ino = -1
caps = {}   # dst_ip -> cap units (receiver-driven)
prev = {}   # dev -> (bytes, ts)


def fifo_write(line):
    global fifo_fd, fifo_ino
    try:
        ino = os.stat(FIFO).st_ino
    except FileNotFoundError:
        return False
    if fifo_fd < 0 or ino != fifo_ino:
        if fifo_fd >= 0:
            os.close(fifo_fd)
            fifo_fd = -1
        try:
            fifo_fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
            fifo_ino = ino
        except OSError:
            return False
    try:
        os.write(fifo_fd, line.encode())
        return True
    except OSError:
        os.close(fifo_fd)
        fifo_fd = -1
        return False


def vport_rx_bytes(dev):
    out = subprocess.run(["ethtool", "-S", dev], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if " vport_rx_bytes:" in line:
            return int(line.split(":")[1])
    return None


print(f"tx_agent2: {len(PAIRS)} pairs, local R + receiver caps @{HZ}Hz", flush=True)
while True:
    t = time.time_ns()
    # drain cap updates from the receiver
    while True:
        r, _, _ = select.select([sock], [], [], 0)
        if not r:
            break
        data, _ = sock.recvfrom(2048)
        for line in data.decode().splitlines():
            toks = line.split()
            if len(toks) == 3:
                caps[toks[0]] = int(toks[1])
    entries = []
    for ip, p in PAIRS.items():
        cap = caps.get(ip, 0)
        if cap == 0:
            continue
        v = vport_rx_bytes(p["dev"])
        if v is None:
            continue
        pv, pt = prev.get(p["dev"], (v, t))
        prev[p["dev"]] = (v, t)
        dt = t - pt
        rate = int((v - pv) * 8 * (1 << 20) // (dt * 200)) if dt > 0 else 0
        entries.append(f"0x{p['ft']:x} {cap} {rate}")
    if entries:
        fifo_write(f"0x{0xb47c0000 | len(entries):x} " + " ".join(entries) + "\n")
    time.sleep(max(0.0, 1.0 / HZ - (time.time_ns() - t) / 1e9))
