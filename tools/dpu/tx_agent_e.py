#!/usr/bin/env python3
"""Scheme-E sender-side agent: the unified response law (design_e §3.5).
Runs on the sender DPU Arm. Replaces tx_agent2 for flow-sets under scheme E
(M1: tcp class only; rdma actuation lands in M1b).

Per telemetry record {fsid: {s, r, e}} from the owner (receiver DPU):
    s == 0:  R_f <- min(R_f + A, Tree_f)        (A: additive increase)
    s  > 0:  R_f <- R_f * (1 - beta * s)**(T/Tref)  (weightless MD, wall-clock dosed)
app-limited freeze: r < theta_al*R for m consecutive ticks -> clamp R to
r*(1+eps) and stop AI until r comes back up.
fail-open: no telemetry for N1 periods -> freeze; for N2 -> ramp to Tree_f.
Actuation: pace_f = min(R_f, Tree_f); tcp -> host pace shim (UDP ->
DirectBpfMapWriter); rdma -> M1b (logged only until then).

Params from registry e_params; Tree_f is the static stub (control.
tree_stub_bps) until M5's sender tree. A = 0.05% LineRate/period here
(smaller than §5.4's 0.5-2% suggestion: at a 6G target that would be a
+-25% sawtooth, blowing the M1 +-5% acceptance; M4 revisits).
"""
import argparse
import json
import os
import socket
import struct
import time

from fastfill import waterfill_ceilings

FIFO = "/tmp/rp_fifo"   # PCC RP mailbox (batched: 0xb47c000N ft bud rate ...)

# binary telemetry (must match rx_agent.Telemetry): header (seq16, n) then
# n x (fsid[64], s float, r_bps u32, e_bps u32)
_THDR = struct.Struct("<HH")
_TREC = struct.Struct("<64sfQQQ")   # + ceil_f (see rx Telemetry.REC)


def parse_telemetry(data):
    """bytes -> (seq, {fsid: {'s','r','e'}}); tolerant of short buffers."""
    if len(data) < _THDR.size:
        return None, {}
    seq, n = _THDR.unpack_from(data, 0)
    recs = {}
    off = _THDR.size
    for _ in range(n):
        if off + _TREC.size > len(data):
            break
        fb, s, r, e, c = _TREC.unpack_from(data, off)
        off += _TREC.size
        recs[fb.rstrip(b"\x00").decode("ascii", "ignore")] = {
            "s": s, "r": r, "e": e, "c": c}
    return seq, recs


