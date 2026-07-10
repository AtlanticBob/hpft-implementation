#!/usr/bin/env python3
"""M1 acceptance: steady state 6G+-5% wire, convergence <10 periods."""
import json

FS = "sgpu01/vf0>sgpu02/vf0|tcp"
TARGET = 6e9

rx = [json.loads(l) for l in open("rx_e.jsonl")]
tx = [json.loads(l) for l in open("tx_e.jsonl")]
t0 = float(open("m1_t0.txt").read())
t1 = float(open("m1_t1.txt").read())

run = [r for r in rx if t0 <= r["ts"] <= t1 and FS in r.get("r", {})]
print("run ticks:", len(run))

# convergence: first tick with r>0 -> first tick entering the +-5% band
# and staying there for >=20 consecutive ticks
start = next(r["ts"] for r in run if r["r"][FS] > 0)
band = [(r["ts"], abs(r["r"][FS] - TARGET) <= 0.05 * TARGET) for r in run]
conv_ts = None
for i in range(len(band)):
    if band[i][1] and all(b for _, b in band[i:i + 20]) and i + 20 <= len(band):
        conv_ts = band[i][0]
        break
if conv_ts:
    print("convergence: %.2fs = %.1f periods (T=50ms)"
          % (conv_ts - start, (conv_ts - start) / 0.05))
else:
    print("convergence: NEVER entered a 20-tick stable +-5% band")

# steady state: [start+5s, end-2s]
ss = [r for r in run if start + 5 <= r["ts"] <= t1 - 2]
rr = sorted(r["r"][FS] for r in ss)
n = len(rr)
mean = sum(rr) / n
print("steady r_f: mean=%.3fG (%+.1f%% of 6G) p5=%.3fG p50=%.3fG p95=%.3fG"
      % (mean / 1e9, (mean / TARGET - 1) * 100, rr[int(n * .05)] / 1e9,
         rr[n // 2] / 1e9, rr[int(n * .95)] / 1e9))
inband = sum(1 for v in rr if abs(v - TARGET) <= 0.05 * TARGET)
print("per-tick in +-5%% band: %.1f%%  in +-10%%: %.1f%%"
      % (100 * inband / n,
         100 * sum(1 for v in rr if abs(v - TARGET) <= 0.1 * TARGET) / n))
ee = [r["e"].get(FS, 0) for r in ss]
svals = [r["s"].get(FS, 0) for r in ss]
print("steady e_f: mean=%.3fG   s_f: mean=%.3f  s>0 ticks: %.1f%%"
      % (sum(ee) / len(ee) / 1e9, sum(svals) / len(svals),
         100 * sum(1 for v in svals if v > 0) / len(svals)))

# tx side
txr = [t for t in tx if t0 <= t["ts"] <= t1 and t.get("fs") == FS and "R" in t]
modes = {}
for t in txr:
    modes[t.get("mode", "?")] = modes.get(t.get("mode", "?"), 0) + 1
Rs = sorted(t["R"] for t in txr if t.get("mode") in ("ai", "md", "app_limited"))
print("tx modes:", modes)
if Rs:
    print("R_f: p5=%.3fG p50=%.3fG p95=%.3fG"
          % (Rs[int(len(Rs) * .05)] / 1e9, Rs[len(Rs) // 2] / 1e9,
             Rs[int(len(Rs) * .95)] / 1e9))

# first 30 periods timeline
print("\nfirst 30 ticks after start (r_G, e_G, s):")
for r in run[:30]:
    print("  %+6.2fs r=%6.2f e=%6.2f s=%.3f"
          % (r["ts"] - start, r["r"][FS] / 1e9, r["e"].get(FS, 0) / 1e9,
             r["s"].get(FS, 0)))
