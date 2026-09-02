#!/usr/bin/env python3
"""Sender-side agent: the fence law of design v4 (§5). Runs on the sender
DPU Arm.

Per telemetry record from the owner (receiver DPU) the fence takes one
multiplicative step (design v4 §5.2). The record carries the normalised
virtual queue q and nothing else; the sender differences consecutive q to
recover dq itself:

    R <- R * exp(alpha * min(m, m_max)) * exp(-kappa * dq)
           * exp(-(kappa/D) * min(q, D))

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
import math
import os
import re
import socket
import struct
import time

import mmap

import fastfill
import hw_maxrate

FIFO = "/tmp/rp_fifo"   # PCC RP mailbox (batched: 0xb47c000N ft bud rate ...)

# binary telemetry - MUST match rx_agent.Telemetry.REC exactly; the two
# agents are deployed and restarted together or the loop parses garbage.
# header (seq16, n) then n x (fsid[64], u_bps u64, r_bps u64)
_THDR = struct.Struct("<HH")
_TREC = struct.Struct("<64sQQQ")   # (fsid, u/g bps, r bps, d us)


def parse_telemetry(data):
    """bytes -> (seq, {fsid: {'u','r'}}); tolerant of short buffers."""
    if len(data) < _THDR.size:
        return None, {}
    seq, n = _THDR.unpack_from(data, 0)
    recs = {}
    off = _THDR.size
    for _ in range(n):
        if off + _TREC.size > len(data):
            break
        fb, u, r, d_us = _TREC.unpack_from(data, off)
        off += _TREC.size
        recs[fb.rstrip(b"\x00").decode("ascii", "ignore")] = {
            "u": u, "r": r, "d": d_us / 1e6}
    return seq, recs






def v4_step(R, q, q_prev, below, alpha, m_max, kappa, D, floor, cap,
            keep_silence=False):
    """Design v4 (§5.2), one feedback: three multiplicative factors.
    probe   x exp(alpha*m_hat)  m = consecutive feedbacks with an empty
                                queue, m_hat = min(m, m_max); the step
                                grows with the silence and is capped
    brake   x exp(-kappa dq)    dq = signed queue change since last feedback
    repay   x exp(-(kappa/D) q) by queue length
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
        R1 = max(R1, min(R1 * math.exp(alpha * min(below, m_max)), cap))
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
    R1 *= math.exp(-kappa * dq) * math.exp(-(kappa / D) * min(q, D))
    return max(R1, floor), dq, below


def conf_step(R, T, Ehat, q, dt, k, d_r, tau_r, floor, gamma=0.0, delta=0.15,
              under=0, under_n=20, A=0.0, a_ref=0.0):
    """Fence design v4 (2026-08-26), one feedback step.
    R tracks Ehat in log space with the repayment term exp(-(k dt/D_r) q);
    when the queue is empty and Ehat is above R it jumps straight up.
    Trust: recovers at 1/tau_r ONLY while the flow-set owes nothing
    (q == 0) AND is clearly under its share (gamma < -delta/2: its own CC
    is holding it back), and only once that has held for under_n
    consecutive feedbacks (one loop delay). Decays by the queue, a full
    queue clearing it within one period.
    Why each clause (lab, conf_i8 2026-08-28): with the gate on gamma
    alone, the repayment phase itself - R pushed below E to drain the
    queue - reads as "under share", trust recovered DURING repayment,
    12% trust x 200G line rate leaked 24G into every TCP flow-set, the
    link ran at 103%, DCQCN got CNPs and RDMA starved at 0.5G. The q == 0
    clause removes that cycle; the consecutive-period clause keeps 20 ms
    arrival noise from opening the gate. Third clause (sim, join
    transient): a flow-set CLIMBING under the receiver's delta margin
    also reads gamma = -delta/(1+delta) by construction (E chases A), so
    it earned trust while joining and 5% trust x line rate threw it to
    3x its share; recovery therefore also requires the arrival to be
    flat over the run (A within delta/2 of where the run started) - a
    CC-limited flow is flat, a climbing one is not.
    Returns (R, T, under, a_ref)."""
    Ehat = max(float(Ehat), floor)
    R0 = max(R, floor)
    if q <= 0.0 and Ehat > R0:
        R1 = Ehat
    else:
        a = 1.0 - math.exp(-k * dt)
        R1 = max(R0 * (Ehat / R0) ** a * math.exp(-(k * dt / d_r) * q), floor)
    # The trust itself lives in the executor (it is the only place that
    # sees the tenant CC's rate next to ours; from the sender agent a
    # CC-limited flow and one we are fencing look the same, since at zero
    # trust the wire IS our rate). The agent forwards the queue fraction
    # q/D_r; the executor decays its trust by it and recovers trust only
    # while the CC asks for less than the fence allows.
    T1 = min(q / d_r, 1.0)
    return R1, T1, under, a_ref


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
                 "shim_last", "epoch", "phase", "acts", "T", "Ehat", "under",
                 "q_prev", "dq", "b", "below", "as_avg", "enforced",
                 "a_ref")

    def __init__(self, tree, now):
        self.R = tree          # optimistic start at the tree share (§4.2)
        self.last_rx = now
        self.last_step = now   # wall-clock anchor for the tracking step
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
        self.epoch = -1        # mimd gating: last epoch index acted in
        self.phase = 0.0       # mimd gating: per-flow-set phase offset (s)
        self.acts = 0          # mimd: multiplicative steps taken
        self.T = 0.0           # conf: trust in the tenant CC (starts at 0)
        self.Ehat = 0.0        # (v3 leftover, unused by v4)
        self.q_prev = 0.0      # v4: last feedback's queue (periods)
        self.dq = 0.0          # v4: last queue growth
        self.b = 0             # v4: last direction bit
        self.below = 0         # v4: consecutive feedbacks below share with no queue
        self.enforced = False  # v4: has the fence ever actually held this flow-set
        self.as_avg = 0.0      # v4: own send rate, ~100 ms average (for the probe cap)
        self.under = 0         # conf: consecutive feedbacks clearly under share
        self.a_ref = 0.0       # conf: arrival when that run started


