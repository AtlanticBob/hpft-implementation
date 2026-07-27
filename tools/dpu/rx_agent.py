#!/usr/bin/env python3
"""Receiver-side agent: the hierarchical virtual scheduler (design.md
§3.2-§3.4). Runs on the receiver DPU Arm as root.

Pipeline per control period T:
  meter   hybrid r_f (see below): fresh per-dst-VF totals x flow-set mix
  sched   demand D_f = r_f(1+delta) -> 3-layer weighted water-filling
          (VM cap MaxRate_d -> class -> per-sender) -> entitled rate e_f
          and the fair-share ceiling ceil_f (§3.2)
  marker  vq_f <- clip(vq_f + (r_f - e_f)T, 0, V); s_f = min(vq_f/V, 1)
  target  u_f = ceil_f * (1 - gamma * s_f)   (§3.4: policy ceiling x audit
          discount; bounded, u_f >= (1-gamma)*ceil_f)
  telem   one UDP datagram per sender DPU per tick, TWO numbers per active
          flow-set: {fsid: {u, r}}. u drives the sender's tracking law
          (§4.2); r is the class-level arrival rate the sender forwards to
          the RDMA executor as rate feedback (§5.2) and uses as its own
          tree demand input (§4.3). A record is sent every tick even with
          no excess (u = ceil_f then) - the record IS the explicit
          freshness permit, and its absence is what triggers the sender's
          fail-open (§4.4).

e_f, s_f, ceil_f and vq_f stay local: they are the intermediate ledger the
target is synthesised from, kept in the jsonl log for offline analysis but
no longer on the wire (v1 shipped {s, r, e, ceil} because the MIMD law
re-derived its own caps and floors at the sender; the v2 law consumes one
number).

Hybrid r_f measurement (lab fact, 2026-07-09): megaflow HW byte counters
are exact but only refresh ~1 Hz (mlx5 fc bulk-query period, hardcoded in
5.15). Representor vport counters are fresh at any rate but per-VF totals.
So: r_f = R_d(vport, fresh every tick) x share_f(megaflow bytes over a
sliding window). Megaflow KEYS appear instantly, so a single new flow-set
on a dst is attributed 100% from its first tick; multiple simultaneous new
flow-sets split equally until counters land (<=1 s). Single-class-per-VF
scenarios (M1/M1b/M3) are exact; class-mix transitions (M2) carry <=1 s
attribution lag - reported with M2 results.

Direct class-split source (2026-07-13, --vport-meter): a DPU-local helper
(vport_meter.c) publishes per-VF vport counters - received_ib (RoCE) and
received_eth (kernel-path TCP+ip) octets, live in FW with no caching
quantum, stamped by the writer in this DPU's own monotonic clock - into an
mmap'd tmpfs file. When fresh, it replaces both the receiver-host rate
exporter AND the total-minus-TCP subtraction: RDMA is read directly, not
inferred, and both classes come from one point and one clock. Validated
against host ground truth (results/measure_v2_20260713): byte-exact
totals, incast8 steady mae 0.36% (1s bins), and it kills the subtraction
residual that mis-reported 0.7-2G of phantom RDMA whenever TCP flowed
while RDMA was silent. Attribution preference: vport meter > host
exporter > megaflow mix (each stage falls back on staleness, so the
agent runs unchanged without the helper; the helper runs as the
hpft-vport-meter systemd unit on the receiver DPU).

Implementation choice (design silent on r_f = 0): an idle flow-set's vq
drains fast (V per period). Frozen marks would otherwise pin the sender at
the floor after the app pauses, which contradicts fail-open intent.

Policy (weights/MaxRate) hot-reloads when the registry file mtime changes.
"""
import argparse
import json
import mmap
import os
import re
import socket
import struct
import subprocess
import time

from fastfill import waterfill_ceilings

CLASS_RULES = [
    (50, "udp,tp_dst=4791"),   # RoCEv2
    (45, "tcp"),               # TCP
    (40, "ip"),                # remaining IP (diagnostics only)
]
SCHED_CLASSES = ("tcp", "rdma")   # ip_other is metered but never scheduled

RE_ETH = re.compile(r"eth\(src=([0-9a-f:]{17}),dst=([0-9a-f:]{17})\)")
RE_TYPE = re.compile(r"eth_type\(0x([0-9a-f]+)\)")
RE_IPV4 = re.compile(r"ipv4\(([^)]*)\)")
RE_UDP_DST = re.compile(r"udp\([^)]*dst=(\d+)")
RE_BYTES = re.compile(r"\bbytes:(\d+)")


class Unixctl:
    """Persistent JSON-RPC client for the ovs-vswitchd unixctl socket."""

    def __init__(self, rundir="/var/run/openvswitch", target="ovs-vswitchd"):
        self.rundir = rundir
        self.target = target
        self.sock = None
        self._id = 0

    def _connect(self):
        pid = int(open(os.path.join(self.rundir, self.target + ".pid")).read())
        path = os.path.join(self.rundir, "%s.%d.ctl" % (self.target, pid))
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(path)
        self.sock = s

    def _close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def call(self, method, params):
        for attempt in (0, 1):
            try:
                if self.sock is None:
                    self._connect()
                self._id += 1
                req = {"method": method, "params": params, "id": self._id}
                self.sock.sendall(json.dumps(req).encode())
                buf = b""
                while True:
                    chunk = self.sock.recv(1 << 18)
                    if not chunk:
                        raise OSError("unixctl EOF")
                    buf += chunk
                    try:
                        obj, _ = json.JSONDecoder().raw_decode(buf.decode())
                    except ValueError:
                        continue  # partial response, keep reading
                    if obj.get("error") is not None:
                        raise RuntimeError("unixctl error: %r" % (obj["error"],))
                    return obj["result"]
            except OSError:
                self._close()
                if attempt:
                    raise
        raise OSError("unreachable")


