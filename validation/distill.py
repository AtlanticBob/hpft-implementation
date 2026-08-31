#!/usr/bin/env python3
"""results/<tag>/ -> data/<tag>_*.csv, and the five verdicts of README §三.

Reads (the only script that reads raw results/):
  flows.txt        the flow table the run used (one row per flow-set)
  t0.txt warm.txt  absolute T0 and warm-up; experiment clock = T0 + warm
  vpm_series.csv   receiver vport-meter counters (per VF, RoCE/other, 100 ms)
  rx.jsonl         receiver agent: per flow-set attributed rate r, expected
                   rate e, virtual queue d (ms), every 20 ms
  flow<k>_*.log    perftest / iperf3 client output (application goodput)
Writes:
  data/<tag>_vmclass.csv    t, vf<i>_rdma, vf<i>_tcp  (Gb/s per 100 ms, wire)
  data/<tag>_flowsets.csv   one row per (phase, flow-set): expected, attributed
                            mean/sd, goodput over the whole run
  data/<tag>_events.csv     one row per event: convergence time per group
  data/<tag>_verdict.csv    the five criteria, pass/fail, detail
usage: distill.py <tag>
"""
import csv, json, os, re, sys
import numpy as np
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "..", "..", "hpft-paper", "paper", "common"))
import vpm

C_ROOT = 200e9 * 0.92          # C' (headroom 8 %, root only)
VM_CAP = 50e9                  # every VM sold at 50 G; the VM cap carries no headroom
DELTA = 0.15                   # demand margin the receiver reserves for a lender
RDMA_WIRE = 1.073              # wire bytes / application bytes for 1024 B RoCE WRITE (measured V4 2026-08-28)
STEADY_SKIP, STEADY_TAIL = 5.0, 1.0   # steady window inside a phase
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
COLLAPSE_FRAC, COLLAPSE_HOLD, AGG_MIN, EVENT_GRACE = 0.5, 1.0, 0.90, 2.0


def load_flows(path):
    rows = []
    for line in open(path):
        if not line.strip() or line.startswith("#"):
            continue
        h, sv, dv, cls, n, a, b, opt = line.split()
        rows.append(dict(host=h, sv=int(sv), dv=int(dv), cls=cls, n=int(n), start=float(a), end=float(b), opt=opt,
                         fsid=f"{h}/vf{sv}>sgpu02/vf{dv}|{cls}"))
    return rows


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


def external_at(rows, t):
    """Gb/s of traffic HyperFront neither schedules nor shapes (udp rows) at t."""
    tot = 0.0
    for r in rows:
        if r["cls"] == "udp" and r["start"] <= t < r["end"]:
            m = re.match(r"gbps=(\d+(?:\.\d+)?)", r["opt"])
            tot += float(m.group(1)) if m else 0.0
    return tot



