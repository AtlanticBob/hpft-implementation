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
"""
import json
import re
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/zhaoxiang/hyperfront/hpft-implementation/tools/tcp_shaper/tools")
from tcp_shaper_lib import (  # noqa: E402
    DirectBpfMapWriter,
    build_pair_cfg_update,
    make_generation,
    vnic_index,
)

TCP_PIN_DIR = Path("/sys/fs/bpf/hpft_tcp_edt")
TCP_REGISTRY = "/home/zhaoxiang/hyperfront/hpft-implementation/config/lab-tcp-registry.json"
BURST_BYTES = 262_144
LISTEN = ("0.0.0.0", 9711)
RE_EVNIC = re.compile(r"^(\w+)/vf(\d+)$")


def evnic_map(tcp_reg):
    """'sgpu01/vf0' -> tcp-registry vnic_id, matched by (host, vf_index)."""
    by_host_idx = {(v["host"], v["vf_index"]): v["vnic_id"]
                   for v in tcp_reg["vnics"]}
    out = {}
    for (host, idx), vid in by_host_idx.items():
        out["%s/vf%d" % (host, idx)] = vid
    return out


def seed_pair_states(tcp_reg):
    """The datapath paces a pair only when BOTH cfg and state exist; the
    apply tool seeds state for its static rule list (the straight pairs)
    only, so cfg writes for any other pair were silently ignored and
    cross-pair TCP ran unpaced (stress D1, 2026-07-11). Seed state for
    every local-src -> remote-dst pair; noexist never clobbers a live one."""
    state_pin = TCP_PIN_DIR / "maps" / "hpft_pair_state"
    indices = vnic_index(tcp_reg)
    host = socket.gethostname()
    seeded = 0
    for s in tcp_reg["vnics"]:
        if str(s.get("host")) != host:
            continue
        for d in tcp_reg["vnics"]:
            if str(d.get("host")) == host:
                continue
            key = (indices[s["vnic_id"]] << 32) | indices[d["vnic_id"]]
            cmd = (["bpftool", "map", "update", "pinned", str(state_pin),
                    "key", "hex"]
                   + ["%02x" % b for b in struct.pack("<Q", key)]
                   + ["value", "hex"] + ["00"] * 24 + ["noexist"])
            if subprocess.run(cmd, capture_output=True).returncode == 0:
                seeded += 1
    print("pace_shim: pair_state seeded %d new" % seeded, flush=True)


def main():
    tcp_reg = json.load(open(TCP_REGISTRY))
    emap = evnic_map(tcp_reg)
    writer = DirectBpfMapWriter(TCP_PIN_DIR)
    writer.fd("hpft_pair_cfg")  # open now, fail fast if EDT not applied
    seed_pair_states(tcp_reg)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(LISTEN)
    print("pace_shim: listening %s:%d pin=%s vnics=%d"
          % (*LISTEN, TCP_PIN_DIR, len(emap)), flush=True)
    n, err = 0, 0
    last_log = time.monotonic()
    while True:
        data, addr = sock.recvfrom(2048)
        t0 = time.monotonic_ns()
        try:
            msg = json.loads(data)
            upd = build_pair_cfg_update(
                registry=tcp_reg,
                src_vnic=emap[msg["src_vnic"]],
                dst_vnic=emap[msg["dst_vnic"]],
                rate_bps=int(msg["rate_bps"]),
                burst_bytes=BURST_BYTES,
                generation=make_generation())
            writer.update(upd)
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
