#!/usr/bin/env python3
"""design.md §4.3 layer one: per-VM MaxRate in the NIC hardware limiter.

The two layers of sender-side enforcement are meant to be independent.
Layer two (the weighted water-filling tree in tx_agent_e) is where the
policy semantics live - oversubscription shrink, inter-class isolation,
borrowing - but it is software, and the SELLING PRINCIPLE ("no VM ever
sends above the allowance it bought, no matter who else is idle") is
supposed to survive that software failing. It only does if something
class-blind, static and outside the control loop is also holding the
line. That something is `devlink port function rate`, on the VF
representor's rate leaf, on the DPU serving that VF's host.

The semantics were verified on hardware (M5d, 2026-07-10): with the
receiver oversubscribed, a VF hard-capped at 8G measured 7.72G while the
software layer redistributed the share it declined to the other three at
12.99G each against 13.1G predicted. Neither layer knows about the other;
whichever is tighter binds, and what a hard cap releases flows back to
the rest work-conserving.

A warning attached to this primitive is stale and should not be believed
without reading its sequel: the 2026-07-05 probe concluded that setting
tx_max wedged the vport and that it must never be touched. Five days
later the same firmware (32.49.1014) took ~14 loaded operations - four
VFs set to 20G mid-flow, one VF stepped through six values without
pausing traffic, then all released - with no wedge and no packet loss.
The soak retracted the warning explicitly.

Why this is Python when the rest of the fast path is C: it is not on any
path. It runs at startup and again only when someone edits the policy
file - a few times a day at most. All it does is shell out to the
`devlink` CLI and parse JSON, so a C rewrite would buy nothing and cost
genetlink plumbing.

usage:
  hw_maxrate.py --show                    what the hardware is enforcing
  hw_maxrate.py --sync                    apply policy MaxRate from registry
  hw_maxrate.py --clear                   release every local cap
"""
import argparse
import json
import os
import subprocess

# The trap in this primitive is that tx_max is not in one unit - it is in
# two, and they differ by 8. Measured on iproute2 here, 2026-07-28:
#
#   set ... tx_max 30000000000   ->  shows "tx_max 30Gbit"
#                                ->  -j reports  "tx_max": 3750000000
#
# i.e. a BARE INTEGER ON INPUT IS BITS/S, while the JSON READBACK IS
# BYTES/S. Getting this backwards is silent - the cap simply lands 8x off
# in whichever direction - so both conversions live here and nowhere else,
# and every value crossing this module's API is bits/s.
#
# Never pass a suffixed form: the same firmware has been seen to read
# "3gbps" as 3 GB/s = 24 Gbit/s.
BITS_PER_BYTE = 8


def _run(argv, timeout=10):
    if os.geteuid() != 0:
        argv = ["sudo", "-n"] + argv
    p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError("%s: %s" % (" ".join(argv),
                                       p.stderr.decode().strip()))
    return p.stdout.decode()


