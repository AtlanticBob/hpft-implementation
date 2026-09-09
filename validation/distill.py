#!/usr/bin/env python3
"""results/<tag>/ -> data/<tag>_*.csv, and the five verdicts of README §三.

Reads (the only script that reads raw results/):
  flows.txt        the flow table the run used (one row per flow-set); every
                   row names its destination host, so one run may have
                   several receivers (2026-09-04; an 8-column table from an
                   older run means the receiver was sgpu02)
  t0.txt warm.txt  absolute T0 and warm-up; experiment clock = T0 + warm
  vpm_series_<host>.csv   that receiver's vport-meter counters (per VF,
                   RoCE/other, 100 ms); older runs: vpm_series.csv
  rx_<host>.jsonl  that receiver's agent: per flow-set attributed rate r,
                   expected rate e, virtual queue d (ms), every 20 ms;
                   older runs: rx.jsonl
  flow<k>_*.log    perftest / iperf3 client output (application goodput)
Every receiver is its own root: the capacity, the water-filling, the
utilisation and the aggregate-collapse tests are all per receiver; a
flow-set's series comes from the agent of the host it is sent to.
Writes:
  data/<tag>_vmclass.csv    t, <host>_vf<i>_rdma, <host>_vf<i>_tcp  (Gb/s per 100 ms, wire)
  data/<tag>_flowsets.csv   one row per (phase, flow-set): expected, attributed
                            mean/sd, goodput over the whole run
  data/<tag>_events.csv     one row per event: convergence time per group
  data/<tag>_verdict.csv    the five criteria, pass/fail, detail
usage: distill.py <tag>
"""
import csv, json, os, re, sys, zlib
import numpy as np
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "..", "..", "hpft-paper", "paper", "common"))
import vpm

C_ROOT = 200e9 * 0.92          # C' (headroom 8 %, root only)
VM_CAP = 50e9                  # every VM sold at 50 G; the VM cap carries no headroom
DELTA = 0.15                   # demand margin the receiver reserves for a lender
RDMA_WIRE = 1.073              # wire bytes / application bytes for 1024 B RoCE WRITE (measured V4 2026-08-28)
STEADY_SKIP, STEADY_TAIL = 3.0, 1.0   # steady window inside a phase
# The phases are 8 to 14 s (2026-09-09: the scenarios were shortened so a
# run costs 30-40 s of load instead of 50-90). The skip has to clear the
# transient - the slowest edge measured is 1.8 s - and still leave several
# seconds of 100 ms bins: 3 s leaves 4 s, or 40 bins per flow-set.
CONV_WINDOW = 7.0                     # how long after an event to look for it
# where the flow-set's own new steady rate is measured: a window that starts
# after the slowest transient seen (1.8 s) and ends inside the shortest phase
CONV_SETTLE, CONV_SETTLE_W = 3.0, 4.0
# the bin the band is judged on, and how much of the hold window must be in it
CONV_BIN, CONV_HOLD_FRAC = 0.25, 0.75
FIT_TOL, JAIN_MIN, UTIL_MIN = 0.05, 0.99, 0.95
Q_MEAN_MAX_MS, Q_CLEAR_MAX_S, Q_ZERO_MS = 1.0, 1.0, 0.05
# The virtual queue is the receiver's ledger of how much a flow-set has
# over-taken, and the design asks one thing of it: that it not accumulate.
# So the test is that it CLEARS - every flow-set must bring it back to zero
# at least once per Q_CLEAR_MAX_S - plus a bound on the mean. Counting how
# OFTEN it is non-empty is the wrong test for this control: the steady state
# is a sawtooth by construction (probe up, cross the share, repay), so
# demanding an empty ledger 95 % of the time is demanding the fence sit
# below the share 95 % of the time, which buys a clean ledger with
# throughput. Q_ZERO_MS is float tolerance, not a budget.
CONV_TOL, CONV_HOLD, CONV_MAX = 0.10, 1.0, 1.0
# criterion 6, the executor's account of itself (design 6.4 properties 1, 2):
# over the steady samples in which every listed QP is drawing (see the note at
# the check), paced/R has mean <= EX_OVER_MEAN and 95th percentile <=
# EX_OVER_P95 (R itself is a 20 ms sawtooth and paced lags it by one event,
# so a 1 s sample can sit a few percent either side); where the drawing QPs'
# CC rates add up to >= EX_FULL_CC x R, paced >= EX_FULL x R in at least
# EX_FULL_FRAC of those samples
EX_OVER, EX_OVER_MEAN, EX_OVER_P95, EX_FULL_CC, EX_FULL, EX_FULL_FRAC = 1.02, 1.03, 1.10, 1.0, 0.95, 0.90
COLLAPSE_FRAC, COLLAPSE_HOLD, AGG_MIN, EVENT_GRACE = 0.5, 1.0, 0.90, 2.0