class FlowState:
    __slots__ = ("R", "al_count", "last_rx", "last_seq", "mode", "pace", "r",
                 "log_R", "log_age", "log_mode", "R_good", "fr", "ai_run",
                 "e_last", "ceil_last", "esc_since", "esc_log")

    def __init__(self, tree, now):
        self.R = tree          # Q20: optimistic start at the tree share
        self.al_count = 0
        self.last_rx = now
        self.last_seq = -1
        self.mode = "fresh"
        self.pace = 0.0
        self.r = 0             # last telemetry r_f (sender-tree demand)
        self.log_R = -1.0
        self.log_age = 0
        self.log_mode = ""
        self.R_good = 0.0      # F2: last known-good rate (DCQCN target-rate)
        self.fr = False        # F2: fast-recovery active
        self.ai_run = 0        # F2: consecutive unmarked AI ticks (HAI)
        self.e_last = 0.0      # last granted e_f from telemetry
        self.ceil_last = 0.0   # last fair-share ceiling from telemetry
        self.esc_since = 0.0   # executor-escape tripwire: since when r >> pace
        self.esc_log = 0.0     # last escape alarm emitted


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
    """design_e §3.6: the sender-side tree over the local uplink, sharing
    the rx scheduler's structure (src VM MaxRate -> class weights -> fs).
    Tree_f is computed as the fs's fair-share CEILING (its demand set to
    infinity, siblings at their measured demands): §3.6's demand-capped
    fill contradicts Q20 (a new flow with r=0 would get Tree ~= 0, not
    "the full locally-permitted allowance") and §3.5's fail-open ramp
    target - recorded as a design note, pending user confirmation.
    Class borrowing between the two actuator planes (fq+edt / PCC) falls
    out of the work-conserving fill: this IS the budget arbiter, feasible
    in one process because both actuators hang off this agent.
    cap-hit boost: a flow using >=theta of its pace advertises appetite."""

    def __init__(self, policy, line, headroom, delta, floor):
        self.policy = policy
        self.cap = line * (1.0 - headroom)
        self.delta = delta
        self.floor = floor

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

    def trees(self, flows):
        demand = {}
        for f, st in flows.items():
            d = max(st.r * (1.0 + self.delta), self.floor)
            if st.pace > 0 and st.r >= 0.85 * st.pace:
                d = max(d, st.pace * (1.0 + self.delta))   # cap-hit boost
            demand[f] = d
        # single-pass ceiling cascade (same math as the per-fs N-fill
        # loop; property-tested) - O(N log N)
        tree = {}
        for f, d in demand.items():
            src_dst, cls = f.rsplit("|", 1)
            src = src_dst.split(">")[0]
            tree.setdefault(src, {}).setdefault(cls, {})[f] = d
        vms = self.policy["vms"]
        vm_items, vm_repl = {}, {}
        for src, classes in tree.items():
            dsum = sum(d for fs in classes.values() for d in fs.values())
            maxr = vms.get(src, {}).get("max_rate_bps") or float("inf")
            vm_items[src] = (vms.get(src, {}).get("weight", 1),
                             min(maxr, dsum))
            vm_repl[src] = maxr
        vm_ceil = waterfill_ceilings(self.cap, vm_items, vm_repl)
        out = {}
        for src, classes in tree.items():
            cw = vms.get(src, {}).get("class_weights", {})
            cls_items = {c: (cw.get(c, 1), sum(fs.values()))
                         for c, fs in classes.items()}
            cls_ceil = waterfill_ceilings(vm_ceil[src], cls_items)
            for c, fs in classes.items():
                out.update(waterfill_ceilings(
                    cls_ceil[c], {f: (1, d) for f, d in fs.items()}))
        return out


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

    def write_batch(self, entries):
        """entries: [(flowtag_int, budget_bps, rate_bps)]"""
        if not entries:
            return
        parts = ["0x%x" % (0xb47c0000 | len(entries))]
        for ft, bud, rate in entries:
            parts.append("0x%x %d %d"
                         % (ft, self._units(bud), self._units(rate)))
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

    def set_rate(self, src, dst, rate_bps):
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
                self.acked += 1 if json.loads(data).get("ok") else 0
            except ValueError:
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
    # Period-scaling (2026-07-11): all parameters were calibrated at a 50ms
    # reference period. To keep the SAME wall-clock behaviour at any period:
    #   - per-period increments (A) scale by period/ref (same rate per second)
    #   - period-count timeouts (N1/N2/m_al) scale by ref/period (same
    #     wall-clock timeout regardless of how many ticks fit in it)
    #   - MD is exponentiated by period/ref: R *= (1-beta*s)**(period/ref),
    #     so a sustained mark sheds the same fraction per wall-clock second
    #     at any tick rate. Unscaled per-tick MD at 1ms was a 50x overdose:
    #     a ~20ms mark burst took R from 3G to the floor in ~11 ticks, and
    #     the floor-crush is what wedges the RP into its family-3 state
    #     (crawl / non-enforcement; observed 2026-07-11 step test t+16s).
    ref = ep.get("ref_period_ms", 50) / 1e3
    a_scale = period / ref
    n_scale = ref / period
    # A: same law form for both classes, per-class rate constant permitted
    # (design_e §3.5 v2.1 / Q24; M4 calibration)
    a_by_cls = ep.get("a_frac_linerate_by_class", {})
    a_default = ep.get("a_frac_linerate", 0.0005) * line * a_scale

    def A_of(fsid):
        cls = fsid.rsplit("|", 1)[1]
        frac = a_by_cls.get(cls) if cls in a_by_cls \
            else ep.get("a_frac_linerate", 0.0005)
        return frac * line * a_scale if cls in a_by_cls else a_default
    beta = ep["beta"]
    theta_al = ep["theta_al"]
    m_al = max(1, round(ep["m_al"] * n_scale))
    eps_al = ep["eps_al"]
    n1 = ep["n1_freeze"] * n_scale
    n2 = ep["n2_failopen"] * n_scale
    fast_rec = bool(ep.get("fast_recovery", False))
    hai_after = int(round(ep.get("hai_after", 0) * n_scale))
    hai_max = int(ep.get("hai_max", 8))
    probe_over = ep.get("probe_over_grant", 1.05)
    probe_gain = ep.get("probe_gain", 2.0)
    bud_slew_per_s = ep.get("rdma_bud_slew_per_s", 1.0)   # 1.0 = no slew
    # MIAD experiment (branch miad-experiment). law_skeleton: "aimd" (default,
    # the tuned production law) or "miad" (simple multiplicative-increase /
    # additive-decrease: s=0 -> R*=(1+mi_alpha), hard-capped at e_hat; s>0 ->
    # R-=ad_beta*line*s, additive step modulated by the graded mark. Both
    # per-tick constants period-scale by a_scale like A. No HAI/FR/app-limited
    # /probe-margin - the bare skeleton, to test whether MI's rate-invariant
    # ramp fits a rate CEILING better than A's absolute step.)
    law_skeleton = ep.get("law_skeleton", "aimd")
    mi_alpha = ep.get("mi_alpha", 0.1)
    ad_beta = ep.get("ad_beta", 0.01)

    def probe_cap(ceil_f, r_now):
        """Probe ceiling with a realization-aware margin (stress D2/D3,
        2026-07-11). A fixed ceil*probe_over cap makes any executor that
        realizes >1/probe_over of its pace (>95.2% at 1.05) sit above the
        grant FOREVER: the VQ integrates the overshoot, saturates in
        seconds and fires a periodic marking storm (3/30 D2 reps
        collapsed; every D3c hold window was punctured). The margin only
        exists to compensate executor under-realization (wire/budget
        0.92-0.95 observed), so scale it by how far the wire actually is
        from the ceiling: full margin while the flow is well below ceil,
        linearly to zero as r reaches ceil. Fixed points (r = rho * pace):
        rho=1.0 -> r = ceil exactly (no sustained overshoot, VQ empty);
        rho=0.92 -> 0.966*ceil (identical to the old anchor); rho=0.95 ->
        0.983*ceil (-1.5% vs old); rho=0.98 -> 0.993*ceil where the old
        anchor overshot by 2.9% and stormed."""
        margin = ceil_f * (probe_over - 1.0)
        boost = probe_gain * max(0.0, ceil_f - r_now)
        return ceil_f + min(margin, boost)
    floor = ctl["pace_floor_bps"]
    shim = PaceShim(ctl["pace_shim"][local_host])
    mailbox = RpMailbox(line)
    flowtags = {v["vnic_id"]: int(v["flowtag"], 16)
                for v in reg["vnics"] if "flowtag" in v}
    # per-(src,dst) flowtags for cross-pair RDMA: the tag is a stable hash of
    # (src function, dst function) - NOT per-src - so a src VF talking to
    # several dsts needs one budget entry per pair (registry rdma_flowtags,
    # probed via 0xdea 2026-07-11). Falls back to the per-src tag, which keeps
    # the four straight pairs byte-identical to the old behaviour.
    pair_ft = {k: int(v, 16)
               for k, v in reg.get("rdma_flowtags", {}).items() if ">" in k}
    stree = SenderTree(reg["policy"], line, ep["headroom"],
                       ep["delta_demand"], floor)
    trees = {}   # fsid -> Tree_f, recomputed on telemetry / ticker

    def tree_of(f):
        return trees.get(f, ctl["tree_stub_bps"])

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", ep["telemetry_port"]))
    sock.settimeout(period)

    flows = {}   # fsid -> FlowState
    logf = open(args.log, "a", buffering=1)
    print("tx_agent_e: local=%s T=%.0fms law=%s A=cls%s beta=%.2f "
          "mi_alpha=%.3f ad_beta=%.3f tree=dynamic(§3.6) shim=%s log=%s"
          % (local_host, period * 1e3, law_skeleton, a_by_cls or a_default,
             beta, mi_alpha, ad_beta, ctl["pace_shim"][local_host],
             args.log), flush=True)

    def actuate(fsid, st, r_bps, rdma_batch):
        pace = max(min(st.R, tree_of(fsid)), floor)
        src_dst, cls = fsid.rsplit("|", 1)
        src, dst = src_dst.split(">")
        if cls == "tcp":
            if pace != st.pace:
                shim.set_rate(src, dst, pace)
        elif cls == "rdma":
            ft = pair_ft.get(src_dst, flowtags.get(src))
            if ft is not None:
                rdma_batch.append((ft, pace, r_bps))
        st.pace = pace
        # executor-escape tripwire (log-only). Every stress-D1 failure class
        # was a silent one: an unpaced EDT pair, an unmatched flowtag and a
        # wedged RP all kept every layer reporting healthy while the wire
        # ignored the pace. r persistently above pace is the one signal the
        # control plane can already see. 1.5x / 1s absorbs RC-retransmit
        # inflation (~1.3x observed) and transition bursts.
        tnow = time.monotonic()
        if r_bps > 1.5 * pace:
            if st.esc_since == 0.0:
                st.esc_since = tnow
            elif tnow - st.esc_since >= 1.0 and tnow - st.esc_log >= 5.0:
                print("pace-escape %s r=%.2fG pace=%.2fG dur=%.0fs"
                      % (fsid, r_bps / 1e9, pace / 1e9, tnow - st.esc_since),
                      flush=True)
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
    while True:
        # --- telemetry-driven law ---
        try:
            data, _ = sock.recvfrom(65536)
            seq, recs_all = parse_telemetry(data)
        except socket.timeout:
            seq, recs_all = None, {}
        except OSError as e:
            print("tx_agent_e: bad telemetry: %s" % e, flush=True)
            seq, recs_all = None, {}
        now = time.monotonic()
        rdma_batch = []
        if recs_all:
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
            trees = stree.trees(flows)      # §3.6 tree follows demand
            tree_us = int((time.monotonic() - _t0) * 1e6)
            for fsid in fresh:
                flows[fsid].R = tree_of(fsid)   # Q20: start at tree share
            for fsid, rec in recs.items():
                st = flows[fsid]
                st.last_rx = now
                st.last_seq = seq if seq is not None else -1
                s, r = rec.get("s", 0.0), rec.get("r", 0)
                st.e_last = rec.get("e", st.e_last)
                st.ceil_last = rec.get("c", st.ceil_last)
                # app-limited detection: the app is app-limited only if it
                # is not filling its GRANT e_f - compare r to min(R, e_f),
                # not to R alone. With Q20's optimistic start R begins far
                # above the grant, and (worse at 1ms) the 13ms budget-push
                # coalescing keeps r lagging R; comparing to R alone then
                # misfires and clamps a cap-limited flow down to r*1.1,
                # pinning it below its cap. A transient r~0 also must not
                # count (it would clamp R to the floor).
                if law_skeleton == "miad":
                    # ---- simple MIAD skeleton (miad-experiment) ----
                    if s > 0:
                        # additive decrease, modulated by the graded mark
                        # s_f (0..1): barely-over-share steps down gently,
                        # badly-over-share (s->1) decisively. Line-anchored
                        # absolute step (like A), period-scaled by a_scale.
                        st.R -= ad_beta * line * a_scale * s
                        st.mode = "ad"
                    else:
                        # multiplicative increase, HARD-capped at the fair-
                        # share ceiling e_hat (simplest form, no probe
                        # margin). MI = rate-invariant ramp; the e_hat cap
                        # is mandatory (unbounded MI is geometric runaway).
                        cap = st.ceil_last or st.e_last or tree_of(fsid)
                        st.R = min(st.R * (1.0 + mi_alpha * a_scale), cap)
                        st.mode = "mi"
                    # protective floor (executor anti-wedge; orthogonal to
                    # the increase/decrease law) - keep pace out of the RP
                    # stall region even under a persistent mark
                    if st.ceil_last > 0:
                        st.R = max(st.R, 0.3 * st.ceil_last)
                    st.R = max(st.R, floor)
                    actuate(fsid, st, r, rdma_batch)
                    st.log_age += 1
                    changed = (st.mode != st.log_mode or st.log_R < 0
                               or abs(st.R - st.log_R) > 0.01 * st.log_R)
                    if changed or st.log_age >= 20:
                        st.log_R = st.R
                        st.log_age = 0
                        st.log_mode = st.mode
                        logf.write(json.dumps(
                            {"ts": round(time.time(), 4), "fs": fsid,
                             "seq": st.last_seq, "s": s, "r": r,
                             "R": int(st.R), "pace": int(st.pace),
                             "tree": int(tree_of(fsid)), "tus": tree_us,
                             "mode": st.mode}) + "\n")
                    continue
                # ---- AIMD skeleton (production default) ----
                ref = min(st.R, st.e_last) if st.e_last > 0 else st.R
                if 0 < r < theta_al * ref:
                    st.al_count += 1
                else:
                    st.al_count = 0
                if s > 0:
                    # F2/DCQCN-style: remember the pre-cut rate as the
                    # recovery target; transient marks bounce back fast,
                    # persistent marks ratchet R_good down naturally
                    if fast_rec:
                        st.R_good = st.R
                        st.fr = True
                    st.ai_run = 0
                    st.R *= (1.0 - beta * s) ** a_scale
                    # MD undershoot bound (2026-07-11 M2@1ms): MD's job is to
                    # converge R down to the grant; a budget far BELOW the
                    # measured arrival rate is pure overshoot, and near-floor
                    # budgets are what wedge the RP into its family-3 state
                    # (bud=12.6G lvl=0.02G observed). R still tracks a
                    # collapsing wire down - r follows R through actuation -
                    # but geometrically, never 50x below reality in one storm.
                    if r > 0:
                        st.R = max(st.R, 0.3 * r)
                    # policy-anchored floor: the 0.3*r bound rides a
                    # collapsing wire down (r falls -> floor falls) and
                    # parked budgets at 0.9-0.95G, exactly the <~1G region
                    # where a deep-paced QP wedges into its RC stall (V1
                    # trace t+109-113). ceil is policy-side and does not
                    # collapse with the wire, so 0.3*ceil keeps the pace
                    # out of the stall region no matter how long the
                    # marking persists; policy changes move the floor
                    # within one telemetry tick.
                    if st.ceil_last > 0:
                        st.R = max(st.R, 0.3 * st.ceil_last)
                    st.mode = "md"
                elif st.al_count >= m_al:
                    if fast_rec and st.R_good < st.R:
                        pass   # keep the higher pre-dip R_good
                    st.R = max(r * (1.0 + eps_al), floor)
                    st.ai_run = 0
                    if fast_rec:
                        st.fr = True
                    st.mode = "app_limited"
                elif fast_rec and st.fr and st.R < 0.95 * st.R_good:
                    # FR: converge by averaging (~5 ticks), but only INTO
                    # the granted rate - a bounce past the demand-lagged
                    # grant self-marks and starves (observed f12): fast
                    # into the grant, AI beyond it.
                    tgt = st.R_good
                    # cap the bounce with the same realization-aware margin
                    # as AI/HAI: e*1.15 sanctioned a 15% overshoot whenever
                    # e sat at the fair share, and the post-collapse
                    # bounce -> overmark -> re-cut cycle rode exactly that
                    # (stress fix validation V1, t+108 re-crush trace)
                    anchor = st.ceil_last or st.e_last
                    if anchor > 0:
                        tgt = min(tgt, probe_cap(anchor, r))
                    if st.R < 0.95 * tgt:
                        st.R = min((st.R + tgt) / 2.0, tree_of(fsid))
                        # the FR jump itself makes r lag R for a tick or
                        # two; without this reset the al clamp re-fires
                        # and locks recovery into a ~7%/tick staircase
                        # (solo-RDMA regression, 2026-07-10)
                        st.al_count = 0
                        st.mode = "fr"
                    else:
                        st.fr = False
                        st.R = min(st.R + A_of(fsid), tree_of(fsid))
                        st.mode = "ai"
                else:
                    st.fr = False
                    st.ai_run += 1
                    mult = 1
                    if hai_after and st.ai_run > hai_after:
                        mult = min(st.ai_run - hai_after + 1, hai_max)
                    # AI/HAI probes at most probe_over above the fair-share
                    # CEILING. Unbounded HAI (16G/s wall-clock at any
                    # period) blew straight through the cap after every
                    # mark-free 0.5s: dip -> HAI to 14G in 1s -> VQ
                    # saturates -> MD crash -> repeat, a 15-25s relaxation
                    # oscillation that was THE class-contention instability
                    # (M2 2026-07-11). The anchor must be ceil_f, NOT the
                    # demand-capped e_f: e = min(fair, r*1.15) is
                    # self-referential through the sender's own rate and
                    # trapped a crushed flow at 0.27G with zero marks
                    # (same lesson as the Q22 VQ-drain fix).
                    cap = tree_of(fsid)
                    anchor = st.ceil_last or st.e_last
                    if anchor > 0:
                        cap = min(cap, probe_cap(anchor, r))
                    st.R = min(st.R + A_of(fsid) * mult, cap)
                    if st.al_count == 0:
                        st.R_good = max(st.R_good, st.R)
                    st.mode = "ai" if mult == 1 else "hai"
                st.R = max(st.R, floor)
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
                         "seq": st.last_seq, "s": s, "r": r,
                         "R": int(st.R), "pace": int(st.pace),
                         "tree": int(tree_of(fsid)), "tus": tree_us,
                         "mode": st.mode}) + "\n")
        # --- local ticker: fail-open (design_e §3.5) ---
        if now - last_ticker >= period:
            last_ticker = now
            mailbox.ensure_open()
            for fsid, st in flows.items():
                age = now - st.last_rx
                if age > n2 * period:
                    st.R = min(st.R + A_of(fsid), tree_of(fsid))
                    st.mode = "fail_open"
                    actuate(fsid, st, 0, rdma_batch)
                    logf.write(json.dumps(
                        {"ts": round(time.time(), 4), "fs": fsid,
                         "R": int(st.R), "pace": int(st.pace),
                         "mode": st.mode}) + "\n")
                elif age > n1 * period and st.mode not in ("frozen", "fail_open"):
                    st.mode = "frozen"
                    logf.write(json.dumps(
                        {"ts": round(time.time(), 4), "fs": fsid,
                         "R": int(st.R), "mode": st.mode}) + "\n")
        # coalesce RDMA budgets: keep the latest per flowtag, flush the
        # freshest snapshot to the FIFO only at the mailbox rate.
        # Budget hysteresis (stress D1, 2026-07-11): the RP treats ANY budget
        # change as a cap change - it re-arms the settle-hold and marks the
        # rate sample used, so its integral level control never steps while
        # the budget keeps moving. At 1ms the hai probe cap tracks a ceil
        # that jitters with the whole dst's rate vector, so a crushed pair's
        # budget changed on every flush and its level stayed proportionally
        # crushed forever (0.27G wire under a 4.3G budget, self-sustaining:
        # the probing that should recover the flow froze the executor).
        # Re-sending the same budget while the drift is <3% keeps the budget
        # quasi-static (the 20Hz-era semantics the RP was built against);
        # the rate field stays fresh every flush, so hold expires after 3
        # samples and the integral climbs the level back (~12.5%/step).
        for ft, bud, rate in rdma_batch:
            latest_rdma[ft] = (bud, rate)
        if latest_rdma and now - last_rdma_push >= rdma_push_s:
            mailbox.ensure_open()
            rp_dither_flip = not rp_dither_flip
            # Budget DESCENT slew limit (three-loop limit cycle, E1
            # 2026-07-11): the law's R sawtooth swings the budget +-50% at
            # ~1s period; the RP's integral chases it through a ~100ms
            # measure+apply loop delay and undershoots the level to near
            # zero (0.09G under a 1.38G budget) - a coupled VQ/AIMD/RP
            # limit cycle. Descending budget writes are slew-limited so
            # the RP's target moves slower than its loop delay; ascent
            # stays free (recovery must be fast). slew_per_s = fraction
            # of the budget surviving one second of continuous descent.
            dt_flush = now - last_rdma_push
            decay = bud_slew_per_s ** dt_flush
            entries = []
            for ft, (bud, rate) in latest_rdma.items():
                sent = rdma_sent_budget.get(ft)
                if sent is None or bud == 0:
                    sent = bud
                elif bud >= sent:
                    if bud - sent > 0.03 * sent:
                        sent = bud
                elif bud_slew_per_s >= 1.0:
                    # slew disabled: plain 3% hysteresis both ways. (A
                    # naive sent*decay with decay=1.0 silently made
                    # budgets NON-DESCENDING - the executor kept the
                    # fail-open-era budget forever, D4 2026-07-11.)
                    if sent - bud > 0.03 * sent:
                        sent = bud
                else:
                    # slewed descent writes every flush: each write's
                    # settle-hold freezes the RP integral while the
                    # proportional feed-forward walks the level down
                    # smoothly - exactly the wanted descent behaviour.
                    sent = max(bud, sent * decay)
                rdma_sent_budget[ft] = sent
                entries.append(
                    (ft, sent,
                     max(rate + (1 if rp_dither_flip else -1)
                         * max(0.03 * rate, 2e5), 2e5)))
            mailbox.write_batch(entries)
            last_rdma_push = now
        shim.drain_acks()
        if now - last_print >= 5.0:
            act = " ".join("%s R=%.2fG %s" % (f, st.R / 1e9, st.mode)
                           for f, st in sorted(flows.items()))
            print("shim sent=%d acked=%d errs=%d fifo w=%d e=%d | %s"
                  % (shim.sent, shim.acked, shim.errs, mailbox.writes,
                     mailbox.errs, act or "no flows"), flush=True)
            last_print = now


if __name__ == "__main__":
    main()
