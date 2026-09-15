#!/usr/bin/env python3
"""Sender-side agent: the rate law of design v4 (§5). Runs on the sender
DPU Arm.

Per telemetry record from the owner (receiver DPU) the fence takes one
multiplicative step (design v4 §5.2). The record carries the normalised
virtual queue q and nothing else; the sender differences consecutive q to
recover dq itself:

    R <- R * (1 + alpha * min(m, m_max))                    (queue empty)
    R <- R * (1 - clip(kappa * dq + (kappa/D) * min(q, D), -1/2, 1/2))
    (step_form = exp applies the same exponent as e^x instead; same to first order)

The three factors do one thing each: probe up while the ledger is empty
with a step that grows with the silence (§5.3), brake on how fast the
ledger is filling, repay on how full it is. Nothing here carries a rate
unit - the whole law lives in log space on dimensionless inputs, which is
why the constants do not change with link speed or flow count
(design_theory_v4.md §5).

The probe is separately bounded (§5.3): it may not climb past twice the
flow-set's own send rate, the local tree plus one tolerance, or maxrate,
and the bound only blocks the probe - it never pushes the fence down.

Startup (§5.4): a fresh flow-set begins at the fence floor with the
silence counter already at its cap, because a flow-set that has never
seen a queue has no evidence about where its share is, and that is the one
case where the largest step is the right one.

fail-open: no telemetry for n1_freeze_s -> R frozen; for n2_failopen_s ->
R tracks Tree_f, so the flow degrades to sender-local policy only.
Actuation: pace_f = max(min(R_f, Tree_f), floor); tcp -> host pace shim
(UDP -> DirectBpfMapWriter); rdma -> PCC RP mailbox as a flow-pair budget.
"""
import argparse
import json
import zlib
import errno
import math
import os
import re
import socket
import struct
import sys
import time

import mmap

import fastfill
import hw_maxrate

FIFO = "/tmp/rp_fifo"   # RDMA executor mailbox: 0xb47f|n {set id, rate}, 0xb48e|n {key, set id}

# binary telemetry - MUST match rx_agent.Telemetry.REC exactly; the two
# agents are deployed and restarted together or the loop parses garbage.
# header (seq16, n) then n x (fsid[64], r_bps u64, d_us u64)
_THDR = struct.Struct("<HH")
_TREC = struct.Struct("<64sQQ")   # (fsid, r bps, d us)


def parse_telemetry(data):
    """bytes -> (seq, {fsid: {'r','d'}}); tolerant of short buffers."""
    if len(data) < _THDR.size:
        return None, {}
    seq, n = _THDR.unpack_from(data, 0)
    recs = {}
    off = _THDR.size
    for _ in range(n):
        if off + _TREC.size > len(data):
            break
        fb, r, d_us = _TREC.unpack_from(data, off)
        off += _TREC.size
        recs[fb.rstrip(b"\x00").decode("ascii", "ignore")] = {
            "r": r, "d": d_us / 1e6}
    return seq, recs


def v4_step(R, q, q_prev, below, alpha, m_max, kappa, D, floor, cap,
            keep_silence=False, linear=False):
    """Design v4 (§5.2), one feedback: three multiplicative factors, written
    as 1 + x (linear=True, the default) or e^x (linear=False).
    probe   x (1 + alpha*m_hat) m = consecutive feedbacks with an empty
                                queue, m_hat = min(m, m_max); the step
                                grows with the silence and is capped
    brake   x (1 - kappa dq)    dq = signed queue change since last feedback
    repay   x (1 - (kappa/D) q) by queue length; brake and repay share one
                                exponent, bounded to +-1/2 in the linear form
    q, dq are in periods of expected bytes (dimensionless); no time in here.

    Why the step grows with the silence (§5.5): probing too hard only
    costs anything when the fence is ALREADY at the share - that step
    crosses, the excess becomes queue, and it has to be repaid; if the
    fence is far below, the same step is free. How likely "still at the
    share" is falls monotonically as the silence lengthens, so the step
    should start near zero and grow. A THRESHOLD ("switch to a big step
    after N quiet feedbacks") is the crude form of the same idea and must
    err on one side: the quiet stretch of an ordinary sawtooth is not
    reliably shorter than N - the receiver's per-sender split jitters by
    a few percent and one jitter empties the queue for a few extra
    feedbacks - so the big step fires in the steady state and the fence
    runs far past its share within one loop delay. Measured on
    V7_conf6_20260829_r18, quiet phase: 31% of queue-free probe intervals
    ran at the fast band, and the fence reached +38% over its share.
    m_max bounds the worst-case overshoot at alpha*m_max*tau (§5.3 bounds
    where the probe may go, which is a different thing).
    Returns (R, dq, below)."""
    # signed queue change while a queue exists: growing -> cut, draining ->
    # let the fence back up in proportion. The drain-side half is what keeps
    # the fence from being pushed far below the share while the queue is
    # being paid off (with only the repayment term acting during the drain,
    # the fence undershoots, the probe then overshoots, and the loop cycles
    # - offline model, 2026-08-28)
    dq = (q - q_prev) if (q > 0.0 or q_prev > 0.0) else 0.0
    R1 = max(R, floor)
    if q <= 0.0:
        # probe whenever the queue is empty: the receiver's queue is the only
        # signal; a probe that crosses the share shows up as a queue within
        # one loop delay and is taken back by the brake and the repayment.
        below += 1
        # §5.3, cap = min(maxrate, 2 x own send rate over ~100 ms): the
        # inner min stops the probe at the cap, the outer max makes the cap
        # incapable of pulling R DOWN. Clipping R to the cap outright fed on
        # itself - a dip in the send-rate sample cut R, which cut the send
        # rate, which cut the cap - and rode a 1-QP flow to the floor.
        # The silence keeps counting while the cap holds the probe: the step
        # is already bounded by m_max, so all a long block does is let an
        # app-limited flow resume at the maximum step, which is right - a
        # flow that has been using a fraction of its share IS far from it.
        step = alpha * min(below, m_max)
        R1 = max(R1, min(R1 * ((1.0 + step) if linear else math.exp(step)), cap))
    elif not keep_silence:
        below = 0
    # keep_silence: this flow-set has never yet been held by its fence, so a
    # queue it produced says nothing about where its share is and must not
    # zero the silence. Every new RDMA flow-set does this: the executor
    # admits an unknown QP at a blind allowance before any budget reaches
    # it, the burst overruns the entitlement, and the queue that follows
    # used to zero the silence of a flow-set that had just been told to
    # start at maximum uncertainty - so the probe ramped from alpha*1
    # instead of alpha*m_max and took 1100 ms instead of 630 ms to reach
    # the share (measured V2, 2026-08-30). The exemption ends for good at
    # the first evidence that the fence is in force; it must not be a
    # standing condition, because a fence falling faster than the wire can
    # follow also shows a send rate above the fence, and THAT queue is ours
    # and must reset the silence. The brake and the repayment act on the
    # queue either way - it is real and must be paid.
    x = kappa * dq + (kappa / D) * min(q, D)
    if linear:
        # 1 - x instead of e^-x, bounded to a half either way so the factor
        # stays positive and one feedback can never do more than halve or add
        # half (Swift's max_mdf). The bound was never reached in 80k logged
        # feedbacks; it is the safety rail, not the law.
        R1 *= 1.0 - max(-0.5, min(0.5, x))
    else:
        R1 *= math.exp(-x)
    return max(R1, floor), dq, below


def track_step(R, target, dt, k, floor):
    """First-order step of R towards `target` in log space. Under design v4
    this is no longer the law - it is the fail-open actuator (§5.5): when
    telemetry is stale the fence tracks the local tree instead of freezing.

    One step towards `target`
    in log space, over a wall-clock interval dt.

        R <- R * (u/R) ** alpha,     alpha = 1 - exp(-k*dt)

    This is the exact discretisation of zdot = k*(ln u - z), z = ln R. At
    the nominal 1 ms tick alpha = 0.0198 against a literal k*T = 0.0200
    (0.99% apart); unlike k*T, alpha can never exceed 1, so a
    late or coalesced update converges towards the target instead of
    shooting past it.

    `floor` (the 50 Mbps pace floor, §6) is what gives the logarithm a
    domain: u = ceil*(1-gamma*s) is >= 0.75*ceil by construction, so it
    can only reach 0 when the flow-set's policy ceiling is itself 0
    (capacity exhausted, or MaxRate 0) - and then the floor is the right
    answer. It also keeps the pace above the executor's quantisation
    step. Both reasons are numerical; it is not protecting the executor
    from low rates, which were measured to be safe.
    Kept module-level and pure so the convergence closed-forms in
    design_theory §2.4 can be checked against the production expression."""
    u = max(float(target), floor)
    r0 = max(R, floor)
    return max(r0 * (u / r0) ** (1.0 - math.exp(-k * dt)), floor)


