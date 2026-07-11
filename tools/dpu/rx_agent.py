#!/usr/bin/env python3
"""Scheme-E receiver-side agent: the hierarchical virtual scheduler
(design_e §3.3-§3.4). Runs on the receiver DPU Arm as root.

Pipeline per control period T:
  meter   hybrid r_f (see below): fresh per-dst-VF totals x flow-set mix
  sched   demand D_f = r_f(1+delta) -> 3-layer weighted water-filling
          (VM cap MaxRate_d -> class -> per-sender) -> entitled rate e_f
  marker  vq_f <- clip(vq_f + (r_f - e_f)T, 0, Vmax); s_f = min(vq_f/V, 1)
  telem   one UDP datagram per sender DPU per tick: {fsid: {s, r, e}};
          s_f = 0 is sent too (explicit freshness permit, §3.5 fail-open)

Hybrid r_f measurement (lab fact, 2026-07-09): megaflow HW byte counters
are exact but only refresh ~1 Hz (mlx5 fc bulk-query period, hardcoded in
5.15). Representor vport counters are fresh at any rate but per-VF totals.
So: r_f = R_d(vport, fresh every tick) x share_f(megaflow bytes over a
sliding window). Megaflow KEYS appear instantly, so a single new flow-set
on a dst is attributed 100% from its first tick; multiple simultaneous new
flow-sets split equally until counters land (<=1 s). Single-class-per-VF
scenarios (M1/M1b/M3) are exact; class-mix transitions (M2) carry <=1 s
attribution lag - reported with M2 results.

Implementation choice (design silent on r_f = 0): an idle flow-set's vq
drains fast (V per period). Frozen marks would otherwise pin the sender at
the floor after the app pauses, which contradicts fail-open intent.

Policy (weights/MaxRate) hot-reloads when the registry file mtime changes.
"""
import argparse
import json
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

    def __init__(self, rep_of_vnic, mix_window_s, rate_window_s=0.006):
        self.rep_of_vnic = rep_of_vnic    # local vnic_id -> representor dev
        self.window = mix_window_s
        self.rate_window = rate_window_s
        self.hist = []                    # (t, fs_delta_bytes) for the mix
        self.ring = {}                    # vnic -> deque of (t, bytes)
        self._fd = {}
        self.r_d = {}                     # vnic -> fresh vport rate
        self.mix_shares = {}              # fsid -> fraction of its dst total
        self.by_dst = {}                  # dst vnic -> set(fsid)
        self.unattributed = 0.0

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

    def rates(self):
        """Compute r_f from the last sample() x cached class-mix."""
        rates = {}
        self.unattributed = 0.0
        for dst, total in self.r_d.items():
            if total <= 0:
                continue
            members = self.by_dst.get(dst)
            if not members:
                self.unattributed += total
                continue
            for f in members:
                share = self.mix_shares.get(f, 1.0 / len(members))
                if share > 0:
                    rates[f] = total * share
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


class DemandHold:
    """F1 (standing-deficit fix, 2026-07-10): demand estimate holds the
    windowed peak so a tenant-CC dip does not instantly collapse the
    grant (e_sum p5 was 2.4G of 6G; see standing_deficit_analysis.md).
    Two half-window buckets -> peak decays within [hold/2, hold].
    Cost: borrowing reclaim is delayed by <= hold (~10 periods)."""

    def __init__(self, hold_s):
        self.hold = hold_s
        self.b = {}   # fs -> [bucket_start, peak_cur, peak_prev]

    def peaks(self, rates, now):
        if self.hold <= 0:
            return rates
        out = {}
        for f, r in rates.items():
            b = self.b.get(f)
            if b is None:
                b = self.b[f] = [now, r, 0.0]
            if now - b[0] >= self.hold / 2:
                b[2] = b[1] if now - b[0] < self.hold else 0.0
                b[0], b[1] = now, r
            b[1] = max(b[1], r)
            out[f] = max(r, b[1], b[2])
        for f in list(self.b):
            if f not in rates:
                del self.b[f]
        return out


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
    """Per-flow-set virtual-queue integrator -> mark s_f (design_e §3.3)."""

    def __init__(self, v_full, v_max):
        self.v_full = v_full    # bits at which s saturates to 1
        self.v_max = v_max      # anti-windup clip
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


