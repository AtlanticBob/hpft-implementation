#!/usr/bin/env python3
"""Analysis for the step-3 hard cases: flip (class alternation), ms
(intra-class multi-sender), fs (helper fail-safe drill).
usage: analyze_hardcases.py flip|ms|fs
"""
import json
import sys


def load_gt(fn):
    rows = []
    for line in open(fn):
        f = line.split()
        if f[0] == "t":
            continue
        rows.append([float(f[0])] + [int(x) for x in f[1:]])
    out = {}
    for n in range(4):
        rd, tc = [], []
        for i in range(1, len(rows)):
            dt = rows[i][0] - rows[i - 1][0]
            if dt <= 0:
                continue
            t = rows[i][0]
            rd.append((t, (rows[i][1 + 2 * n] - rows[i - 1][1 + 2 * n])
                       * 4 * 8 / dt))
            tc.append((t, (rows[i][2 + 2 * n] - rows[i - 1][2 + 2 * n])
                       * 8 / dt))
        out["vf%d|rdma" % n] = rd
        out["vf%d|tcp" % n] = tc
    return out


def load_agent(fn):
    r, meta = {}, []
    for line in open(fn):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        meta.append((rec["ts"], rec.get("ha", 0), rec.get("ma", 0)))
        for f, v in rec.get("r", {}).items():
            r.setdefault(f, []).append((rec["ts"], v))
    return r, meta


def binseries(series, t0, t1, binw):
    bins = {}
    for t, v in series:
        if t0 <= t < t1:
            bins.setdefault(int((t - t0) / binw), []).append(v)
    return {b: sum(v) / len(v) for b, v in bins.items()}


def mae(a, g, floor=5e8):
    errs = [abs(a.get(b, 0.0) - v) / v for b, v in g.items() if v > floor]
    return (sum(errs) / len(errs), len(errs)) if errs else (float("nan"), 0)


def scan_lag(new_s, gt_s, t0, t1):
    nb = binseries(new_s, t0, t1, 0.1)
    best = (1e9, 0.0)
    for lag_ms in range(-300, 301, 10):
        gb = binseries([(t - lag_ms / 1e3, v) for t, v in gt_s], t0, t1, 0.1)
        m, n = mae(nb, gb)
        if n and m < best[0]:
            best = (m, lag_ms / 1e3)
    return best[1]