def install_class_rules(bridge):
    """Idempotent: only add rules that are missing (add-flow on an existing
    match would trigger revalidation and reset megaflow counters)."""
    out = subprocess.run(["ovs-ofctl", "dump-flows", bridge],
                         capture_output=True, text=True, check=True).stdout
    added = []
    for prio, match in CLASS_RULES:
        if not _rule_present(out, prio, match):
            subprocess.run(["ovs-ofctl", "add-flow", bridge,
                            "priority=%d,%s,actions=NORMAL" % (prio, match)],
                           check=True)
            added.append((prio, match))
    return added


def _rule_present(dump, prio, match):
    want = "priority=%d,%s" % (prio, match)
    return any(want + " " in line or line.rstrip().endswith(want)
               for line in dump.splitlines())


class FlowSetMeter:
    """Megaflow byte counters -> per-flow-set arrival rates."""

    def __init__(self, mac2vnic, local_macs):
        self.mac2vnic = mac2vnic
        self.local_macs = local_macs
        self.prev = {}          # megaflow key -> bytes
        self.unknown_macs = set()

    def classify(self, line):
        m = RE_ETH.search(line)
        if not m:
            return None
        src_mac, dst_mac = m.group(1), m.group(2)
        if dst_mac not in self.local_macs:
            return None         # only traffic arriving at local VMs
        t = RE_TYPE.search(line)
        if not t or int(t.group(1), 16) != 0x0800:
            return None         # non-IP (ARP/LLDP): ignore
        src = self.mac2vnic.get(src_mac)
        dst = self.mac2vnic.get(dst_mac)
        if src is None or dst is None:
            self.unknown_macs.add(src_mac if src is None else dst_mac)
            return None
        ip4 = RE_IPV4.search(line)
        fields = ip4.group(1) if ip4 else ""
        if "proto=6" in fields:
            cls = "tcp"
        elif "proto=17" in fields:
            u = RE_UDP_DST.search(line)
            cls = "rdma" if u and u.group(1) == "4791" else "ip_other"
        else:
            cls = "ip_other"
        return "%s>%s|%s" % (src, dst, cls)

    def tick(self, dump_text):
        """Returns (fs_delta_bytes, fs_present). fs_present holds every
        flow-set with a live megaflow key (keys show up instantly even
        though their counters lag ~1s). First call only snapshots."""
        cur = {}
        fs_bytes = {}
        fs_present = set()
        for line in dump_text.splitlines():
            b = RE_BYTES.search(line)
            if not b:
                continue
            key = line.split(", packets:")[0]
            nbytes = int(b.group(1))
            cur[key] = nbytes
            fsid = self.classify(line)
            if fsid is None:
                continue
            fs_present.add(fsid)
            pv = self.prev.get(key)
            if pv is None or nbytes < pv:
                # new megaflow (bytes since creation, i.e. since last tick)
                # or counter reset after evict+reinstall: count from 0
                delta = nbytes
            else:
                delta = nbytes - pv
            if delta:
                fs_bytes[fsid] = fs_bytes.get(fsid, 0) + delta
        first = not self.prev
        self.prev = cur
        if first:
            return {}, fs_present
        return fs_bytes, fs_present


class VportMeter:
    """Reader for the vport_meter.c shared-memory file: per-VF vport
    counters with the ib(RoCE)/eth(kernel-path) class split, queried from
    FW by a local helper at ~1ms and published under a per-record seqlock.
    (t_ns, bytes) are stamped together BY THE WRITER, so like the host
    exporter they cannot decouple bytes from time (the +-60% bug family);
    unlike it they live in this DPU's own CLOCK_MONOTONIC domain, so no
    cross-host, cross-clock subtraction is involved. Validated byte-exact
    against host port_rcv_data / netdev rx_bytes (2026-07-13).

    Fail-safe: a record older than STALE_S (helper dead/wedged) drops its
    vnic out of self.fresh and the caller falls back to the old
    attribution chain; if nothing has been fresh for >1s the mmap is
    reopened (helper restart = new file)."""

    HDR = struct.Struct("<8sII8H")
    STALE_S = 0.1

    def __init__(self, path, vport_of_vnic):
        self.path = path
        self.vport_of_vnic = vport_of_vnic   # local vnic_id -> esw vport
        self.mm = None
        self.slot_of_vnic = {}
        self.ring = {}                       # vnic -> [(t_s, ib, eth)]
        self.last_t = {}                     # vnic -> t_ns last ingested
        self.fresh = set()
        self._next_open = 0.0
        self._last_any_fresh = 0.0

    def _open(self, now):
        if now < self._next_open:
            return False
        self._next_open = now + 1.0
        self._close()
        try:
            f = open(self.path, "rb")
        except OSError:
            return False
        try:
            self.mm = mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ)
        except (OSError, ValueError):
            return False
        finally:
            f.close()
        hdr = self.HDR.unpack_from(self.mm, 0)
        if hdr[0] != b"HPFTVPM1":
            self._close()
            return False
        slot = {hdr[3 + i]: i for i in range(hdr[1])}
        self.slot_of_vnic = {vn: slot[vp]
                             for vn, vp in self.vport_of_vnic.items()
                             if vp in slot}
        self._last_any_fresh = now
        return True

    def _close(self):
        if self.mm is not None:
            try:
                self.mm.close()
            except OSError:
                pass
        self.mm = None
        self.slot_of_vnic = {}

    def sample(self, now, nkeep):
        """Ingest the newest record per local vnic; sets self.fresh."""
        self.fresh = set()
        if self.mm is None and not self._open(now):
            return self.fresh
        for vnic, slot in self.slot_of_vnic.items():
            off = 64 + 64 * slot
            ok = False
            for _ in range(3):
                s1 = struct.unpack_from("<Q", self.mm, off)[0]
                if s1 & 1:
                    continue        # writer mid-update
                t_ns, rx_ib, rx_eth = struct.unpack_from(
                    "<3Q", self.mm, off + 8)
                if s1 == struct.unpack_from("<Q", self.mm, off)[0]:
                    ok = True
                    break
            if not ok or t_ns == 0 or now - t_ns / 1e9 > self.STALE_S:
                continue
            self.fresh.add(vnic)
            if t_ns != self.last_t.get(vnic):
                self.last_t[vnic] = t_ns
                ring = self.ring.setdefault(vnic, [])
                ring.append((t_ns / 1e9, rx_ib, rx_eth))
                while len(ring) > nkeep:
                    ring.pop(0)
        if self.fresh:
            self._last_any_fresh = now
        elif self.mm is not None and now - self._last_any_fresh > 1.0:
            self._close()           # helper restarted? force reopen
        return self.fresh

    def rates(self, vnic):
        """(rdma_bps, kern_bps) over the sample window, or None."""
        ring = self.ring.get(vnic)
        if not ring or len(ring) < 2:
            return None
        (t0, ib0, e0), (t1, ib1, e1) = ring[0], ring[-1]
        dt = t1 - t0
        if dt <= 0 or ib1 < ib0 or e1 < e0:
            return None
        return ((ib1 - ib0) * 8 / dt, (e1 - e0) * 8 / dt)


