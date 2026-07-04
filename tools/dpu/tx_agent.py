#!/usr/bin/env python3
"""Sender-side agent (runs on the sender DPU Arm).

Receives {dst_ip, cap, rx_rate} datagrams from the receiver agent and pushes
them to the RP as one batched mailbox message via the RP control FIFO.
dst_ip -> flowtag mapping is static config until the pair key carries dst.
"""
import os
import socket
import sys

FIFO = "/tmp/rp_fifo"
LISTEN = ("10.0.4.201", 9709)
FT_MAP = {
    "10.1.0.2": 0x74249a41,
    "10.1.1.2": 0x11f4386b,
}

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(LISTEN)
fifo_fd = -1
fifo_ino = -1


def fifo_write(line):
    """Reopen on inode change (RP restarts recreate the FIFO); non-blocking
    open skips the tick when no reader is attached."""
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


print(f"tx_agent: listening {LISTEN} -> {FIFO}", flush=True)
while True:
    data, _ = sock.recvfrom(2048)
    entries = []
    for line in data.decode().splitlines():
        toks = line.split()
        if len(toks) != 3:
            continue
        ip, cap, rx = toks[0], int(toks[1]), int(toks[2])
        ft = FT_MAP.get(ip)
        if ft is None or cap == 0:
            continue
        entries.append(f"0x{ft:x} {cap} {rx}")
    if entries:
        fifo_write(f"0x{0xb47c0000 | len(entries):x} " + " ".join(entries) + "\n")