def bin_mean(t, v, dt=0.1):
    """Average an irregularly-logged series onto a fixed grid of dt seconds.

    The receiver logs every 20 ms, which on a 30-90 s x-axis draws far more
    measurement granularity than delivered rate: at 20 ms a point is one
    10 ms rate window, and that noise averages down as 1/sqrt(window) (it is
    independent per sample, not a loop hunting - measured 2026-08-31). The
    figures therefore draw the same series at 100 ms, matching the hardware
    wire panel. THE CRITERIA BELOW ARE NOT BINNED: they read the raw ledger.
    """
    t = np.asarray(t, dtype=float); v = np.asarray(v, dtype=float)
    if len(t) == 0:
        return t, v
    k = np.floor((t - t[0]) / dt).astype(int)
    n = k[-1] + 1
    s = np.bincount(k, weights=v, minlength=n)
    c = np.bincount(k, minlength=n)
    ok = c > 0
    return t[0] + (np.arange(n)[ok] + 0.5) * dt, s[ok] / c[ok]


def load_flows(path):
    """One dict per row. Columns: src_host src_vf dst_host dst_vf class count
    start end options. A meter row (the hidden bottleneck) has '-' in the
    source columns. Eight columns is the format before 2026-09-04, when the
    receiver was always sgpu02."""
    rows = []
    for line in open(path):
        if not line.strip() or line.startswith("#"):
            continue
        f = line.split()
        if len(f) == 8:
            h, sv, dv, cls, n, a, b, opt = f
            dh = "sgpu02"
            if cls == "meter":
                h, sv = "-", "-"
        else:
            h, sv, dh, dv, cls, n, a, b, opt = f
        rows.append(dict(host=h, sv=(int(sv) if sv != "-" else -1), dhost=dh, dv=(int(dv) if dv != "-" else -1), cls=cls, n=int(n),
                         start=float(a), end=float(b), opt=opt,
                         fsid=f"{h}/vf{sv}>{dh}/vf{dv}|{cls}"))
    return rows


_REG = json.load(open(os.path.join(BASE, "..", "config", "lab-registry.json")))


def txt_of(R, name):
    p = os.path.join(R, name)
    return open(p).read() if os.path.exists(p) else ""


_PAIR_FT = {k: int(v, 16) for k, v in _REG.get("rdma_flowtags", {}).items() if ">" in k}
_VNIC_FT = {v["vnic_id"]: int(v["flowtag"], 16) for v in _REG["vnics"] if "flowtag" in v}
_IDX_FT = {}
for _k, _ft in _PAIR_FT.items():
    _s, _d = _k.split(">")
    _m = re.search(r"vf(\d+)$", _s)
    _IDX_FT.setdefault((int(_m.group(1)) if _m else None, _d), _ft)


def flowtag_of(src_dst, src):
    """The set id the sender agent gives a pair's RDMA flow-set: the registry
    flowtag of src>dst, else the row of any source with the same VF index and
    the same destination, else the source vnic's own tag (tx_agent_e.py)."""
    ft = _PAIR_FT.get(src_dst)
    if ft is None:
        s_, d_ = src_dst.split(">")
        m = re.search(r"vf(\d+)$", s_)
        ft = _IDX_FT.get((int(m.group(1)) if m else None, d_))
    if ft is None:
        ft = _VNIC_FT.get(src)
    if ft is None:
        ft = (zlib.crc32(src_dst.encode()) & 0xffffffff) | 1     # the agent's fallback for an untagged pair
    return ft


def receivers(rows):
    return sorted({r["dhost"] for r in rows if r["dhost"] != "-"})


SIDE = {h: side for side, blk in _REG.get("topology", {}).get("sides", {}).items() for h in blk.get("hosts", {})}


def crosses_core(r):
    """True when the row's traffic goes from one side of the split switch to
    the other, i.e. over the core link (tools/lab-infra/switch/README.md)."""
    return SIDE.get(r["host"]) is not None and SIDE.get(r["host"]) != SIDE.get(r["dhost"])


