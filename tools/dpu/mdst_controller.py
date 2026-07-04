#!/usr/bin/env python3
"""Multi-dst controller (sender DPU Arm). Proves qpn->pair override for the
P2-2 validation: one src vNIC to two dst vNICs, independent caps.

Inputs:
  - 9709 from receiver rx_agent: "<dst_ip> <cap_units> <rx_rate_units>" (per dst)
  - 9710 from sender-host qpn_resolver: "<src_ip> <lqpn> <dst_ip>"
Output (RP FIFO):
  - 0xB48D explicit pair config {pair_idx, flowtag, dst_tag, budget, rx_rate}
  - 0xB48E qpn map {qpn, pair_idx}
Pair key = (src_flowtag, dst_ip); receiver per-dst rate is the control R.
"""
import os
import select
import socket
import time

FIFO = "/tmp/rp_fifo"
RX_PORT = ("0.0.0.0", 9709)
QPN_PORT = ("0.0.0.0", 9710)
HZ = 20
SRC_FT = {                      # src_ip -> PCC flowtag (measured via 0xdea)
    "10.1.0.1": 0x74249a41, "10.1.1.1": 0x11f4386b,
    "10.1.2.1": 0xde985a90, "10.1.3.1": 0x7973f1b0,
}

rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); rx_sock.bind(RX_PORT); rx_sock.setblocking(False)
qp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); qp_sock.bind(QPN_PORT); qp_sock.setblocking(False)

caps = {}          # dst_ip -> (cap, rx_rate)
qpn_dst = {}       # lqpn -> (src_ip, dst_ip)
pair_ids = {}      # (src_ip, dst_ip) -> pair_idx
next_pid = [0]
fifo_fd = -1
fifo_ino = -1


def fifo_write(line):
    global fifo_fd, fifo_ino
    try:
        ino = os.stat(FIFO).st_ino
    except FileNotFoundError:
        return
    if fifo_fd < 0 or ino != fifo_ino:  # reopen on RP restart (new inode)
        if fifo_fd >= 0:
            os.close(fifo_fd); fifo_fd = -1
        try:
            fifo_fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK); fifo_ino = ino
        except OSError:
            return
    try:
        os.write(fifo_fd, line.encode())
    except OSError:
        os.close(fifo_fd); fifo_fd = -1


def pid_for(src, dst):
    k = (src, dst)
    if k not in pair_ids:
        pair_ids[k] = next_pid[0] % 16
        next_pid[0] += 1
    return pair_ids[k]


def drain(sock, handler):
    while True:
        r, _, _ = select.select([sock], [], [], 0)
        if not r:
            break
        data, _ = sock.recvfrom(4096)
        for line in data.decode().splitlines():
            handler(line.split())


print("mdst_controller: qpn-override multi-dst mode", flush=True)
while True:
    t = time.time()
    drain(rx_sock, lambda tk: caps.__setitem__(tk[0], (int(tk[1]), int(tk[2]))) if len(tk) == 3 else None)
    qpn_dst.clear()
    drain(qp_sock, lambda tk: qpn_dst.__setitem__(int(tk[1]), (tk[0], tk[2])) if len(tk) == 3 else None)

    # build pair config for every (src,dst) that has a cap and active qpns
    active = {}                # (src,dst) -> [qpns]
    for lqpn, (src, dst) in qpn_dst.items():
        if dst in caps and caps[dst][0] > 0 and src in SRC_FT:
            active.setdefault((src, dst), []).append(lqpn)

    pair_entries = []
    qpn_entries = []
    for (src, dst), qpns in active.items():
        pid = pid_for(src, dst)
        cap, rx = caps[dst]
        dst_tag = int(dst.split(".")[-1])          # low octet as dst identifier
        pair_entries.append(f"{pid} 0 {dst_tag} {cap} {rx}")
        for q in qpns:
            qpn_entries.append(f"{q} {pid}")

    if pair_entries:
        fifo_write(f"0x{0xb48d0000 | len(pair_entries):x} " + " ".join(pair_entries) + "\n")
    if qpn_entries:
        # chunk to fit the 512B request buffer (63 pairs max per RPC)
        for i in range(0, len(qpn_entries), 60):
            chunk = qpn_entries[i:i + 60]
            fifo_write(f"0x{0xb48e0000 | len(chunk):x} " + " ".join(chunk) + "\n")
    time.sleep(max(0.0, 1.0 / HZ - (time.time() - t)))