class Telemetry:
    """One binary datagram per destination per tick. At a 1ms period, JSON
    encode/decode of a per-flow-set dict is a real cost; a fixed struct is
    an order of magnitude cheaper and smaller on the wire. Wire format:
      header  '<HH'  = (seq_lo16, n_records)
      record  '<64s f Q Q'  = (fsid[64], s, r_bps, e_bps)
    fsid strings are short ('sgpu01/vf0>sgpu02/vf0|tcp' ~ 26 B); r/e use
    uint64 because bps at 200G line rate overflows uint32.

    Dual-cast (2026-07-11): each record goes both to the sender DPU (which
    runs the RDMA response law + mailbox) and, for the tcp class, to the
    sender host's pace shim, so the TCP law runs one hop closer to its
    actuator. Destinations resolved from control.telemetry_ip (DPU) and
    control.pace_shim (host)."""

    HDR = struct.Struct("<HH")
    REC = struct.Struct("<64sfQQ")

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
        for fsid, s, r, e in recs:
            out.append(self.REC.pack(fsid.encode()[:64], s, int(r), int(e)))
        return b"".join(out)

    def send(self, marks, rates, ents):
        self.seq += 1
        per_dpu = {}     # dpu ip -> [records]
        per_shim = {}    # (shim ip, port) -> [tcp records]
        for f, s in marks.items():
            src, cls = f.split(">")[0], f.rsplit("|", 1)[1]
            host = self.vnic_host.get(src)
            rec = (f, s, rates.get(f, 0), ents.get(f, 0))
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

    v_full = ep["v_periods"] * period * line * ep["headroom"]
    v_max = ep["vmax_over_v"] * v_full

    added = install_class_rules(args.bridge)
    print("rx_agent[E]: bridge=%s rules_added=%s T=%.0fms local=%s "
          "V=%.0fMbit log=%s meter_only=%s"
          % (args.bridge, added or "none", period * 1e3, local_host,
             v_full / 1e6, args.log, args.meter_only), flush=True)

    uc = Unixctl()
    meter = FlowSetMeter(mac2vnic, local_macs)
    hybrid = HybridRates(
        {v["vnic_id"]: v["representor"] for v in reg["vnics"]
         if v["host"] == local_host},
        ep.get("mix_window_s", 2.0),
        rate_window_s=ep.get("rate_window_s", 0.006))
    sched = Scheduler(reg["policy"], line, ep["headroom"], ep["delta_demand"])
    last_seen = {}
    marker = VQMarker(v_full, v_max)
    ctl = reg["control"]
    telem = Telemetry(vnic_host, ctl["telemetry_ip"], ep["telemetry_port"],
                      shim_ip=ctl.get("shim_ip"),
                      shim_port=ctl.get("shim_telemetry_port"))
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
        rates = hybrid.rates()
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
            ents, marks = {}, {}
        else:
            nowm = now
            for f in sched_rates:
                last_seen[f] = nowm
            active = dict(sched_rates)
            grace = ep.get("share_floor_grace_s", 0.5)
            for f, t_ in list(last_seen.items()):
                if f not in active:
                    if nowm - t_ <= grace:
                        active[f] = 0.0
                    else:
                        del last_seen[f]
            if ep.get("share_floor", False):
                shat = sched._fill({f: float("inf") for f in active})
                demand = {f: max(r * (1.0 + ep["delta_demand"]),
                                 shat.get(f, 0.0))
                          for f, r in active.items()}
            else:
                demand = {f: r * (1.0 + ep["delta_demand"])
                          for f, r in active.items()}
            ents, ceils = sched.entitlements(active, demand)
            marks = marker.step(sched_rates, ents, ceils, dt)
            telem.send(marks, sched_rates, ents)

        # throttled logging (default ~50 Hz), never every 1ms tick
        if nticks_total % log_every == 0 and nticks_total:
            rec = {"ts": round(time.time(), 4), "dt_s": round(dt, 6),
                   "read_ms": round(dump_ms, 3), "nfs": len(sched_rates),
                   "r": {f: int(v) for f, v in rates.items()},
                   "e": {f: int(v) for f, v in ents.items()},
                   "s": {f: round(v, 4) for f, v in marks.items()},
                   "vq": {f: int(v) for f, v in marker.vq.items()}}
            logf.write(json.dumps(rec) + "\n")

        nticks_total += 1
        if now - last_print >= 1.0:
            act = " ".join("%s r=%.2fG e=%.2fG s=%.2f"
                           % (f, rates.get(f, 0) / 1e9,
                              ents.get(f, 0) / 1e9, marks.get(f, 0))
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