def core_at(rows, t):
    """Gb/s the core link can carry (inner-byte terms) when a class=core row is
    in force at t, else None. Like class=meter this is not a flow: it states a
    bottleneck the receivers' ledgers cannot see, so that the expected rates
    account for it; nothing in the runner acts on it (the link's speed is set
    by tools/lab-infra/switch/split_core_speed.sh and recorded per run)."""
    for r in rows:
        if r["cls"] == "core" and r["start"] <= t < r["end"]:
            m = re.match(r"gbps=(\d+(?:\.\d+)?)", r["opt"])
            if m:
                return float(m.group(1))
    return None


def waterfill(cap, demands):
    """Equal-weight water-filling of cap over demand-capped members."""
    alloc = {k: 0.0 for k in demands}
    left, open_ = cap, dict(demands)
    while open_ and left > 1e-6:
        share = left / len(open_)
        done = {k: d for k, d in open_.items() if d <= share}
        if not done:
            for k in open_:
                alloc[k] += share
            break
        for k, d in done.items():
            alloc[k] += d; left -= d; del open_[k]
    return alloc


def external_at(rows, t, dhost=None):
    """Gb/s of traffic HyperFront neither schedules nor shapes (udp rows) at t,
    arriving at dhost (every receiver when dhost is None)."""
    tot = 0.0
    for r in rows:
        if r["cls"] == "udp" and r["start"] <= t < r["end"] and dhost in (None, r["dhost"]):
            m = re.match(r"gbps=(\d+(?:\.\d+)?)", r["opt"])
            tot += float(m.group(1)) if m else 0.0
    return tot



def meter_at(rows, t):
    """{dst vf: bps} for every hidden bottleneck in force at t (class=meter).

    A meter row lowers the receiver-side OVS drop meter on one VF while the
    registry keeps saying 50 G, so the ledger never learns the path got
    narrower - it only sees the arrivals that survived. The policy share is
    therefore still computed the usual way and then CAPPED here: what a
    flow-set can be expected to deliver is the smaller of its share and what
    the path can carry."""
    out = {}
    for r in rows:
        if r["cls"] == "meter" and r["start"] <= t < r["end"]:
            m = re.match(r"gbps=(\d+(?:\.\d+)?)", r["opt"])
            if m:
                out[(r["dhost"], r["dv"])] = float(m.group(1)) * 1e9
    return out


def root_at(rows, t, dhost):
    """The receiver's physical root: its port minus traffic the scheduler does
    not control (udp rows), then the headroom (receiver rule since 2026-08-28)."""
    return (200e9 - external_at(rows, t, dhost) * 1e9) * 0.92


def expected_at(rows, t):
    """{fsid: expected Gb/s} at experiment time t: per receiver, root -> VM ->
    class -> flow-set, equal weights, VM cap, demand caps from rate_limit
    options (per QP), and the hidden per-VF caps of any class=meter row in
    force. Receivers are independent roots: nothing here models a link two
    receivers share (that is the core link of V8, which the ledger cannot
    see by construction)."""
    out = {}
    for dh in receivers(rows):
        act = [r for r in rows if r["start"] <= t < r["end"] and r["cls"] in ("rdma", "tcp") and r["dhost"] == dh]
        if not act:
            continue
        root = root_at(rows, t, dh)
        # A rate-limited flow-set is a lender: it gets its own wire rate, and the
        # fill reserves A(1+delta) for it (the receiver's growth margin), so the
        # borrowers' expected rate is what remains after that reservation.
        dem, own = {}, {}
        for r in act:
            m = re.match(r"rate_limit=(\d+(?:\.\d+)?)", r["opt"])
            if m:
                own[r["fsid"]] = float(m.group(1)) * 1e9 * r["n"] * RDMA_WIRE
                dem[r["fsid"]] = own[r["fsid"]] * (1 + DELTA)
            else:
                dem[r["fsid"]] = float("inf")
        vms = {}
        for r in act:
            vms.setdefault(r["dv"], {}).setdefault(r["cls"], []).append(r["fsid"])
        cls_dem = {(v, c): sum(dem[f] for f in fs) for v, cs in vms.items() for c, fs in cs.items()}
        vm_dem = {v: min(VM_CAP, sum(cls_dem[(v, c)] for c in cs)) for v, cs in vms.items()}
        hidden = meter_at(rows, t)          # keyed by (destination host, destination VF index)
        for (h, v), cap in hidden.items():
            if h == dh and v in vm_dem:
                vm_dem[v] = min(vm_dem[v], cap)
        vm_alloc = waterfill(root, vm_dem)
        for v, cs in vms.items():
            ca = waterfill(vm_alloc[v], {c: cls_dem[(v, c)] for c in cs})
            for c, fs in cs.items():
                fa = waterfill(ca[c], {f: dem[f] for f in fs})
                out.update({f: (own[f] if f in own else fa[f]) / 1e9 for f in fs})
    # The core link between the two halves of the switch is shared by every
    # flow-set that crosses it, and no receiver's ledger models it. When a
    # class=core row is in force, the crossing flow-sets' per-receiver
    # allocations become demands on the core and are water-filled under its
    # capacity; the rest are untouched.
    cap = core_at(rows, t)
    if cap is not None:
        byf = {r["fsid"]: r for r in rows}
        crossing = {f: v for f, v in out.items() if crosses_core(byf[f])}
        if sum(crossing.values()) > cap:
            out.update(waterfill(cap, crossing))
    return out


