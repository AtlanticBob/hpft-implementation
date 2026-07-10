#!/usr/bin/env python3
"""Control-plane cost vs flow-set count, from instrumented agents."""
import json
import statistics as st

rx = [json.loads(l) for l in open("sc_rx.jsonl") if '"nfs"' in l]
buckets = {}
for r in rx:
    n = r.get("nfs", 0)
    if n > 0:
        buckets.setdefault(n, []).append(r)

print("rx per-tick cost by active flow-set count:")
print("%4s %6s | %9s %9s %9s | %9s" %
      ("nfs", "ticks", "read_ms", "sched_us", "mark+tel", "tick_total_ms"))
for n in sorted(buckets):
    rows = buckets[n]
    if len(rows) < 40:
        continue
    rd = [r["read_ms"] for r in rows]
    su = [r["us"][0] for r in rows]
    mt = [r["us"][1] for r in rows]
    tot = [r["read_ms"] + (r["us"][0] + r["us"][1]) / 1e3 for r in rows]
    print("%4d %6d | p50=%5.1f  p50=%6.0f  p50=%6.0f | p50=%5.1f p95=%5.1f"
          % (n, len(rows), st.median(rd), st.median(su), st.median(mt),
             st.median(tot), sorted(tot)[int(len(tot) * .95)]))

tx = [json.loads(l) for l in open("sc_tx.jsonl") if '"tus"' in l]
tb = {}
for t in tx:
    # bucket tree cost by flow count inferred later; use tus timeline
    tb.setdefault(t["ts"] // 30 * 30, []).append(t["tus"])
print("\ntx tree-recompute cost (30s windows):")
for k in sorted(tb):
    v = tb[k]
    print("  t=%d n=%5d tree_us p50=%6.0f p95=%6.0f"
          % (k, len(v), st.median(v), sorted(v)[int(len(v) * .95)]))

print("\nagent CPU%% per stage (delta jiffies / elapsed, HZ=100):")
rows = [l.split() for l in open("cpu_samples.txt")]
for a, b in zip(rows, rows[1:]):
    dt = int(b[1]) - int(a[1])
    drx = int(b[2].split("=")[1]) - int(a[2].split("=")[1])
    dtx = int(b[3].split("=")[1]) - int(a[3].split("=")[1])
    print("  %-12s %3ds  rx=%4.1f%%  tx=%4.1f%%"
          % (b[0], dt, drx / dt, dtx / dt))