class FlowState:
    __slots__ = ("R", "last_rx", "last_step", "last_seq", "mode", "pace", "r",
                 "log_R", "log_age", "log_mode", "esc_since", "esc_log",
                 "shim_last", "q_prev", "dq", "below", "as_avg", "enforced",
                 "held_avg")

    def __init__(self, now):
        self.R = 0.0           # set by the first feedback (§5.4 start)
        self.last_rx = now
        self.last_step = now   # wall-clock anchor for the fail-open tracking step
        self.last_seq = -1
        self.mode = "fresh"
        self.pace = 0.0
        self.r = 0             # last telemetry r_f (sender-tree demand)
        self.log_R = -1.0
        self.log_age = 0
        self.log_mode = ""
        self.esc_since = 0.0   # executor-escape tripwire: since when r >> pace
        self.esc_log = 0.0     # last escape alarm emitted
        self.shim_last = 0.0   # last TCP rate push to the host shim
        self.q_prev = 0.0      # last feedback's queue (periods)
        self.dq = 0.0          # last queue growth
        self.below = 0         # consecutive feedbacks with an empty queue
        self.enforced = False  # has the fence ever actually held this flow-set
        self.as_avg = 0.0      # own send rate, ~100 ms average (for the probe cap)
        self.held_avg = 0.0    # part of its bytes sent at the pace, ~100 ms average


class SenderTree:
    """design_v4.md §5.5: the sender-side tree over the local uplink, sharing
    the rx scheduler's structure (src VM MaxRate -> class weights -> fs).
    Tree_f is the fs's fair-share CEILING (its own demand set to infinity,
    siblings at their measured demands) rather than a demand-capped fill:
    a new flow-set starts at Tree_f (§4.2) and fail-open climbs to it
    (§4.4), and under a demand-capped fill a flow-set with r=0 would get
    Tree ~= 0, which contradicts both.
    Class borrowing between the two actuator planes (fq+edt / PCC) falls
    out of the work-conserving fill: this IS the budget arbiter, feasible
    in one process because both actuators hang off this agent."""

    def __init__(self, policy, line, headroom, delta, floor, theta=0.85):
        self.policy = policy
        self.cap = line * (1.0 - headroom)
        self.delta = delta
        self.floor = floor
        self.theta = theta

    def _alloc(self, demand):
        if not demand:
            return {}
        vms = self.policy["vms"]
        fsids = list(demand)
        vm_ids, cls_ids = {}, {"rdma": 0, "tcp": 1}
        src_idx, cls_idx, wfs, dem = [], [], [], []
        for f in fsids:
            src_dst, c = f.rsplit("|", 1)
            src = src_dst.split(">")[0]
            if src not in vm_ids:
                vm_ids[src] = len(vm_ids)
            src_idx.append(vm_ids[src])
            cls_idx.append(cls_ids.setdefault(c, len(cls_ids)))
            wfs.append(1.0)
            dem.append(float(demand[f]))
        n_vm, n_cls = len(vm_ids), len(cls_ids)
        vm_w = [1.0] * n_vm
        vm_max = [float("inf")] * n_vm
        cls_w = [1.0] * (n_vm * n_cls)
        for src, i in vm_ids.items():
            p = vms.get(src, {})
            vm_w[i] = float(p.get("weight", 1))
            m = p.get("max_rate_bps")
            vm_max[i] = float(m) if m else float("inf")
            cwd = p.get("class_weights", {})
            for c, j in cls_ids.items():
                cls_w[i * n_cls + j] = float(cwd.get(c, 1))
        alloc, _ = fastfill.entitlements_c(fsids, src_idx, cls_idx, wfs, dem,
                                           n_vm, n_cls, vm_w, vm_max, cls_w,
                                           self.cap)
        return alloc

    def trees(self, flows):
        """Tree_f for every local flow-set (design v4 5.6), two fills.

        Pass 1 gives every flow-set its SHARE S_f under the current
        occupants (everyone demands the root). Pass 2 gives the
        ALLOCATION G_f: a flow-set that is using >= theta of the pace we
        gave it still wants more and demands R_f - the most its receiver
        would let it take, and more than that it could not use - while one
        that is not asks only for what it uses. Tree_f = max(G_f, S_f), so
        a flow-set that is not pressing keeps its whole share and can
        climb back into it at once, exactly as a lender does at the
        receiver (4.3), and a flow-set with no traffic at all still gets
        S_f rather than nothing (which is what a new flow-set and the
        fail-open path both start from).

        Why demand is NOT the measured send rate for a pressing
        flow-set: send rate <- pace <- Tree <- demand closes a loop, and
        this loop has neither damping nor a stability criterion, so
        wherever it binds at the same time as the fence it is the one
        that decides the transient (measured V2 exit, 2026-08-30: the
        fence reached the new share in 0.78 s and was never the
        constraint, while the tree wandered between 23.9 and 25.0 G for
        ~600 ms). R_f comes from the receiver's damped loop instead, and
        an unpressed flow-set's send rate is set by its application - so
        neither branch is a function of this tree's own output. The
        theta test is still a downstream observation, but it only
        classifies; it does not produce a number."""
        big = self.cap
        share = self._alloc({f: big for f in flows})
        demand = {}
        for f, st in flows.items():
            if st.pace > 0 and st.r >= self.theta * st.pace:
                demand[f] = max(st.R, self.floor)
            else:
                demand[f] = max(st.r * (1.0 + self.delta), self.floor)
        alloc = self._alloc(demand)
        return {f: max(alloc.get(f, 0.0), share.get(f, 0.0)) for f in flows}


class RpMailbox:
    """PCC RP FIFO writer (rdma class). Budget = pace_f; rate = the
    receiver-measured r_f from telemetry (class-split!) - the RP's inner
    per-QP level control converges the pair's RDMA wire rate to the budget.
    Feeding rep rx_bytes here (tx_agent2's way) counted TCP+RDMA together
    and crushed RDMA whenever TCP shared the VF (2026-07-09 root cause)."""

    def __init__(self, line_bps):
        self.line = line_bps
        self.fd = -1
        self.ino = -1
        self.writes = self.errs = 0
        # why a write did not land, by cause: ENXIO = no reader (the
        # executor is not running), EAGAIN = the pipe is full (the executor
        # is running but not draining), nofifo = the FIFO does not exist.
        # One number for all three hid which of them sgpu02 was in (2026-09-11).
        self.why = {}
        # set whenever the FIFO is found recreated, i.e. rp_service.sh has
        # restarted the executor and its device memory is empty again: the
        # caller must re-send everything the executor can only learn from
        # us (the QP bindings, the unknown-flow allowance)
        self.restarted = False

    def _units(self, bps):
        return max(1, round(bps / self.line * (1 << 20)))

    def ensure_open(self):
        """Hold the FIFO write end open at all times: if every writer
        closes, the RP's stdin reader hits EOF and never reads again."""
        self._open()

    def _open(self):
        """(Re)open the write end; the FIFO is recreated by rp_service.sh on
        every executor restart, which shows as a new inode."""
        try:
            ino = os.stat(FIFO).st_ino
        except FileNotFoundError:
            self.why["nofifo"] = self.why.get("nofifo", 0) + 1
            return False
        if self.fd < 0 or ino != self.ino:
            if self.fd >= 0:
                os.close(self.fd)
                self.fd = -1
            try:
                self.fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as e:
                k = errno.errorcode.get(e.errno, str(e.errno))
                self.why[k] = self.why.get(k, 0) + 1
                return False
            if self.ino != -1:
                self.restarted = True
            self.ino = ino
        return True

    def write_sets(self, entries):
        """entries: [(set_id, budget_bps)] -> 0xb47f batch.

        The per-QP executor (2026-09-06) budgets a FLOW SET, not a hardware
        flow tag: with ROCE_CC_SHAPER_COALESCE=SOURCE_QP the tag is per QP,
        so the old 0xb47c format, whose key was the tag, no longer names
        anything the policy plane knows. The set id is the pair's registry
        flowtag reused as an opaque name, so both sides keep one vocabulary."""
        if not entries:
            return
        parts = ["0x%x" % (0xb47f0000 | len(entries))]
        for sid, bud in entries:
            parts.append("0x%x %d" % (sid, self._units(bud)))
        if self._fifo_write(" ".join(parts) + "\n"):
            self.writes += 1
        else:
            self.errs += 1

    def write_qpmap(self, entries):
        """entries: [(key, set_id)] -> 0xb48e batch, so the executor can tell
        which flow set a QP's events belong to. key = vhca_id << 24 | qpn
        (the bare qpn when no vhca table is available): QP numbers are
        unique per function only, and the executor sees the function as
        the event's vhca_id."""
        if not entries:
            return
        parts = ["0x%x" % (0xb48e0000 | len(entries))]
        for qpn, sid in entries:
            parts.append("%d 0x%x" % (qpn, sid))
        if self._fifo_write(" ".join(parts) + "\n"):
            self.writes += 1
        else:
            self.errs += 1

    def _fifo_write(self, line):
        if not self._open():
            return False
        try:
            os.write(self.fd, line.encode())
            return True
        except OSError as e:
            k = errno.errorcode.get(e.errno, str(e.errno))
            self.why[k] = self.why.get(k, 0) + 1
            os.close(self.fd)
            self.fd = -1
            return False


