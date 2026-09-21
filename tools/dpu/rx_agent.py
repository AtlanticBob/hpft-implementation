#!/usr/bin/env python3
"""Receiver-side agent: the virtual-queue ledger of design v4 (§4). Runs on
every DPU Arm as root (each node is a receiver for whatever the flow table
sends it).

Pipeline per control period T:
  meter   A_f: fresh per-dst-VF hardware totals (vport counters, split
          into RoCE and kernel-path bytes by the vport_meter helper) x the
          division among that VF's senders (the senders' own reports, see
          SenderLiveness; the megaflow byte mix when a sender reports
          nothing)
  sched   physical root C' = (port - unscheduled arrival)(1-h), then TWO
          water-fillings over the policy tree (VM cap -> class -> sender):
          the first with everyone demanding infinity gives the SHARE, the
          second gives lenders a finite demand and yields the ENTITLEMENT
          E_f (§4.4)
  ledger  Q_f <- clip(Q_f + A_f - E_f, 0, D*E_f);  q_f = Q_f / E_f (§4.5)
  telem   one UDP datagram per sender DPU per tick with one record per
          present flow-set: the virtual queue (microseconds of expected
          bytes; the sender divides by the period to get q) and the
          attributed arrival r (read by the sender's uplink tree as demand,
          not by the law - IMPLEMENTATION.md section 4 item 1). A record
          every tick even when q = 0 - the record IS the freshness permit,
          and its absence triggers the sender's fail-open.

E_f, the share and the ceiling stay local: they are what the ledger is
computed from, kept in the jsonl log for offline analysis but never on the
wire.

Membership comes from the OVS megaflow table (keys appear at once even
though their byte counters refresh only about once a second); an idle
flow-set (A_f = 0) takes A_f < E_f every tick, so its ledger drains to zero
and stays there: silence is never charged.

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
import sys
import time

import fastfill

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

    def sample(self, now, nkeep, window_s=0.02):
        self.window_s = window_s
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
                # Trim by TIME on the meter's own timestamps, not by sample
                # count: the meter's round over 8 vports takes ~0.8 ms, so
                # nkeep samples sized for a 1 ms cadence (21 for a 20 ms
                # window) spanned ~270 ms and every receiver-side rate
                # lagged the wire by ~130 ms (found 2026-08-29 with synced
                # DPU clocks). The meter's timestamps are exact, so a time
                # window is jitter-robust here; keep at least two samples.
                horizon = ring[-1][0] - self.window_s
                while len(ring) > 2 and ring[1][0] <= horizon:
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
    tells us. It is both the gate ("is this sender sending at all") and the
    ratio the pool is divided in (HybridRates._split); the pool itself stays
    the receiver's own measurement. Each sender reports every flow set of
    its own ("<src>><dst>|<class>": its VF's total times the flow set's part
    of it, tx_agent_e.SenderLiveness) and every VF's total ("<vnic>|<class>");
    a flow set is looked up under its own name first and under its VF's
    total only when the sender has not named it yet.

    Advisory by construction: an absent or stale feed leaves attribution
    exactly as it was (baseline arms run no sender agent at all).
    """

    STALE_S = 0.3
    IDLE_BPS = 50e6      # below the pace floor => not sending
    SMOOTH_S = 0.0       # ratio smoothing (see poll); 0 = use each report as it comes.
                         # 100 ms was tried (2026-08-29, r10): for a VM with 2-3
                         # senders the ratio IS inside the control loop, and the
                         # lag turned 5 % attribution jitter into 33 % real
                         # oscillation with 40 ms queues. Jitter is the lesser evil.

    def __init__(self, port):
        self.rate = {}                 # "host/vnic|class" or fsid -> bps
        self.kt = {}                   # the same keys -> when last reported
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
                # Smooth the reported rates over ~100 ms before they become
                # the split RATIO: each report is one ~1 ms sample of the
                # sender's meter and jitters by a few percent while the
                # fence moves every feedback (v4). The pool it splits is the
                # receiver's own 20 ms rate, so smoothing the ratio adds no
                # lag to the total and takes the jitter out of every
                # flow-set's arrival (5 % -> 2 % at 20 ms, V1 2026-08-29).
                a = min(1.0, (now - self.t) / self.SMOOTH_S) if (self.t and self.SMOOTH_S > 0) else 1.0
                for k, v in msg["r"].items():
                    old = self.rate.get(k)
                    self.rate[k] = float(v) if old is None else old + (float(v) - old) * a
                    self.kt[k] = now
                self.t = now
            except (ValueError, KeyError):
                continue

    def _reported(self, fsid, now):
        """the flow set's own fresh report, else its VF's total, else None"""
        if now - self.kt.get(fsid, -1e9) <= self.STALE_S:
            return self.rate[fsid]
        return self.rate.get("%s|%s" % (fsid.split(">")[0], fsid.rsplit("|", 1)[1]))

    def rates_for(self, fsids, now):
        """{fsid: sender-reported bps} when EVERY fsid has a fresh report.
        None if any member is unknown or the feed is stale."""
        if self.sock is None or now - self.t > self.STALE_S:
            return None
        out = {}
        for f in fsids:
            v = self._reported(f, now)
            if v is None:
                return None
            out[f] = float(v)
        return out

    def idle(self, fsid, now):
        """True only when the sender positively reports ~zero for this
        flow set. Unknown or stale => False (never gate on silence)."""
        if self.sock is None or now - self.t > self.STALE_S:
            return False
        v = self._reported(fsid, now)
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
        self.young_active = set()         # partially-visible newcomers
        self.liveness = None              # SenderLiveness (advisory)
        self.attr_tol = 0.15              # set from delta_demand in main(): how far a pool may exceed the senders'
                                          # own reports before the excess is read
                                          # as an undiscovered sender's traffic

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
            for vnic in self.meter.sample(now, nkeep, self.rate_window):
                mr = self.meter.rates(vnic)
                if mr is not None:
                    self.r_d[vnic] = mr[0] + mr[1]

    def rates(self, now=None):
        """Compute r_f from the last sample() x class attribution.

        The class split comes from the vport meter (both classes read
        directly from the per-VF ib/eth hardware buckets); with the meter
        stale the megaflow byte mix splits the sysfs total across classes
        and senders alike. Within a class the pool is divided among the
        senders by _split."""
        rates = {}
        self.unattributed = 0.0
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
            # Sender-reported ratio: the sender's own vport TX counters are
            # exact and ~1 ms fresh, so when every member of this pool has
            # a fresh report, divide the receiver's exact total in THAT
            # ratio and skip the ~1 s megaflow cache and its priors
            # altogether. The total stays the receiver's; only the division
            # moves to the sender (platform_notes.md, section 2).
            if True:
                w_s = live.rates_for(fs, now)
                if w_s is not None and sum(w_s.values()) > 0:
                    wsum = sum(w_s.values())
                    # The pool is the receiver's exact total for this (VM,
                    # class) and it counts EVERY sender, including one whose
                    # flow-set the receiver has not admitted yet: membership
                    # comes from the megaflow dump, which is ~1 s stale, while
                    # the bytes appear in the vport counter at once. Dividing
                    # the pool over the members we happen to know charges them
                    # for bytes no known sender claims to have sent, and the
                    # ledger cannot tell that mark from real over-taking.
                    # Measured on V2 (2026-08-31): for the 128 ms between the
                    # newcomer's first byte and its admission, the incumbent
                    # TCP flow-set was attributed 48.3 G against a true 22.7,
                    # its ledger reached 191 ms, and its fence was then cut to
                    # 2.9 G - a quarter of the 11.5 G share it had just been
                    # given. That single mark is what V2's join convergence
                    # has been failing on.
                    #
                    # So never attribute to a flow-set more than its OWN
                    # sender reports having sent, beyond attr_tol for the skew
                    # between two counters read at different instants. Below
                    # that bound this is algebraically what it always was
                    # (w_s[f] * pool / wsum), so the steady state is untouched;
                    # it engages only when the pool exceeds every known
                    # sender's account of it, which is the signature of a
                    # sender we cannot yet name. The excess is charged to
                    # nobody - the alternative, charging a flow-set for a
                    # stranger's bytes, is the bug above.
                    k = min(pool / wsum, 1.0 + self.attr_tol)
                    for f in fs:
                        if w_s[f] > 0:
                            out[f] = w_s[f] * k
                    return
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