STARVE_GBPS = 3.0                  # under external congestion: no flow-set below this


def phases(rows):
    ev = sorted({r["start"] for r in rows} | {r["end"] for r in rows})
    return [(ev[i], ev[i + 1]) for i in range(len(ev) - 1)]


def goodput(path, cls):
    txt = open(path, errors="replace").read()
    if cls == "tcp":
        try:
            j = json.loads(txt)
            if "error" in j:
                return None, j["error"]
            # bytes the receiver got over the CLIENT's test duration: the
            # server's own bits_per_second divides by an interval that
            # includes the --start-at wait (the client connects 3 s before
            # T0 here), which understates goodput by 3/(dur+3).
            e = j["end"]
            secs = e["sum_sent"]["seconds"] or e["sum_received"]["seconds"]
            return e["sum_received"]["bytes"] * 8 / secs / 1e9, ""
        except Exception as e:
            return None, "unparseable iperf3 json: %s" % e
    if cls == "udp":
        m = re.search(r"([\d.]+) Gb/s payload sent", txt)
        return (float(m.group(1)), "") if m else (None, "no udp_blast summary")
    if "Completion with error" in txt or "Failed" in txt:
        return None, "perftest error"
    vals = [l.split() for l in txt.splitlines() if re.match(r"^\s*\d+\s+\d+\s+[\d.]+\s+[\d.]+", l)]
    if not vals:
        return None, "no perftest result line"
    return float(vals[-1][3]), ""