class HybridRates:
    """r_f = fresh per-dst-VF vport rate x windowed megaflow byte share
    (see module docstring).

    Fast/slow split for the 1ms control period (2026-07-11): the vport
    counters are read every tick (the fast path, ~6us for 4 VFs), but the
    per-VF-total rate is measured over a sliding window that looks back a
    few milliseconds so the ~0.87ms counter-refresh quantum does not turn
    single ticks into zero/spike noise. The megaflow class-mix is refreshed
    only on the slow loop (update_mix, ~5 Hz) because the mix source is
    itself 1s-stale; between refreshes the fast path reuses cached shares.
    """

    def __init__(self, rep_of_vnic, mix_window_s, rate_window_s=0.006,
                 meter=None):
        self.rep_of_vnic = rep_of_vnic    # local vnic_id -> representor dev
        self.window = mix_window_s
        self.rate_window = rate_window_s
        self.meter = meter                # VportMeter or None
        self.attr_meter = set()           # dsts meter-attributed this tick
        self.hist = []                    # (t, fs_delta_bytes) for the mix
        self.ring = {}                    # vnic -> deque of (t, bytes)
        self._fd = {}
        self.r_d = {}                     # vnic -> fresh vport rate
        self.mix_shares = {}              # fsid -> fraction of its dst total
        self.by_dst = {}                  # dst vnic -> set(fsid)
        self.unattributed = 0.0
        # receiver-host kernel byte reports (rate exporter, 2026-07-11):
        # (t_host_ns, rx_bytes) pairs are stamped together AT READ TIME on
        # the host, so unlike the local loop they cannot decouple bytes
        # from time (the +-60% bug family). RoCE bypasses the kernel
        # netdev counter entirely (measured: 16.4GB of RDMA moved it by
        # 2386 bytes), so this is a fresh kernel-path(TCP+ip)-only source.
        self.host_ring = {}               # vnic -> [(t_host_ns, rx_bytes)]
        self.host_seen = {}               # vnic -> local monotonic of report
        self.attr_host = set()            # dsts host-attributed this tick

    def _rep_tx_bytes(self, dev):
        fd = self._fd.get(dev)
        if fd is None:
            try:
                fd = self._fd[dev] = open(
                    "/sys/class/net/%s/statistics/tx_bytes" % dev)
            except OSError:
                return None
        try:
            fd.seek(0)
            return int(fd.read())
        except (OSError, ValueError):
            self._fd.pop(dev, None)
            return None

    def update_mix(self, fs_deltas, fs_present, now):
        """Slow path (~5 Hz): recompute per-dst class-mix shares from the
        windowed megaflow byte deltas."""
        self.hist.append((now, fs_deltas))
        while self.hist and self.hist[0][0] < now - self.window:
            self.hist.pop(0)
        win = {}
        for _, deltas in self.hist:
            for f, n in deltas.items():
                win[f] = win.get(f, 0) + n
        by_dst = {}
        for f in set(fs_present) | set(win):
            dst = f.rsplit("|", 1)[0].split(">")[1]
            by_dst.setdefault(dst, set()).add(f)
        shares = {}
        for dst, members in by_dst.items():
            wtot = sum(win.get(f, 0) for f in members)
            for f in members:
                shares[f] = (win.get(f, 0) / wtot) if wtot > 0 \
                    else 1.0 / len(members)
        self.mix_shares = shares
        self.by_dst = by_dst

    def sample(self, now, period):
        """Read every dst-VF vport counter and update its rate estimate.
        MUST be called at the very top of the loop, before any slow work,
        so the sampling cadence is set purely by the loop's sleep schedule
        and never perturbed by a dump landing between two samples - that
        perturbation decoupled the byte count from the timestamp and turned
        a clean 6G into +-60% noise (2026-07-11). Rate is over a FIXED
        NUMBER of samples (not a time window, which is not jitter-robust):
        N spans ~rate_window at a 1ms period (smoothing the 0.87ms counter
        quantum) and is exactly 2 (tick-to-tick) at 50ms."""
        nkeep = max(2, round(self.rate_window / period) + 1)
        self.r_d = {}
        for vnic, dev in self.rep_of_vnic.items():
            v = self._rep_tx_bytes(dev)
            if v is None:
                continue
            ring = self.ring.setdefault(vnic, [])
            ring.append((now, v))
            while len(ring) > nkeep:
                ring.pop(0)
            t0, v0 = ring[0]
            dt = now - t0
            if dt > 0 and v >= v0:
                self.r_d[vnic] = (v - v0) * 8 / dt
        # vport meter (direct class split): when fresh, its ib+eth sum
        # replaces the sysfs total for the same vnic - one source, one
        # clock, so class rates and the total can never disagree. The
        # sysfs ring above is still maintained every tick, so a meter
        # death fails over with a warm window.
        if self.meter is not None:
            for vnic in self.meter.sample(now, nkeep):
                mr = self.meter.rates(vnic)
                if mr is not None:
                    self.r_d[vnic] = mr[0] + mr[1]

    def host_update(self, vnic, t_host_ns, rx_bytes, now):
        """Ingest one receiver-host counter snapshot for a local dst VF."""
        ring = self.host_ring.setdefault(vnic, [])
        if ring and t_host_ns <= ring[-1][0]:
            return                        # stale/reordered datagram
        ring.append((t_host_ns, rx_bytes))
        horizon = t_host_ns - int(self.rate_window * 2e9)
        while len(ring) > 2 and ring[0][0] < horizon:
            ring.pop(0)
        self.host_seen[vnic] = now

    def _host_kernel_rate(self, vnic, now):
        """Fresh kernel-path (tcp+ip) arrival rate for a dst VF from the
        host counter; None when the exporter is absent/stale (>0.1s) so
        the caller falls back to the megaflow mix."""
        if now - self.host_seen.get(vnic, -1e9) > 0.1:
            return None
        ring = self.host_ring.get(vnic)
        if not ring or len(ring) < 2:
            return None
        (t0, b0), (t1, b1) = ring[0], ring[-1]
        if t1 <= t0 or b1 < b0:
            return None
        return (b1 - b0) * 8 / ((t1 - t0) / 1e9)

    def rates(self, now=None):
        """Compute r_f from the last sample() x class attribution.

        Class-level split preference: (1) vport meter - both classes read
        directly from the per-VF ib/eth hardware buckets, no subtraction;
        (2) receiver-host kernel counter - r_kernel from the host,
        r_rdma = vport total - r_kernel (any TCP error pollutes RDMA);
        (3) megaflow mix. The mix (1s HW cache, 2s window) cannot track
        second-scale contention - it split every instant ~50/50 and
        mis-marked both classes (M2, 2026-07-11: rx read rdma at 2.7x its
        true rate). The mix remains the intra-class splitter for
        multi-sender flow-sets whichever class source is active."""
        rates = {}
        self.unattributed = 0.0
        self.attr_host = set()
        self.attr_meter = set()
        if now is None:
            now = time.monotonic()
        for dst, total in self.r_d.items():
            if total <= 0:
                continue
            members = self.by_dst.get(dst)
            if not members:
                self.unattributed += total
                continue
            pools = None
            if self.meter is not None and dst in self.meter.fresh:
                mr = self.meter.rates(dst)
                if mr is not None:
                    pools = {"rdma": mr[0], "kern": mr[1]}
                    self.attr_meter.add(dst)
            if pools is None:
                kern = self._host_kernel_rate(dst, now)
                if kern is not None:
                    self.attr_host.add(dst)
                    pools = {"kern": min(kern, total)}
                    pools["rdma"] = max(total - pools["kern"], 0.0)
            if pools is None:
                for f in members:
                    share = self.mix_shares.get(f, 1.0 / len(members))
                    if share > 0:
                        rates[f] = total * share
                continue
            groups = {"kern": [], "rdma": []}
            for f in members:
                cls = f.rsplit("|", 1)[1]
                groups["rdma" if cls == "rdma" else "kern"].append(f)
            for g, fs in groups.items():
                pool = pools[g]
                if pool <= 0:
                    continue
                if not fs:
                    self.unattributed += pool
                    continue
                wsum = sum(self.mix_shares.get(f, 0.0) for f in fs)
                for f in fs:
                    share = (self.mix_shares.get(f, 0.0) / wsum) \
                        if wsum > 0 else 1.0 / len(fs)
                    if share > 0:
                        rates[f] = pool * share
        return rates


