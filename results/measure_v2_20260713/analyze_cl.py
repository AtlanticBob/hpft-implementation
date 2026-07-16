#!/usr/bin/env python3
"""Step-4 closed-loop acceptance:
  i8c  : accuracy of the production (vport-meter) attribution vs gt, and
         closed-loop behavior (from gt) compared with the step-2 baseline
         run i8m (old attribution controlling).
  flipc: phantom-RDMA verification in the controller's actual input.
usage: analyze_cl.py i8c|flipc
"""
import json
import sys

from analyze_hardcases import load_gt, load_agent, binseries, mae, scan_lag

FS = ["sgpu01/vf%d>sgpu02/vf%d|%s" % (n, n, c)
      for n in range(4) for c in ("rdma", "tcp")]
GK = {f: "vf%s|%s" % (f.split("vf")[1][0], f.rsplit("|", 1)[1]) for f in FS}


def acc(tag):
    gt = load_gt("%s_gt.tsv" % tag)
    prod, meta = load_agent("%s_prod.jsonl" % tag)
    e = [t for t, r in gt["vf0|rdma"] if r > 5e9]
    t0, t1 = e[0], e[-1]
    lag = scan_lag(prod.get(FS[1], []), gt["vf0|tcp"], t0 + 5, t1 - 3)
    gt = {k: [(t - lag, v) for t, v in s] for k, s in gt.items()}
    from collections import Counter
    print("%s window %.1fs lag %+dms  ma dist %s" % (tag, t1 - t0,
          lag * 1e3, Counter(m for t, _, m in meta
                             if t0 + 5 < t < t1 - 3).most_common(3)))
    for binw in (0.1, 1.0):
        agg = {"rdma": [], "tcp": []}
        for f in FS:
            g = binseries(gt[GK[f]], t0 + 5, t1 - 3, binw)
            a = binseries(prod.get(f, []), t0 + 5, t1 - 3, binw)
            errs = [(a.get(b, 0.0) - v) / v for b, v in g.items()
                    if v > 5e8]
            if errs:
                agg[f.rsplit("|", 1)[1]].append(
                    (sum(abs(x) for x in errs) / len(errs),
                     sum(errs) / len(errs)))
        for cls, ss in agg.items():
            print("  %s bin=%.1fs: mae %5.2f%% bias %+5.2f%%"
                  % (cls, binw, 100 * sum(s[0] for s in ss) / len(ss),
                     100 * sum(s[1] for s in ss) / len(ss)))
    return gt, t0, t1


def loop_metrics(gtfile):
    gt = load_gt(gtfile)
    e = [t for t, r in gt["vf0|rdma"] if r > 5e9]
    t0, t1 = e[0], e[-1]
    out = {}
    for n in range(4):
        for cls in ("rdma", "tcp"):
            k = "vf%d|%s" % (n, cls)
            b = binseries(gt[k], t0 + 5, t1 - 3, 0.1)
            v = sorted(b.values())
            mean = sum(v) / len(v)
            std = (sum((x - mean) ** 2 for x in v) / len(v)) ** 0.5
            b1 = binseries(gt[k], t0 + 5, t1 - 3, 1.0)
            collapse = sum(1 for x in b1.values() if x < 5e8)
            out[k] = (mean / 1e9, std / 1e9, collapse)
    return out, t1 - t0


def i8c():
    acc("i8c")
    print("\n== closed-loop from gt: i8m (old attribution) vs i8c (new)")
    old, d0 = loop_metrics("i8m_gt.tsv")
    new, d1 = loop_metrics("i8c_gt.tsv")
    print("  %-9s %-24s %-24s" % ("", "i8m mean/std/collapse_s",
                                  "i8c mean/std/collapse_s"))
    tots = [0.0, 0.0]
    for k in sorted(old):
        o, n = old[k], new[k]
        tots[0] += o[0]
        tots[1] += n[0]
        print("  %-9s %6.2fG %5.2fG %3ds      %6.2fG %5.2fG %3ds"
              % (k, o[0], o[1], o[2], n[0], n[1], n[2]))
    print("  util sum: i8m %.1fG  i8c %.1fG (of 97G)" % (tots[0], tots[1]))
    for cls in ("rdma", "tcp"):
        for name, m in (("i8m", old), ("i8c", new)):
            ks = [k for k in m if k.endswith(cls)]
            print("  %s %s: class sum %.2fG  mean std %.2fG  collapse %ds"
                  % (cls, name, sum(m[k][0] for k in ks),
                     sum(m[k][1] for k in ks) / len(ks),
                     sum(m[k][2] for k in ks)), end="")
        print()


def flipc():
    gt = load_gt("flipc_gt.tsv")
    prod, meta = load_agent("flipc_prod.jsonl")
    FR, FT = FS[0], FS[1]
    e = [t for t, r in gt["vf0|tcp"] if r > 2e9]
    t0, t1 = e[0], e[-1]
    lag = scan_lag(prod.get(FT, []), gt["vf0|tcp"], t0 + 2, t1 - 2)
    gt = {k: [(t - lag, v) for t, v in s] for k, s in gt.items()}
    edges = []
    prev = 0.0
    for t, r in gt["vf0|rdma"]:
        if (prev <= 1e9) != (r <= 1e9):
            edges.append(t)
        prev = r
    inedge = lambda t: any(abs(t - te) < 0.5 for te in edges)
    print("flipc: window %.1fs lag %+dms edges %d"
          % (t1 - t0, lag * 1e3, len(edges)))
    g = binseries(gt["vf0|rdma"], t0, t1, 0.1)
    a = binseries(prod.get(FR, []), t0, t1, 0.1)
    leak = sorted(a.get(b, 0.0) for b, v in g.items()
                  if v < 1e8 and not inedge(t0 + b * 0.1 + 0.05))
    print("  rdma-off leak IN CONTROLLER INPUT: med %.3fG p95 %.3fG "
          "max %.3fG (n=%d)  [old was med 0.694G p95 1.945G]"
          % (leak[len(leak) // 2] / 1e9, leak[int(.95 * len(leak))] / 1e9,
             leak[-1] / 1e9, len(leak)))
    for cls, fsid in (("rdma", FR), ("tcp", FT)):
        gg = binseries(gt["vf0|%s" % cls], t0, t1, 0.1)
        aa = binseries(prod.get(fsid, []), t0, t1, 0.1)
        plat = {b: v for b, v in gg.items()
                if not inedge(t0 + b * 0.1 + 0.05)}
        m, n = mae(aa, plat)
        print("  %s plateau mae %.2f%% (n=%d)" % (cls, m * 100, n))
    tcp_gt = binseries(gt["vf0|tcp"], t0 + 2, t1 - 2, 1.0)
    v = sorted(tcp_gt.values())
    print("  tcp gt during run: mean %.2fG p5 %.2fG"
          % (sum(v) / len(v) / 1e9, v[int(.05 * len(v))] / 1e9))


if __name__ == "__main__":
    {"i8c": i8c, "flipc": flipc}[sys.argv[1]]()
