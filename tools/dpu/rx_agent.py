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
not on the wire - the sender consumes one number.

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

import fastfill
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


class SenderLiveness:
    """Receiver side of the sender-liveness feed (tx_agent_e publishes it).

    The pool a dst receives is measured exactly and freshly; only its
    DIVISION among senders is estimated, and that estimate rests on
    megaflow byte counters the hardware caches for ~1 s. A sender that
    stops therefore keeps a share of the pool for up to the mix window,
    which dilutes the survivors' measured rate and makes them crawl into
    the freed share. No receiver-side counter can do better - but the
    sender's own vport TX counters are fresh at ~1 ms, so the sender
    tells us. Used ONLY as a gate ("is this sender sending at all"), never
    as the ratio: the ratio stays a receiver-side measurement.

    Advisory by construction: an absent or stale feed leaves attribution
    exactly as it was (baseline arms run no sender agent at all).
    """

    STALE_S = 0.3
    IDLE_BPS = 50e6      # below the pace floor => not sending

    def __init__(self, port):
        self.rate = {}                 # "host/vnic|class" -> bps
        self.t = 0.0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind(("0.0.0.0", port))
        except OSError:
            self.sock = None
            return
        self.sock.setblocking(False)

    def poll(self, now):
        if self.sock is None:
            return
        while True:
            try:
                data, _ = self.sock.recvfrom(65536)
            except (BlockingIOError, OSError):
                break
            try:
                msg = json.loads(data.decode())
                # keys arrive as "<vnic_id>|<class>" and vnic_id already
                # carries the host ("sgpu01/vf0"), which is exactly the
                # form the flow-set id uses -- store verbatim.
                for k, v in msg["r"].items():
                    self.rate[k] = float(v)
                self.t = now
            except (ValueError, KeyError):
                continue

    def idle(self, fsid, now):
        """True only when the sender positively reports ~zero for this
        (src, class). Unknown or stale => False (never gate on silence)."""
        if self.sock is None or now - self.t > self.STALE_S:
            return False
        src, cls = fsid.split(">")[0], fsid.rsplit("|", 1)[1]
        v = self.rate.get("%s|%s" % (src, cls))
        return v is not None and v < self.IDLE_BPS


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
        self.first_seen = {}              # fsid -> when it entered the mix
        self.unmeasured = set()           # fsids with no bytes in the window
        self.prev_ents = {}               # last tick's entitlements (prior)
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
        self.young_active = set()         # partially-visible newcomers
        self.liveness = None              # SenderLiveness (advisory)

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
            self.first_seen.setdefault(f, now)
        for f in [f for f in self.first_seen if f not in fs_present
                  and f not in win]:
            del self.first_seen[f]
        shares = {}
        unmeasured = set()
        # Partially-visible newcomers: younger than the window AND already
        # showing bytes. Their ratio understates them (the dump is ~1 s
        # cached), so the split must not trust it. A newcomer with NO bytes
        # is the `unmeasured` case below; a member with no bytes that is
        # LEAVING must not be counted here at all, or its stale prior keeps
        # holding the survivors down (D3 leave direction, 2026-07-30).
        self.young_active = {f for f in win
                             if win.get(f, 0) > 0
                             and now - self.first_seen.get(f, 0) < self.window}
        for dst, members in by_dst.items():
            # "no bytes in the window" means two completely different
            # things, and conflating them is what let one sender be
            # credited with another's traffic. A flow-set that has been in
            # the mix for longer than the window and still shows nothing is
            # genuinely idle and its share is 0. One that appeared less
            # than a window ago is merely UNMEASURED: the megaflow dump is
            # hardware-cached ~1 s and this window is 2 s, so a sender that
            # just started legitimately has no history yet.
            #
            # Giving an unmeasured member 0 is not a small error, it is the
            # worst possible one: its whole rate lands on its peers, the
            # peer then reads far above the budget it was given, and the
            # RP's water level digs that peer to zero. Measured 2026-07-28
            # with two senders on one dst: the incumbent was reported at
            # 58.3G against a true 29G while the newcomer read 0.00G.
            #
            # Until its own bytes arrive, an unmeasured member is assumed
            # to look like the average of its measured peers - the least
            # committal guess that keeps the split conservative, and the
            # exactly right one for the common case of a peer joining at a
            # similar rate. It expires by itself after one window.
            eff = {}
            for f in members:
                b = win.get(f, 0)
                eff[f] = b
                # UNMEASURED means "we have not had a chance to see it
                # yet", which needs both halves: no bytes in the window
                # AND young enough that the hardware-cached dump could
                # not have reported it. A flow-set that has been here
                # longer than the window and shows nothing has genuinely
                # stopped, and treating it as unmeasured is not
                # harmless - it keeps a phantom member in the split, so
                # a survivor's own traffic gets divided with a flow that
                # left. Measured 2026-07-28: after its peer stopped, the
                # remaining sender read 14.4G against a true 28.8G and
                # was then granted only that half.
                if b <= 0 and now - self.first_seen.get(f, now) < self.window:
                    unmeasured.add(f)
            wtot = sum(eff.values())
            for f in members:
                shares[f] = (eff[f] / wtot) if wtot > 0 \
                    else 1.0 / len(members)
        self.mix_shares = shares
        self.by_dst = by_dst
        self.unmeasured = unmeasured

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
                self._split(list(members), total, rates)
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
                self._split(fs, pool, rates, now)
        return rates

    def _split(self, fs, pool, out, now=None):
        """Divide one exactly-measured pool among its flow-sets.

        Which ruler splits this pool. The megaflow byte ratio is
                # a MEASUREMENT and is preferred - but only when every
                # member has actually been measured. A member with no bytes
                # in the window is not evidence of silence: the dump is
                # hardware-cached and a sender that just started can go
                # seconds without appearing, during which the ratio hands
                # its entire rate to its peers. Measured 2026-07-28: two
                # RDMA senders on one dst reported 58.3G / 0.00G against a
                # true 29 / 29, and the peer's executor then dug its own
                # rate to zero for being "over budget".
                #
                # When any member is unmeasured, split by the previous
                # tick's ENTITLEMENTS instead. That prior is exactly the
                # right one and it handles both cases with no extra rule:
                # a sender we granted a share is assumed to be using it,
                # while a genuinely idle flow-set has a demand-capped
                # entitlement near zero and so still gets ~nothing. The
        sum being split is the exact vport total either way - only its
        division is in question.
        """
        if not fs:
            return
        w = {f: self.mix_shares.get(f, 0.0) for f in fs}
        # Attribution-lag guard (2026-07-30, eval D3/vtune finding): a member
        # YOUNGER than the mix window is unreliable even when it has bytes -
        # the megaflow cache is ~1 s stale, so a newcomer shows a sliver of
        # its true rate and the ratio hands the remainder to its peers, whose
        # ledgers then charge a wrongful mark for ~2 s (u pinned at 0.75x
        # ceiling). Young member present => split the pool by the previous
        # tick's entitlements, same prior as the unmeasured case.
        # A partially-visible newcomer (young AND already sending) makes the
        # byte ratio untrustworthy: it understates the newcomer and hands its
        # rate to the incumbents, whose ledgers then charge a wrongful mark
        # for ~2 s. Fall back to the same prior the unmeasured case uses.
        # Departing members are deliberately NOT counted here - they carry no
        # bytes, and treating them as young keeps survivors from rising into
        # the freed share (D3 leave direction).
        # Gate on the sender's own fresh view first: a member the sender
        # positively reports as not sending contributes nothing to this
        # pool, so its stale megaflow bytes must not claim a share of it.
        # This is what lets survivors take the freed capacity immediately
        # instead of waiting out the mix window.
        live = self.liveness
        if live is not None and now is not None:
            act = [f for f in fs if not live.idle(f, now)]
            if act and len(act) < len(fs):
                fs = act
        young = bool(self.young_active & set(fs))
        if young or any(f in self.unmeasured for f in fs):
            # ABSENT from the prior and PRESENT-BUT-NEAR-ZERO are different
            # again, and defaulting the absent case to 0 reproduces the very
            # bug this prior exists to fix: a flow-set created this tick has
            # no previous entitlement, would weigh 0, and its peer would once
            # more be credited with the whole pool. An unknown newcomer is
            # assumed to look like the average of the flow-sets we do know;
            # one we know to be idle keeps its own near-zero weight.
            known = [self.prev_ents[f] for f in fs if f in self.prev_ents]
            fill = (sum(known) / len(known)) if known else 1.0
            w = {f: self.prev_ents.get(f, fill) for f in fs}
            if sum(w.values()) <= 0:
                w = {f: 1.0 for f in fs}
        wsum = sum(w.values())
        for f in fs:
            share = (w[f] / wsum) if wsum > 0 else 1.0 / len(fs)
            if share > 0:
                out[f] = pool * share


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
        """See _entitlements_py for the reference implementation.

        The C path computes the same two outputs in one crossing of the
        ctypes boundary; it is property-tested to agree with the Python
        path pointwise (fastfill_test.py). Falls back automatically when
        the shared object is not built, so a machine without it still
        runs - just with the old ceiling on flow-set count."""
        if fastfill.USING_C:
            demand = demand_in if demand_in is not None else \
                {f: r * (1.0 + self.delta) for f, r in rates.items()}
            if demand:
                return self._entitlements_c(demand)
        return self._entitlements_py(rates, demand_in)

    def _entitlements_c(self, demand):
        vms = self.policy["vms"]
        upw = self.policy.get("per_sender_weights", {})
        fsids = list(demand)
        vm_ids, cls_ids = {}, {"rdma": 0, "tcp": 1}
        dst_idx, cls_idx, wfs, dem = [], [], [], []
        for f in fsids:
            src_dst, c = f.rsplit("|", 1)
            src, d = src_dst.split(">")
            if d not in vm_ids:
                vm_ids[d] = len(vm_ids)
            dst_idx.append(vm_ids[d])
            cls_idx.append(cls_ids.setdefault(c, len(cls_ids)))
            wfs.append(float(upw.get("%s|%s|%s" % (d, c, src), 1)))
            dem.append(float(demand[f]))
        n_vm, n_cls = len(vm_ids), len(cls_ids)
        vm_w = [1.0] * n_vm
        vm_max = [float("inf")] * n_vm
        cls_w = [1.0] * (n_vm * n_cls)
        for d, i in vm_ids.items():
            p = vms.get(d, {})
            vm_w[i] = float(p.get("weight", 1))
            m = p.get("max_rate_bps")
            vm_max[i] = float(m) if m else float("inf")
            cwd = p.get("class_weights", {})
            for c, j in cls_ids.items():
                cls_w[i * n_cls + j] = float(cwd.get(c, 1))
        return fastfill.entitlements_c(fsids, dst_idx, cls_idx, wfs, dem,
                                       n_vm, n_cls, vm_w, vm_max, cls_w,
                                       self.c_root)

    def _entitlements_py(self, rates, demand_in=None):
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