def waterfill(capacity, items):
    """Bounded weighted water-filling (design_e §0). items: {key: (weight,
    cap)}; returns {key: share}. Frozen (saturated) items release the rest."""
    alloc = {k: 0.0 for k in items}
    active = {k: v for k, v in items.items() if v[1] > 0}
    remaining = capacity
    while active and remaining > 1e-3:
        wsum = sum(w for w, _ in active.values())
        # water level rises until the cheapest cap saturates or money runs out
        t_sat = min((cap - alloc[k]) / w for k, (w, cap) in active.items())
        t = min(t_sat, remaining / wsum)
        for k, (w, cap) in list(active.items()):
            alloc[k] += w * t
            if alloc[k] >= cap - 1e-3:
                alloc[k] = cap
                del active[k]
        remaining = capacity - sum(alloc.values())
        if t < t_sat and t_sat != float("inf"):
            break   # capacity exhausted before next saturation
    return alloc


class Scheduler:
    """3-layer water-filling -> e_f (design_e §3.3)."""

    def __init__(self, policy, line_rate, headroom, delta):
        self.policy = policy
        self.headroom = headroom
        self.c_root = line_rate * (1.0 - headroom)
        self.delta = delta

    def set_downlink(self, speed_bps):
        """Track the live downlink speed (M3: p1 renegotiated to 100G).
        Root capacity must follow, or the account never sees the shortage."""
        self.c_root = speed_bps * (1.0 - self.headroom)

    def entitlements(self, rates, demand_in=None):
        """rates: {fsid: r_f} -> ({fsid: e_f}, {fsid: ceil_f}).

        e_f: demand-capped water-filling share (design §3.3) - the grant.
        ceil_f: same tree with infinite demands - the fair-share ceiling.
        The VQ charges by excess over e_f but DRAINS by (ceil_f - r): with
        drain keyed to demand-capped e_f a crushed flow-set (r tiny -> e
        tiny) could never drain its vq and stayed marked forever (observed
        2026-07-09). At equilibrium r ~= ceil_f so drain ~= 0: no free
        unmarking."""
        demand = demand_in if demand_in is not None else \
            {f: r * (1.0 + self.delta) for f, r in rates.items()}
        e = self._fill(demand)
        # ceil_f: this fs wants infinity, OTHERS keep their actual demands
        # (a global all-infinite fill dilutes the ceiling by phantom
        # low-rate siblings; observed 2026-07-09 in M1b). Single-pass
        # cascade (waterfill_ceilings) instead of one full fill per fs:
        # O(N log N), property-tested equal to the N-fill reference.
        tree = {}
        for f in demand:
            src_dst, cls = f.rsplit("|", 1)
            src, dst = src_dst.split(">")
            tree.setdefault(dst, {}).setdefault(cls, {})[f] = (src, demand[f])
        vms = self.policy["vms"]
        upw = self.policy.get("per_sender_weights", {})
        vm_items, vm_repl = {}, {}
        for dst, classes in tree.items():
            dsum = sum(d for cls in classes.values() for _, d in cls.values())
            maxr = vms.get(dst, {}).get("max_rate_bps") or float("inf")
            vm_items[dst] = (vms.get(dst, {}).get("weight", 1),
                             min(maxr, dsum))
            vm_repl[dst] = maxr   # fs->inf leaves the VM MaxRate-capped
        vm_ceil = waterfill_ceilings(self.c_root, vm_items, vm_repl)
        ceil = {}
        for dst, classes in tree.items():
            cw = vms.get(dst, {}).get("class_weights", {})
            cls_items = {c: (cw.get(c, 1), sum(d for _, d in fs.values()))
                         for c, fs in classes.items()}
            cls_ceil = waterfill_ceilings(vm_ceil[dst], cls_items)
            for c, fs in classes.items():
                fs_items = {f: (upw.get("%s|%s|%s" % (dst, c, src), 1), d)
                            for f, (src, d) in fs.items()}
                ceil.update(waterfill_ceilings(cls_ceil[c], fs_items))
        return e, ceil

    def _fill(self, demand):
        # group: dst VM -> class -> [fsid]
        tree = {}
        for f in demand:
            src_dst, cls = f.rsplit("|", 1)
            src, dst = src_dst.split(">")
            tree.setdefault(dst, {}).setdefault(cls, {})[f] = (src, demand[f])

        vms = self.policy["vms"]
        upw = self.policy.get("per_sender_weights", {})
        # layer 1: root capacity across dst VMs
        vm_items = {}
        for dst, classes in tree.items():
            dsum = sum(d for cls in classes.values() for _, d in cls.values())
            maxr = vms.get(dst, {}).get("max_rate_bps") or float("inf")
            vm_items[dst] = (vms.get(dst, {}).get("weight", 1), min(maxr, dsum))
        vm_share = waterfill(self.c_root, vm_items)

        e = {}
        for dst, classes in tree.items():
            # layer 2: VM share across classes
            cw = vms.get(dst, {}).get("class_weights", {})
            cls_items = {c: (cw.get(c, 1), sum(d for _, d in fs.values()))
                         for c, fs in classes.items()}
            cls_share = waterfill(vm_share[dst], cls_items)
            # layer 3: class share across senders
            for c, fs in classes.items():
                fs_items = {f: (upw.get("%s|%s|%s" % (dst, c, src), 1), d)
                            for f, (src, d) in fs.items()}
                e.update(waterfill(cls_share[c], fs_items))
        return e