def waterfill(capacity, items):
    """Bounded weighted water-filling; items: {key: (weight, cap)}."""
    alloc = {k: 0.0 for k in items}
    active = {k: v for k, v in items.items() if v[1] > 0}
    while active and capacity - sum(alloc.values()) > 1e-3:
        remaining = capacity - sum(alloc.values())
        wsum = sum(w for w, _ in active.values())
        t_sat = min((cap - alloc[k]) / w for k, (w, cap) in active.items())
        t = min(t_sat, remaining / wsum)
        for k, (w, cap) in list(active.items()):
            alloc[k] += w * t
            if alloc[k] >= cap - 1e-3:
                alloc[k] = cap
                del active[k]
        if t < t_sat and t_sat != float("inf"):
            break
    return alloc


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

    def _fill(self, demand):
        tree = {}
        for f, d in demand.items():
            src_dst, cls = f.rsplit("|", 1)
            src = src_dst.split(">")[0]
            tree.setdefault(src, {}).setdefault(cls, {})[f] = d
        vms = self.policy["vms"]
        vm_items = {}
        for src, classes in tree.items():
            dsum = sum(d for fs in classes.values() for d in fs.values())
            maxr = vms.get(src, {}).get("max_rate_bps") or float("inf")
            vm_items[src] = (vms.get(src, {}).get("weight", 1),
                             min(maxr, dsum))
        vm_share = waterfill(self.cap, vm_items)
        out = {}
        for src, classes in tree.items():
            cw = vms.get(src, {}).get("class_weights", {})
            cls_items = {c: (cw.get(c, 1), sum(fs.values()))
                         for c, fs in classes.items()}
            cls_share = waterfill(vm_share[src], cls_items)
            for c, fs in classes.items():
                fs_items = {f: (1, d) for f, d in fs.items()}
                out.update(waterfill(cls_share[c], fs_items))
        return out

    def _alloc_c(self, demand):
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
        share = self._fill_any({f: big for f in flows})
        demand = {}
        for f, st in flows.items():
            if st.pace > 0 and st.r >= self.theta * st.pace:
                demand[f] = max(st.R, self.floor)
            else:
                demand[f] = max(st.r * (1.0 + self.delta), self.floor)
        alloc = self._fill_any(demand)
        return {f: max(alloc.get(f, 0.0), share.get(f, 0.0)) for f in flows}

    def _fill_any(self, demand):
        if fastfill.USING_C and demand:
            return self._alloc_c(demand)
        return self._fill(demand)


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

    def _units(self, bps):
        return max(1, round(bps / self.line * (1 << 20)))

    def ensure_open(self):
        """Hold the FIFO write end open at all times: if every writer
        closes, the RP's stdin reader hits EOF and never reads again
        (rp_service.sh's dummy keep-open writer is a 20-min sleep;
        observed dead reader 2026-07-09 after the old tx_agent2 - the
        last permanent writer - was retired)."""
        try:
            ino = os.stat(FIFO).st_ino
        except FileNotFoundError:
            return
        if self.fd < 0 or ino != self.ino:
            if self.fd >= 0:
                os.close(self.fd)
                self.fd = -1
            try:
                self.fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
                self.ino = ino
            except OSError:
                pass

    def write_batch(self, entries, trust=False):
        """entries: [(flowtag_int, budget_bps, rate_bps[, trust_fxp16])].
        trust=True selects the 0xb47d format (four words per entry): the
        device blends rate = T*cc + (1-T)*level per QP."""
        if not entries:
            return
        parts = ["0x%x" % ((0xb47d0000 if trust else 0xb47c0000) | len(entries))]
        for e in entries:
            ft, bud, rate = e[0], e[1], e[2]
            parts.append("0x%x %d %d"
                         % (ft, self._units(bud), self._units(rate)))
            if trust:
                parts.append("%d" % (e[3] if len(e) > 3 else 0))
        if self._fifo_write(" ".join(parts) + "\n"):
            self.writes += 1
        else:
            self.errs += 1

    def _fifo_write(self, line):
        # reopen on inode change: rp_service.sh recreates the FIFO
        try:
            ino = os.stat(FIFO).st_ino
        except FileNotFoundError:
            return False
        if self.fd < 0 or ino != self.ino:
            if self.fd >= 0:
                os.close(self.fd)
                self.fd = -1
            try:
                self.fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
                self.ino = ino
            except OSError:
                return False
        try:
            os.write(self.fd, line.encode())
            return True
        except OSError:
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

    def set_rate(self, src, dst, rate_bps, trust=None, start=False):
        msg = {"src_vnic": src, "dst_vnic": dst, "rate_bps": int(rate_bps)}
        if trust is not None:
            msg["trust"] = round(float(trust), 4)
        if start:
            msg["start"] = 1        # §5.4 start window: loss is not evidence
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
                self.acked += 1 if json.loads(data).get("ok") else 0
            except ValueError:
                pass


