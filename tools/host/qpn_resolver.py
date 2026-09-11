#!/usr/bin/env python3
"""Sender-host qpn->dst resolver (runs on every sender host).

Transparent management-plane resolution of which dst vNIC each local QP talks
to, without touching tenant code/data:
  - local `rdma res show qp` gives {lqpn -> rqpn} per src VF device
  - each peer host's `rdma res show qp` gives {(lqpn, rqpn) -> that VF's dst_ip},
    matched against our (rqpn, lqpn)
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
LIVE = ("RTS", "RTR")     # connected QP states; see qps_local

ap = argparse.ArgumentParser()
ap.add_argument("--registry", default="/home/zhaoxiang/hyperfront/hpft-implementation/config/lab-registry.json")
ap.add_argument("--local-host", default=None, help="default: this machine's hostname")
ap.add_argument("--peer", required=True,
                help="receiver host(s) whose QPs we join against, comma-separated "
                     "(every other node since 2026-09-04)")
_args = ap.parse_args()

_reg = json.load(open(_args.registry))
_me = _args.local_host or socket.gethostname()
PEERS = [h for h in _args.peer.split(",") if h]

_node = next((n for n in _reg["nodes"] if n["host"] == _me), None)
if _node is None:
    sys.exit(f"qpn_resolver: {_me} is not in the registry node table")
AGENT = (_node["dpu_ctl_ip"], int(_reg["e_params"]["telemetry_port"]))

# src VF: rdma device -> src_ip           (this host's vnics)
SRC = {v["rdma_dev"]: v["ip"] for v in _reg["vnics"] if v["host"] == _me}
# peer VF: (peer host, rdma device) -> dst_ip   (what a QP on that peer device terminates as)
PEER_DST = {(v["host"], v["rdma_dev"]): v["ip"]
            for v in _reg["vnics"] if v["host"] in PEERS}
if not SRC or not PEER_DST:
    sys.exit(f"qpn_resolver: no vnics for {_me} or {PEERS} in {_args.registry}")


PSN_MOD = 1 << 24


def _psn_gap(a, b):
    d = (a - b) % PSN_MOD
    return min(d, PSN_MOD - d)


def qps_local(dev):
    """{lqpn: (rqpn, sq-psn, rq-psn)} for a local rdma device."""
    out = subprocess.run(["rdma", "-j", "res", "show", "qp", "link", f"{dev}/1"],
                         capture_output=True, text=True).stdout
    try:
        # Connected states only (RTS, and RTR: a perftest WRITE server's
        # QPs never leave RTR): a QP of the previous run lingers in ERR or
        # RESET with numbers the new run may reuse, and a pairing built on
        # it binds the new QP to the wrong flow set (2026-09-08)
        return {q["lqpn"]: (q["rqpn"], q.get("sq-psn", 0), q.get("rq-psn", 0))
                for q in json.loads(out)
                if q.get("type") == "RC" and "rqpn" in q and q.get("state") in LIVE}
    except Exception:
        return {}


def qps_peer():
    """{(peer_lqpn, peer_rqpn): [(dst_ip, sq-psn, rq-psn), ...]} across all
    peers, one ssh per peer.

    The key is the QP PAIR, not the peer's lqpn alone: QP numbers are only
    unique per device, so with several peers two of them can hand out the
    same number, and a local QP is matched only when its (rqpn, lqpn) is
    the mirror image of the peer's (lqpn, rqpn). Even the pair is not
    unique in a full mesh: every host allocates QP numbers in step, so a
    peer's QP can mirror ours number for number while being connected to a
    third host. Measured 2026-09-11 (2-7b mesh96): sgpu04/vf0 held a pair
    that mirrored three of sgpu01/vf0's QPs to sgpu03/vf0, and because the
    table kept whichever peer was read last, those three QPs were bound to
    sgpu01's flow set towards sgpu04 - one set had 23 members, the other 17,
    and the second sent 30 % above its budget on the first one's pool. So
    every candidate is kept and match() decides between them by PSN."""
    res = {}
    for peer in PEERS:
        devs = [d for (h, d) in PEER_DST if h == peer]
        cmd = "; ".join(f'echo D {dev}; rdma -j res show qp link {dev}/1' for dev in devs)
        # accept-new: a host whose key is not in this user's known_hosts
        # yet must not silently produce an empty table (sgpu03 and sgpu04
        # did exactly that, 2026-09-07: every QP of theirs ran unbound at
        # the executor's unknown-flow allowance while everything reported
        # healthy). A CHANGED key is still refused.
        out = subprocess.run(["ssh", "-o", "BatchMode=yes",
                              "-o", "StrictHostKeyChecking=accept-new",
                              "-o", "ConnectTimeout=5", peer, cmd],
                             capture_output=True, text=True).stdout
        dev = None
        buf = ""
        for line in out.splitlines():
            if line.startswith("D "):
                if dev and buf:
                    _absorb(res, peer, dev, buf)
                dev = line[2:].strip()
                buf = ""
            else:
                buf += line
        if dev and buf:
            _absorb(res, peer, dev, buf)
    return res


def _absorb(res, peer, dev, buf):
    dst = PEER_DST.get((peer, dev))
    if not dst:
        return
    try:
        for q in json.loads(buf):
            if q.get("type") == "RC" and "lqpn" in q and "rqpn" in q and q.get("state") in LIVE:
                res.setdefault((q["lqpn"], q["rqpn"]), []).append(
                    (dst, q.get("sq-psn", 0), q.get("rq-psn", 0)))
    except Exception:
        pass


def match(cands, sq, rq):
    """The dst of the one candidate that is connected to this QP. Two ends
    of one connection count the same packets: this end's send PSN is the
    other end's expected receive PSN (apart from what was sent between the
    two queries - tens of thousands at most), and the same holds the other
    way. Whichever direction carries the data, one of the two gaps is small,
    while for a QP that merely mirrors the numbers both are random 24-bit
    distances. The closest candidate wins."""
    if len(cands) == 1:
        return cands[0][0]
    return min(cands, key=lambda c: min(_psn_gap(sq, c[2]), _psn_gap(c[1], rq)))[0]


sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
print(f"qpn_resolver: {_me} {len(SRC)} src VFs -> peers {','.join(PEERS)} -> agent {AGENT} @{HZ}Hz", flush=True)
ambiguous, last_log = 0, time.time()
while True:
    t = time.time()
    peer = qps_peer()          # (peer_lqpn, peer_rqpn) -> candidates
    lines = []
    for dev, src_ip in SRC.items():
        for lqpn, (rqpn, sq, rq) in qps_local(dev).items():
            cands = peer.get((rqpn, lqpn))
            if cands:
                ambiguous += len(cands) > 1
                lines.append(f"{src_ip} {lqpn} {match(cands, sq, rq)}")
    if lines:
        sock.sendto("\n".join(lines).encode(), AGENT)
    if t - last_log >= 60:
        if ambiguous:
            print(f"qpn_resolver: {ambiguous} QP lookups had several mirror candidates in the last minute (decided by PSN)", flush=True)
        ambiguous, last_log = 0, t
    time.sleep(max(0.0, 1.0 / HZ - (time.time() - t)))