class VQMarker:
    """Per-flow-set virtual-queue integrator -> mark s_f (design.md §3.3).

    The anti-windup clip is V itself (§3.3): a deeper integration bin only
    makes the mark ring on for extra ticks after the excess has stopped,
    deepening the undershoot."""

    def __init__(self, v_full):
        self.v_full = v_full    # bits at which s saturates to 1
        self.v_max = v_full     # anti-windup clip = V (§3.3)
        self.vq = {}            # fsid -> bits

    def step(self, rates, ents, ceils, dt_s):
        marks = {}
        for f in set(self.vq) | set(rates):
            r = rates.get(f, 0.0)
            e = ents.get(f, 0.0)
            if r <= 0:
                # idle flow-set: drain fast (see module docstring)
                nv = self.vq.get(f, 0.0) - self.v_full
            elif r > e:
                nv = self.vq.get(f, 0.0) + (r - e) * dt_s   # real excess
            else:
                # drain by unused fair-share ceiling, not by demand-capped
                # e_f (see Scheduler.entitlements docstring)
                nv = self.vq.get(f, 0.0) - max(ceils.get(f, e) - r, 0.0) * dt_s
            nv = min(max(nv, 0.0), self.v_max)
            if nv <= 0 and r <= 0:
                self.vq.pop(f, None)   # fully drained and idle: forget
                continue
            self.vq[f] = nv
            marks[f] = min(nv / self.v_full, 1.0)
        return marks