class SenderLiveness:
    """Fresh per-(src vnic, class) TX rates, published to the receiver.

    Why: the receiver splits a dst's exactly-measured pool among its
    senders using megaflow byte ratios, and those counters are ~1 s
    hardware-cached. When a sender STOPS, its stale bytes keep claiming a
    share for up to the mix window (2 s), so the survivors' measured rate
    stays diluted and they crawl into the freed share (D3 leave, ~2.9 s).
    The sender does not have that problem: its own vport TX counters are
    fresh at ~1 ms. Publishing them lets the receiver gate attribution on
    "is this sender actually sending", which no receiver-side signal can
    answer in under a second.

    Payload is tiny and advisory: {"h": host, "t": mono_ns,
    "r": {"<vnic>|<class>": bps}}. The receiver falls back to its old
    chain whenever this feed is absent or stale, so a sender without the
    helper (baseline arms) behaves exactly as before.
    """

    HDR = struct.Struct("<8sII8H")
    STALE_S = 0.1

    def __init__(self, path, vport_of_vnic, dst_ip, port, host):
        self.path, self.vport_of_vnic = path, vport_of_vnic
        self.addr, self.host = (dst_ip, port), host
        self.mm = None
        self.slot = {}
        self.prev = {}          # vnic -> (t_s, tx_ib, tx_eth)
        self.rate = {}          # "vnic|class" -> bps
        self._next_open = 0.0
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
        if self.rate:
            msg = {"h": self.host, "t": time.monotonic_ns(),
                   "r": {k: int(v) for k, v in self.rate.items()}}
            try:
                self.sock.sendto(json.dumps(msg).encode(), self.addr)
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", default="/opt/hpft/lab-registry.json")
    ap.add_argument("--local-host", default=None,
                    help="host whose vnics we pace (default: registry sender_host)")
    ap.add_argument("--log", default="/tmp/hpft_txagent_e.jsonl")
    args = ap.parse_args()

    reg = json.load(open(args.registry))
    ep = reg["e_params"]
    ctl = reg["control"]
    local_host = args.local_host or reg["sender_host"]
    line = reg["line_rate_bps"]
    period = ep["period_ms"] / 1e3
    # The law is a wall-clock rate constant (k, s^-1) applied over the
    # MEASURED interval since this flow-set's last step, not a per-tick
    # constant: nothing here needs recalibrating when period_ms changes,
    # and telemetry jitter or a dropped datagram costs accuracy, not
    # stability. The exact discretisation of zdot = k(ln u - z) over dt is
    #   z <- z + (1 - exp(-k*dt)) * (ln u - z)
    # i.e. R <- R*(u/R)**alpha with alpha = 1-exp(-k*dt). At the nominal
    # 1 ms tick alpha = 0.0198 vs a literal k*T = 0.0200
    # (0.99% apart); unlike k*T it can never exceed 1, so a late datagram
    # converges towards the target instead of shooting past it.
    k = ep["k"]
    # dt is clamped: below one period it is measurement noise, above
    # dt_max the target is stale enough that we would rather keep the
    # smooth 1/k approach than jump most of the way to it in one step
    # (alpha at 10 periods is 0.18).
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
    evict_s = ep.get("n3_evict_s", ep.get("flow_evict_s", 30))
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

    # law=conf is design v4 (design_v4.md §5): probe by silence, brake on
    # the ledger's rate of change, repay on its depth. conf_v3 is the
    # ablation arm that is also handed the explicit rate.
    law = ep.get("law", "conf")
    vq_repay = float(ep.get("d_repay_s", 0.06))

    # Pure-factor MIMD arm. gate_s is the loop delay: one multiplicative
    # step per gate per flow-set. mi_gate selects HOW the once-per-delay
    # rule is enforced:
    #   phase  - deterministic: epochs of gate_s, each flow-set offset by a
    #            hash of its id, so the population's steps are spread over
    #            the delay instead of landing in the same millisecond
    #   random - sparse MI: every record acts with probability T/gate_s
    #            (one step per delay on average, desynchronised by chance)
    #   none   - act on every record (the overreaction control arm)
    _rng = __import__("random").Random(0x4851)


    # vq2: ONE target, tracked with the v2 first-order step.
    #   u = g * (1 + (d_star - d)/d_repay), floored at phi_min*g
    # "your share, corrected by how far your queue is from the standing
    # delay d_star". Linearised: x'' + k x' + (k/d_repay) x = 0, so
    # zeta = 0.5*sqrt(k*d_repay) for every flow-set regardless of share.

    # conf (fence design v4): Ehat = A/(1+gamma); R tracks Ehat with the
    # log first-order step plus the repayment term exp(-(k dt/D_r) q);
    # exception: q == 0 and Ehat > R jumps straight up. Trust
    # T' = (1-T)/tau_r - (q/D_r)(T/D_r). The executor blends r = T c + (1-T) R.
    conf_tr = float(ep.get("trust_recover_s", 1.0))
    # design v4 (per-feedback, dimensionless): probe alpha, brake kappa,
    # repayment horizon D in feedbacks, fence cap = mult x own send rate
    # probe (§5.2/§5.5): step = alpha * min(m, m_max) per feedback, m =
    # consecutive empty-queue feedbacks. alpha = alpha_max / m_max, where
    # alpha_max is the overshoot budget (alpha_max * tau <= 10%) and m_max
    # is how long a silence has to be before the share is believed to have
    # moved. alpha has units of "per feedback squared" - it is the growth
    # rate of the step, not the step - so it scales with the SQUARE of the
    # period, while kappa scales linearly.
    v4_alpha = float(ep.get("alpha", 3e-4))
    v4_mmax = float(ep.get("m_max", 100))
    v4_kappa = float(ep.get("kappa", k * period))
    v4_D = float(ep.get("D", vq_repay / period))
    # Budget hysteresis toward the RDMA executor: how far the fence has to
    # move before a new budget is written instead of re-sending the last one.
    # 0 = always write the current value.
    rdma_hyst = float(ep.get("rdma_budget_hyst", 0.03))
    tree_theta = float(ep.get("tree_backlog_theta", 0.85))
    # 5.3 third term; togglable so the tree's contribution to the probe
    # ceiling can be isolated against a control run
    probe_cap_tree = bool(ep.get("probe_cap_tree", True))
    tree_period_s = float(ep.get("tree_period_ms", 100)) / 1e3
    v4_cap_mult = float(ep.get("r_cap_mult", 2.0))
    # §5.4: a flow-set starts at the port's headroom h*C - the capacity the
    # receiver keeps free for transients - so a newcomer can never push the
    # port past line rate even when everyone else is at their share, and it
    # starts ABOVE any realistic share, so the ledger brings it down in ~D
    # periods (the damped closed-loop regime) instead of the fence climbing
    # from a floor at alpha_max (the open-loop regime, an order of magnitude
    # slower over the same distance). The probe ceiling never sits below
    # the start value. No absolute number: h and C are policy/platform.
    v4_start = float(ep["headroom"]) * float(line)
    v4_cap_floor = v4_start
    # Absolute floor of the fence: the lowest rate the executor shapes
    # correctly (platform quantity, §7) - below it the RDMA executor
    # miscounts QPs and releases several times the budget when it rises.
    v4_floor = max(floor, float(ep.get("r_floor_bps", 1e9)))

    def step_law(st, rec, now, fsid=""):
        if law == "conf_v3":
            # ablation arm: the same receiver ledger, but the sender also gets
            # the explicit rate (A/E on the wire) and tracks it (v3 law)
            dt = min(max(now - st.last_step, dt_min), dt_max)
            st.last_step = now
            gam = rec.get("u", 1e6) / 1e6 - 1.0
            q_s = rec.get("d", 0.0)                 # seconds
            A = float(rec.get("r", 0))
            if A > 0 and 1.0 + gam > 0:
                st.Ehat = A / (1.0 + gam)
            if st.mode == "fresh":
                st.R = min(st.R, st.Ehat if st.Ehat > 0 else v4_cap_floor); st.mode = "v3"
            st.R, _T, _u, _a = conf_step(st.R, 0.0, st.Ehat, q_s, dt, k, vq_repay, conf_tr, v4_floor)
            st.q_prev = q_s / period; st.T = min(q_s / vq_repay, 1.0)
        elif law == "conf":
            # design v4: q (periods), b (1 = at share), own send rate for the cap
            q = rec.get("d", 0.0) / period          # seconds -> periods
            b = 1 if rec.get("u", 0.0) >= 5e5 else 0
            src, cls = fsid.split(">")[0], fsid.rsplit("|", 1)[1]
            a_s = live.rate.get("%s|%s" % (src, cls)) if live is not None else None
            if a_s is not None:
                st.as_avg += (float(a_s) - st.as_avg) * min(1.0, (now - st.last_step) / 0.1) if st.as_avg > 0 else float(a_s) - st.as_avg
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
            cap = line if a_s is None else max(v4_cap_mult * st.as_avg, v4_cap_floor)
            if probe_cap_tree:
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
                st.mode = "v4"
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
                                            not st.enforced)
            st.q_prev, st.b = q, b
            st.last_step = now
            st.T = min(q / v4_D, 1.0)               # queue fraction for the executors' trust
    shim = PaceShim(ctl["pace_shim"][local_host])
    # sender-liveness feed (advisory, see SenderLiveness): tells the receiver
    # which senders are actually sending, at vport freshness (~1 ms), so its
    # attribution does not have to wait out the ~1 s megaflow cache when a
    # sender stops. Absent/stale feed => receiver keeps its old chain.
    live = None
    _rx_host = reg.get("receiver_host")
    _live_ip = ctl.get("telemetry_ip", {}).get(_rx_host)
    if _live_ip:
        _vport_of_vnic = {}
        for v in reg["vnics"]:
            if v["host"] == local_host and v.get("representor"):
                _m = re.search(r"vf(\d+)$", v["representor"])
                if _m:
                    _vport_of_vnic[v["vnic_id"]] = int(_m.group(1)) + 1
        if _vport_of_vnic:
            live = SenderLiveness("/dev/shm/hpft_vpm", _vport_of_vnic,
                                  _live_ip, int(ep.get("liveness_port", 9713)),
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
        return tree_applied.get(f, trees.get(f, ctl["tree_stub_bps"]))

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
    last_any_rx = time.monotonic()   # last telemetry from ANY flow-set
    logf = open(args.log, "a", buffering=1)
    print("tx_agent_e: local=%s T=%.0fms law=%s k=%.1f/s (tau=%.0fms, "
          "alpha@T=%.4f) failopen=%.2f/%.1fs evict=%.0fs tree=%s shim=%s "
          "log=%s"
          % (local_host, period * 1e3, law, k, 1e3 / k,
             1.0 - math.exp(-k * period), n1_s, n2_s, evict_s,
             "C" if fastfill.USING_C else "python",
             ctl["pace_shim"][local_host], args.log), flush=True)

    def actuate(fsid, st, r_bps, rdma_batch):
        pace = max(min(st.R, tree_of(fsid)), floor)
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
                shim.set_rate(src, dst, pace,
                              st.T if law in ("conf", "conf_v3") else None,   # T = queue fraction q/D_r
                              start=(law == "conf" and not st.enforced))
                st.shim_last = tnow
        elif cls == "rdma":
            ft = pair_ft.get(src_dst, flowtags.get(src))
            if ft is not None:
                # r_bps is the receiver's rate for this flow-set and it
                # drives the RP's water level. Withholding it when a dst
                # has several senders was tried and is WORSE: the device
                # falls back to its own TX-event estimate, which undercounts,
                # so it reads under budget and lets the wire run to the
                # sender tree (measured 2.6x the granted pace). The fix
                # belongs where the error was - in the receiver's split -
                # not in refusing to use its output.
                # 4th word: queue fraction q/D_r in the low 16 bits; bit 16 =
                # the flow-set is still in its start window (§5.4), during
                # which the executor takes no loss evidence for the trust.
                rdma_batch.append((ft, pace, r_bps,
                                   (st.T, law == "conf" and not st.enforced)
                                   if law in ("conf", "conf_v3") else None))
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
    latest_rdma = {}    # flowtag -> (budget_bps, rx_rate_bps)
    # dsts whose RDMA rate the receiver can only split by estimate, i.e.
    # those carrying more than one RDMA sender. Recomputed per datagram
    # from the FULL record set (not the local filter): a remote sender we
    # do not pace still makes our own dst's split an estimate.
    ambiguous_dsts = set()
    rdma_sent_budget = {}   # flowtag -> last budget written (hysteresis)
    rdma_push_s = ep.get("rdma_push_ms", 13) / 1e3
    last_rdma_push = time.monotonic()
    # The RP device code takes a control step only when the FED rate
    # changes; a wire held at a constant (e.g. floor) rate therefore
    # freezes the RP forever - budget updates alone do not un-freeze it
    # (M2@1ms deadlock, 2026-07-11: bud=12.6G, lvl stuck at 0.02G while
    # the fed rate never moved). Alternate the fed rate by +-max(3%, one
    # 2^20-unit) every push so the RP always sees a change and keeps
    # converging its level toward the budget.
    rp_dither_flip = False
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
                    data = more
                    tele_skipped += 1
            except (BlockingIOError, socket.timeout, OSError):
                pass
            finally:
                sock.settimeout(sock_timeout)
            seq, recs_all = parse_telemetry(data)
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
            srcs_per_dst = {}
            for f in recs_all:
                sd, c = f.rsplit("|", 1)
                if c == "rdma":
                    s_, d_ = sd.split(">")
                    srcs_per_dst.setdefault(d_, set()).add(s_)
            ambiguous_dsts = {d for d, ss in srcs_per_dst.items()
                              if len(ss) > 1}
            recs = {f: rec for f, rec in recs_all.items()
                    if f.split(">")[0].startswith(local_host + "/")}
            fresh = []
            for fsid, rec in recs.items():
                st = flows.get(fsid)
                if st is None:
                    st = flows[fsid] = FlowState(0.0, now)
                    fresh.append(fsid)
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
            apply_trees(now)                # §5.1 transition limiting
            tree_us = int((time.monotonic() - _t0) * 1e6)
            for fsid in fresh:
                # Optimistic start (§4.2), but no more optimistic than what
                # the receiver has ALREADY said. Tree_f is the sender-local
                # allowance and is the right start when nothing better is
                # known; the very record that creates this flow-set carries
                # u_f, which is the receiver's ceiling for it right now.
                # Starting above u and tracking down means the first budget
                # written to the executor is knowingly too large.
                #
                # It matters most in exactly the case that hurts: joining a
                # dst that is already at its MaxRate. There u is the joint
                # fair share while Tree is the whole VM allowance, so the
                # old start doubled the load on a saturated node for the
                # 50 ms the law needs to come down - and the RDMA executor
                # reads a 2x over-budget condition as something to dig out
                # of, hard, for both senders at once (2026-07-28).
                u0 = recs[fsid].get("u", 0)
                if law == "conf":
                    rr = recs[fsid].get("r", 0)
                    g1 = u0 / 1e6 if u0 > 0 else 1.0
                    u0 = (rr / g1) if (rr > 0 and g1 > 0) else 0
                flows[fsid].R = min(tree_of(fsid), u0) if u0 > 0 \
                    else tree_of(fsid)
            for fsid, rec in recs.items():
                st = flows[fsid]
                st.last_rx = now
                st.last_seq = seq if seq is not None else -1
                u, r = rec.get("u", 0.0), rec.get("r", 0)
                # THE LAW (§4.2): one first-order step towards the target.
                # Nothing guards this line: the target is not a guess, it
                # is bounded at the receiver, so there is nothing for an
                # increase cap or a decrease floor to protect against.
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
                         "seq": st.last_seq, "u": int(u), "r": r,
                         "d": round(rec.get("d", 0.0) * 1e3, 3),
                         "acts": st.acts, "T": round(st.T, 3),
                         "Ehat": int(st.Ehat), "q": round(st.q_prev, 3), "b": st.b, "dq": round(st.dq, 4),
                         "As": int(live.rate.get("%s|%s" % (fsid.split(">")[0], fsid.rsplit("|", 1)[1]), 0)) if live is not None else -1,
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
            # re-assert the unknown-flow cap every 5 s (an RP restart
            # clears it; the write is one short line)
            if now - _unk_sent >= 5.0:
                _unk_sent = now
                mailbox._fifo_write("0xccf %d\n" % _unk_units)
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
        # coalesce RDMA budgets: keep the latest per flowtag, flush the
        # freshest snapshot to the FIFO only at the mailbox rate.
        # Budget hysteresis (rdma_budget_hyst). It was added when the RP
        # searched for its water level with an integral: any budget change
        # re-armed a settle-hold, so a budget that kept moving stopped the
        # search from ever stepping. That executor is gone - the device now
        # ASSIGNS level = budget/N every epoch, statelessly, and explicitly
        # discards the freshness gate ((void)do_ctrl) - so the hysteresis
        # now only makes the device pace on a stale budget. It costs
        # nothing to drop: the push happens on its own timer and carries
        # every pair in one message either way, so this only decides which
        # number that message contains.
        #
        # There is deliberately NO rate limit on descending budget writes.
        # One would only be needed if the budget could swing violently at
        # the RP's own inner-loop timescale, and it cannot: equilibrium
        # here is signal-free so there is no sawtooth, and the target moves
        # with a 1/k time constant under a bounded 25%
        # discount - and a descent limit here would only make the executor
        # (1.05 s^-1) the convergence bottleneck instead of the law
        # (k*ln2 = 13.9 s^-1). Descent is one step, and the RP's
        # proportional feed-forward scales its level by the budget ratio
        # (design_v4.md §6).
        for ft, bud, rate, tr in rdma_batch:
            latest_rdma[ft] = (bud, rate, tr)
        if latest_rdma and now - last_rdma_push >= rdma_push_s:
            mailbox.ensure_open()
            rp_dither_flip = not rp_dither_flip
            entries = []
            trust_mode = False
            for ft, (bud, rate, tr) in latest_rdma.items():
                sent = rdma_sent_budget.get(ft)
                if sent is None or bud == 0 or abs(bud - sent) > rdma_hyst * sent:
                    sent = bud
                rdma_sent_budget[ft] = sent
                ent = [ft, sent,
                       max(rate + (1 if rp_dither_flip else -1)
                           * max(0.03 * rate, 2e5), 2e5)]
                if tr is not None:
                    trust_mode = True
                    qf, start = tr
                    ent.append(int(round(min(max(qf, 0.0), 1.0) * 65535))
                               | (0x10000 if start else 0))
                entries.append(tuple(ent))
            mailbox.write_batch(entries, trust=trust_mode)
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
            print("shim sent=%d acked=%d errs=%d fifo w=%d e=%d | %s"
                  % (shim.sent, shim.acked, shim.errs, mailbox.writes,
                     mailbox.errs, act or "no flows"), flush=True)
            last_print = now


if __name__ == "__main__":
    main()