class HwMaxRate:
    """Binds registry vnics to devlink rate leaves and keeps them in sync.

    Every operation is best-effort by construction: a DPU where devlink
    is unavailable, or a representor that is not present, must degrade to
    "layer two only" rather than take the sender agent down with it. The
    hard cap is a backstop for a software failure, so it cannot itself be
    a way for the software to fail.
    """

    def __init__(self, local_host, vnics):
        self.local_host = local_host
        # vnic_id -> representor netdev, for THIS DPU's host only
        self.reps = {v["vnic_id"]: v["representor"] for v in vnics
                     if v.get("host") == local_host and v.get("representor")}
        self.handles = {}     # vnic_id -> devlink port handle
        self.applied = {}     # vnic_id -> tx_max in bits/s as last set
        self.error = None

    # ------------------------------------------------------------ discovery
    def discover(self):
        """Map representor netdevs to rate-leaf handles.

        The rate leaf for a VF is named by its devlink PORT index, so the
        mapping has to come from `devlink port show` - the rate objects
        themselves carry no netdev.
        """
        self.handles = {}
        try:
            ports = json.loads(_run(["devlink", "port", "show", "-j"]))["port"]
        except Exception as e:            # noqa: BLE001 - see class docstring
            self.error = "port show: %s" % e
            return self.handles
        by_netdev = {p.get("netdev"): h for h, p in ports.items()
                     if p.get("netdev")}
        for vnic, rep in self.reps.items():
            h = by_netdev.get(rep)
            if h:
                self.handles[vnic] = h
        missing = sorted(set(self.reps) - set(self.handles))
        self.error = ("no rate leaf for %s" % ",".join(missing)
                      if missing else None)
        return self.handles

    def current(self):
        """vnic_id -> tx_max in bits/s as the HARDWARE reports it (0 = off).

        Read back rather than remembered: a firmware reset, an agent
        restart or a hand-run devlink command all leave the process's idea
        of the caps stale, and this layer is only worth having if what it
        claims matches what the NIC is doing.
        """
        out = {}
        for vnic, h in self.handles.items():
            try:
                j = json.loads(_run(["devlink", "port", "function", "rate",
                                     "show", h, "-j"]))
                node = list(j["rate"].values())[0]
                out[vnic] = int(node.get("tx_max", 0)) * BITS_PER_BYTE
            except Exception:             # noqa: BLE001
                out[vnic] = None
        return out

    # -------------------------------------------------------------- apply
    def _set(self, handle, tx_max_bps):
        """Write a cap in BITS/s - the unit the CLI takes on input."""
        _run(["devlink", "port", "function", "rate", "set", handle,
              "tx_max", str(int(tx_max_bps))])

    def sync(self, caps_bps):
        """Enforce caps_bps ({vnic_id: bits/s}); return (changed, errors).

        Only differences are written. Re-writing an unchanged tx_max is
        not free - it reprograms the vport's eSwitch scheduler - and this
        is called on every policy reload, so making it a no-op when the
        policy did not actually move that VF's allowance keeps a reload
        from touching the data path at all.
        """
        changed, errors = {}, {}
        live = self.current()
        for vnic, want_bps in caps_bps.items():
            h = self.handles.get(vnic)
            if h is None:
                continue
            want_bps = int(want_bps or 0)
            if live.get(vnic) == want_bps:
                continue
            try:
                self._set(h, want_bps)
                self.applied[vnic] = want_bps
                changed[vnic] = want_bps
            except Exception as e:        # noqa: BLE001
                errors[vnic] = str(e)
        return changed, errors

    def clear(self):
        """Release every local cap (tx_max 0 = unlimited)."""
        changed, errors = {}, {}
        for vnic, h in self.handles.items():
            try:
                self._set(h, 0)
                self.applied.pop(vnic, None)
                changed[vnic] = 0
            except Exception as e:        # noqa: BLE001
                errors[vnic] = str(e)
        return changed, errors


def caps_from_policy(policy, vnics, local_host):
    """{vnic_id: bits/s} for local VFs that the policy gives a MaxRate.

    A VM with no max_rate_bps is deliberately left untouched rather than
    set to the line rate: "unsold" and "sold everything" are different
    states, and only the first should leave the hardware out of the way.
    """
    vms = policy.get("vms", {})
    out = {}
    for v in vnics:
        if v.get("host") != local_host:
            continue
        m = vms.get(v["vnic_id"], {}).get("max_rate_bps")
        if m:
            out[v["vnic_id"]] = int(m)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", default="/opt/hpft/lab-registry.json")
    ap.add_argument("--local-host", default=None)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--show", action="store_true")
    g.add_argument("--sync", action="store_true")
    g.add_argument("--clear", action="store_true")
    args = ap.parse_args()

    reg = json.load(open(args.registry))
    local_host = args.local_host or os.uname().nodename
    if local_host not in {v.get("host") for v in reg["vnics"]}:
        # DPUs are not named after the host they serve; fall back to the
        # registry's own sender/receiver pairing.
        local_host = {reg["sender_dpu"]: reg["sender_host"],
                      reg["receiver_dpu"]: reg["receiver_host"]}.get(
                          os.uname().nodename, reg["sender_host"])

    hw = HwMaxRate(local_host, reg["vnics"])
    hw.discover()
    if hw.error:
        print("hw_maxrate: %s" % hw.error)

    if args.show:
        live = hw.current()
        want = caps_from_policy(reg["policy"], reg["vnics"], local_host)
        print("%-14s %-9s %-12s %-12s" % ("vnic", "rep", "hardware", "policy"))
        for vnic in sorted(hw.reps):
            h = live.get(vnic)
            print("%-14s %-9s %-12s %-12s"
                  % (vnic, hw.reps[vnic],
                     "-" if h is None else
                     ("off" if h == 0 else "%.2fG" % (h / 1e9)),
                     "%.2fG" % (want[vnic] / 1e9) if vnic in want else "-"))
        return 0

    if args.clear:
        changed, errors = hw.clear()
    else:
        changed, errors = hw.sync(
            caps_from_policy(reg["policy"], reg["vnics"], local_host))
    for vnic, bps in sorted(changed.items()):
        print("hw_maxrate: %s -> %s"
              % (vnic, "off" if not bps else "%.2fG" % (bps / 1e9)))
    for vnic, e in sorted(errors.items()):
        print("hw_maxrate: %s FAILED: %s" % (vnic, e))
    if not changed and not errors:
        print("hw_maxrate: already in sync")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
