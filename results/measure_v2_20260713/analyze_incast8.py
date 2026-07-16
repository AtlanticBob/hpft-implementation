#!/usr/bin/env python3
"""Reconcile incast8 triple recording: old (prod, vport-minus-TCP) vs new
(bypass, vport ib/eth direct) attribution against host ground truth.

Alignment: prod and bypass share the DPU clock. gt (sgpu02) is NTP+push
synced to ~10ms; residual lag is fine-scanned by minimizing the binned
error of the all-VF total rate between gt and the bypass meter.
"""
import json
import sys

DIR = "."
FS = ["sgpu01/vf%d>sgpu02/vf%d|%s" % (n, n, c)
      for n in range(4) for c in ("rdma", "tcp")]


def load_gt(fn):
    rows = []
    for line in open(fn):
        f = line.split()
        if f[0] == "t":
            continue
        rows.append([float(f[0])] + [int(x) for x in f[1:]])
    # series per fsid: gt columns: rcv4_vf0 rx_vf0 rcv4_vf1 rx_vf1 ...
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
        out["sgpu01/vf%d>sgpu02/vf%d|rdma" % (n, n)] = rd
        out["sgpu01/vf%d>sgpu02/vf%d|tcp" % (n, n)] = tc
    return out


def load_agent(fn, key="r"):
    out = {}
    meta = []
    for line in open(fn):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        meta.append((rec["ts"], rec.get("ha", 0), rec.get("ma", 0)))
        for f, v in rec.get(key, {}).items():
            out.setdefault(f, []).append((rec["ts"], v))
    return out, meta


def binseries(series, t0, t1, binw):
    bins = {}
    for t, v in series:
        if t0 <= t < t1:
            bins.setdefault(int((t - t0) / binw), []).append(v)
    return {b: sum(v) / len(v) for b, v in bins.items()}


def errstats(a_bins, g_bins, floor=2e8):
    errs = []
    for b, g in g_bins.items():
        if g < floor:
            continue
        errs.append(((a_bins.get(b, 0.0)) - g) / g)
    if not errs:
        return None
    errs.sort()
    n = len(errs)
    mae = sum(abs(e) for e in errs) / n
    bias = sum(errs) / n
    return mae, bias, abs(errs[int(.05 * n)]), abs(errs[int(.95 * n)]), n


def main():
    tag = sys.argv[1] if len(sys.argv) > 1 else "i8m"
    gt = load_gt("%s/%s_gt.tsv" % (DIR, tag))
    old, om = load_agent("%s/%s_prod.jsonl" % (DIR, tag))
    new, nm = load_agent("%s/%s_bypass.jsonl" % (DIR, tag))

    # traffic window from gt vf0 rdma edge
    e = [t for t, r in gt[FS[0]] if r > 5e9]
    t0, t1 = e[0], e[-1]
    print("gt traffic window: %.2f .. %.2f (%.1fs)" % (t0, t1, t1 - t0))
    print("prod ha dist:", sorted(set(h for t, h, m in om if t0 < t < t1)),
          " bypass ma dist:",
          sorted(set(m for t, h, m in nm if t0 < t < t1)))

    # fine-scan gt lag against bypass total (all VF, both classes)
    best = (1e9, 0.0)

    def sumbins(sd, t0, t1, binw, lag=0.0):
        acc = {}
        for k in FS:
            for b, v in binseries([(t + lag, x) for t, x in sd.get(k, [])],
                                  t0, t1, binw).items():
                acc[b] = acc.get(b, 0.0) + v
        return acc
    nbins = sumbins(new, t0 + 5, t1 - 3, 0.1)
    for lag_ms in range(-300, 301, 10):
        gbins = sumbins(gt, t0 + 5, t1 - 3, 0.1, lag=-lag_ms / 1e3)
        s = errstats(nbins, gbins)
        if s and s[0] < best[0]:
            best = (s[0], lag_ms / 1e3)
    lag = best[1]
    print("gt lag vs dpu clock: %+.0f ms (residual mae %.3f%%)"
          % (lag * 1e3, best[0] * 100))
    gt = {k: [(t - lag, v) for t, v in s] for k, s in gt.items()}

    for label, lo, hi, binw in (("steady", t0 + 5, t1 - 3, 0.1),
                                ("steady", t0 + 5, t1 - 3, 1.0),
                                ("transient", t0 - 0.5, t0 + 5, 0.1)):
        print("\n== %s [%+.1f..%+.1f]s bin=%.1fs" % (label, lo - t0,
                                                     hi - t0, binw))
        agg = {"rdma": [[], []], "tcp": [[], []]}
        for f in FS:
            g = binseries(gt[f], lo, hi, binw)
            so = errstats(binseries(old.get(f, []), lo, hi, binw), g)
            sn = errstats(binseries(new.get(f, []), lo, hi, binw), g)
            cls = f.rsplit("|", 1)[1]
            if so:
                agg[cls][0].append(so)
            if sn:
                agg[cls][1].append(sn)
            if binw == 1.0 or label == "transient":
                continue
            print("  %-28s old mae %5.2f%% bias %+5.2f%% | "
                  "new mae %5.2f%% bias %+5.2f%%"
                  % (f, so[0] * 100, so[1] * 100, sn[0] * 100, sn[1] * 100))
        for cls in ("rdma", "tcp"):
            for i, name in ((0, "old"), (1, "new")):
                ss = agg[cls][i]
                if not ss:
                    continue
                mae = sum(s[0] for s in ss) / len(ss)
                bias = sum(s[1] for s in ss) / len(ss)
                p95 = max(s[3] for s in ss)
                print("  %-4s %s: mae %5.2f%%  bias %+5.2f%%  "
                      "worst-p95 %5.2f%%" % (cls, name, mae * 100,
                                             bias * 100, p95 * 100))

    # the ~1G question: per-class steady means from ground truth
    print("\n== gt steady means (Gbps) — the RDMA-vs-TCP question")
    for f in FS:
        g = binseries(gt[f], t0 + 5, t1 - 3, 1.0)
        go = binseries(old.get(f, []), t0 + 5, t1 - 3, 1.0)
        gn = binseries(new.get(f, []), t0 + 5, t1 - 3, 1.0)
        m = lambda d: sum(d.values()) / max(len(d), 1) / 1e9
        print("  %-28s gt %6.2f | old %6.2f | new %6.2f"
              % (f, m(g), m(go), m(gn)))


if __name__ == "__main__":
    main()
