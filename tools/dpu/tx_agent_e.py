#!/usr/bin/env python3
"""Scheme-E sender-side agent: the unified response law (design_e §3.5).
Runs on the sender DPU Arm. Replaces tx_agent2 for flow-sets under scheme E
(M1: tcp class only; rdma actuation lands in M1b).

Per telemetry record {fsid: {s, r, e}} from the owner (receiver DPU):
    s == 0:  R_f <- min(R_f + A, Tree_f)        (A: additive increase)
    s  > 0:  R_f <- R_f * (1 - beta * s)        (weightless MD)
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
import time

from fastfill import waterfill_ceilings

FIFO = "/tmp/rp_fifo"   # PCC RP mailbox (batched: 0xb47c000N ft bud rate ...)


class FlowState:
    __slots__ = ("R", "al_count", "last_rx", "last_seq", "mode", "pace", "r",
                 "log_R", "log_age", "log_mode", "R_good", "fr", "ai_run", "e_last")

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
    # A: same law form for both classes, per-class rate constant permitted
    # (design_e §3.5 v2.1 / Q24; M4 calibration)
    a_by_cls = ep.get("a_frac_linerate_by_class", {})
    a_default = ep.get("a_frac_linerate", 0.0005) * line

    def A_of(fsid):
        cls = fsid.rsplit("|", 1)[1]
        return a_by_cls.get(cls, ep.get("a_frac_linerate", 0.0005)) * line \
            if cls in a_by_cls else a_default
    beta = ep["beta"]
    theta_al, m_al, eps_al = ep["theta_al"], ep["m_al"], ep["eps_al"]
    n1, n2 = ep["n1_freeze"], ep["n2_failopen"]
    fast_rec = bool(ep.get("fast_recovery", False))
    hai_after = int(ep.get("hai_after", 0))
    hai_max = int(ep.get("hai_max", 8))
    floor = ctl["pace_floor_bps"]
    shim = PaceShim(ctl["pace_shim"][local_host])
    mailbox = RpMailbox(line)
    flowtags = {v["vnic_id"]: int(v["flowtag"], 16)
                for v in reg["vnics"] if "flowtag" in v}
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
    print("tx_agent_e: local=%s T=%.0fms A=cls%s beta=%.2f tree=dynamic(§3.6) "
          "shim=%s log=%s" % (local_host, period * 1e3, a_by_cls or a_default, beta,
                              ctl["pace_shim"][local_host],
                              args.log), flush=True)

    def actuate(fsid, st, r_bps, rdma_batch):
        pace = max(min(st.R, tree_of(fsid)), floor)
        src_dst, cls = fsid.rsplit("|", 1)
        src, dst = src_dst.split(">")
        if cls == "tcp":
            if pace != st.pace:
                shim.set_rate(src, dst, pace)
        elif cls == "rdma":
            ft = flowtags.get(src)
            if ft is not None:
                # write every tick: the RP inner loop wants a fresh rate
                rdma_batch.append((ft, pace, r_bps))
        st.pace = pace

    last_print = time.monotonic()
    last_ticker = time.monotonic()
    while True:
        # --- telemetry-driven law ---
        try:
            data, _ = sock.recvfrom(65536)
            msg = json.loads(data)
        except socket.timeout:
            msg = None
        except (ValueError, OSError) as e:
            print("tx_agent_e: bad telemetry: %s" % e, flush=True)
            msg = None
        now = time.monotonic()
        rdma_batch = []
        if msg:
            recs = {f: rec for f, rec in msg.get("fs", {}).items()
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
                st.last_seq = msg.get("seq", -1)
                s, r = rec.get("s", 0.0), rec.get("r", 0)
                st.e_last = rec.get("e", st.e_last)
                # app-limited detection
                if r < theta_al * st.R:
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
                    st.R *= (1.0 - beta * s)
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
                    if st.e_last > 0:
                        tgt = min(tgt, st.e_last * 1.15)
                    if st.R < 0.95 * tgt:
                        st.R = min((st.R + tgt) / 2.0, tree_of(fsid))
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
                    st.R = min(st.R + A_of(fsid) * mult, tree_of(fsid))
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
        mailbox.write_batch(rdma_batch)
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