def flip():
    gt = load_gt("flip_gt.tsv")
    old, _ = load_agent("flip_prod.jsonl")
    new, _ = load_agent("flip_bypass.jsonl")
    FR, FT = "sgpu01/vf0>sgpu02/vf0|rdma", "sgpu01/vf0>sgpu02/vf0|tcp"
    e = [t for t, r in gt["vf0|tcp"] if r > 2e9]
    t0, t1 = e[0], e[-1]
    lag = scan_lag(new.get(FT, []), gt["vf0|tcp"], t0 + 2, t1 - 2)
    gt = {k: [(t - lag, v) for t, v in s] for k, s in gt.items()}
    print("flip: window %.1fs, gt lag %+.0f ms" % (t1 - t0, lag * 1e3))
    # RDMA on/off edges from gt
    edges = []
    prev = 0.0
    for t, r in gt["vf0|rdma"]:
        if (prev <= 1e9) != (r <= 1e9):
            edges.append(t)
        prev = r
    print("rdma on/off edges: %d" % len(edges))
    inedge = lambda t: any(abs(t - te) < 0.5 for te in edges)
    for cls, fsid in (("rdma", FR), ("tcp", FT)):
        g = binseries(gt["vf0|%s" % cls], t0, t1, 0.1)
        for name, src in (("old", old), ("new", new)):
            a = binseries(src.get(fsid, []), t0, t1, 0.1)
            all_m, n_all = mae(a, g)
            eg = {b: v for b, v in g.items() if inedge(t0 + b * 0.1 + 0.05)}
            sg = {b: v for b, v in g.items() if not inedge(t0 + b * .1 + .05)}
            em, n_e = mae(a, eg)
            sm, n_s = mae(a, sg)
            print("  %s %s: mae all %5.2f%% (n=%d) | edge+-0.5s %5.2f%% "
                  "(n=%d) | plateau %5.2f%% (n=%d)"
                  % (cls, name, all_m * 100, n_all, em * 100, n_e,
                     sm * 100, n_s))
    # cross-leak during rdma-off plateaus: reported rdma when gt says 0
    for name, src in (("old", old), ("new", new)):
        a = binseries(src.get(FR, []), t0, t1, 0.1)
        g = binseries(gt["vf0|rdma"], t0, t1, 0.1)
        leak = [a.get(b, 0.0) for b, v in g.items()
                if v < 1e8 and not inedge(t0 + b * 0.1 + 0.05)]
        if leak:
            leak.sort()
            print("  rdma-off leak %s: med %.3fG p95 %.3fG max %.3fG (n=%d)"
                  % (name, leak[len(leak) // 2] / 1e9,
                     leak[int(.95 * len(leak))] / 1e9, leak[-1] / 1e9,
                     len(leak)))


def ms():
    gt = load_gt("ms_gt.tsv")
    old, _ = load_agent("ms_prod.jsonl")
    new, _ = load_agent("ms_bypass.jsonl")
    F0 = "sgpu01/vf0>sgpu02/vf0|rdma"
    F3 = "sgpu01/vf3>sgpu02/vf0|rdma"
    FT = "sgpu01/vf0>sgpu02/vf0|tcp"
    tj = float(open("ms_tjoin.txt").read())
    tl = float(open("ms_tleave.txt").read())
    e = [t for t, r in gt["vf0|rdma"] if r > 2e9]
    t0, t1 = e[0], e[-1]
    lag = scan_lag(new.get(FT, []), gt["vf0|tcp"], t0 + 2, t1 - 2)
    gt = {k: [(t - lag, v) for t, v in s] for k, s in gt.items()}
    print("ms: window %.1fs, join %+.1fs leave %+.1fs (rel t0), "
          "gt lag %+.0f ms" % (t1 - t0, tj - t0, tl - t0, lag * 1e3))
    # class-total accuracy during the 2-sender phase
    g = binseries(gt["vf0|rdma"], tj + 3, tl - 2, 0.1)
    for name, src in (("old", old), ("new", new)):
        tot = {}
        for fs in (F0, F3):
            for b, v in binseries(src.get(fs, []), tj + 3, tl - 2,
                                  0.1).items():
                tot[b] = tot.get(b, 0.0) + v
        m, n = mae(tot, g)
        print("  2-sender rdma class total %s: mae %.2f%% (n=%d)"
              % (name, m * 100, n))
    # per-sender share convergence after join/leave (megaflow-mix residual)
    for name, src in (("old", old), ("new", new)):
        b0 = binseries(src.get(F0, []), t0, t1, 0.1)
        b3 = binseries(src.get(F3, []), t0, t1, 0.1)
        share = {b: b3[b] / (b3[b] + b0.get(b, 0.0))
                 for b in b3 if b3[b] + b0.get(b, 0.0) > 1e9}
        steady = [share[b] for b in share
                  if tj + 4 < t0 + b * 0.1 < tl - 2]
        if not steady:
            print("  %s: no steady share" % name)
            continue
        target = sum(steady) / len(steady)
        conv = None
        for b in sorted(share):
            t = t0 + b * 0.1
            if t < tj:
                continue
            if abs(share[b] - target) < 0.1 * target:
                conv = t - tj
                break
        # after leave: reported vf3 rate should die
        die = None
        for b in sorted(b3):
            t = t0 + b * 0.1
            if t > tl and b3[b] < 5e8:
                die = t - tl
                break
        print("  %s: steady vf3 share %.3f, join->converge %.1fs, "
              "leave->vf3-dead %.1fs"
              % (name, target, -1 if conv is None else conv,
                 -1 if die is None else die))


def fs():
    gt = load_gt("fs_gt.tsv")
    new, nm = load_agent("fs_bypass.jsonl")
    FR = "sgpu01/vf0>sgpu02/vf0|rdma"
    FT = "sgpu01/vf1>sgpu02/vf1|tcp"
    tk = float(open("fs_tkill.txt").read())
    tr = float(open("fs_trestart.txt").read())
    # ma timeline around kill/restart
    ts_ma = [(t, m) for t, _, m in nm]
    def first(cond, pts):
        for t, m in pts:
            if cond(t, m):
                return t
    lastma = max(t for t, m in ts_ma if m == 2 and t < tk + 2)
    backma = first(lambda t, m: t > tr and m == 2, ts_ma)
    print("fs: kill->last meter-attributed %+.2fs, restart->recovered "
          "%+.2fs" % (lastma - tk, backma - tr))
    # r_f continuity during degraded phase vs gt
    e = [t for t, r in gt["vf0|rdma"] if r > 2e9]
    t0, t1 = e[0], e[-1]
    lag = scan_lag(new.get(FT, []), gt["vf1|tcp"], t0 + 2, t1 - 2)
    gt = {k: [(t - lag, v) for t, v in s] for k, s in gt.items()}
    for label, lo, hi in (("meter", t0 + 2, tk), ("degraded", tk + 0.5,
                          tr), ("recovered", backma + 0.5, t1 - 1)):
        for cls, fsid, gk in (("rdma", FR, "vf0|rdma"),
                              ("tcp", FT, "vf1|tcp")):
            g = binseries(gt[gk], lo, hi, 0.1)
            a = binseries(new.get(fsid, []), lo, hi, 0.1)
            m, n = mae(a, g)
            print("  %-9s %s mae %5.2f%% (n=%d)" % (label, cls,
                                                    m * 100, n))
    err = [l for l in open("fs_bypass.out")
           if "Error" in l or "Traceback" in l]
    print("  bypass stderr exceptions: %d" % len(err))


if __name__ == "__main__":
    {"flip": flip, "ms": ms, "fs": fs}[sys.argv[1]]()
