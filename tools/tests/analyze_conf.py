#!/usr/bin/env python3
"""law=conf run: per-class steady statistics plus what the fence state
looked like - trust T, virtual queue q, gamma - from the receiver and
sender logs. usage: analyze_conf.py <tag> [t_from t_to]"""
import json
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
DIR = REPO / "results" / "regression"
tag = sys.argv[1]
a, b = (float(sys.argv[2]), float(sys.argv[3])) if len(sys.argv) > 3 else (40.0, 85.0)
t0 = float((DIR / f"{tag}_t0.txt").read_text())
rx = [json.loads(l) for l in (DIR / f"{tag}_rx.jsonl").read_text().splitlines() if l.strip()]
st = [r for r in rx if a <= r["ts"] - t0 <= b]
keys = sorted(k for k in st[len(st) // 2]["r"])
print("=== %s  window %.0f-%.0f s, %d receiver records, %d flow-sets" % (tag, a, b, len(st), len(keys)))
for cls in ("rdma", "tcp"):
    ks = [k for k in keys if k.endswith(cls)]
    if not ks:
        continue
    means, sds, dq, gq = [], [], [], []
    for k in ks:
        v = [r["r"][k] / 1e9 for r in st if k in r["r"]]
        means.append(statistics.mean(v)); sds.append(statistics.pstdev(v))
        dq += [r.get("d", {}).get(k, 0.0) for r in st]
        gq += [r["u"][k] / 1e6 - 1.0 for r in st if k in r.get("u", {})]
    print("  %-5s n=%2d  rate mean %.2fG (min %.2f max %.2f)  sd mean %.2f max %.2f | q>0 %.0f%% mean q %.2f ms | gamma mean %+.3f"
          % (cls, len(ks), statistics.mean(means), min(means), max(means), statistics.mean(sds), max(sds),
             100 * sum(1 for x in dq if x > 0) / max(len(dq), 1), statistics.mean(dq), statistics.mean(gq) if gq else 0))
for host in ("sgpu01", "sgpu03", "sgpu04"):
    p = DIR / f"{tag}_tx_{host}.jsonl"
    if not p.exists():
        continue
    tx = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    rows = [r for r in tx if "T" in r and a <= r["ts"] - t0 <= b]
    if not rows:
        continue
    for cls in ("rdma", "tcp"):
        rs = [r for r in rows if r["fs"].endswith(cls)]
        if not rs:
            continue
        T = [r["T"] for r in rs]; R = [r["R"] / 1e9 for r in rs]; E = [r.get("Ehat", 0) / 1e9 for r in rs]
        print("  tx %s %-5s records %5d  trust mean %.3f max %.3f | R mean %.2fG  Ehat mean %.2fG"
              % (host, cls, len(rs), statistics.mean(T), max(T), statistics.mean(R), statistics.mean(E)))