def expected_at(rows, t):
    """{fsid: expected Gb/s} at experiment time t: root -> VM -> class -> flow-set,
    equal weights, VM cap, demand caps from rate_limit options (per QP)."""
    act = [r for r in rows if r["start"] <= t < r["end"] and r["cls"] in ("rdma", "tcp")]
    if not act:
        return {}
    # the root is physical: the port minus traffic the scheduler does not
    # control (udp rows), then the headroom (receiver rule since 2026-08-28)
    root = (200e9 - external_at(rows, t) * 1e9) * 0.92
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
    vm_alloc = waterfill(root, vm_dem)
    out = {}
    for v, cs in vms.items():
        ca = waterfill(vm_alloc[v], {c: cls_dem[(v, c)] for c in cs})
        for c, fs in cs.items():
            fa = waterfill(ca[c], {f: dem[f] for f in fs})
            out.update({f: (own[f] if f in own else fa[f]) / 1e9 for f in fs})
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
            return j["end"]["sum_received"]["bits_per_second"] / 1e9, ""
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
    # ---- wire per VM per class (vport meter) ----
    vp = vpm.load(os.path.join(R, "vpm_series.csv"))
    zero = vpm.clock_zero(vp, warm)
    with open(os.path.join(D, f"{tag}_vmclass.csv"), "w") as f:
        cols, series = [], []
        for i in sorted(vp):
            t, a = vpm.rate(vp[i], zero, 1, end); _, b = vpm.rate(vp[i], zero, 2, end)
            cols += [f"vf{i}_rdma", f"vf{i}_tcp"]; series += [a, b]
        f.write("t," + ",".join(cols) + "\n")
        for k, tt in enumerate(t):
            f.write(f"{tt:.2f}," + ",".join("%.3f" % s[k] for s in series) + "\n")
    # ---- receiver agent series on the experiment clock ----
    rx = [json.loads(l) for l in open(os.path.join(R, "rx.jsonl")) if l.strip()]
    rx = [(x["ts"] - z, x) for x in rx if 0 <= x["ts"] - z <= end]
    fs = [r["fsid"] for r in rows]

    def rate(x, f): return x["r"].get(f, 0.0) / 1e9

    # ---- per phase per flow-set ----
    verdict, flow_rows = [], []
    fit_fail, jain_fail, util_fail, q_fail = [], [], [], []
    for (a, b) in phases(rows):
        exp = expected_at(rows, a + 0.5 * (b - a))
        ext = external_at(rows, a + 0.5 * (b - a))
        win = [(t, x) for t, x in rx if a + STEADY_SKIP <= t <= b - STEADY_TAIL]
        if not win:
            continue
        means = {}
        for f in exp:
            v = [rate(x, f) for _, x in win]
            means[f] = (np.mean(v), np.std(v))
            flow_rows.append((f"{a:.0f}-{b:.0f}", f, exp[f], means[f][0], means[f][1]))
            if exp[f] > 0 and abs(means[f][0] - exp[f]) / exp[f] > FIT_TOL:
                fit_fail.append(f"{a:.0f}-{b:.0f}s {f} {means[f][0]:.2f} vs {exp[f]:.2f}")
            if ext > 0 and means[f][0] < STARVE_GBPS:
                fit_fail.append(f"{a:.0f}-{b:.0f}s (external) {f} starved at {means[f][0]:.2f}G")
        groups = {}
        for f, e in exp.items():
            groups.setdefault(round(e, 2), []).append(means[f][0])
        for e, v in groups.items():
            if len(v) > 1:
                jain = sum(v) ** 2 / (len(v) * sum(x * x for x in v)) if sum(v) > 0 else 0
                if jain < JAIN_MIN:
                    jain_fail.append(f"{a:.0f}-{b:.0f}s group {e}G Jain {jain:.3f}")
        tot_exp = sum(exp.values())
        if tot_exp >= 0.999 * (200.0 - ext) * 0.92:
            agg = np.mean([sum(rate(x, f) for f in exp) for _, x in win])
            if agg < UTIL_MIN * tot_exp:
                util_fail.append(f"{a:.0f}-{b:.0f}s aggregate {agg:.1f} of {tot_exp:.1f}G")
        qs = [x.get("d", {}).get(f, 0.0) for _, x in win for f in exp]
        qm = float(np.mean(qs)) if qs else 0.0
        # longest a flow-set went without its ledger reaching zero
        worst_f, worst_gap = None, 0.0
        for f in exp:
            # A flow-set absent from "d" has an EMPTY ledger: the receiver
            # writes that field only for flow-sets whose queue is positive
            # (rx_agent.py, `if v > 0`). So the 0.0 default is the reading,
            # not a missing sample - do not "fix" it into a skip, which
            # turns every cleared moment into no data and fails every run.
            seq = [(t, x.get("d", {}).get(f, 0.0)) for t, x in win]
            if not seq:
                continue
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
            q_fail.append(f"{a:.0f}-{b:.0f}s " + "; ".join(why))
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
        if external_at(rows, e - 0.5) != external_at(rows, e + 0.5):
            changed = list(after)          # background came or went: everyone must re-settle
        by_cls = {}
        for f in changed:
            by_cls.setdefault(f.rsplit("|", 1)[1], []).append(f)
        for cls, group in by_cls.items():
            # per flow-set: first time it is inside +-CONV_TOL of its new
            # expected rate and stays there for CONV_HOLD - "stays" meaning at
            # least 90 % of the 20 ms samples in that window, so that a single
            # sample blip on one of 24 flow-sets does not restart the clock;
            # the group's convergence is its slowest member
            seq = [(t, x) for t, x in rx if e <= t <= e + 15]
            per = []
            for f in group:
                inb = [abs(rate(x, f) - after[f]) <= CONV_TOL * after[f] for _, x in seq]
                ts = [t for t, _ in seq]
                c = None
                for i in range(len(seq)):
                    j = i
                    while j < len(seq) and ts[j] - ts[i] < CONV_HOLD:
                        j += 1
                    if j >= len(seq):
                        break
                    if inb[i] and sum(inb[i:j]) >= 0.9 * (j - i):
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
        for t, x in rx:
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
    low_since = None
    for t, x in rx:
        exp = expected_at(rows, t)
        if not exp or in_grace(t):
            low_since = None; continue
        if sum(rate(x, f) for f in exp) < AGG_MIN * sum(exp.values()):
            low_since = t if low_since is None else low_since
            if t - low_since >= COLLAPSE_HOLD:
                col_fail.append(f"aggregate below 90% of expected from {low_since:.1f}s"); break
        else:
            low_since = None
    # ---- health: application logs ----
    health_fail, gp = [], {}
    for k, r in enumerate(rows):
        p = [x for x in os.listdir(R) if x.startswith(f"flow{k}_")]
        if not p:
            health_fail.append(f"row {k} no log"); continue
        g, err = goodput(os.path.join(R, p[0]), r["cls"])
        gp[k] = g
        if g is None or g <= 0:
            health_fail.append(f"row {k} {r['fsid']}: {err or 'zero goodput'}")
    with open(os.path.join(D, f"{tag}_goodput.csv"), "w") as f:
        f.write("row,fsid,start_s,end_s,goodput_gbps\n")
        for k, r in enumerate(rows):
            f.write("%d,%s,%.0f,%.0f,%s\n" % (k, r["fsid"], r["start"], r["end"], "" if gp.get(k) is None else "%.2f" % gp[k]))
    verdict = [("1 稳态贴合", not (fit_fail or jain_fail or util_fail), "; ".join(fit_fail + jain_fail + util_fail) or "ok"),
               ("2 队列消得掉", not q_fail, "; ".join(q_fail) or "ok"),
               ("3 收敛 ≤ 1 s", not conv_fail, "; ".join(conv_fail) or "ok"),
               ("4 不塌", not col_fail, "; ".join(col_fail) or "ok"),
               ("5 平台健康", not health_fail, "; ".join(health_fail) or "ok")]
    with open(os.path.join(D, f"{tag}_verdict.csv"), "w") as f:
        w = csv.writer(f); w.writerow(["criterion", "pass", "detail"])
        for c, ok, d in verdict:
            w.writerow([c, "PASS" if ok else "FAIL", d])
    for c, ok, d in verdict:
        print("%-12s %s  %s" % (c, "PASS" if ok else "FAIL", d[:300]))


if __name__ == "__main__":
    main(sys.argv[1])