class RootCongestion:
    """Hysteretic saturation state of ONE node of the policy tree.

    Named for the root because that is where it started, but the test is
    not special to the root: any node whose capacity is fully claimed is
    a place where flow-sets contend, and a flow-set below a saturated
    node is contending whether or not it happens to be realising much
    right now. One instance per node - see NodeSaturation.

    Entering closes borrowing, which lowers utilisation, which would
    immediately look like "not congested" again - so the exit threshold
    has to sit below the entry one or the two regimes chatter every tick.
    """

    def __init__(self, enter=0.95, leave=0.85):
        self.enter = enter
        self.leave = leave
        self.on = False

    def update(self, total, capacity):
        if capacity > 0:
            if total >= self.enter * capacity:
                self.on = True
            elif total <= self.leave * capacity:
                self.on = False
        return self.on


class NodeSaturation:
    """Saturation of every dst-VM node, the layer between root and class.

    Why this exists, and why it is a better criterion than the share test
    it takes over from. Backlog discrimination has to answer "is this
    flow-set held back by US, or does it simply have nothing to send",
    and the share test answers it by thresholding the flow-set's own
    measured rate against its share. That quantity is the one thing the
    receiver CANNOT measure per sender: a dst VF's total is exact from
    its vport counter, but splitting that total among several senders is
    an estimate from a megaflow byte ratio that lags seconds behind a
    sender joining. Threshold an estimate and you get the estimate's
    errors amplified into policy - measured 2026-07-28, two RDMA senders
    on one dst, the split reported 58.3G / 0.00G against a true 29 / 29.

    The saturation test needs no split. The SUM of the estimates is the
    exact vport total no matter how badly the split is wrong, because the
    split is a partition of it. So compare that sum against the node's
    own capacity: if a dst VM is at its MaxRate, every sender aimed at it
    is contending, full stop, and each of their ceilings must collapse to
    the joint fair share instead of each being computed as though it were
    alone. Without this the ceilings are individually right and jointly
    impossible - each flow-set's e-hat is defined with its OWN demand set
    to infinity, so two senders both acting on theirs sum to twice the
    cap, and the virtual queue cannot claw that back: its discount is
    bounded by gamma = 0.25 by construction.

    Root congestion was always this same test on one particular node. It
    stays as it was; this generalises it downward.
    """

    def __init__(self, enter=0.95, leave=0.85):
        self.enter, self.leave = enter, leave
        self.nodes = {}

    def update(self, active, caps):
        """active: {fsid: rate}; caps: {dst_vnic: max_rate_bps}.

        Returns the set of saturated dst vnics.
        """
        totals = {}
        for f, r in active.items():
            pair = f.rsplit("|", 1)[0]
            if ">" not in pair:
                continue
            dst = pair.split(">")[1]
            totals[dst] = totals.get(dst, 0.0) + r
        for dst in list(self.nodes):
            if dst not in totals:
                del self.nodes[dst]
        out = set()
        for dst, tot in totals.items():
            cap = caps.get(dst)
            if not cap:
                continue
            n = self.nodes.get(dst)
            if n is None:
                n = self.nodes[dst] = RootCongestion(self.enter, self.leave)
            if n.update(tot, cap):
                out.add(dst)
        return out