# receiver-host rate exporter wire format (hpft_rate_exporter.py):
#   header '<HH' = (seq_lo16, n_records)
#   record '<16sQQ' = (vnic_id[16], rx_bytes, t_host_monotonic_ns)
RATE_HDR = struct.Struct("<HH")
RATE_REC = struct.Struct("<16sQQ")


class Telemetry:
    """One binary datagram per destination per tick. At a 1ms period, JSON
    encode/decode of a per-flow-set dict is a real cost; a fixed struct is
    an order of magnitude cheaper and smaller on the wire. Wire format:
      header  '<HH'  = (seq_lo16, n_records)
      record  '<64s Q Q'  = (fsid[64], u_bps, r_bps)
    fsid strings are short ('sgpu01/vf0>sgpu02/vf0|tcp' ~ 26 B); u/r use
    uint64 because bps at 200G line rate overflows uint32.

    THIS FORMAT IS A SYNCHRONIZED BOTH-ENDS CHANGE: tx_agent_e._TREC must
    match exactly, and both agents must be deployed and restarted together
    or the control loop parses garbage.

    Dual-cast (2026-07-11): each record goes both to the sender DPU (which
    runs the response law + mailbox) and, for the tcp class, to the sender
    host's pace shim, so the TCP law would run one hop closer to its
    actuator. Destinations resolved from control.telemetry_ip (DPU) and
    control.shim_ip/shim_telemetry_port (host) - the latter two are absent
    from the registry, so the shim leg is currently inert."""

    HDR = struct.Struct("<HH")
    REC = struct.Struct("<64sQQ")

    def __init__(self, vnic_host, telemetry_ip, port, shim_ip=None,
                 shim_port=None):
        self.vnic_host = vnic_host        # vnic_id -> host
        self.telemetry_ip = telemetry_ip  # host -> its DPU ctl IP
        self.port = port
        self.shim_ip = shim_ip or {}      # host -> host-side shim IP
        self.shim_port = shim_port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.seq = 0

    def _pack(self, recs):
        out = [self.HDR.pack(self.seq & 0xffff, len(recs))]
        for fsid, u, r in recs:
            out.append(self.REC.pack(fsid.encode()[:64], int(u), int(r)))
        return b"".join(out)

    def send(self, targets, rates):
        self.seq += 1
        per_dpu = {}     # dpu ip -> [records]
        per_shim = {}    # (shim ip, port) -> [tcp records]
        for f, u in targets.items():
            src, cls = f.split(">")[0], f.rsplit("|", 1)[1]
            host = self.vnic_host.get(src)
            rec = (f, u, rates.get(f, 0))
            ip = self.telemetry_ip.get(host)
            if ip is not None:
                per_dpu.setdefault(ip, []).append(rec)
            if cls == "tcp" and self.shim_port and host in self.shim_ip:
                per_shim.setdefault((self.shim_ip[host], self.shim_port),
                                    []).append(rec)
        for ip, recs in per_dpu.items():
            try:
                self.sock.sendto(self._pack(recs), (ip, self.port))
            except OSError:
                pass
        for (ip, port), recs in per_shim.items():
            try:
                self.sock.sendto(self._pack(recs), (ip, port))
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", default="/opt/hpft/lab-registry.json")
    ap.add_argument("--bridge", default="underlay-p1")
    ap.add_argument("--uplink", default="p1")
    ap.add_argument("--local-host", default=None)
    ap.add_argument("--log", default="/tmp/hpft_rxagent_e.jsonl")
    ap.add_argument("--period-ms", type=float, default=None)
    ap.add_argument("--duration", type=float, default=0)
    ap.add_argument("--meter-only", action="store_true",
                    help="M0 mode: no scheduler/marks/telemetry")
    ap.add_argument("--vport-meter", default="/dev/shm/hpft_vpm",
                    metavar="PATH",
                    help="vport_meter.c mmap file: direct per-VF ib/eth "
                         "class-split counters, preferred over the host "
                         "rate exporter when fresh (default on; the "
                         "attribution chain degrades cleanly when the "
                         "file is absent/stale, so no helper = old "
                         "behavior; pass '' to disable explicitly)")
    args = ap.parse_args()

    reg = json.load(open(args.registry))
    reg_mtime = os.stat(args.registry).st_mtime
    ep = reg["e_params"]
    period = (args.period_ms or ep["period_ms"]) / 1e3
    local_host = args.local_host or reg["receiver_host"]
    line = reg["line_rate_bps"]
    mac2vnic = {v["mac"]: v["vnic_id"] for v in reg["vnics"] if "mac" in v}
    local_macs = {v["mac"] for v in reg["vnics"]
                  if v.get("mac") and v["host"] == local_host}
    vnic_host = {v["vnic_id"]: v["host"] for v in reg["vnics"]}

    # V is a FIXED bit quantity (the excess-integral tolerated before the
    # mark saturates), so it must NOT scale with the control period: the
    # VQ accumulates the time integral of (r - e), which over a given
    # wall-clock window is the same no matter how finely it is ticked.
    # Scaling V with period (an early formula) made the VQ 50x more
    # sensitive at 1ms - a few measurement spikes above the cap saturated
    # it and fired spurious marks, pinning the flow ~20% below its cap.
    # Stated directly as wall-clock x line rate x headroom (design.md §6):
    # v_seconds = 0.1 -> V = 600 Mbit, i.e. ~100 ms of headroom-scale
    # sustained excess pushes the mark to saturation.
    v_full = ep["v_seconds"] * line * ep["headroom"]
    gamma = ep["gamma"]

    added = install_class_rules(args.bridge)
    print("rx_agent: bridge=%s rules_added=%s T=%.0fms local=%s "
          "V=%.0fMbit gamma=%.2f log=%s meter_only=%s vport_meter=%s"
          % (args.bridge, added or "none", period * 1e3, local_host,
             v_full / 1e6, gamma, args.log, args.meter_only,
             args.vport_meter or "off"), flush=True)

    vpm = None
    if args.vport_meter:
        # esw vport number for pf<P>vf<N> is N+1 (host PF is vport 0)
        vport_of_vnic = {}
        for v in reg["vnics"]:
            if v["host"] == local_host and v.get("representor"):
                m = re.search(r"vf(\d+)$", v["representor"])
                if m:
                    vport_of_vnic[v["vnic_id"]] = int(m.group(1)) + 1
        vpm = VportMeter(args.vport_meter, vport_of_vnic)

    uc = Unixctl()
    meter = FlowSetMeter(mac2vnic, local_macs)
    hybrid = HybridRates(
        {v["vnic_id"]: v["representor"] for v in reg["vnics"]
         if v["host"] == local_host},
        ep.get("mix_window_s", 2.0),
        rate_window_s=ep.get("rate_window_s", 0.006),
        meter=vpm)
    sched = Scheduler(reg["policy"], line, ep["headroom"], ep["delta_demand"])
    last_seen = {}
    marker = VQMarker(v_full)
    ctl = reg["control"]
    telem = Telemetry(vnic_host, ctl["telemetry_ip"], ep["telemetry_port"],
                      shim_ip=ctl.get("shim_ip"),
                      shim_port=ctl.get("shim_telemetry_port"))
    hsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    hsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    hsock.bind(("0.0.0.0", ctl.get("rate_export_port", 9712)))
    hsock.setblocking(False)
    logf = open(args.log, "a", buffering=1)

    # The slow work (megaflow dump for class-mix, policy reload, downlink
    # speed) runs INLINE every slow_every ticks, not in a thread. A thread
    # does NOT help: the dump's cost is CPU-bound megaflow parsing that
    # holds the GIL, so a "background" thread still starves the fast loop
    # AND jitters its vport sampling (measured: r_f went from a clean 6G
    # to +-60% noise). Inline every ~200ms it costs ~1ms once per 200
    # ticks (0.5% duty) and leaves the fast-loop cadence regular in
    # between. The mix source is 1s-stale anyway.
    log_every = max(1, int(ep.get("log_every_ms", 20) / (period * 1e3)))
    slow_every = max(1, round(ep.get("slow_loop_ms", 200) / (period * 1e3)))
    dump_ms = 0.0

    t_start = time.monotonic()
    next_tick = t_start
    last_print = t_start
    nticks_total = 0
    last_speed = line

    while True:
        now = time.monotonic()
        if args.duration and now - t_start >= args.duration:
            break

        # ---- sample vport counters FIRST, before any slow work, so the
        # sampling cadence is regular (set by the sleep schedule) ----
        hybrid.sample(now, period)

        # drain receiver-host kernel byte reports (class-attribution truth)
        while True:
            try:
                data, _ = hsock.recvfrom(2048)
            except (BlockingIOError, OSError):
                break
            try:
                _, n = RATE_HDR.unpack_from(data, 0)
                off = RATE_HDR.size
                for _ in range(n):
                    vb, bts, tns = RATE_REC.unpack_from(data, off)
                    off += RATE_REC.size
                    hybrid.host_update(vb.rstrip(b"\0").decode(),
                                       tns, bts, now)
            except (struct.error, UnicodeDecodeError):
                pass

        # ---- slow work, inline, every slow_every ticks ----
        if nticks_total % slow_every == 0:
            t0 = time.monotonic()
            try:
                txt = uc.call("dpctl/dump-flows", ["type=offloaded"])
            except (OSError, RuntimeError):
                try:
                    txt = subprocess.run(
                        ["ovs-appctl", "dpctl/dump-flows", "type=offloaded"],
                        capture_output=True, text=True, check=True).stdout
                except Exception:  # noqa: BLE001
                    txt = ""
            if txt:
                fs_deltas, fs_present = meter.tick(txt)
                hybrid.update_mix(fs_deltas, fs_present, time.monotonic())
            dump_ms = (time.monotonic() - t0) * 1e3
            try:
                mt = os.stat(args.registry).st_mtime
                if mt != reg_mtime:
                    reg_mtime = mt
                    sched.policy = json.load(open(args.registry))["policy"]
                    print("rx_agent: policy reloaded", flush=True)
            except (OSError, ValueError) as e:
                print("rx_agent: policy reload failed: %s" % e, flush=True)
            try:
                spd = int(open("/sys/class/net/%s/speed" % args.uplink).read())
                if spd > 0 and spd * 1e6 != last_speed:
                    last_speed = spd * 1e6
                    sched.set_downlink(spd * 1e6)
                    print("rx_agent: downlink %d Mbps -> C_root %.1fG"
                          % (spd, sched.c_root / 1e9), flush=True)
            except (OSError, ValueError):
                pass

        # ---- fast loop: rates -> waterfill -> VQ -> telemetry ----
        rates = hybrid.rates(now)
        dt = period
        sched_rates = {f: r for f, r in rates.items()
                       if f.rsplit("|", 1)[1] in SCHED_CLASSES}
        # keep every PRESENT scheduled flow-set in the report even when its
        # rate momentarily reads 0, so the sender gets a continuous s=0
        # "fresh permit" (design §3.4) and never flaps into fail-open. The
        # present set is the megaflow keys (by_dst), which persist across a
        # brief zero-rate tick; without this a 1ms loop dropped cap-limited
        # flows on RP burst gaps and death-spiralled into fail-open.
        for members in hybrid.by_dst.values():
            for f in members:
                if (f.rsplit("|", 1)[1] in SCHED_CLASSES
                        and f not in sched_rates):
                    sched_rates[f] = 0.0
        if args.meter_only:
            ents, ceils, marks, targets = {}, {}, {}, {}
        else:
            nowm = now
            for f in sched_rates:
                last_seen[f] = nowm
            active = dict(sched_rates)
            # flow-set membership grace: a flow-set whose megaflow key
            # momentarily disappears stays in the fill (at r=0) for this
            # long, so a one-tick gap does not reshuffle everyone's share.
            # (Named share_floor_grace_s until 2026-07-27 - it never had
            # anything to do with the deleted share_floor.)
            grace = ep.get("fs_grace_s", 0.5)
            for f, t_ in list(last_seen.items()):
                if f not in active:
                    if nowm - t_ <= grace:
                        active[f] = 0.0
                    else:
                        del last_seen[f]
            if ep.get("backlog_floor", False):
                # backlog-aware share floor (2026-07-13): the r*(1+delta)
                # demand estimate caps a class at sum(per-flow demands), so a
                # BACKLOGGED class with fewer flows (or a lower realize rate)
                # is capped below its weighted share and the sibling class
                # borrows the surplus -> class ratio drifts off the policy
                # weight (28fs 3v4: RDMA/TCP 0.75). Fix: a flow realizing
                # >= theta of its weighted share (shat, the all-backlogged
                # fill) is backlogged -> demand=inf so it claims its full
                # weighted class share; a genuinely idle flow (r << shat)
                # keeps r*(1+delta) so work-conserving borrowing survives.
                # This is the naive "floor every flow at its weighted share"
                # (which kills borrowing, design.md §9 tradeoff 3) made
                # backlog-selective; theta = theta_b of design.md §3.2.2.
                shat = sched._fill({f: float("inf") for f in active})
                theta = ep.get("backlog_theta", 0.5)
                # a finite "big" (root capacity), NOT inf: it saturates the
                # class demand cap exactly like inf but survives int(ceil_f)
                # in the telemetry pack (inf overflows).
                big = sched.c_root
                demand = {f: (big
                              if shat.get(f, 0.0) > 0 and r >= theta * shat[f]
                              else r * (1.0 + ep["delta_demand"]))
                          for f, r in active.items()}
            else:
                demand = {f: r * (1.0 + ep["delta_demand"])
                          for f, r in active.items()}
            ents, ceils = sched.entitlements(active, demand)
            marks = marker.step(sched_rates, ents, ceils, dt)
            # §3.4 target synthesis: the policy ceiling discounted by the
            # audited sustained excess. Bounded by construction - no
            # excess leaves the target AT the ceiling, saturated excess
            # can press it no lower than (1-gamma)*ceil - so the sender's
            # law needs no floor of its own.
            targets = {f: ceils.get(f, ents.get(f, 0.0)) * (1.0 - gamma * s)
                       for f, s in marks.items()}
            telem.send(targets, sched_rates)

        # throttled logging (default ~50 Hz), never every 1ms tick
        if nticks_total % log_every == 0 and nticks_total:
            rec = {"ts": round(time.time(), 4), "dt_s": round(dt, 6),
                   "read_ms": round(dump_ms, 3), "nfs": len(sched_rates),
                   "ha": len(hybrid.attr_host),
                   "ma": len(hybrid.attr_meter),
                   # raw meter class rates per dst VF [rdma, kern]: the
                   # direct-read numbers before any megaflow sub-split,
                   # for offline old-vs-new attribution reconciliation
                   "rv": {d: [int(x) for x in (vpm.rates(d) or (0, 0))]
                          for d in hybrid.attr_meter},
                   "r": {f: int(v) for f, v in rates.items()},
                   # u is the only one of these on the wire; e/c/s/vq are
                   # the local ledger the target is synthesised from, kept
                   # here for offline accounting against design_theory §2.8
                   "u": {f: int(v) for f, v in targets.items()},
                   "e": {f: int(v) for f, v in ents.items()},
                   "c": {f: int(v) for f, v in ceils.items()},
                   "s": {f: round(v, 4) for f, v in marks.items()},
                   "vq": {f: int(v) for f, v in marker.vq.items()}}
            logf.write(json.dumps(rec) + "\n")

        nticks_total += 1
        if now - last_print >= 1.0:
            act = " ".join("%s r=%.2fG u=%.2fG s=%.2f"
                           % (f, rates.get(f, 0) / 1e9,
                              targets.get(f, 0) / 1e9, marks.get(f, 0))
                           for f in sorted(marks) if rates.get(f, 0) > 0)
            print("t=%.0fs ticks/s=%d dump_ms=%.1f | %s"
                  % (now - t_start, nticks_total // max(int(now - t_start), 1),
                     dump_ms, act or "idle"), flush=True)
            last_print = now

        next_tick += period
        sleep = next_tick - time.monotonic()
        if sleep > 0:
            time.sleep(sleep)
        else:
            next_tick = time.monotonic()   # overran: don't try to catch up


if __name__ == "__main__":
    main()