class Scheduler:
    """Three-layer water-filling (root -> VM -> class -> flow-set), design
    v4 section 4.4, computed by the C allocator (fastfill.c)."""

    def __init__(self, policy, line_rate, headroom, delta):
        self.policy = policy
        self.headroom = headroom
        self.c_root = line_rate * (1.0 - headroom)
        self.delta = delta

    def set_downlink(self, speed_bps):
        """Track the live downlink speed (M3: p1 renegotiated to 100G).
        Root capacity must follow, or the account never sees the shortage."""
        self.c_root = speed_bps * (1.0 - self.headroom)

    def entitlements(self, demand, vm_minus=None):
        """{fsid: demand bps} -> ({fsid: e_f}, {fsid: ceil_f}); e_f is the
        demand-capped fill, ceil_f the same tree with this flow-set's own
        demand infinite. vm_minus: {dst_vm: bps} already taken on that VM
        by traffic the scheduler does not control."""
        if not demand:
            return {}, {}
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
            if vm_minus and d in vm_minus:
                vm_max[i] = max(vm_max[i] - float(vm_minus[d]), 0.0)
            cwd = p.get("class_weights", {})
            for c, j in cls_ids.items():
                cls_w[i * n_cls + j] = float(cwd.get(c, 1))
        return fastfill.entitlements_c(fsids, dst_idx, cls_idx, wfs, dem,
                                       n_vm, n_cls, vm_w, vm_max, cls_w,
                                       self.c_root)