class PaceShim:
    """UDP client to the host-side DirectBpfMapWriter shim (tcp class)."""

    def __init__(self, addr):
        host, port = addr.split(":")
        self.addr = (host, int(port))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sent = self.acked = self.errs = 0
        # the TCP executor's wire bytes per pair, pushed by the shim every
        # 10 ms ({"src>dst": bytes}), and when this agent received them
        self.tx, self.tx_t = {}, 0.0

    def set_rate(self, src, dst, rate_bps):
        """The flow-set's rate R and nothing else: the executor splits it
        over the flow-set's connections by their own CC windows (design
        v4 section 6), so no trust and no start window travel here."""
        msg = {"src_vnic": src, "dst_vnic": dst, "rate_bps": int(rate_bps)}
        try:
            self.sock.sendto(json.dumps(msg).encode(), self.addr)
            self.sent += 1
        except OSError:
            self.errs += 1

    def drain_acks(self):
        while True:
            try:
                data, _ = self.sock.recvfrom(2048)
            except (BlockingIOError, OSError):
                return
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if "tx" in msg:
                self.tx, self.tx_t = msg["tx"], time.monotonic()
            elif msg.get("ok"):
                self.acked += 1


class SenderLiveness:
    """Fresh per-flow-set TX rates, published to the flow set's receiver.

    Why: the receiver's hardware counts arrivals per (destination VM,
    class) only, so it divides that exact total over the senders in the
    proportions the senders report (platform notes, section 2), and it
    gates on the report to know at once when a sender stops - the ~1 s
    hardware-cached megaflow counters cannot tell it that in under a
    second. The sender's own vport TX counters are fresh at ~1 ms but count
    per VF, and a VF that sends to several receivers would report its
    total to each of them: in a full mesh every receiver then divides
    every pool evenly and cannot see which sender is over (2-7b mesh96,
    2026-09-11: RDMA flow sets got 0.59-1.26 of their entitlement while the
    ledger read 0.94-0.98 for all of them). So each VF's fresh total is
    divided over the VF's flow sets in the proportions the executors
    count: the RDMA executor answers every mailbox round with each set's
    bytes (pcc_host keeps the running totals in /dev/shm/hpft_rp_tx, about
    every 16 ms), the TCP executor counts per pair and the pace shim pushes
    the totals every 10 ms. A VF with one flow set of a class - every VF
    in a star - reports its whole total, exactly as before; so does one
    whose executor has no fresh counts.

    Payload is tiny and advisory: {"h": host, "t": mono_ns, "r":
    {"<vnic>|<class>": bps, "<src>><dst>|<class>": bps}}, the per-VF totals
    to every other node (the receiver's fallback) plus, to each receiver,
    its own flow sets. The receiver falls back to its old chain whenever
    this feed is absent or stale, so a sender without the helper (baseline
    arms) behaves exactly as before.
    """

    HDR = struct.Struct("<8sII8H")
    STALE_S = 0.1
    TXC = struct.Struct("<8sQQII")   # pcc_host's hpft_txc header, 32 bytes
    TXC_PATH = "/dev/shm/hpft_rp_tx"
    PART_WIN_S = 0.010   # shortest interval a proportion is taken over
    PART_STALE_S = 0.2   # counts older than this: back to the whole total

    def __init__(self, path, vport_of_vnic, addr_of_host, port, host):
        self.path, self.vport_of_vnic = path, vport_of_vnic
        # every other node may be a receiver (2026-09-04), so the per-VF
        # totals go to all of them; a node that is not receiving ignores it
        self.addr_of_host = {h: (ip, port) for h, ip in addr_of_host.items()}
        self.host = host
        self.mm = None
        self.slot = {}
        self.prev = {}          # vnic -> (t_s, tx_ib, tx_eth)
        self.rate = {}          # "vnic|class" -> bps
        self.fs_rate = {}       # fsid -> bps: its VF's total x its part
        self._next_open = 0.0
        # set by main once they exist: the local flow sets (fsid -> state),
        # the RDMA set id of a pair, and the shim that carries TCP's counts
        self.flows = {}
        self.flowtag_of = None
        self.shim = None
        self._fs = {}           # fsid -> (src, dst host, class, counter key)
        self.txc = None
        self._txc_next_open = 0.0
        self.win = {"rdma": None, "tcp": None}   # (t, {key: bytes}) a part starts from
        self.part = {}          # fsid -> its part of its VF's class total
        self.held = {}          # fsid -> part of its bytes sent at the pace (RDMA)
        self.part_t = {"rdma": 0.0, "tcp": 0.0}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)

    def _open(self, now):
        if now < self._next_open:
            return False
        self._next_open = now + 1.0
        try:
            f = open(self.path, "rb")
            self.mm = mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ)
            f.close()
        except (OSError, ValueError):
            self.mm = None
            return False
        hdr = self.HDR.unpack_from(self.mm, 0)
        if hdr[0] != b"HPFTVPM1":
            self.mm = None
            return False
        slot = {hdr[3 + i]: i for i in range(hdr[1])}
        self.slot = {vn: slot[vp] for vn, vp in self.vport_of_vnic.items()
                     if vp in slot}
        return True

    def tick(self, now):
        if self.mm is None and not self._open(now):
            return
        for vnic, sl in self.slot.items():
            off = 64 + 64 * sl
            try:
                s1 = struct.unpack_from("<Q", self.mm, off)[0]
                if s1 & 1:
                    continue
                t_ns, _rx_ib, _rx_eth, tx_ib, tx_eth = struct.unpack_from(
                    "<5Q", self.mm, off + 8)
                if s1 != struct.unpack_from("<Q", self.mm, off)[0]:
                    continue
            except (ValueError, struct.error):
                self.mm = None
                return
            if t_ns == 0 or now - t_ns / 1e9 > self.STALE_S:
                continue
            t = t_ns / 1e9
            prev = self.prev.get(vnic)
            self.prev[vnic] = (t, tx_ib, tx_eth)
            if not prev or t - prev[0] <= 0:
                continue
            dt = t - prev[0]
            if tx_ib >= prev[1]:
                self.rate["%s|rdma" % vnic] = (tx_ib - prev[1]) * 8 / dt
            if tx_eth >= prev[2]:
                self.rate["%s|tcp" % vnic] = (tx_eth - prev[2]) * 8 / dt
        if not self.rate:
            return
        self._parts(now)
        fr = {}
        for f in self.flows:
            if f not in self._fs:
                self._fs[f] = self._describe(f)
            d = self._fs[f]
            if d is None:
                continue
            tot = self.rate.get("%s|%s" % (d[0], d[2]))
            if tot is None:
                continue
            fresh = now - self.part_t[d[2]] <= self.PART_STALE_S
            fr[f] = tot * (self.part.get(f, 1.0) if fresh else 1.0)
        self.fs_rate = fr
        base = {k: int(v) for k, v in self.rate.items()}
        t = time.monotonic_ns()
        for h, addr in self.addr_of_host.items():
            r = dict(base)
            for f, v in fr.items():
                if self._fs[f][1] == h:
                    r[f] = int(v)
            try:
                self.sock.sendto(json.dumps({"h": self.host, "t": t, "r": r}).encode(), addr)
            except OSError:
                pass

    def held_fraction(self, fsid):
        """part of the flow set's bytes its executor sent at the pace, over
        the last window in which it sent anything; None where the executor
        does not count it (TCP) or before the first such window"""
        return self.held.get(fsid)

    def own_rate(self, fsid):
        """the flow set's own send rate, bps, or None before the first sample"""
        v = self.fs_rate.get(fsid)
        if v is not None:
            return v
        return self.rate.get("%s|%s" % (fsid.split(">")[0], fsid.rsplit("|", 1)[1]))

    def _describe(self, fsid):
        """(src vnic, dst host, class, counter key) of a local flow set"""
        try:
            src_dst, cls = fsid.rsplit("|", 1)
            src, dst = src_dst.split(">")
        except ValueError:
            return None
        if src not in self.vport_of_vnic or cls not in ("rdma", "tcp"):
            return None
        if cls == "rdma":
            key = self.flowtag_of(src_dst, src) if self.flowtag_of else None
        else:
            key = src_dst
        return (src, dst.split("/")[0], cls, key)

    def _read_txc(self, now):
        """(t, {set id: bytes/32}) from pcc_host's running totals, or None"""
        if self.txc is None:
            if now < self._txc_next_open:
                return None
            self._txc_next_open = now + 1.0
            try:
                f = open(self.TXC_PATH, "rb")
                self.txc = mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ)
                f.close()
            except (OSError, ValueError):
                self.txc = None
                return None
        try:
            for _ in range(3):
                magic, s1, t_ns, n, _pad = self.TXC.unpack_from(self.txc, 0)
                if magic != b"HPFTRPT1":
                    break
                if s1 & 1:
                    continue
                ents, held = {}, {}
                for k in range(min(n, 64)):
                    sid, h32, b = struct.unpack_from("<IIQ", self.txc, 32 + 16 * k)
                    ents[sid] = b
                    held[sid] = h32
                if struct.unpack_from("<Q", self.txc, 8)[0] == s1:
                    if now - t_ns / 1e9 > self.PART_STALE_S:
                        # an executor that has stopped, or a file that has
                        # been replaced under this map: open it afresh
                        break
                    return t_ns / 1e9, ents, held
        except (ValueError, struct.error):
            pass
        self.txc = None
        return None

    def _parts(self, now):
        """Each local flow set's part of its VF's class total, from the
        bytes its executor counted since the window began. A VF with one
        flow set of the class, or with no counts in the window, gets no
        entry and so reports its whole total."""
        snaps = {"rdma": self._read_txc(now)}
        tx_t = self.shim.tx_t if self.shim is not None else 0.0
        snaps["tcp"] = (tx_t, self.shim.tx) if tx_t and now - tx_t <= self.PART_STALE_S else None
        for cls, snap in snaps.items():
            if snap is None:
                continue
            w = self.win[cls]
            if w is None or any(snap[1].get(k, 0) < v for k, v in w[1].items()):
                self.win[cls] = snap          # first sample, or the counts restarted
                continue
            if snap[0] - w[0] < self.PART_WIN_S:
                continue
            by_src = {}
            for f in self.flows:
                d = self._fs.get(f)
                if d is None or d[2] != cls:
                    continue
                by_src.setdefault(d[0], []).append((f, snap[1].get(d[3], 0) - w[1].get(d[3], 0)))
            for members in by_src.values():
                tot = sum(b for _, b in members)
                for f, b in members:
                    if len(members) > 1 and tot > 0:
                        self.part[f] = b / tot
                    else:
                        self.part.pop(f, None)
            if len(snap) > 2 and len(w) > 2:
                # the executor's held bytes over the same window: only a
                # window in which the flow set sent anything says anything
                # about whether it was held, so an idle one leaves it be
                for f in self.flows:
                    d = self._fs.get(f)
                    if d is None or d[2] != cls:
                        continue
                    db = snap[1].get(d[3], 0) - w[1].get(d[3], 0)
                    if db > 0:
                        dh = (snap[2].get(d[3], 0) - w[2].get(d[3], 0)) & 0xffffffff
                        self.held[f] = min(1.0, dh / db)
            self.win[cls] = snap
            self.part_t[cls] = now

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", default="/opt/hpft/lab-registry.json")
    ap.add_argument("--local-host", default=None,
                    help="host whose vnics we pace (default: registry sender_host)")
    ap.add_argument("--log", default="/tmp/hpft_txagent_e.jsonl")
    args = ap.parse_args()

    reg = json.load(open(args.registry))
    if not fastfill.USING_C:
        sys.exit("tx_agent_e: libfastfill.so is missing next to fastfill.py "
                 "(deploy_check.sh --deploy builds it); refusing to run")
    ep = reg["e_params"]
    ctl = reg["control"]
    local_host = args.local_host or reg["sender_host"]
    line = reg["line_rate_bps"]
    period = ep["period_ms"] / 1e3
    # k (s^-1) is the rate constant of the fail-open tracking step only
    # (design v4 5.6: after n2_failopen_s R tracks the local tree in log
    # space with time constant 1/k). It is applied over the MEASURED
    # interval since the flow-set's last step, clamped: below one period
    # it is measurement noise, above dt_max the target is stale enough that
    # the smooth approach is preferred to one large jump.
    k = ep["k"]
    dt_min, dt_max = period, 10.0 * period
    n1_s = ep["n1_freeze_s"]
    n2_s = ep["n2_failopen_s"]
    floor = ctl["pace_floor_bps"]
    # TCP rate heartbeat to the host shim. The RDMA side re-flushes its
    # budget every rdma_push_ms regardless of change; this is the same
    # idea for the UDP hop to the EDT maps. Not a control-loop parameter -
    # the law is unaffected - purely delivery robustness, so it can be
    # slow relative to the 1 ms period.
    tcp_refresh_s = ep.get("tcp_refresh_ms", 100) / 1e3
    # Forget a flow-set the receiver has stopped
    # reporting for this long. Without it the table only ever grows -
    # every flow-set ever seen stays in fail-open forever, ticking,
    # actuating and writing budgets.
    evict_s = ep.get("n3_evict_s", 30)
    # §5.1 transition-process clause: the contract binds the TRANSITION,
    # not only the steady state - a rate change violent enough that the
    # controlled transport reads it as a fault is a dropped packet in
    # effect. The law's own output already satisfies this (the target
    # moves with a 1/k time constant, so consecutive budget flushes
    # differ by a few percent). What needs limiting is an externally
    # injected STEP, and the path it takes is Tree_f: pace = min(R, Tree)
    # and Tree does NOT go through the tracking law, so an operator
    # dropping a MaxRate by 20x reaches the executor in one write.
    # Measured: that step risks a terminal RDMA connection failure (1 in
    # 3), while steady operation at the same low rate is entirely safe -
    # the danger is the RATIO OF THE FALL, not the destination.
    #
    # Limited as a halving per interval rather than a fixed slope, so it
    # is scale-free like everything else here; the interval is one budget
    # flush.
    #
    # Why this can never become the convergence bottleneck, and the
    # argument is structural rather than a rate comparison. (A rate
    # comparison would be wrong: the law's INSTANTANEOUS descent is k
    # times the log gap, not k, so against a 20x target step it runs at
    # 20*ln20 = 60 s^-1, faster than this limiter's ln2/0.013 = 53.) The
    # real reason is min(): the limiter only ever holds Tree ABOVE what it
    # would otherwise be, and pace = min(R, Tree), so a lagging Tree can
    # only be the un-selected side. Whatever descent the law commands
    # through R passes through untouched. A limiter placed on the budget
    # instead would sit DOWNSTREAM of the min, and could throttle the law
    # - which is why this one is on Tree.
    tree_halve_s = ep.get("tree_step_halving_s", 0.013)
    tree_applied = {}      # fsid -> Tree_f as actually applied
    tree_ts = time.monotonic()

    def track(st, target, now):
        """track_step bound to one flow-set's wall-clock anchor."""
        dt = min(max(now - st.last_step, dt_min), dt_max)
        st.last_step = now
        st.R = track_step(st.R, target, dt, k, floor)

    # design v4 (per-feedback, dimensionless): probe alpha, brake kappa,
    # repayment horizon D in feedbacks, fence cap = mult x own send rate
    # probe (§5.2/§5.3): step = alpha * min(m, m_max) per feedback, m =
    # consecutive empty-queue feedbacks. alpha = alpha_max / m_max, where
    # alpha_max is the overshoot budget (alpha_max * tau <= 10%) and m_max
    # is how long a silence has to be before the share is believed to have
    # moved. alpha has units of "per feedback squared" - it is the growth
    # rate of the step, not the step - so it scales with the SQUARE of the
    # period, while kappa scales linearly.
    v4_alpha = float(ep.get("alpha", 3e-4))
    v4_mmax = float(ep.get("m_max", 100))
    v4_kappa = float(ep.get("kappa", 0.1))
    v4_D = float(ep.get("D", 30))
    # The form of the step. "linear" (the default, design v4 5.2) applies the
    # exponent as 1 + x, bounded to a half; "exp" applies it as e^x, which is
    # the same to first order (measured: the two factors differ by under 0.1%
    # in 99% of feedbacks, and three-run A/Bs on V1, V2 and V7 were
    # indistinguishable) and is kept selectable for reference.
    v4_linear = str(ep.get("step_form", "linear")).lower() != "exp"
    print("tx_agent_e: v4 step form = %s" % ("linear (1+am, 1-x with |x|<=1/2)" if v4_linear else "exp"), flush=True)
    tree_theta = float(ep.get("tree_backlog_theta", 0.85))
    tree_period_s = float(ep.get("tree_period_ms", 100)) / 1e3
    # 5.3: the probe may not pass twice the flow-set's own send rate; the 2
    # is the meaning of the rule (how much an application may amplify on
    # its return), not a tuning object. The send rate is averaged over the
    # observation window the receiver's decisions use.
    v4_cap_mult = 2.0
    as_avg_s = float(ep.get("decision_avg_s", 0.1))
    # 5.3's cap is there for a flow set its application keeps below the
    # fence: its queue never appears, so the probe would never stop. A flow
    # set whose executor sends most of its bytes AT the pace is not that
    # flow set - it has more to send than it is let out - and capping it at
    # twice its own average pins a bursty one (request-response, 2-6) at
    # the start value. held_min is the part of its bytes that must have
    # gone out at the pace for the cap to be lifted.
    held_min = float(ep.get("held_min", 0.5))
    # §5.4: a flow-set starts at the port's headroom h*C - the capacity the
    # receiver keeps free for transients - so a newcomer can never push the
    # port past line rate even when everyone else is at their share, and it
    # starts ABOVE any realistic share, so the ledger brings it down in ~D
    # periods (the damped closed-loop regime) instead of the fence climbing
    # from a floor at alpha_max (the open-loop regime, an order of magnitude
    # slower over the same distance). The probe ceiling never sits below
    # the start value. No absolute number: h and C are policy/platform.
    v4_start = float(ep["headroom"]) * float(line)
    # Absolute floor of the fence: the lowest rate the executor shapes
    # correctly (platform quantity, §7) - below it the RDMA executor
    # miscounts QPs and releases several times the budget when it rises.
    v4_floor = max(floor, float(ep.get("r_floor_bps", 1e9)))

    def step_law(st, rec, now, fsid):
        """Design v4 §5, one feedback: q (periods) and the flow-set's own
        send rate (for the probe cap) in, a new R out."""
        q = rec.get("d", 0.0) / period          # seconds -> periods
        a_s = live.own_rate(fsid) if live is not None else None
        if a_s is not None:
            st.as_avg += (float(a_s) - st.as_avg) * min(1.0, (now - st.last_step) / as_avg_s) if st.as_avg > 0 else float(a_s) - st.as_avg
        hf = live.held_fraction(fsid) if live is not None else None
        if hf is not None:
            st.held_avg += (hf - st.held_avg) * min(1.0, (now - st.last_step) / as_avg_s)
        # 5.3: the probe may not climb into rates the flow-set could
        # never use. pace = min(R, Tree), so a fence above the local
        # tree buys nothing and only lengthens the fall when the share
        # drops - measured on V2, the incumbents sat at 2 x their send
        # rate (46 G) while the tree held them at 23 G, so a halving of
        # the share meant falling a factor of four instead of two.
        # The margin matters: 5.6 takes R as a pressing flow-set's
        # demand on the tree, so capping R AT the tree would make
        # demand == tree, the fill would sit at its own fixed point and
        # the tree could never grow again. One delta of headroom keeps
        # the demand above the tree and the growth path open.
        cap = line if a_s is None else max(v4_cap_mult * st.as_avg, v4_start)
        if st.held_avg >= held_min:
            cap = line      # backlogged at its pace: not application-limited
        cap = min(cap, tree_of(fsid) * (1.0 + ep["delta_demand"]))
        if st.mode == "fresh":
            # §5.4: start at the port's headroom (bounded by the local
            # tree), not at a floor. The silence starts at its cap, not
            # at zero: a flow-set that has never seen a queue has no
            # evidence at all about where its share is, which is the
            # maximum-uncertainty state and the one case where the
            # largest step is the right one.
            st.R = min(v4_start, tree_of(fsid), line)
            st.below = int(v4_mmax)
        # The fence has taken hold once the flow-set's OWN measured send
        # rate is within the fence. No measurement is not evidence of
        # anything: latching on a_s == None (as this once did) declared
        # the fence in force before the first sample and disabled the
        # adoption rule below for the flow-set's whole life.
        if not st.enforced and a_s is not None and float(a_s) \
                <= max(st.R, v4_floor) * (1.0 + ep["delta_demand"]):
            st.enforced = True          # latched: the fence has taken hold
        # An empty ledger is a statement by the receiver that this
        # flow-set did NOT exceed its entitlement, so the rate it is
        # already putting on the wire is one the receiver has just
        # certified. The fence may adopt it at once; probing up to a
        # rate that has already been sent and accounted for is work the
        # loop does not need to do. In the steady state the flow is
        # held at its fence, so this is a no-op; it fires only where
        # the wire is ahead of the fence, which is exactly a flow-set
        # whose executor admitted it at a startup allowance before any
        # budget arrived - and that is the 810 ms the newcomer used to
        # spend climbing from the floor at the maximum step.
        if q <= 0.0 and not st.enforced and a_s is not None:
            st.R = max(st.R, min(float(a_s), cap, line))
        st.R, st.dq, st.below = v4_step(st.R, q, st.q_prev, st.below,
                                        v4_alpha, v4_mmax, v4_kappa, v4_D,
                                        v4_floor, min(cap, line),
                                        not st.enforced, v4_linear)
        st.q_prev = q
        st.last_step = now
    shim = PaceShim(ctl["pace_shim"][local_host])
    # sender-liveness feed (advisory, see SenderLiveness): tells the receiver
    # which senders are actually sending, at vport freshness (~1 ms), so its
    # attribution does not have to wait out the ~1 s megaflow cache when a
    # sender stops. Absent/stale feed => receiver keeps its old chain.
    live = None
    _live_ips = {h: ip for h, ip in ctl.get("telemetry_ip", {}).items()
                 if h != local_host}
    if _live_ips:
        _vport_of_vnic = {}
        for v in reg["vnics"]:
            if v["host"] == local_host and v.get("representor"):
                _m = re.search(r"vf(\d+)$", v["representor"])
                if _m:
                    _vport_of_vnic[v["vnic_id"]] = int(_m.group(1)) + 1
        if _vport_of_vnic:
            live = SenderLiveness("/dev/shm/hpft_vpm", _vport_of_vnic,
                                  _live_ips, int(ep.get("liveness_port", 9713)),
                                  local_host)
    mailbox = RpMailbox(line)
    # What an RDMA flow the executor has no budget for yet may send
    # (mailbox 0xccf); without it a joiner runs at line rate until its
    # first budget lands. This is the SAME question 5.4 answers for the
    # fence - what may a flow-set use before anything is known about it -
    # so making it take the same answer - the fence's floor - was tried
    # (leave the key null and it still does). It is WORSE: measured on V2,
    # 2026-08-30, the join event went from 1168 to 1473 ms on RDMA and 922
    # to 1823 ms on TCP, while the transient it was meant to remove barely
    # moved (queue peak 50 -> 45 ms, rate peak 29.8 -> 26.0 G). The reason
    # is that the peak is not the joiner bursting at all: it is the
    # INCUMBENT holding the joiner's unused share while the joiner ramps
    # (4.3 lending), so throttling the joiner lengthens the very lending it
    # was supposed to shorten. The allowance is kept above the floor
    # deliberately: a joiner that is already on the wire and drawing no
    # queue is a joiner whose fence can adopt that rate at once (5.2), and
    # that is what makes the join fast.
    # §5.4: before its first budget a flow-set is admitted at the start
    # value R0 = h*C. The RDMA executor shapes per QP and does not yet know
    # which flow-set an unknown QP belongs to, so the per-QP allowance is R0
    # divided by the QPs a flow-set is expected to open (a deployment
    # quantity from the registry).
    unknown_bps = v4_start / float(ep.get("rdma_qps_per_flowset", 4))
    _unk_units = mailbox._units(unknown_bps)
    _unk_sent = 0.0

    def push_unknown_rate():
        # 0xcce <units> 21 on the per-QP executor (0xccf was the old one)
        mailbox._fifo_write("0xcce %d 21\n" % _unk_units)
    flowtags = {v["vnic_id"]: int(v["flowtag"], 16)
                for v in reg["vnics"] if "flowtag" in v}
    # per-(src,dst) flowtags for cross-pair RDMA: the tag is a stable hash of
    # (src function, dst function) - NOT per-src - so a src VF talking to
    # several dsts needs one budget entry per pair (registry rdma_flowtags,
    # probed via 0xdea 2026-07-11). Falls back to the per-src tag, which keeps
    # the four straight pairs byte-identical to the old behaviour.
    pair_ft = {key: int(v, 16)
               for key, v in reg.get("rdma_flowtags", {}).items()
               if ">" in key}
    # The tag does not depend on the SOURCE card (registry rdma_flowtags
    # comment: sgpu03/sgpu04 return sgpu01's tags for every pair to sgpu02,
    # checked 2026-08-23) but it DOES depend on the destination: vf0>vf0 is
    # 0x74249a41 towards sgpu02 and 0x1e3dac89 towards sgpu04 (probed via
    # 0xdea, 2026-09-04). So a pair with no row of its own takes the row of
    # any other source with the same source VF index and the same
    # destination vnic; tools/lab-infra/flowtag_probe.sh measures the rows.
    def _vf_idx(vnic):
        m = re.search(r"vf(\d+)$", vnic)
        return int(m.group(1)) if m else None
    idx_ft = {}
    for key, ft in pair_ft.items():
        s_, d_ = key.split(">")
        idx_ft.setdefault((_vf_idx(s_), d_), ft)

    def flowtag_of(src_dst, src):
        ft = pair_ft.get(src_dst)
        if ft is None:
            s_, d_ = src_dst.split(">")
            ft = idx_ft.get((_vf_idx(s_), d_))
        if ft is None:
            ft = flowtags.get(src)
        if ft is None:
            # A pair the registry has no tag for (vf4..vf7 on the hosts
            # other than sgpu01, 2026-09-08: V3's new tenants ran unbound
            # for the whole run because this returned None and no budget
            # or binding was ever pushed). The id is only a name shared
            # by the budget push and the binding push, so any stable
            # non-zero 32-bit value of the pair string will do.
            ft = (zlib.crc32(src_dst.encode()) & 0xffffffff) | 1
        return ft
    # ---- {qpn -> flow set} from the host-side qpn resolver ----------
    # The resolver sends "<src_ip> <lqpn> <dst_ip>" lines to this agent's
    # telemetry socket. Telemetry itself is binary, so the two are told
    # apart by looking at the bytes.
    ip2vnic = {v["ip"]: v["vnic_id"] for v in reg["vnics"] if v.get("ip")}
    # vnic -> vhca_id, asked of the firmware once at startup (vhca_of, the
    # DPU is the eswitch manager; a host VF cannot answer this about
    # itself). Host pf<P>vf<N> is eswitch vport N+1 on this platform.
    vf_vhca = {}
    _vh_dev = ep.get("dpu_rdma_dev", "mlx5_1")
    _vh_vport = {}
    for v in reg["vnics"]:
        if v["host"] == local_host and v.get("representor"):
            _m = re.search(r"vf(\d+)$", v["representor"])
            if _m:
                _vh_vport[v["vnic_id"]] = int(_m.group(1)) + 1
    if _vh_vport:
        try:
            _out = __import__("subprocess").run(
                ["/opt/hpft/vhca_of", _vh_dev,
                 ",".join(str(p) for p in sorted(set(_vh_vport.values())))],
                capture_output=True, text=True, timeout=10).stdout
            _vp_vh = {}
            for _ln in _out.split("\n"):
                _f = _ln.split()
                if len(_f) == 2 and _f[1].isdigit():
                    _vp_vh[int(_f[0])] = int(_f[1])
            vf_vhca = {vn: _vp_vh[vp] for vn, vp in _vh_vport.items()
                       if vp in _vp_vh}
        except Exception as e:  # noqa: BLE001
            print("tx_agent_e: vhca_of failed (%s)" % e, flush=True)
    if len(vf_vhca) == len(_vh_vport):
        print("tx_agent_e: vhca %s" % " ".join(
            "%s=%d" % (k.split("/")[-1], v) for k, v in sorted(vf_vhca.items())),
            flush=True)
    else:
        print("tx_agent_e: NO vhca table (%d/%d) - QP bindings keyed by bare "
              "qpn, two VFs holding the same QP number will be confused"
              % (len(vf_vhca), len(_vh_vport)), flush=True)
    qp_set = {}      # binding key -> set id
    qp_sent = {}     # binding key -> set id already pushed to the executor

    def _is_qpmap(buf):
        if not buf or len(buf) > 60000:
            return False
        return all(c in b"0123456789. \n" for c in buf[:64])

    def _take_qpmap(buf, _flowtag_of=None):
        # Each resolver datagram is the COMPLETE current table (every
        # local RC QP with a known peer), so the binding table is rebuilt
        # from it: a QP that is gone leaves the table, instead of every
        # QP ever seen staying in it for the life of the agent.
        try:
            text = buf.decode("ascii", "ignore")
        except Exception:
            return
        seen = {}
        for ln in text.split("\n"):
            f = ln.split()
            if len(f) != 3:
                continue
            sv, dv = ip2vnic.get(f[0]), ip2vnic.get(f[2])
            if not sv or not dv:
                continue
            try:
                qpn = int(f[1])
            except ValueError:
                continue
            ft = _flowtag_of("%s>%s" % (sv, dv), sv)
            if ft is not None:
                vh = vf_vhca.get(sv)
                key = ((vh & 0xff) << 24) | (qpn & 0xffffff) if vh is not None else qpn
                seen[key] = ft
        # Bind on the QP number alone wherever the number names one flow set
        # on this host. The executor keys a QP by (vhca_id, qpn) and reads
        # the vhca_id out of the event, and that read is not always right:
        # dumps from three V2 runs carry records pairing one VF's vhca_id
        # with another VF's QP number, and the QP that owns the number is
        # then left with no record, no set and no share. The number itself
        # always reads correctly, so a second entry keyed on it alone lets
        # such a record still find its set (the executor already falls back
        # to the bare number). A number held by two VFs whose flow sets
        # differ gets set 0 instead, which the executor treats as no answer:
        # there the pair is the only thing that can tell them apart.
        by_qpn = {}
        for k_, ft_ in seen.items():
            by_qpn.setdefault(k_ & 0xffffff, set()).add(ft_)
        for qpn_, fts_ in by_qpn.items():
            seen[qpn_] = fts_.pop() if len(fts_) == 1 else 0
        qp_set.clear()
        qp_set.update(seen)
        for k_ in [k_ for k_ in qp_sent if k_ not in seen]:
            del qp_sent[k_]
        _publish_qpmap()

    # The agent's side of the binding evidence. The executor's own dump
    # (rp_sample.dump_qps) says which QP records it holds and which of them
    # found a set; this file says which QPs the agent believes in, so a QP
    # that binds to nothing can be placed on one side of the push or the
    # other in a single artefact taken at one instant.
    _qpmap_last = [None]

    def _publish_qpmap():
        cur = sorted((k >> 24, k & 0xffffff, sid, qp_sent.get(k) == sid)
                     for k, sid in qp_set.items())
        if cur == _qpmap_last[0]:
            return
        _qpmap_last[0] = cur
        try:
            with open("/tmp/hpft_txagent_qpmap.txt.new", "w") as f:
                f.write("agent qpmap n=%d ts=%.3f\n" % (len(cur), time.time()))
                for vh, qpn, sid, sent in cur:
                    f.write("vhca %d qpn 0x%x set 0x%x %s\n"
                            % (vh, qpn, sid, "pushed" if sent else "NOT-PUSHED"))
            os.replace("/tmp/hpft_txagent_qpmap.txt.new",
                       "/tmp/hpft_txagent_qpmap.txt")
        except OSError:
            pass

    stree = SenderTree(reg["policy"], line, ep["headroom"],
                       ep["delta_demand"], floor, tree_theta)
    trees = {}   # fsid -> Tree_f, recomputed on tree_period_s (5.6)
    tree_recomputed = 0.0
    tree_keys = frozenset()

    # §4.3 layer one. The hardware cap is programmed from the same policy
    # that feeds the tree, so the two layers can never be configured apart,
    # and it is reasserted on every reload because a firmware reset or a
    # hand-run devlink command silently drops it.
    hw = hw_maxrate.HwMaxRate(local_host, reg["vnics"])
    hw.discover()

    def sync_hw(why):
        if hw.error:
            # Layer two still enforces the same numbers; what is lost is
            # the guarantee that it keeps enforcing them if this agent
            # stops. Worth a loud line, not worth refusing to run.
            print("tx_agent_e: hardware MaxRate UNAVAILABLE (%s) - selling "
                  "principle is software-only this run" % hw.error, flush=True)
            return
        changed, errors = hw.sync(hw_maxrate.caps_from_policy(
            stree.policy, reg["vnics"], local_host))
        for v, bps in sorted(changed.items()):
            print("tx_agent_e: hw MaxRate %s -> %.2fG (%s)"
                  % (v, bps / 1e9, why), flush=True)
        for v, e in sorted(errors.items()):
            print("tx_agent_e: hw MaxRate %s FAILED: %s" % (v, e), flush=True)

    sync_hw("startup")

    # Policy hot-reload, the sender's counterpart to the receiver's. Both
    # layers must move together: reloading only the tree would leave the
    # hardware enforcing the old allowance (and a raise would not take
    # effect at all), while reloading only the hardware would leave the
    # tree handing out shares the NIC refuses to send.
    reg_mtime = os.stat(args.registry).st_mtime
    reload_period_s = 0.2
    last_reload_chk = time.monotonic()

    def maybe_reload(now_):
        nonlocal reg_mtime, last_reload_chk
        if now_ - last_reload_chk < reload_period_s:
            return
        last_reload_chk = now_
        try:
            mt = os.stat(args.registry).st_mtime
            if mt == reg_mtime:
                return
            reg_mtime = mt
            stree.policy = json.load(open(args.registry))["policy"]
            print("tx_agent_e: policy reloaded", flush=True)
            sync_hw("reload")
        except (OSError, ValueError) as e:
            print("tx_agent_e: policy reload failed: %s" % e, flush=True)

    def tree_of(f):
        # the tree is recomputed the moment a flow-set appears, so the last
        # fallback (the whole uplink share) is only ever read before that
        return tree_applied.get(f, trees.get(f, stree.cap))

    def apply_trees(now_):
        """Let Tree_f rise freely, limit how fast it may fall."""
        nonlocal tree_ts
        dt_ = max(now_ - tree_ts, 0.0)
        tree_ts = now_
        floor_ratio = 2.0 ** (-dt_ / tree_halve_s) if tree_halve_s > 0 else 0.0
        for f, want in trees.items():
            prev = tree_applied.get(f)
            if prev is None or want >= prev:
                tree_applied[f] = want          # ascent is immediate
            else:
                tree_applied[f] = max(want, prev * floor_ratio)
        for f in [f for f in tree_applied if f not in trees]:
            del tree_applied[f]

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", ep["telemetry_port"]))
    sock.settimeout(period)

    flows = {}   # fsid -> FlowState
    if live is not None:
        live.flows, live.flowtag_of, live.shim = flows, flowtag_of, shim
    last_any_rx = time.monotonic()   # last telemetry from ANY flow-set
    logf = open(args.log, "a", buffering=1)
    print("tx_agent_e: local=%s T=%.0fms kappa=%.3f D=%.0f alpha=%.1e m_max=%.0f "
          "failopen=%.2f/%.1fs (k=%.1f/s) evict=%.0fs shim=%s log=%s"
          % (local_host, period * 1e3, v4_kappa, v4_D, v4_alpha, v4_mmax,
             n1_s, n2_s, k, evict_s, ctl["pace_shim"][local_host], args.log),
          flush=True)

    def actuate(fsid, st, r_bps, rdma_batch):
        # design v4 §5.5: min(R, U), never below the platform minimum rate
        pace = max(min(st.R, tree_of(fsid)), v4_floor)
        src_dst, cls = fsid.rsplit("|", 1)
        src, dst = src_dst.split(">")
        tnow = time.monotonic()
        if cls == "tcp":
            # Re-send on a heartbeat, not only on change. The hop to the
            # shim is fire-and-forget UDP with no ack path back into the
            # law, and the EDT pair_cfg map is recreated by any re-apply
            # of the actuator - after which a pair stays UNPACED until the
            # law happens to move a rate. The RDMA path has always had
            # this property (it reflushes every rdma_push_ms even when the
            # budget has not changed); the TCP path did not.
            if abs(pace - st.pace) > 0.005 * max(st.pace, 1.0) or tnow - st.shim_last >= tcp_refresh_s:
                shim.set_rate(src, dst, pace)
                st.shim_last = tnow
        elif cls == "rdma":
            ft = flowtag_of(src_dst, src)
            if ft is not None:
                # the flow set's rate R, keyed by the pair's registry
                # flowtag reused as the set id; the executor splits it over
                # the set's QPs by their own CC allowances (section 6)
                rdma_batch.append((ft, pace))
        st.pace = pace
        # executor-escape tripwire (log-only). Every stress-D1 failure class
        # was a silent one: an unpaced EDT pair, an unmatched flowtag and a
        # wedged RP all kept every layer reporting healthy while the wire
        # ignored the pace. r persistently above pace is the one signal the
        # control plane can already see.
        #
        # The threshold is per class, and neither class reads 1.00 when
        # healthy: r is the receiver's WIRE byte count while the pace is
        # applied to skb bytes, so framing (L2/L3/L4 headers, preamble,
        # IPG) shows up as inflation. Calibrated 2026-07-27: a TCP pair
        # paced at 10G delivers 9.79G of iperf3 goodput (cap enforced to
        # 2%) and reads 10.6G on the wire, i.e. 1.08x. RDMA additionally
        # carries RC retransmits (~1.3x observed).
        # 1.25 for TCP sits clear of the 1.08 baseline and would still
        # have caught the 2026-07-27 incast escape (1.46x), which the
        # single old 1.5x threshold let run for 85 s unseen.
        esc_ratio = 1.5 if cls == "rdma" else 1.25
        if r_bps > esc_ratio * pace:
            if st.esc_since == 0.0:
                st.esc_since = tnow
            elif tnow - st.esc_since >= 1.0 and tnow - st.esc_log >= 5.0:
                print("pace-escape %s r=%.2fG pace=%.2fG (%.2fx, trip %.2fx) "
                      "dur=%.0fs" % (fsid, r_bps / 1e9, pace / 1e9,
                                     r_bps / max(pace, 1.0), esc_ratio,
                                     tnow - st.esc_since), flush=True)
                st.esc_log = tnow
        else:
            if st.esc_log > 0.0:
                print("pace-escape clear %s r=%.2fG pace=%.2fG"
                      % (fsid, r_bps / 1e9, pace / 1e9), flush=True)
            st.esc_since = st.esc_log = 0.0

    last_print = time.monotonic()
    last_ticker = time.monotonic()
    # RDMA FIFO write coalescing: the mailbox behind the FIFO absorbs only
    # ~75 batches/s (13ms each, a firmware floor), so at a 1ms control period
    # we must NOT write the FIFO every tick or it backs up with stale
    # budgets. Instead keep the latest budget per flowtag and flush the
    # freshest snapshot at the mailbox rate. The RDMA congestion response
    # stays event-speed on the DPA; only the policy target is rate-limited.
    latest_rdma = {}    # set id -> R (bps)
    rdma_push_s = ep.get("rdma_push_ms", 13) / 1e3
    last_rdma_push = time.monotonic()
    # Per-datagram cost histogram, the sender's counterpart to the
    # receiver's. It matters MORE here: tx was measured to be the tighter
    # of the two agents, rebuilding its whole sender tree on every
    # telemetry datagram. Averages hide the tail and the tail is what
    # turns a late decision into a missed one.
    proc_us = []
    proc_report = time.monotonic()
    tele_skipped = 0
    sock_timeout = sock.gettimeout()
    while True:
        # --- telemetry-driven law ---
        try:
            data, _ = sock.recvfrom(65536)
            if _is_qpmap(data):
                _take_qpmap(data, flowtag_of)
                data = None
            # LATEST WINS: the receiver sends one datagram per period and
            # this loop costs more than a period under load (24 flow-sets:
            # tree rebuild, 8 law steps, shim writes, log lines), so the
            # socket buffer filled and every decision was taken on a
            # datagram ~130 ms old (V1, 2026-08-28) - a loop delay the law
            # cannot tolerate. q and b are levels, so skipping stale
            # datagrams loses nothing; only the newest one is processed.
            sock.setblocking(False)
            try:
                while True:
                    more, _ = sock.recvfrom(65536)
                    if _is_qpmap(more):
                        _take_qpmap(more, flowtag_of)
                        continue
                    if data is not None:
                        tele_skipped += 1
                    data = more
            except (BlockingIOError, socket.timeout, OSError):
                pass
            finally:
                sock.settimeout(sock_timeout)
            seq, recs_all = parse_telemetry(data) if data is not None else (None, {})
        except socket.timeout:
            seq, recs_all = None, {}
        except OSError as e:
            print("tx_agent_e: bad telemetry: %s" % e, flush=True)
            seq, recs_all = None, {}
        now = time.monotonic()
        if live is not None:
            live.tick(now)
        rdma_batch = []
        if recs_all:
            last_any_rx = now
            recs = {f: rec for f, rec in recs_all.items()
                    if f.split(">")[0].startswith(local_host + "/")}
            for fsid, rec in recs.items():
                st = flows.get(fsid)
                if st is None:
                    st = flows[fsid] = FlowState(now)
                st.r = rec.get("r", 0)
            _t0 = time.monotonic()
            # 5.6: the tree is an upper bound, not a second controller, so
            # it is rebuilt on its own period rather than on every
            # telemetry datagram - a cap that moves as fast as the thing
            # it caps is not a cap. A change in WHICH flow-sets exist is
            # a structural change and is taken at once.
            keys_now = frozenset(flows)
            if (now - tree_recomputed >= tree_period_s
                    or keys_now != tree_keys or not trees):
                trees = stree.trees(flows)
                tree_recomputed, tree_keys = now, keys_now
            apply_trees(now)                # §5.5 transition limiting
            tree_us = int((time.monotonic() - _t0) * 1e6)
            for fsid, rec in recs.items():
                st = flows[fsid]
                st.last_rx = now
                st.last_seq = seq if seq is not None else -1
                r = rec.get("r", 0)
                step_law(st, rec, now, fsid)
                st.mode = "track"
                actuate(fsid, st, r, rdma_batch)
                # throttled logging: on change (>1% R move or mode flip)
                # plus a 1 s heartbeat - full-rate logging wrote ~20
                # lines/s/fs (340/s at 20 fs)
                st.log_age += 1
                changed = (st.mode != st.log_mode or st.log_R < 0
                           or abs(st.R - st.log_R) > 0.01 * st.log_R)
                if changed or st.log_age >= 20:
                    st.log_R = st.R
                    st.log_age = 0
                    st.log_mode = st.mode
                    logf.write(json.dumps(
                        {"ts": round(time.time(), 4), "fs": fsid,
                         "seq": st.last_seq, "r": r,
                         "d": round(rec.get("d", 0.0) * 1e3, 3),
                         "q": round(st.q_prev, 3), "dq": round(st.dq, 4),
                         "As": int(live.own_rate(fsid) or 0) if live is not None else -1,
                         "Hd": round(st.held_avg, 3),
                         "R": int(st.R), "pace": int(st.pace),
                         "tree": int(tree_of(fsid)), "tus": tree_us,
                         "mode": st.mode}) + "\n")
        # --- local ticker: fail-open (design_v4.md §5.5) ---
        # No telemetry for n1_freeze_s: R frozen (the law needs a fresh
        # permit to move at all). Still nothing after n2_failopen_s: the
        # SAME tracking step, with the target set to the sender-local
        # allowance Tree_f - the flow degrades to sender-side policy only,
        # neither wedged nor uncontrolled. Using the tracking step here
        # rather than a fixed additive ramp means no separate ramp constant
        # to tune, and the approach is asymptotic, so a fail-open flow can
        # never overshoot Tree_f the way a linear ramp plus min() clamp
        # could.
        if now - last_ticker >= period:
            last_ticker = now
            maybe_reload(now)
            mailbox.ensure_open()
            # re-assert the unknown-flow allowance every 5 s (an RP restart
            # clears it; the write is one short line)
            if now - _unk_sent >= 5.0:
                _unk_sent = now
                push_unknown_rate()
            # An executor restart (rp_service.sh start, done before every
            # experiment) empties the device: every QP binding we ever
            # pushed is gone, and the diff below would never push them
            # again for a QP number that comes back. So forget what was
            # sent and let the next push carry the whole table.
            if mailbox.restarted:
                mailbox.restarted = False
                qp_sent.clear()
                push_unknown_rate()
                print("tx_agent_e: executor restarted - re-sending %d QP "
                      "bindings" % len(qp_set), flush=True)
            # Eviction, and why it is gated on telemetry being alive: a
            # flow-set going quiet and the telemetry channel dying look
            # identical from one flow-set's record stream. They are told
            # apart by everyone else - a real outage silences EVERY
            # flow-set at once. So a flow-set is only forgotten while
            # OTHER records are still arriving. Under a genuine outage
            # nothing is evicted and every flow-set stays in fail-open,
            # which is what §4.4 asks for: degraded to sender-local
            # policy, still capped by Tree_f, never uncapped.
            #
            # Executor state is deliberately left alone. The RP keeps the
            # last budget in device memory and the EDT map keeps the last
            # rate, so a forgotten flow-set stays paced at what it last
            # had - never faster. Releasing them would be the one way to
            # turn eviction into an uncapped flow.
            telemetry_alive = (now - last_any_rx) < n1_s
            dead = [f for f, st in flows.items()
                    if now - st.last_rx > evict_s] if telemetry_alive else []
            for f in dead:
                logf.write(json.dumps(
                    {"ts": round(time.time(), 4), "fs": f,
                     "mode": "evicted"}) + "\n")
                del flows[f]
            for fsid, st in flows.items():
                age = now - st.last_rx
                if age > n2_s:
                    track(st, tree_of(fsid), now)
                    st.mode = "fail_open"
                    actuate(fsid, st, 0, rdma_batch)
                    # same throttle as the telemetry path: this branch runs
                    # EVERY tick, so logging it unconditionally wrote ~820
                    # lines/s/flow-set at 1 ms (7155 lines for one 8.7 s
                    # outage, measured 2026-07-27) into the DPU's tmpfs.
                    st.log_age += 1
                    if (st.mode != st.log_mode or st.log_R < 0
                            or abs(st.R - st.log_R) > 0.01 * st.log_R
                            or st.log_age >= 1000):
                        st.log_R = st.R
                        st.log_age = 0
                        st.log_mode = st.mode
                        logf.write(json.dumps(
                            {"ts": round(time.time(), 4), "fs": fsid,
                             "R": int(st.R), "pace": int(st.pace),
                             "mode": st.mode}) + "\n")
                elif age > n1_s and st.mode not in ("frozen", "fail_open"):
                    # freeze: also re-anchor last_step, so the first record
                    # after the outage takes ONE period's step, not one
                    # outage's worth (dt is clamped to 10T anyway).
                    st.last_step = now
                    st.mode = "frozen"
                    logf.write(json.dumps(
                        {"ts": round(time.time(), 4), "fs": fsid,
                         "R": int(st.R), "mode": st.mode}) + "\n")
        # coalesce the RDMA rates: keep the latest per flow set and flush
        # the freshest snapshot to the FIFO at the mailbox rate
        for ft, bud in rdma_batch:
            latest_rdma[ft] = bud
        if latest_rdma and now - last_rdma_push >= rdma_push_s:
            mailbox.ensure_open()
            # One 0xb47f line per push carrying every flow set's R; the
            # host-side forwarder keeps only the newest of these when the
            # mailbox (about 16 ms per round) falls behind the push period.
            # Bindings are incremental and rare, and go out as their own
            # line only when there is something new.
            mailbox.write_sets(sorted(latest_rdma.items()))
            newmap = [(q, sid) for q, sid in qp_set.items() if qp_sent.get(q) != sid]
            if newmap:
                mailbox.write_qpmap(newmap[:60])
                for q, sid in newmap[:60]:
                    qp_sent[q] = sid
                _publish_qpmap()
            last_rdma_push = now
        shim.drain_acks()
        if recs_all:
            proc_us.append((time.monotonic() - now) * 1e6)
        if now - proc_report >= 10.0 and proc_us:
            q = sorted(proc_us)
            n_ = len(q)
            print("proc_us n=%d p50=%.0f p90=%.0f p99=%.0f max=%.0f "
                  "over_period=%d tele_skipped=%d"
                  % (n_, q[n_ // 2], q[int(n_ * 0.90)], q[int(n_ * 0.99)],
                     q[-1], sum(1 for x in q if x > period * 1e6), tele_skipped),
                  flush=True)
            proc_us = []
            proc_report = now
            tele_skipped = 0
        if now - last_print >= 5.0:
            act = " ".join("%s R=%.2fG %s" % (f, st.R / 1e9, st.mode)
                           for f, st in sorted(flows.items()))
            print("shim sent=%d acked=%d errs=%d fifo w=%d e=%d%s | %s"
                  % (shim.sent, shim.acked, shim.errs, mailbox.writes,
                     mailbox.errs,
                     " (" + " ".join("%s=%d" % kv for kv in sorted(mailbox.why.items())) + ")"
                     if mailbox.why else "",
                     act or "no flows"), flush=True)
            last_print = now


if __name__ == "__main__":
    main()
