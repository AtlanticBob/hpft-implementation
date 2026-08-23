#!/usr/bin/env python3
"""TCP+RDMA incast: per-flow-set steady rate, fairness, utilisation.

The flow-set list and the expected share are DISCOVERED, not written down:
the run's sender set is an argument to the runner now, so a fixed list of
eight sgpu01->sgpu02 keys would silently report on a subset of a four-node
run and call the rest missing. Every key the receiver logged is a flow-set;
the expected share is the root the receiver actually schedules against,
divided by how many there are.
"""
import json, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
DIR = REPO / "results" / "regression"
STEADY = (40, 85)          # seconds into the run


def root_bps():
    """C' the receiver schedules against: line rate less the headroom."""
    r = json.load(open(REPO / "config" / "lab-registry.json"))
    return r["line_rate_bps"] * (1.0 - r["e_params"]["headroom"])


def load(tag):
    try:
        t0 = float((DIR / f"{tag}_t0.txt").read_text().strip())
    except FileNotFoundError:
        return None, None
    per, keys = {}, set()
    for jl in (DIR / f"{tag}_rx.jsonl").read_text().splitlines():
        try:
            rec = json.loads(jl)
        except Exception:
            continue
        s = int(rec["ts"] - t0)
        for k, v in rec.get("r", {}).items():
            keys.add(k)
            per.setdefault(k, {}).setdefault(s, []).append(v / 1e9)
    return {k: {x: sum(v) / len(v) for x, v in d.items()} for k, d in per.items()}, keys


def rep(tag):
    P, keys = load(tag)
    if P is None:
        return None
    xs = sorted(set().union(*[set(P[k]) for k in keys])) if keys else []
    st = [x for x in xs if STEADY[0] <= x <= STEADY[1]]
    mean = {}
    for k in keys:
        pts = [P[k][x] for x in st if x in P[k]]
        mean[k] = sum(pts) / len(pts) if pts else 0.0
    # A key the receiver saw once and never again is a leftover from the
    # previous run's tail, not a flow-set of this one.
    mean = {k: v for k, v in mean.items() if v > 0.05}
    vals = list(mean.values())
    if not vals:
        return None
    n = len(vals)
    agg = sum(vals)
    root = root_bps() / 1e9
    share = root / n
    jain = (sum(vals) ** 2) / (n * sum(v * v for v in vals))
    rdma = sum(v for k, v in mean.items() if k.endswith("rdma"))
    conv = None
    for x in xs:
        if x < 3:
            continue
        if all(0.7 * share <= P[k].get(x, 0) <= 1.3 * share for k in mean):
            conv = x
            break
    return dict(mean=mean, agg=agg, util=100 * agg / root, jain=jain, rdma=rdma,
                tcp=agg - rdma, mn=min(vals), mx=max(vals), conv=conv, n=n,
                share=share, root=root)


tag = sys.argv[1] if len(sys.argv) > 1 else "i8_zero"
r = rep(tag)
if r is None:
    print("no data:", tag)
    sys.exit()


def jain(v):
    return (sum(v) ** 2) / (len(v) * sum(x * x for x in v)) if v else 0.0


mean = r["mean"]
print("=== %s : %d flow-sets, root %.0fG ===" % (tag, r["n"], r["root"]))
print("aggregate=%.0fG util=%.0f%%  conv=%ss" % (r["agg"], r["util"], r["conv"]))

# Fairness is only a flat average when every group is entitled to the same
# share. Per-VM class weights split a dst VM between TCP and RDMA, so a class
# with fewer senders on that VM is ENTITLED to more per flow-set: a single
# Jain over the whole run reports correct policy as unfairness. Grouped is
# what the policy actually promises.
for cls in ("rdma", "tcp"):
    v = [x for k, x in mean.items() if k.endswith(cls)]
    if v:
        print("  %-5s n=%-3d total=%6.1fG  each %.1f-%.1fG  Jain=%.3f"
              % (cls, len(v), sum(v), min(v), max(v), jain(v)))
dst = {}
for k, x in mean.items():
    dst.setdefault(k.split(">")[1].split("|")[0], []).append(x)
print("  per dst VM (want %.1fG each):" % (r["root"] / max(len(dst), 1)))
for d in sorted(dst):
    print("    %-14s %6.1fG over %d flow-sets  Jain=%.3f" % (d, sum(dst[d]), len(dst[d]), jain(dst[d])))
print("  flat Jain over all %d flow-sets = %.3f  (only meaningful when every"
      % (r["n"], r["jain"]))
print("  dst VM carries the same class mix)")
for k in sorted(mean):
    print("    %-40s %6.2f G" % (k, mean[k]))