class Telemetry:
    """One binary datagram per sender DPU per tick. Wire format:
      header  '<HH'       = (seq_lo16, n_records)
      record  '<64s Q Q Q'  = (fsid[64], r_bps, d_us, e_bps)
    r is the attributed arrival (the sender's tree reads it as demand; the
    law does not use it), d the virtual queue in microseconds, e the
    entitlement. fsid strings are short ('sgpu01/vf0>sgpu02/vf0|tcp' ~ 26 B);
    uint64 because bps at 200G line rate overflows uint32.

    e is NOT read by the design's own law, and that is the design: the sender
    is never handed a rate to obey, only the queue its own excess produced.
    It is on the wire so that the control-law ablation (evaluation E2.5) can
    run the variants that do obey a rate - explicit-rate and EyeQ-style -
    against the real law under one wire format instead of two.

    THIS FORMAT IS A SYNCHRONIZED BOTH-ENDS CHANGE: tx_agent_e._TREC must
    match exactly, and both agents must be deployed and restarted together
    or the control loop parses garbage."""

    HDR = struct.Struct("<HH")
    REC = struct.Struct("<64sQQQ")

    def __init__(self, vnic_host, telemetry_ip, port):
        self.vnic_host = vnic_host        # vnic_id -> host
        self.telemetry_ip = telemetry_ip  # host -> its DPU ctl IP
        self.port = port
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
        for fsid, r, d_us, e in recs:
            b = fsb.get(fsid)
            if b is None:
                b = fsb[fsid] = fsid.encode()[:64]
            out.append(pack(b, int(r), int(d_us), int(e)))
        return b"".join(out)

    def send(self, fsids, rates, delays, ents=None):
        """One record per flow-set in `fsids`, grouped by the sender's DPU."""
        self.seq += 1
        ents = ents or {}
        per_dpu = {}     # dpu ip -> [records]
        for f in fsids:
            src = f.split(">")[0]
            ip = self.telemetry_ip.get(self.vnic_host.get(src))
            if ip is not None:
                per_dpu.setdefault(ip, []).append(
                    (f, rates.get(f, 0), delays.get(f, 0.0) * 1e6, ents.get(f, 0)))
        for ip, recs in per_dpu.items():
            try:
                self.sock.sendto(self._pack(recs), (ip, self.port))
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
    if not fastfill.USING_C:
        sys.exit("rx_agent: libfastfill.so is missing next to fastfill.py "
                 "(deploy_check.sh --deploy builds it); refusing to run")
    reg_mtime = os.stat(args.registry).st_mtime
    ep = reg["e_params"]
    period = (args.period_ms or ep["period_ms"]) / 1e3
    local_host = args.local_host or reg["receiver_host"]
    line = reg["line_rate_bps"]
    mac2vnic = {v["mac"]: v["vnic_id"] for v in reg["vnics"] if "mac" in v}
    local_macs = {v["mac"] for v in reg["vnics"]
                  if v.get("mac") and v["host"] == local_host}
    vnic_host = {v["vnic_id"]: v["host"] for v in reg["vnics"]}

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

    added = install_class_rules(args.bridge)
    print("rx_agent: bridge=%s rules_added=%s T=%.0fms local=%s C=%.0fG "
          "log=%s vport_meter=%s"
          % (args.bridge, added or "none", period * 1e3, local_host,
             c_link / 1e9, args.log, args.vport_meter or "off"), flush=True)

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
    hybrid.attr_tol = float(ep["delta_demand"])   # the one tolerance, 2 of the design
    sched = Scheduler(reg["policy"], line, ep["headroom"], ep["delta_demand"])
    last_seen = {}
    ctl = reg["control"]
    telem = Telemetry(vnic_host, ctl["telemetry_ip"], ep["telemetry_port"])
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
    # design v4 section 4: two water-fillings give the entitlement E_f, the
    # ledger is Q += (A-E) clipped to [0, D*E], and the wire carries only
    # q = Q/E.
    conf_q = {}                       # fsid -> Q bits
    conf_other, conf_other_vm = 0.0, {}   # smoothed unscheduled arrival: total, per dst VM
    conf_other_tau = float(ep.get("other_tau_s", 0.1))
    conf_aavg, conf_avg_tau = {}, float(ep.get("decision_avg_s", 0.1))   # averaged arrival for the lender decision
    conf_croot = sched.c_root
    # the ledger's cap D in seconds: D periods of expected bytes (4.5)
    conf_dr = float(ep.get("D", 30)) * period
    delays, ents, ceils = {}, {}, {}
    print("rx_agent: ledger cap D=%.0f periods (%.0f ms)"
          % (conf_dr / period, conf_dr * 1e3), flush=True)
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
                print("rx_agent: downlink %.0fG -> C_root %.1fG"
                      % (spd_bps / 1e9, sched.c_root / 1e9), flush=True)

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
        # Expected rate E (design v4 4.4).
        # share = what each flow-set is guaranteed given who is
        # present (everyone asks for everything). A flow-set using
        # less than its share is a LENDER: its E stays at the share
        # (so it can climb back to it in one step - its arrival is
        # not read as "does not want more"), and what it leaves
        # unused is lent through the water-filling by giving it the
        # demand A(1+delta) there. Everyone else asks for everything
        # and is granted share + a fair part of what was lent; that
        # grant is its E, and the virtual queue reclaims it the
        # moment a lender comes back.
        # Physical capacity, not nominal: traffic this scheduler does
        # not control (ip_other - anything that is neither RDMA nor
        # TCP) still crosses the same port and the same VM cap, so
        # the root and the VM caps are what is left after it. Without
        # this the account allocates the full port while the port is
        # already partly taken, the surplus queues at the switch, and
        # the tenants' CCs get crushed there (V7, 2026-08-28: 40 G of
        # UDP left RDMA at 0.7 % while TCP was lent RDMA's share).
        # Smoothed over ~100 ms so one burst does not move the root;
        # lending is bounded by the same water-filling, so it cannot
        # hand out capacity the port does not physically have.
        other_now, other_vm_now = 0.0, {}
        for f, r_ in rates.items():
            if f.rsplit("|", 1)[1] not in SCHED_CLASSES:
                d_ = f.split(">")[1].rsplit("|", 1)[0]
                other_now += max(r_, 0.0)
                other_vm_now[d_] = other_vm_now.get(d_, 0.0) + max(r_, 0.0)
        a_ = min(1.0, dt / conf_other_tau)
        conf_other += (other_now - conf_other) * a_
        for d_ in set(conf_other_vm) | set(other_vm_now):
            conf_other_vm[d_] = conf_other_vm.get(d_, 0.0) + (other_vm_now.get(d_, 0.0) - conf_other_vm.get(d_, 0.0)) * a_
        link_ = sched.c_root / (1.0 - sched.headroom)
        root_nominal = sched.c_root
        # 4.3: (port - unscheduled) x (1-h), never below the floor of a few
        # percent of the port; the floor is on the result, so it is the 5 %
        # of the parameter table and not 5 % less the headroom
        sched.c_root = max((link_ - conf_other) * (1.0 - sched.headroom), 0.05 * link_)
        conf_croot = sched.c_root
        vm_minus = {d_: v_ for d_, v_ in conf_other_vm.items() if v_ > 0}
        big = 10.0 * root_nominal
        share, _ = sched.entitlements({f: big for f in active}, vm_minus)
        # Decisions (who lends, who is at share) use a ~100 ms average
        # of the arrival; the queue keeps using the raw arrival. The
        # 20 ms attribution samples jitter by +-7 % on a VM with three
        # senders, which flipped lender status and the direction bit
        # on noise and made the borrowers' entitlement whipsaw.
        a_ = min(1.0, dt / conf_avg_tau)
        for f in active:
            conf_aavg[f] = conf_aavg.get(f, active[f]) + (max(active.get(f, 0.0), 0.0) - conf_aavg.get(f, active[f])) * a_
        for f in [f for f in conf_aavg if f not in active]:
            del conf_aavg[f]
        # 4.4: a flow-set whose averaged arrival, with the growth margin, is
        # still under its share is a lender; it keeps E = share and lends the
        # rest through the fill. Nothing exempts a newcomer: reading it as a
        # lender while it ramps is the join-edge cost design 8.1 accepts.
        demand = {}
        lenders = set()
        for f in active:
            want = conf_aavg[f] * (1.0 + ep["delta_demand"])
            if want < share.get(f, 0.0):
                demand[f] = want
                lenders.add(f)
            else:
                demand[f] = big
        ents, ceils = sched.entitlements(demand, vm_minus)
        sched.c_root = root_nominal
        ents = dict(ents)
        for f in lenders:
            ents[f] = share[f]
        hybrid.prev_ents = ents
        # Feedback (design v4 4.6): the virtual queue, normalised to
        # periods of expected bytes, and nothing that can be turned
        # back into a rate. Internally the queue is kept in bits
        # with rates, which is the same arithmetic as bytes per
        # period.
        delays = {}
        for f in active:
            A = sched_rates.get(f, 0.0)
            E = ents.get(f, 0.0)
            if E <= 0:
                conf_q.pop(f, None)
                continue
            Qn = conf_q.get(f, 0.0) + (A - E) * dt
            Qn = min(max(Qn, 0.0), conf_dr * E)
            if Qn > 0:
                conf_q[f] = Qn
            else:
                conf_q.pop(f, None)
            delays[f] = Qn / E            # q in seconds = periods * period
        for f in [f for f in conf_q if f not in active]:
            del conf_q[f]
        # Report membership is decided by the FLOW TABLE, never by
        # the instantaneous rate: a flow-set momentarily at r=0 with
        # a drained ledger must still appear, or the sender reads
        # the absent record as a dead control channel and fails
        # open - positive feedback. So every member of `active` (the
        # flow table plus the membership grace) gets a record.
        telem.send(active, sched_rates, delays, ents)

        # throttled logging (default ~50 Hz), never every 1ms tick
        if nticks_total % log_every == 0 and nticks_total:
            rec = {"ts": round(time.time(), 4), "dt_s": round(dt, 6),
                   "read_ms": round(dump_ms, 3), "nfs": len(sched_rates),
                   "ma": len(hybrid.attr_meter),
                   # raw meter class rates per dst VF [rdma, kern]: the
                   # direct-read numbers before any megaflow sub-split,
                   # for offline old-vs-new attribution reconciliation
                   "rv": {d: [int(x) for x in (vpm.rates(d) or (0, 0))]
                          for d in hybrid.attr_meter},
                   "r": {f: int(v) for f, v in rates.items()},
                   # the local ledger: entitlement e and share ceiling c
                   # (never on the wire), the virtual queue d in ms
                   # (written only while positive)
                   "e": {f: int(v) for f, v in ents.items()},
                   "c": {f: int(v) for f, v in ceils.items()},
                   "d": {f: round(v * 1e3, 3) for f, v in delays.items()
                         if v > 0},
                   "oth": int(conf_other), "croot": int(conf_croot)}
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
            act = " ".join("%s r=%.2fG e=%.2fG d=%.1fms"
                           % (f, rates.get(f, 0) / 1e9,
                              ents.get(f, 0) / 1e9,
                              delays.get(f, 0) * 1e3)
                           for f in sorted(ents) if rates.get(f, 0) > 0)
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
