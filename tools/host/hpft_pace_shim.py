#!/usr/bin/env python3
"""Host-side pace shim (runs on the sender host as root, resident).

Scheme E's response-law agent lives on the sender DPU Arm but the TCP
actuator (host fq+edt pinned BPF maps) lives in the host kernel. This shim
is the bridge: one UDP datagram in -> one direct bpf() map write, reusing
the unified-controller's DirectBpfMapWriter path (28-48us steady state,
never forks).

Datagram: {"src_vnic":"sgpu01/vf0","dst_vnic":"sgpu02/vf0","rate_bps":N}
(E-registry vnic naming; translated to the tcp-registry naming here.)
Replies {"ok":true,"latency_us":..} to the sender for path monitoring.

Every TX_PUSH_S it also sends the agent the wire bytes each pair it has
written has let through so far (hpft_pair_state.tx_bytes):
{"tx": {"sgpu01/vf0>sgpu02/vf0": bytes, ...}}. The agent takes their
ratios to divide a VF's vport total over the VF's flow sets.
"""
import json
import re
import socket
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/zhaoxiang/hyperfront/hpft-implementation/tools/tcp_shaper/tools")
from tcp_shaper_lib import (  # noqa: E402
    DirectBpfMapWriter,
    TcpShaperError,
    bpf_map_lookup_elem,
    bpf_map_update_elem,
    build_pair_cfg_update,
    pack_pair_state,
    vnic_index,
)

BPF_NOEXIST = 1


TCP_PIN_DIR = Path("/sys/fs/bpf/hpft_tcp_edt")
TCP_REGISTRY = "/home/zhaoxiang/hyperfront/hpft-implementation/config/lab-tcp-registry.json"
# per CONNECTION now (each has its own clock): one TSO super-packet, the
# least a clock can hand out at once, is also the natural burst
BURST_BYTES = 65_536
LISTEN = ("0.0.0.0", 9711)
TX_PUSH_S = 0.010
# offset of tx_bytes in struct hpft_pair_state: six u64, six u32
TX_BYTES_OFF = 6 * 8 + 6 * 4
RE_EVNIC = re.compile(r"^(\w+)/vf(\d+)$")


def evnic_map(tcp_reg):
    """'sgpu01/vf0' -> tcp-registry vnic_id, matched by (host, vf_index)."""
    by_host_idx = {(v["host"], v["vf_index"]): v["vnic_id"]
                   for v in tcp_reg["vnics"]}
    out = {}
    for (host, idx), vid in by_host_idx.items():
        out["%s/vf%d" % (host, idx)] = vid
    return out


def seed_pair_states(tcp_reg, writer):
    """The datapath paces a pair only when BOTH cfg and state exist; the
    apply tool seeds state for its static rule list (the straight pairs)
    only, so cfg writes for any other pair were silently ignored and
    cross-pair TCP ran unpaced (stress D1, 2026-07-11). Seed state for
    every local-src -> remote-dst pair; NOEXIST never clobbers a live one.
    Written through the bpf() syscall, not bpftool: on a host whose kernel
    has no matching linux-tools package the bpftool on PATH is a wrapper
    that only prints a warning, every seed failed, and all TCP outside the
    straight pairs ran unpaced while the shim reported "seeded 0 new"
    (sgpu01 on 5.15.0-187, found 2026-09-11 behind the 48-flow-set
    failure)."""
    fd = writer.fd("hpft_pair_state")
    indices = vnic_index(tcp_reg)
    host = socket.gethostname()
    seeded = present = failed = 0
    for s in tcp_reg["vnics"]:
        if str(s.get("host")) != host:
            continue
        for d in tcp_reg["vnics"]:
            if str(d.get("host")) == host:
                continue
            key = (indices[s["vnic_id"]] << 32) | indices[d["vnic_id"]]
            try:
                bpf_map_update_elem(fd, struct.pack("<Q", key),
                                    pack_pair_state(), BPF_NOEXIST)
                seeded += 1
            except TcpShaperError as e:
                if "exists" in str(e):
                    present += 1
                else:
                    failed += 1
                    if failed <= 3:
                        print("pace_shim: pair_state seed failed: %s" % e, flush=True)
    print("pace_shim: pair_state seeded %d new, %d already present, %d failed"
          % (seeded, present, failed), flush=True)
    if seeded + present == 0:
        sys.exit("pace_shim: no pair_state entry for this host - TCP would run unpaced")


def main():
    tcp_reg = json.load(open(TCP_REGISTRY))
    emap = evnic_map(tcp_reg)
    writer = DirectBpfMapWriter(TCP_PIN_DIR)
    writer.fd("hpft_pair_cfg")  # open now, fail fast if EDT not applied
    seed_pair_states(tcp_reg, writer)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(LISTEN)
    print("pace_shim: listening %s:%d pin=%s vnics=%d"
          % (*LISTEN, TCP_PIN_DIR, len(emap)), flush=True)
    n, err = 0, 0
    last_log = time.monotonic()
    state_fd = writer.fd("hpft_pair_state")
    state_size = len(pack_pair_state())
    pairs = {}          # "src>dst" (agent naming) -> pair key bytes
    agent = None        # where the rate datagrams come from
    last_push = time.monotonic()
    sock.settimeout(TX_PUSH_S)
    while True:
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            data = None
        now = time.monotonic()
        if agent is not None and pairs and now - last_push >= TX_PUSH_S:
            last_push = now
            tx = {}
            for name, key in pairs.items():
                v = bpf_map_lookup_elem(state_fd, key, state_size)
                if v is not None:
                    tx[name] = struct.unpack_from("<Q", v, TX_BYTES_OFF)[0]
            try:
                sock.sendto(json.dumps({"tx": tx}).encode(), agent)
            except OSError:
                pass
        if data is None:
            continue
        t0 = time.monotonic_ns()
        try:
            msg = json.loads(data)
            upd = build_pair_cfg_update(
                registry=tcp_reg,
                src_vnic=emap[msg["src_vnic"]],
                dst_vnic=emap[msg["dst_vnic"]],
                rate_bps=int(msg["rate_bps"]),
                burst_bytes=BURST_BYTES,
                # the BPF program reads only rate_bps (the flow set's R)
                # and burst_bytes (per connection); flags and generation
                # are stored, never acted on
                flags=0,
                generation=0)
            writer.update(upd)
            pairs["%s>%s" % (msg["src_vnic"], msg["dst_vnic"])] = upd.key
            agent = addr
            n += 1
            reply = {"ok": True,
                     "latency_us": round((time.monotonic_ns() - t0) / 1e3, 1)}
        except Exception as e:  # noqa: BLE001 - resident: never die on input
            err += 1
            reply = {"ok": False, "err": str(e)}
            print("pace_shim: ERR %s on %r" % (e, data[:200]), flush=True)
        try:
            sock.sendto(json.dumps(reply).encode(), addr)
        except OSError:
            pass
        if time.monotonic() - last_log >= 60:
            print("pace_shim: writes=%d errs=%d" % (n, err), flush=True)
            last_log = time.monotonic()


if __name__ == "__main__":
    main()
