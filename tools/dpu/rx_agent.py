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
import subprocess

from fastfill import waterfill_ceilings
import time

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
    (see module docstring)."""

    MIN_DT = 0.03   # s; short ticks (sampling jitter after a slow read)
                    # produce garbage delta/dt spikes -> phantom vq charges

    def __init__(self, rep_of_vnic, mix_window_s):
        self.rep_of_vnic = rep_of_vnic    # local vnic_id -> representor dev
        self.window = mix_window_s
        self.hist = []                    # (t, fs_delta_bytes)
        self.prev = {}                    # vnic -> (bytes, t)
        self._fd = {}
        self.unattributed = 0.0           # bps seen on vports w/o any fs key
        self.last_rates = {}
        self.last_t = 0.0

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

    def tick(self, fs_deltas, fs_present, now):
        if now - self.last_t < self.MIN_DT:
            # jittery short tick: keep counters unsampled (deltas roll into
            # the next tick) and reuse the previous rates
            self.hist.append((now, fs_deltas))
            return self.last_rates
        self.last_t = now
        # fresh totals per dst vnic
        r_d = {}
        for vnic, dev in self.rep_of_vnic.items():
            v = self._rep_tx_bytes(dev)
            if v is None:
                continue
            pv, pt = self.prev.get(vnic, (v, now))
            self.prev[vnic] = (v, now)
            dt = now - pt
            if dt > 0:
                r_d[vnic] = (v - pv) * 8 / dt if v >= pv else 0.0
        # windowed mix
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
        rates = {}
        self.unattributed = 0.0
        for dst, total in r_d.items():
            if total <= 0:
                continue
            members = by_dst.get(dst)
            if not members:
                self.unattributed += total
                continue
            wtot = sum(win.get(f, 0) for f in members)
            for f in members:
                share = (win.get(f, 0) / wtot) if wtot > 0 \
                    else 1.0 / len(members)
                if share > 0:
                    rates[f] = total * share
        self.last_rates = rates
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

    def entitlements(self, rates):
        """rates: {fsid: r_f} -> ({fsid: e_f}, {fsid: ceil_f}).

        e_f: demand-capped water-filling share (design §3.3) - the grant.
        ceil_f: same tree with infinite demands - the fair-share ceiling.
        The VQ charges by excess over e_f but DRAINS by (ceil_f - r): with
        drain keyed to demand-capped e_f a crushed flow-set (r tiny -> e
        tiny) could never drain its vq and stayed marked forever (observed
        2026-07-09). At equilibrium r ~= ceil_f so drain ~= 0: no free
        unmarking."""
        demand = {f: r * (1.0 + self.delta) for f, r in rates.items()}
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
    """One datagram per sender DPU per tick: {seq, ts, fs:{fsid:{s,r,e}}}."""

    def __init__(self, vnic_host, telemetry_ip, port):
        self.vnic_host = vnic_host        # vnic_id -> host
        self.telemetry_ip = telemetry_ip  # host -> its DPU ctl IP
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.seq = 0

    def send(self, marks, rates, ents):
        self.seq += 1
        per_dpu = {}
        for f, s in marks.items():
            src = f.split(">")[0]
            ip = self.telemetry_ip.get(self.vnic_host.get(src))
            if ip is None:
                continue
            per_dpu.setdefault(ip, {})[f] = {
                "s": round(s, 4),
                "r": int(rates.get(f, 0)),
                "e": int(ents.get(f, 0))}
        now = time.time()
        for ip, fs in per_dpu.items():
            msg = {"seq": self.seq, "ts": round(now, 4), "fs": fs}
            try:
                self.sock.sendto(json.dumps(msg).encode(), (ip, self.port))
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
        ep.get("mix_window_s", 2.0))
    sched = Scheduler(reg["policy"], line, ep["headroom"], ep["delta_demand"])
    marker = VQMarker(v_full, v_max)
    telem = Telemetry(vnic_host, reg["control"]["telemetry_ip"],
                      ep["telemetry_port"])
    logf = open(args.log, "a", buffering=1)

    t_start = time.monotonic()
    t_prev = t_start
    next_tick = t_start
    read_ms_acc, read_ms_max, nticks = 0.0, 0.0, 0
    last_print = t_start
    first_tick = True
    nticks_total = 0
    last_speed = line * 1.0

    while True:
        now = time.monotonic()
        if args.duration and now - t_start >= args.duration:
            break
        r0 = time.monotonic()
        try:
            txt = uc.call("dpctl/dump-flows", ["type=offloaded"])
        except (OSError, RuntimeError) as e:
            print("rx_agent: unixctl failed (%s), fallback ovs-appctl" % e,
                  flush=True)
            txt = subprocess.run(
                ["ovs-appctl", "dpctl/dump-flows", "type=offloaded"],
                capture_output=True, text=True, check=True).stdout
        read_ms = (time.monotonic() - r0) * 1e3
        dt = time.monotonic() - t_prev
        t_prev = time.monotonic()

        fs_deltas, fs_present = meter.tick(txt)
        rates = hybrid.tick(fs_deltas, fs_present, time.monotonic())
        sched_rates = {f: r for f, r in rates.items()
                       if f.rsplit("|", 1)[1] in SCHED_CLASSES}
        if args.meter_only:
            ents, marks = {}, {}
            stage_us = (0, 0)
        else:
            _i0 = time.monotonic()
            ents, ceils = sched.entitlements(sched_rates)
            _i1 = time.monotonic()
            marks = marker.step(sched_rates, ents, ceils, dt)
            telem.send(marks, sched_rates, ents)
            _i2 = time.monotonic()
            stage_us = (int((_i1 - _i0) * 1e6), int((_i2 - _i1) * 1e6))

        if not first_tick:
            rec = {"ts": round(time.time(), 4), "dt_s": round(dt, 6),
                   "read_ms": round(read_ms, 3),
                   "nfs": len(sched_rates),
                   "us": stage_us if not args.meter_only else (0, 0),
                   "r": {f: int(v) for f, v in rates.items()},
                   "e": {f: int(v) for f, v in ents.items()},
                   "s": {f: round(v, 4) for f, v in marks.items()},
                   "vq": {f: int(v) for f, v in marker.vq.items()}}
            logf.write(json.dumps(rec) + "\n")
        first_tick = False

        # policy hot-reload on registry mtime change
        try:
            mt = os.stat(args.registry).st_mtime
            if mt != reg_mtime:
                reg_mtime = mt
                reg = json.load(open(args.registry))
                sched.policy = reg["policy"]
                print("rx_agent: policy reloaded", flush=True)
        except (OSError, ValueError) as e:
            print("rx_agent: policy reload failed: %s" % e, flush=True)

        # live downlink speed (~1 Hz): root capacity follows renegotiation
        nticks_total += 1
        if nticks_total % 20 == 1:
            try:
                spd = int(open("/sys/class/net/%s/speed" % args.uplink).read())
                if spd > 0 and spd * 1e6 != last_speed:
                    last_speed = spd * 1e6
                    sched.set_downlink(last_speed)
                    print("rx_agent: downlink %d Mbps -> C_root %.1fG"
                          % (spd, sched.c_root / 1e9), flush=True)
            except (OSError, ValueError):
                pass

        nticks += 1
        read_ms_acc += read_ms
        read_ms_max = max(read_ms_max, read_ms)
        if time.monotonic() - last_print >= 1.0:
            act = " ".join("%s r=%.2fG e=%.2fG s=%.2f"
                           % (f, rates.get(f, 0) / 1e9,
                              ents.get(f, 0) / 1e9, marks.get(f, 0))
                           for f in sorted(marks) if rates.get(f, 0) > 0)
            print("tick=%d read_ms avg=%.1f max=%.1f | %s"
                  % (nticks, read_ms_acc / max(nticks, 1), read_ms_max,
                     act or "idle"), flush=True)
            if meter.unknown_macs:
                print("  unknown_macs=%s" % sorted(meter.unknown_macs),
                      flush=True)
                meter.unknown_macs.clear()
            last_print = time.monotonic()
            read_ms_acc, read_ms_max, nticks = 0.0, 0.0, 0
        next_tick += period
        sleep = next_tick - time.monotonic()
        if sleep > 0:
            time.sleep(sleep)
        else:
            next_tick = time.monotonic()   # overran: don't try to catch up


if __name__ == "__main__":
    main()
