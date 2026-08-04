#!/usr/bin/env python3
"""Sender-host qpn->dst resolver (runs on the sender host, e.g. sgpu01).

Transparent management-plane resolution of which dst vNIC each local QP talks
to, without touching tenant code/data:
  - local `rdma res show qp` gives {lqpn -> rqpn} per src VF device
  - the peer host's `rdma res show qp` gives {lqpn(=our rqpn) -> that VF's dst_ip}
  - join on rqpn: our_lqpn -> rqpn -> dst_ip

Emits UDP lines "<src_ip> <lqpn> <dst_ip>" to the DPU tx agent, which maps
dst_ip->cap and pushes {qpn->pair} to the RP.
"""
import json
import socket
import subprocess
import sys
import time

AGENT = ("192.168.102.2", 9710)          # DPU tx agent qpn-map port
PEER = "sgpu02"
HZ = 5
# src VF: rdma device -> src_ip
SRC = {"mlx5_6": "10.1.0.1", "mlx5_7": "10.1.1.1",
       "mlx5_8": "10.1.2.1", "mlx5_9": "10.1.3.1"}
# peer VF: rdma device -> dst_ip (what a QP on that peer device terminates as)
PEER_DST = {"mlx5_6": "10.1.0.2", "mlx5_7": "10.1.1.2",
            "mlx5_8": "10.1.2.2", "mlx5_9": "10.1.3.2"}


def qps_local(dev):
    """{lqpn: rqpn} for a local rdma device."""
    out = subprocess.run(["rdma", "-j", "res", "show", "qp", "link", f"{dev}/1"],
                         capture_output=True, text=True).stdout
    try:
        return {q["lqpn"]: q["rqpn"] for q in json.loads(out)
                if q.get("type") == "RC" and "rqpn" in q}
    except Exception:
        return {}


def qps_peer():
    """{peer_lqpn: dst_ip} across all peer VF devices, via one ssh."""
    cmd = "; ".join(
        f'echo D {dev}; rdma -j res show qp link {dev}/1' for dev in PEER_DST)
    out = subprocess.run(["ssh", "-o", "BatchMode=yes", PEER, cmd],
                         capture_output=True, text=True).stdout
    res = {}
    dev = None
    buf = ""
    for line in out.splitlines():
        if line.startswith("D "):
            if dev and buf:
                _absorb(res, dev, buf)
            dev = line[2:].strip()
            buf = ""
        else:
            buf += line
    if dev and buf:
        _absorb(res, dev, buf)
    return res


def _absorb(res, dev, buf):
    dst = PEER_DST.get(dev)
    if not dst:
        return
    try:
        for q in json.loads(buf):
            if q.get("type") == "RC" and "lqpn" in q:
                res[q["lqpn"]] = dst
    except Exception:
        pass


sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
print(f"qpn_resolver: {len(SRC)} src VFs -> {AGENT} @{HZ}Hz", flush=True)
while True:
    t = time.time()
    peer = qps_peer()          # peer_lqpn -> dst_ip
    lines = []
    for dev, src_ip in SRC.items():
        for lqpn, rqpn in qps_local(dev).items():
            dst = peer.get(rqpn)
            if dst:
                lines.append(f"{src_ip} {lqpn} {dst}")
    if lines:
        sock.sendto("\n".join(lines).encode(), AGENT)
    time.sleep(max(0.0, 1.0 / HZ - (time.time() - t)))
