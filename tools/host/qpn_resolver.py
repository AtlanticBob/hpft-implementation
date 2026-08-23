#!/usr/bin/env python3
"""Sender-host qpn->dst resolver (runs on every sender host).

Transparent management-plane resolution of which dst vNIC each local QP talks
to, without touching tenant code/data:
  - local `rdma res show qp` gives {lqpn -> rqpn} per src VF device
  - the peer host's `rdma res show qp` gives {lqpn(=our rqpn) -> that VF's dst_ip}
  - join on rqpn: our_lqpn -> rqpn -> dst_ip

Emits UDP lines "<src_ip> <lqpn> <dst_ip>" to the DPU tx agent, which maps
dst_ip->cap and pushes {qpn->pair} to the RP.

WITHOUT THIS RUNNING the RP never learns which QPs belong to a pair, qp_count
stays at 1, and the pair's whole budget is handed to EVERY one of its QPs: an
n-QP flow sends n times its share while the receiver's ledger, the law and the
mailbox all read correct. That is how it presented on the four-node bring-up -
15 G granted, 58 G on the wire, exactly 4x for a 4-QP flow.

The tables it needs (which local VF is which src_ip, which peer device
terminates which dst_ip, where this host's DPU listens) all live in the
registry, so they are read from it rather than written here per host.
"""
import argparse
import json
import socket
import subprocess
import sys
import time

HZ = 5

ap = argparse.ArgumentParser()
ap.add_argument("--registry", default="/home/zhaoxiang/hyperfront/hpft-implementation/config/lab-registry.json")
ap.add_argument("--local-host", default=None, help="default: this machine's hostname")
ap.add_argument("--peer", required=True, help="receiver host whose QPs we join against")
_args = ap.parse_args()

_reg = json.load(open(_args.registry))
_me = _args.local_host or socket.gethostname()
PEER = _args.peer

_node = next((n for n in _reg["nodes"] if n["host"] == _me), None)
if _node is None:
    sys.exit(f"qpn_resolver: {_me} is not in the registry node table")
AGENT = (_node["dpu_ctl_ip"], int(_reg["e_params"]["telemetry_port"]))

# src VF: rdma device -> src_ip           (this host's vnics)
SRC = {v["rdma_dev"]: v["ip"] for v in _reg["vnics"] if v["host"] == _me}
# peer VF: rdma device -> dst_ip          (what a QP on that peer device terminates as)
PEER_DST = {v["rdma_dev"]: v["ip"] for v in _reg["vnics"] if v["host"] == PEER}
if not SRC or not PEER_DST:
    sys.exit(f"qpn_resolver: no vnics for {_me} or {PEER} in {_args.registry}")


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
print(f"qpn_resolver: {_me} {len(SRC)} src VFs -> peer {PEER} -> agent {AGENT} @{HZ}Hz", flush=True)
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