def demand_estimate(active, delta, big, congested, saturated_dsts=()):
    """Per-flow-set demand for the fill (design.md §3.2.1-§3.2.2).

    An idle flow-set (r = 0) is never backlogged. A non-idle one is
    backlogged through either entry:
      share   it is realising at least theta_b of its pure weighted share,
              so its own rate already demonstrates the appetite;
      root    the root is congested, in which case a flow-set below its
              share cannot be short of appetite - there is no spare
              capacity it could have declined, so it is losing a race.
    Backlogged means demand = big (a finite stand-in for infinity, chosen
    so the derived ceiling still packs into a uint64 on the wire), which
    claims the full weighted share; otherwise the r*(1+delta) ratchet.

    Kept module-level so both entries and the hysteresis can be tested
    without a DPU - they had only lab A/B coverage, and a regression in
    either returns the allocator to the starvation it was built to stop.
    """
    out = {}
    for f, r in active.items():
        pair = f.rsplit("|", 1)[0]
        backlogged = r > 0 and (
            congested or
            (saturated_dsts and ">" in pair
             and pair.split(">")[1] in saturated_dsts))
        out[f] = big if backlogged else r * (1.0 + delta)
    return out


class VQMarker:
    """Per-flow-set virtual-queue integrator -> mark s_f (design.md §3.3).

    The anti-windup clip is V itself (§3.3): a deeper integration bin only
    makes the mark ring on for extra ticks after the excess has stopped,
    deepening the undershoot."""

    def __init__(self, v_full, v_mode="global", v_seconds_per_share=None,
                 v_floor=None):
        self.vq = {}            # fsid -> bits
        # ARM SWITCH (experiment, 2026-08-24). "global": V is one constant
        # for every flow-set, as in design.md today. "per_fs": V_f = c*ehat_f,
        # which makes the mark scale-invariant - s is then "how many seconds
        # of full-share excess", identical in meaning for a 1G share and a
        # 100G one, and both zeta and omega_n stop depending on ehat. The
        # arms exist to be measured against each other, not to be chosen from
        # in production.
        self.v_mode = v_mode
        self.v_c = v_seconds_per_share      # seconds, used when per_fs
        self.v_floor = v_floor              # bits, guards ehat -> 0
        self.set_v(v_full)

    def set_v(self, v_full):
        """V follows the downlink bottleneck capacity (§3.3): zeta =
        0.5*sqrt(k*V/(gamma*ehat)) must not depend on link rate, and ehat
        scales with C, so V has to scale with C too. Recomputed whenever
        the scheduler's root capacity moves."""
        self.v_full = v_full    # bits at which s saturates to 1
        self.v_max = v_full     # anti-windup clip = V (§3.3)

    def _v_for(self, f, ceils, ents):
        """The full-scale this flow-set's ledger is measured against."""
        if self.v_mode != "per_fs":
            return self.v_full
        ehat = ceils.get(f) or ents.get(f) or 0.0
        return max(self.v_c * ehat, self.v_floor)

    def step(self, rates, ents, ceils, dt_s):
        marks = {}
        for f in set(self.vq) | set(rates):
            r = rates.get(f, 0.0)
            e = ents.get(f, 0.0)
            vf = self._v_for(f, ceils, ents)
            if r <= 0:
                # idle flow-set: drain fast (see module docstring)
                nv = self.vq.get(f, 0.0) - vf
            elif r > e:
                nv = self.vq.get(f, 0.0) + (r - e) * dt_s   # real excess
            else:
                # drain by unused fair-share ceiling, not by demand-capped
                # e_f (see Scheduler.entitlements docstring)
                nv = self.vq.get(f, 0.0) - max(ceils.get(f, e) - r, 0.0) * dt_s
            nv = min(max(nv, 0.0), vf)
            if nv <= 0 and r <= 0:
                self.vq.pop(f, None)   # fully drained and idle: forget
                continue
            self.vq[f] = nv
            marks[f] = min(nv / vf, 1.0)
        return marks


