#!/usr/bin/env python3
"""Loop-delay decomposition for one run (DPU clocks must be synced, see
tools/lab-infra/dpu_time_sync.sh). For each flow-set of the first sender host:
  fence R (tx log)            -> sender NIC tx As (tx log, from the DPU vport meter)   executor
  sender NIC tx As            -> receiver NIC bucket rv (rx log, per VM per class)     wire + measurement window
  receiver q (rx log)         -> sender q (tx log)                                     feedback path
Lags by cross-correlation on a 5 ms grid, searched in both directions.
usage: lag.py <tag> [t_from t_to]"""
import json, os, sys
import numpy as np
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tag = sys.argv[1]; R = os.path.join(BASE, "results", tag)
a, b = (float(sys.argv[2]), float(sys.argv[3])) if len(sys.argv) > 3 else (5.0, 85.0)
z = float(open(os.path.join(R, "t0.txt")).read()) + float(open(os.path.join(R, "warm.txt")).read())
rx = [json.loads(l) for l in open(os.path.join(R, "rx.jsonl")) if l.strip()]
rows = [l.split() for l in open(os.path.join(R, "flows.txt")) if l.strip() and not l.startswith("#")]
host = rows[0][0]
tx = [x for x in (json.loads(l) for l in open(os.path.join(R, f"tx_{host}.jsonl")) if l.strip()) if "R" in x]
grid = np.arange(a, b, 0.005); rt = [x["ts"] - z for x in rx]


def lag(at, av, bt, bv, span=60):
    A = np.interp(grid, at, av); B = np.interp(grid, bt, bv)
    best = None
    for k in range(-span, span + 1):
        if k >= 0:
            c = np.corrcoef(A[:len(A) - k] if k else A, B[k:])[0, 1]
        else:
            c = np.corrcoef(A[-k:], B[:len(B) + k])[0, 1]
        if best is None or c > best[1]:
            best = (k * 5, c)
    return best


print(f"{tag}: lags in ms (positive = second signal later), window {a:.0f}-{b:.0f} s")
seen = set()
for r in rows:
    if r[0] != host or r[3] not in ("rdma", "tcp"):
        continue
    f = f"{r[0]}/vf{r[1]}>sgpu02/vf{r[2]}|{r[3]}"
    if f in seen:
        continue
    seen.add(f)
    txg = [y for y in tx if y.get("fs") == f]
    if len(txg) < 50:
        continue
    Rt = [y["ts"] - z for y in txg]
    idx = 0 if r[3] == "rdma" else 1
    rv = [x["rv"].get(f"sgpu02/vf{r[2]}", [0, 0])[idx] for x in rx]
    out = [f"  {f:32s}"]
    if "As" in txg[0]:
        l1 = lag(Rt, [y["R"] for y in txg], Rt, [y["As"] for y in txg]); out.append("R->NIC %+4d (%.2f)" % l1)
        l2 = lag(Rt, [y["As"] for y in txg], rt, rv); out.append("NIC->rx bucket %+4d (%.2f)" % l2)
    l3 = lag(Rt, [y["R"] for y in txg], rt, rv); out.append("R->rx bucket %+4d (%.2f)" % l3)
    if "q" in txg[0]:
        l4 = lag(rt, [x.get("d", {}).get(f, 0) for x in rx], Rt, [y["q"] for y in txg]); out.append("rx q->tx q %+4d (%.2f)" % l4)
    print("  ".join(out))