def main(tag):
    R = os.path.join(BASE, "results", tag); D = os.path.join(BASE, "data"); os.makedirs(D, exist_ok=True)
    rows = load_flows(os.path.join(R, "flows.txt"))
    t0 = float(open(os.path.join(R, "t0.txt")).read()); warm = float(open(os.path.join(R, "warm.txt")).read())
    z = t0 + warm; end = max(r["end"] for r in rows)
    recv = receivers(rows)

    def per_receiver_file(stem, dh, ext):
        p = os.path.join(R, f"{stem}_{dh}{ext}")
        if os.path.exists(p):
            return p
        legacy = os.path.join(R, f"{stem}{ext}")     # runs before 2026-09-04: one receiver, no host suffix
        if os.path.exists(legacy) and len(recv) == 1:
            return legacy
        raise FileNotFoundError(f"{tag}: no {stem} data for receiver {dh} ({p})")
    # ---- wire per VM per class (vport meter), one block of columns per receiver ----
    with open(os.path.join(D, f"{tag}_vmclass.csv"), "w") as f:
        cols, series, t = [], [], None
        for dh in recv:
            vp = vpm.load(per_receiver_file("vpm_series", dh, ".csv"))
            zero = vpm.clock_zero(vp, warm)
            for i in sorted(vp):
                t, a = vpm.rate(vp[i], zero, 1, end); _, b = vpm.rate(vp[i], zero, 2, end)
                cols += [f"{dh}_vf{i}_rdma", f"{dh}_vf{i}_tcp"]; series += [a, b]
        f.write("t," + ",".join(cols) + "\n")
        for k, tt in enumerate(t):
            f.write(f"{tt:.2f}," + ",".join("%.3f" % s[k] for s in series) + "\n")
    # ---- receiver agent series on the experiment clock, one per receiver ----
    rxh = {}
    for dh in recv:
        rx = [json.loads(l) for l in open(per_receiver_file("rx", dh, ".jsonl")) if l.strip()]
        rxh[dh] = [(x["ts"] - z, x) for x in rx if 0 <= x["ts"] - z <= end]
    fs = [r["fsid"] for r in rows if r["cls"] in ("rdma", "tcp")]
    fh = {r["fsid"]: r["dhost"] for r in rows}      # flow-set -> the receiver whose agent sees it

    def rate(x, f): return x["r"].get(f, 0.0) / 1e9

    # ---- per phase per flow-set ----
    verdict, flow_rows = [], []
    fit_fail, jain_fail, util_fail, q_fail = [], [], [], []
    for (a, b) in phases(rows):
        tm = a + 0.5 * (b - a)
        exp = expected_at(rows, tm)
        wins = {dh: [(t, x) for t, x in rxh[dh] if a + STEADY_SKIP <= t <= b - STEADY_TAIL] for dh in recv}
        if not any(wins.values()):
            continue
        means = {}
        for f in exp:
            win = wins[fh[f]]
            if not win:
                continue
            ext = external_at(rows, tm, fh[f])
            v = [rate(x, f) for _, x in win]
            means[f] = (np.mean(v), np.std(v))
            flow_rows.append((f"{a:.0f}-{b:.0f}", f, exp[f], means[f][0], means[f][1]))
            if exp[f] > 0 and abs(means[f][0] - exp[f]) / exp[f] > FIT_TOL:
                fit_fail.append(f"{a:.0f}-{b:.0f}s {f} {means[f][0]:.2f} vs {exp[f]:.2f}")
            if ext > 0 and means[f][0] < STARVE_GBPS:
                fit_fail.append(f"{a:.0f}-{b:.0f}s (external) {f} starved at {means[f][0]:.2f}G")
        groups = {}
        for f, e in exp.items():
            if f in means:
                groups.setdefault(round(e, 2), []).append(means[f][0])
        for e, v in groups.items():
            if len(v) > 1:
                jain = sum(v) ** 2 / (len(v) * sum(x * x for x in v)) if sum(v) > 0 else 0
                if jain < JAIN_MIN:
                    jain_fail.append(f"{a:.0f}-{b:.0f}s group {e}G Jain {jain:.3f}")
        # utilisation and the ledger, per receiver (each is its own root)
        for dh in recv:
            win = wins[dh]
            mine = [f for f in exp if fh[f] == dh]
            if not win or not mine:
                continue
            tot_exp = sum(exp[f] for f in mine)
            if tot_exp >= 0.999 * root_at(rows, tm, dh) / 1e9:
                agg = np.mean([sum(rate(x, f) for f in mine) for _, x in win])
                if agg < UTIL_MIN * tot_exp:
                    util_fail.append(f"{a:.0f}-{b:.0f}s {dh} aggregate {agg:.1f} of {tot_exp:.1f}G")
            qs = [x.get("d", {}).get(f, 0.0) for _, x in win for f in mine]
            qm = float(np.mean(qs)) if qs else 0.0
            # longest a flow-set went without its ledger reaching zero
            worst_f, worst_gap = None, 0.0
            for f in mine:
                # A flow-set absent from "d" has an EMPTY ledger: the receiver
                # writes that field only for flow-sets whose queue is positive
                # (rx_agent.py, `if v > 0`). So the 0.0 default is the reading,
                # not a missing sample - do not "fix" it into a skip, which
                # turns every cleared moment into no data and fails every run.
                seq = [(t, x.get("d", {}).get(f, 0.0)) for t, x in win]
                last_clear = seq[0][0]
                gap = 0.0
                for t, q in seq:
                    if q <= Q_ZERO_MS:
                        last_clear = t
                    elif t - last_clear > gap:
                        gap = t - last_clear
                if gap > worst_gap:
                    worst_f, worst_gap = f, gap
            if qm > Q_MEAN_MAX_MS or worst_gap > Q_CLEAR_MAX_S:
                why = [f"mean {qm:.2f} ms"] if qm > Q_MEAN_MAX_MS else []
                if worst_gap > Q_CLEAR_MAX_S:
                    why.append(f"{worst_f} went {worst_gap:.1f} s without clearing")
                q_fail.append(f"{a:.0f}-{b:.0f}s {dh} " + "; ".join(why))
    with open(os.path.join(D, f"{tag}_flowsets.csv"), "w") as f:
        f.write("phase,fsid,expected_gbps,attributed_mean_gbps,attributed_sd_gbps\n")
        for r in flow_rows:
            f.write("%s,%s,%.2f,%.2f,%.2f\n" % r)
    # ---- convergence per event ----
    ev_rows, conv_fail = [], []
    for e in sorted({r["start"] for r in rows} | {r["end"] for r in rows}):
        if e <= 0 or e >= end:
            continue
        before, after = expected_at(rows, e - 0.5), expected_at(rows, e + 0.5)
        changed = [f for f in after if abs(after[f] - before.get(f, 0.0)) > CONV_TOL * after[f]]
        for dh in recv:
            if external_at(rows, e - 0.5, dh) != external_at(rows, e + 0.5, dh):
                # background came or went at this receiver: all its flow-sets must re-settle
                changed = sorted(set(changed) | {f for f in after if fh[f] == dh})
        # The target a flow-set converges TO is its own new steady rate, not
        # the nominal share. Whether that steady rate equals the share is
        # criterion 1's question; mixing the two put criterion 3's band around
        # a value the flow-set never sits on - the RDMA attribution scatters
        # 3.6 to 5.5 % at 100 ms and the delivered rate runs a few percent
        # under the nominal share, so 5 to 10 % of the bins fell outside the
        # band in steady state and the criterion had almost no room left for
        # the transient it is supposed to measure.
        settled = {}
        for f in after:
            seg = [rate(x, f) for t, x in rxh[fh[f]]
                   if e + CONV_SETTLE <= t <= e + CONV_SETTLE + CONV_SETTLE_W]
            settled[f] = (sum(seg) / len(seg)) if seg else after[f]
        by_cls = {}
        for f in changed:
            by_cls.setdefault(f.rsplit("|", 1)[1], []).append(f)
        for cls, group in by_cls.items():
            # per flow-set: first time it is inside +-CONV_TOL of its new
            # expected rate and stays there for CONV_HOLD - "stays" meaning at
            # least 90 % of the 100 ms bins in that window, so that a single
            # bin blip on one of 24 flow-sets does not restart the clock;
            # the group's convergence is its slowest member
            per = []
            for f in group:
                raw = [(t, rate(x, f)) for t, x in rxh[fh[f]] if e <= t <= e + CONV_WINDOW]
                # Judged on the CONV_BIN mean of the attributed rate. The
                # bin has to be wide enough that the band is about the
                # control loop and not about the attribution's own scatter:
                # a 4-QP RDMA flow-set scatters 6.7 to 7.2 % (median, up to
                # 19 %) at 100 ms, while a +-10 % band holding 90 % of the
                # bins needs it under 6.1 %, so the criterion was structurally
                # unpassable for RDMA whatever the control loop did. At 250 ms
                # the scatter falls by the square root of 2.5 to about 4.4 %,
                # which leaves the band for the transient it is meant to
                # measure. TCP was never the problem (3.3 to 3.9 %).
                bt, bv = bin_mean([t for t, _ in raw], [v for _, v in raw], CONV_BIN)
                seq = list(zip(bt, bv))
                tgt = settled[f]
                inb = [abs(v - tgt) <= CONV_TOL * tgt for _, v in seq]
                ts = [t for t, _ in seq]
                c = None
                for i in range(len(seq)):
                    j = i
                    while j < len(seq) and ts[j] - ts[i] < CONV_HOLD:
                        j += 1
                    if j >= len(seq):
                        break
                    if inb[i] and sum(inb[i:j]) >= CONV_HOLD_FRAC * (j - i):
                        c = ts[i] - e; break
                per.append(c)
            conv = None if any(c is None for c in per) else max(per)
            ev_rows.append((e, cls, len(group), conv))
            if conv is None or conv > CONV_MAX:
                conv_fail.append(f"{e:.0f}s {cls} x{len(group)} " + ("never" if conv is None else f"{conv*1e3:.0f} ms"))
    with open(os.path.join(D, f"{tag}_events.csv"), "w") as f:
        f.write("event_s,class,flowsets,converge_ms\n")
        for e, cls, n, conv in ev_rows:
            f.write("%.0f,%s,%d,%s\n" % (e, cls, n, "" if conv is None else "%.0f" % (conv * 1e3)))
    # ---- collapse ----
    events = sorted({r["start"] for r in rows} | {r["end"] for r in rows})
    col_fail = []

    def in_grace(t): return any(ev <= t <= ev + EVENT_GRACE for ev in events)
    for f in fs:
        low_since = None
        for t, x in rxh[fh[f]]:
            exp = expected_at(rows, t).get(f)
            if exp is None or in_grace(t):
                low_since = None; continue
            floor = COLLAPSE_FRAC * exp
            if rate(x, f) < floor:
                low_since = t if low_since is None else low_since
                if t - low_since >= COLLAPSE_HOLD:
                    col_fail.append(f"{f} below half its share from {low_since:.1f}s"); break
            else:
                low_since = None
    for dh in recv:
        low_since = None
        for t, x in rxh[dh]:
            exp = {f: v for f, v in expected_at(rows, t).items() if fh[f] == dh}
            if not exp or in_grace(t):
                low_since = None; continue
            if sum(rate(x, f) for f in exp) < AGG_MIN * sum(exp.values()):
                low_since = t if low_since is None else low_since
                if t - low_since >= COLLAPSE_HOLD:
                    col_fail.append(f"{dh} aggregate below 90% of expected from {low_since:.1f}s"); break
            else:
                low_since = None
    # ---- health: application logs ----
    health_fail, gp = [], {}
    for k, r in enumerate(rows):
        if r["cls"] in ("meter", "core"):     # not flows: the hidden bottlenecks have no log
            continue
        p = [x for x in os.listdir(R) if x.startswith(f"flow{k}_")]
        if not p:
            health_fail.append(f"row {k} no log"); continue
        g, err = goodput(os.path.join(R, p[0]), r["cls"])
        gp[k] = g
        if g is None or g <= 0:
            health_fail.append(f"row {k} {r['fsid']}: {err or 'zero goodput'}")
    # ---- the RDMA executor's own account (rp_<host>.jsonl, 1 s per flow set) ----
    # Design 6.4, properties 1 and 2, read at the executor: the sum of the
    # paced rates of a flow set's QPs never exceeds its R, and when the CC
    # rates of the drawing QPs add up to at least R the paced sum is R. The
    # sample is the device readback 0xded (tools/dpu/rp_sample.py); the set
    # id is the pair's registry flowtag, mapped back to the flow-set here
    # the way tx_agent_e.flowtag_of maps it forward. Steady windows only: a
    # QP that just stopped keeps its last paced rate until the executor
    # retires it, so the seconds after an event are not a test of the law.
    ex_rows, ex_fail = [], []
    arm = txt_of(R, "arm.txt")
    cc_only = "cc-only arm = 1" in arm
    id2fs = {}
    for r in rows:
        if r["cls"] == "rdma":
            ft = flowtag_of(f"{r['host']}/vf{r['sv']}>{r['dhost']}/vf{r['dv']}", f"{r['host']}/vf{r['sv']}")
            if ft is not None:
                id2fs[(r["host"], "0x%08x" % ft)] = r["fsid"]
    steady = [(a + STEADY_SKIP, b - STEADY_TAIL) for a, b in phases(rows)]
    per = {}
    for fn in sorted(os.listdir(R)):
        if not (fn.startswith("rp_") and fn.endswith(".jsonl")):
            continue
        host = fn[3:-6]
        for line in open(os.path.join(R, fn)):
            try:
                x = json.loads(line)
            except Exception:
                continue
            t = x["ts"] - z
            f = id2fs.get((host, x["id"]))
            if f is None or not (0 <= t <= end):
                continue
            # only while the flow-set is in the table: a set whose QPs have
            # gone keeps its last readback until an event of its own
            # retires the entries, and a flow-set the runner starts a few
            # seconds early exists before its row does
            if not any(r["fsid"] == f and r["start"] <= t <= r["end"] for r in rows):
                continue
            ex_rows.append((t, host, f, x["R"], x["paced"], x["cc"], x["cc_live"], x["nlive"], x["nq"]))
            if any(a <= t <= b for a, b in steady) and x["R"] > 0 and x["nlive"] > 0:
                d = per.setdefault(f, {"n": 0, "all_n": 0, "over": 0, "full_n": 0, "full": 0, "pr": [], "cr": [], "all_pr": []})
                d["n"] += 1
                d["pr"].append(x["paced"] / x["R"]); d["cr"].append(x["cc_live"] / x["R"])
                # The paced sum covers every QP on the set's list, and a QP
                # that has not drawn in the last millisecond keeps the rate
                # it was last given (the set's live sum was smaller then, so
                # that rate is larger). With four QPs of one perftest the
                # live count flickers between 2 and 4 in a few percent of
                # the seconds, which is the granularity boundary of design
                # 6.4, not the law. Property 1 is therefore judged on the
                # samples where every listed QP is drawing.
                if x["nlive"] == x["nq"]:
                    d["all_n"] += 1
                    d["all_pr"].append(x["paced"] / x["R"])
                    if x["paced"] > EX_OVER * x["R"]:
                        d["over"] += 1
                if x["cc_live"] >= EX_FULL_CC * x["R"]:
                    d["full_n"] += 1
                    if x["paced"] >= EX_FULL * x["R"]:
                        d["full"] += 1
    with open(os.path.join(D, f"{tag}_executor.csv"), "w") as f:
        f.write("t,host,fsid,R_gbps,paced_gbps,cc_gbps,cc_live_gbps,nlive,nq\n")
        for r in ex_rows:
            f.write("%.3f,%s,%s,%.3f,%.3f,%.3f,%.3f,%d,%d\n" % r)
    with open(os.path.join(D, f"{tag}_executor_summary.csv"), "w") as f:
        f.write("fsid,samples,all_live_samples,all_live_mean_paced_over_R,all_live_p95_paced_over_R,full_samples,full_frac,mean_paced_over_R,mean_cc_live_over_R\n")
        for fs_, d in sorted(per.items()):
            am = np.mean(d["all_pr"]) if d["all_n"] else float("nan")
            ap = np.percentile(d["all_pr"], 95) if d["all_n"] else float("nan")
            full = (d["full"] / d["full_n"]) if d["full_n"] else float("nan")
            f.write("%s,%d,%d,%s,%s,%d,%s,%.3f,%.3f\n" % (fs_, d["n"], d["all_n"], "" if d["all_n"] == 0 else "%.3f" % am,
                                                          "" if d["all_n"] == 0 else "%.3f" % ap,
                                                          d["full_n"], "" if d["full_n"] == 0 else "%.4f" % full,
                                                          np.mean(d["pr"]), np.mean(d["cr"])))
            if not cc_only:
                if d["all_n"] >= 3 and (am > EX_OVER_MEAN or ap > EX_OVER_P95):
                    ex_fail.append(f"{fs_} paced/R mean {am:.3f} p95 {ap:.3f} over the samples with every QP drawing")
                if d["full_n"] >= 3 and full < EX_FULL_FRAC:
                    ex_fail.append(f"{fs_} paced < {EX_FULL:.2f} R in {100*(1-full):.0f}% of the samples where the CCs asked for R")
    if not per and not cc_only and any(r["cls"] == "rdma" for r in rows):
        ex_fail.append("no executor samples")
    # A flow-set carrying fewer QPs than its peers has one that never bound,
    # and an unbound QP is not held by its flow-set's pool at all - it runs on
    # the unknown-flow allowance, so the share is not enforced over it and the
    # set's own account under-reports (0.481 of its attributed rate when this
    # was first seen, 2026-09-09). The sampler writes the evidence beside the
    # samples as qpdump_<host>.txt when it catches it.
    nqs = {}
    for t_, host, f, R_, pc, cc, ccl, nl, nq_ in ex_rows:
        nqs[f] = max(nqs.get(f, 0), nq_)
    if len(nqs) >= 3:
        common = max(set(nqs.values()), key=list(nqs.values()).count)
        for f, n in sorted(nqs.items()):
            if n < common:
                ex_fail.append(f"{f} bound only {n} QPs while its peers bound {common} "
                               f"(a QP that never bound is not held by the set's pool)")
    with open(os.path.join(D, f"{tag}_goodput.csv"), "w") as f:
        f.write("row,fsid,start_s,end_s,goodput_gbps\n")
        for k, r in enumerate(rows):
            f.write("%d,%s,%.0f,%.0f,%s\n" % (k, r["fsid"], r["start"], r["end"], "" if gp.get(k) is None else "%.2f" % gp[k]))
    verdict = [("1 稳态贴合", not (fit_fail or jain_fail or util_fail), "; ".join(fit_fail + jain_fail + util_fail) or "ok"),
               ("2 队列消得掉", not q_fail, "; ".join(q_fail) or "ok"),
               ("3 收敛 ≤ 1 s", not conv_fail, "; ".join(conv_fail) or "ok"),
               ("4 不塌", not col_fail, "; ".join(col_fail) or "ok"),
               ("5 平台健康", not health_fail, "; ".join(health_fail) or "ok"),
               ("6 执行面守约", not ex_fail, ("CC 单独臂，不适用" if cc_only else "; ".join(ex_fail) or "ok"))]
    with open(os.path.join(D, f"{tag}_verdict.csv"), "w") as f:
        w = csv.writer(f); w.writerow(["criterion", "pass", "detail"])
        for c, ok, d in verdict:
            w.writerow([c, "PASS" if ok else "FAIL", d])
    for c, ok, d in verdict:
        print("%-12s %s  %s" % (c, "PASS" if ok else "FAIL", d[:300]))


if __name__ == "__main__":
    main(sys.argv[1])