class VirtualQueueScheduler:
    """The virtual hierarchical fair scheduler (redesign proposal §1).

    Each flow-set owns a virtual queue Q_f (bits). Per period: this
    period's arrival is enqueued, the policy tree hands out C'*T of
    service to the queues that have something in them (weighted
    max-min, caps respected, unused service passed to siblings - exactly
    what an HTB-style scheduler would do), and every queue loses what it
    was served. The demand fed to the water-filling is therefore the
    BACKLOG, Q_f/T + r_f, not an estimate of appetite: a flow-set with a
    backlog is contending, one without is served what it sends. There is
    no delta ratchet, no saturation test, no hysteresis.

    Outputs per flow-set: e_f (service rate this period), g_f (the
    scheduler's share hint - what f would be served if its queue were
    unbounded, siblings as they are; it never depends on f's own rate)
    and d_f = Q_f / g_f, the virtual queueing delay in seconds - the
    unfairness signal, scale-free by construction. Q_f is clipped at
    d_clip * g_f: the queue is a bounded memory of recent excess, not a
    debt.
    """

    def __init__(self, sched, d_clip_s):
        self.sched = sched
        self.q = {}                 # fsid -> bits
        self.d_clip = d_clip_s

    def step(self, active, rates, dt):
        demand = {f: self.q.get(f, 0.0) / dt + rates.get(f, 0.0)
                  for f in active}
        ents, ceils = self.sched.entitlements(active, demand)
        delay = {}
        for f in active:
            r = rates.get(f, 0.0)
            e = ents.get(f, 0.0)
            g = ceils.get(f, e)
            q = self.q.get(f, 0.0) + (r - e) * dt
            if g > 0:
                q = min(max(q, 0.0), self.d_clip * g)
            else:
                q = 0.0
            if q <= 0:
                self.q.pop(f, None)
                delay[f] = 0.0
            else:
                self.q[f] = q
                delay[f] = q / g
        for f in [f for f in self.q if f not in active]:
            del self.q[f]
        return ents, ceils, delay


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
    # (fsid, g_bps, r_bps, d_us): g is the share hint / target, r the
    # measured arrival, d the virtual queueing delay in microseconds
    # (0 under the tracking law, where the audit is folded into g=u).
    REC = struct.Struct("<64sQQQ")

    def __init__(self, vnic_host, telemetry_ip, port, shim_ip=None,
                 shim_port=None):
        self.vnic_host = vnic_host        # vnic_id -> host
        self.telemetry_ip = telemetry_ip  # host -> its DPU ctl IP
        self.port = port
        self.shim_ip = shim_ip or {}      # host -> host-side shim IP
        self.shim_port = shim_port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.seq = 0
        # fsid -> its 64-byte wire field. The encode+pad is identical on
        # every tick for the life of a flow-set, and at a 1 ms period it
        # was being redone for every record of every datagram.
        self._fsb = {}

    def _pack(self, recs):
        out = [self.HDR.pack(self.seq & 0xffff, len(recs))]
        fsb = self._fsb
        pack = self.REC.pack
        for fsid, u, r, d_us in recs:
            b = fsb.get(fsid)
            if b is None:
                b = fsb[fsid] = fsid.encode()[:64]
            out.append(pack(b, int(u), int(r), int(d_us)))
        return b"".join(out)

    def send(self, targets, rates, delays=None):
        self.seq += 1
        per_dpu = {}     # dpu ip -> [records]
        per_shim = {}    # (shim ip, port) -> [tcp records]
        delays = delays or {}
        for f, u in targets.items():
            src, cls = f.split(">")[0], f.rsplit("|", 1)[1]
            host = self.vnic_host.get(src)
            rec = (f, u, rates.get(f, 0), delays.get(f, 0.0) * 1e6)
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
    ap.add_argument("--bridge", default="ovsbr-p1")
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
    #
    # V = v_seconds * headroom * C, anchored on the DOWNLINK BOTTLENECK
    # CAPACITY C, not on the NIC port line rate (design.md §3.3): the
    # audit loop's damping zeta = 0.5*sqrt(k*V/(gamma*ehat)) has ehat
    # scaling with C, so V must scale with C for zeta to be independent
    # of link rate. v_seconds = 0.2 s, C = 100G here -> V = 600 Mbit.
    # C is read from the uplink at startup and tracked on every change.
    gamma = ep["gamma"]

    def v_of(capacity):
        return ep["v_seconds"] * ep["headroom"] * capacity

    def uplink_speed():
        # The capacity to schedule against is the one that can actually be
        # delivered, which is the port's own speed only while nothing
        # downstream is narrower. Where something is - a shaped switch
        # egress, a slower next hop - the port nameplate overstates it, and
        # an allocator working from the overstatement hands out shares the
        # path cannot carry: the excess turns into queueing that the
        # tenants' own CCs then have to absorb, unequally, which is the
        # contention HPFT exists to take off them. capacity_bps in the
        # e_params config states the deliverable figure when it is known.
        cap = ep.get("capacity_bps")
        if cap:
            return float(cap)
        try:
            spd = int(open("/sys/class/net/%s/speed" % args.uplink).read())
            return spd * 1e6 if spd > 0 else None
        except (OSError, ValueError):
            return None

    c_link = uplink_speed() or line
    v_full = v_of(c_link)

    added = install_class_rules(args.bridge)
    print("rx_agent: bridge=%s rules_added=%s T=%.0fms local=%s C=%.0fG "
          "V=%.0fMbit gamma=%.2f fill=%s log=%s meter_only=%s vport_meter=%s"
          % (args.bridge, added or "none", period * 1e3, local_host,
             c_link / 1e9, v_full / 1e6, gamma,
             "C" if fastfill.USING_C else "python", args.log,
             args.meter_only, args.vport_meter or "off"), flush=True)

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
    liveness = SenderLiveness(int(ep.get("liveness_port", 9713)))

    uc = Unixctl()
    meter = FlowSetMeter(mac2vnic, local_macs)
    hybrid = HybridRates(
        {v["vnic_id"]: v["representor"] for v in reg["vnics"]
         if v["host"] == local_host},
        ep.get("mix_window_s", 2.0),
        rate_window_s=ep.get("rate_window_s", 0.006),
        meter=vpm)
    hybrid.liveness = liveness
    sched = Scheduler(reg["policy"], line, ep["headroom"], ep["delta_demand"])
    last_seen = {}
    # V arm (experiment): "global" is the shipping behaviour. "per_fs" sets
    # V_f = v_seconds_per_share * ehat_f; the default c = 4*zeta^2*gamma/k at
    # zeta = 1/sqrt(2) is exactly the seconds the shipping V corresponds to at
    # its own design point, so the two arms differ in how V VARIES, not in its
    # value at that point.
    v_mode = ep.get("v_mode", "global")
    v_c = ep.get("v_seconds_per_share") or (4 * 0.5 * gamma / ep["k"])
    marker = VQMarker(v_full, v_mode=v_mode, v_seconds_per_share=v_c,
                      v_floor=ep.get("v_floor_bits", 1e6))
    print("rx_agent: V arm=%s%s" % (
        v_mode, ("  c=%.1f ms floor=%.0f Mbit" % (v_c * 1e3, ep.get("v_floor_bits", 1e6) / 1e6))
        if v_mode == "per_fs" else "  V=%.0f Mbit" % (v_full / 1e6)), flush=True)
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

    # Per-tick cadence histogram. The scaling numbers say how much work a
    # tick costs on average; they say nothing about the TAIL, and the tail
    # is what corrupts measurement: the rate estimate is a difference over
    # a timestamp, so a stalled loop does not merely delay a decision, it
    # decouples bytes from time (2026-07-11: a clean 6G read as +-60%
    # noise for exactly this reason). A GC pause would show here as a
    # sparse population of multi-millisecond ticks that average away.
    tick_us = []
    tick_slow_us = []
    tick_over = 0
    tick_report = t_start = time.monotonic()
    next_tick = t_start
    last_print = t_start
    nticks_total = 0
    rootcong = RootCongestion()
    nodesat = NodeSaturation()
    # LAW ARM. "track": design.md v2 (demand estimate + audit discount,
    # target u = ceil*(1-gamma*s)). "vq": the redesign - backlog-fed
    # virtual scheduler, wire carries {g, r, d}.
    law = ep.get("law", "track")
    vqs = VirtualQueueScheduler(sched, ep.get("d_clip_s", 0.2))
    delays = {}
    print("rx_agent: law=%s%s" % (law, "  d_clip=%.0fms" % (
        ep.get("d_clip_s", 0.2) * 1e3) if law == "vq" else ""), flush=True)
    last_speed = c_link
    sched.set_downlink(c_link)

    while True:
        now = time.monotonic()
        if args.duration and now - t_start >= args.duration:
            break

        # ---- sample vport counters FIRST, before any slow work, so the
        # sampling cadence is regular (set by the sleep schedule) ----
        hybrid.sample(now, period)
        liveness.poll(now)      # fresh per-(src,class) sending signal

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
            spd_bps = uplink_speed()
            if spd_bps is not None and spd_bps != last_speed:
                last_speed = spd_bps
                sched.set_downlink(spd_bps)
                # V is anchored on C, so it moves with the link (§3.3):
                # holding V fixed across a speed change would make the
                # audit loop's damping link-rate dependent.
                marker.set_v(v_of(spd_bps))
                print("rx_agent: downlink %.0fG -> C_root %.1fG V %.0fMbit"
                      % (spd_bps / 1e9, sched.c_root / 1e9,
                         marker.v_full / 1e6), flush=True)

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
            # Root-congestion state, hysteretic (design.md §3.2.2). The
            # demand estimate exists to enable BORROWING, and borrowing
            # only means anything when there is spare capacity. When the
            # root is saturated there is none, so a flow-set running below
            # its share is not evidence of small appetite - it is evidence
            # that it is losing a race. Down-rating its demand there is
            # what latches starvation in:
            #   2026-07-27 incast8 - four TCP flow-sets started unevenly
            #   (50/4/2/2 G at t+0.3 s, before any RDMA, purely TCP's own
            #   startup race with no ECN). The three losers sat below
            #   theta_b of their share, so their demand collapsed to
            #   r*(1+delta), which raised their RDMA siblings' ceilings to
            #   19-21 G against a 12.1 G share, which kept the link full,
            #   which kept TCP down. A perfectly stable wrong equilibrium:
            #   Jain 0.69, reproducible across three runs.
            # Hysteresis (enter 0.95, leave 0.85) keeps the two regimes
            # from chattering when a genuinely small flow leaves capacity
            # unused and utilisation drops back.
            if law == "vq":
                ents, ceils, delays = vqs.step(active, sched_rates, dt)
                hybrid.prev_ents = ents
                marks = {}
                targets = {f: ceils.get(f, ents.get(f, 0.0)) for f in active}
                telem.send(targets, sched_rates, delays)
            root_congested = rootcong.update(sum(active.values()),
                                             sched.c_root) if law != "vq" else False
            # Every node of the policy tree that is at its capacity, not
            # just the root. A dst VM at its MaxRate saturates the node its
            # senders share, so all of them are contending - and this test
            # needs no per-sender split, because however wrong the split
            # is, the SUM of the estimates is the exact vport total.
            sat = nodesat.update(active, {
                v: p.get("max_rate_bps")
                for v, p in sched.policy.get("vms", {}).items()}) \
                if law != "vq" else set()
            # a finite "big" (root capacity), NOT inf: it saturates the
            # class demand cap exactly like inf but survives int(ceil_f)
            # in the telemetry pack (inf overflows).
            if law != "vq":
                demand = demand_estimate(active, ep["delta_demand"],
                                         sched.c_root, root_congested,
                                         saturated_dsts=sat)
                ents, ceils = sched.entitlements(active, demand)
                # the split prior for the next tick (see HybridRates.rates)
                hybrid.prev_ents = ents
                marks = marker.step(sched_rates, ents, ceils, dt)
            # §3.4 target synthesis: the policy ceiling discounted by the
            # audited sustained excess. Bounded by construction - no
            # excess leaves the target AT the ceiling, saturated excess
            # can press it no lower than (1-gamma)*ceil - so the sender's
            # law needs no floor of its own.
            # Report membership is decided by the FLOW TABLE, never by the
            # instantaneous rate (design.md §3.4). Building this from
            # `marks` violated that: VQMarker.step drops a flow-set that is
            # momentarily at r=0 with a drained ledger, so it vanished from
            # telemetry entirely. Measured 2026-07-27 (deep-pit rep 3): a
            # flow-set whose wire had died appeared in only 14-16% of
            # ticks, and since the v2 law only steps when a record arrives,
            # its effective k fell ~6x and a 200 ms recovery took 958 ms.
            # §3.4 warns about exactly this: an absent record is read as a
            # dead control channel, which is a positive feedback into
            # fail-open. Iterate `active` (present in the flow table, plus
            # the membership grace) and default an absent mark to 0.
            if law != "vq":
                targets = {f: ceils.get(f, ents.get(f, 0.0))
                              * (1.0 - gamma * marks.get(f, 0.0))
                           for f in active}
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
                   "vq": {f: int(v) for f, v in marker.vq.items()},
                   # vq law: virtual queueing delay (ms) per flow-set
                   "d": {f: round(v * 1e3, 3) for f, v in delays.items()
                         if v > 0}}
            logf.write(json.dumps(rec) + "\n")

        _tick_us = (time.monotonic() - now) * 1e6
        # split the histogram: a tick that ran the slow loop is a
        # different animal from one that did not, and conflating them is
        # what makes a predictable, attributable cost look like jitter
        (tick_slow_us if nticks_total % slow_every == 0
         else tick_us).append(_tick_us)
        if _tick_us > period * 1e6:
            tick_over += 1
        if now - tick_report >= 10.0 and tick_us:
            q = sorted(tick_us)
            n_ = len(q)
            qs = sorted(tick_slow_us) or [0.0]
            ns = len(qs)
            print("tick_us fast n=%d p50=%.0f p90=%.0f p99=%.0f p999=%.0f "
                  "max=%.0f | slow n=%d p50=%.0f max=%.0f | over_period=%d"
                  % (n_, q[n_ // 2], q[int(n_ * 0.90)], q[int(n_ * 0.99)],
                     q[min(int(n_ * 0.999), n_ - 1)], q[-1],
                     ns, qs[ns // 2], qs[-1], tick_over), flush=True)
            tick_us = []
            tick_slow_us = []
            tick_over = 0
            tick_report = now

        nticks_total += 1
        if now - last_print >= 1.0:
            act = " ".join("%s r=%.2fG u=%.2fG s=%.2f d=%.1fms"
                           % (f, rates.get(f, 0) / 1e9,
                              targets.get(f, 0) / 1e9, marks.get(f, 0),
                              delays.get(f, 0) * 1e3)
                           for f in sorted(targets) if rates.get(f, 0) > 0)
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
