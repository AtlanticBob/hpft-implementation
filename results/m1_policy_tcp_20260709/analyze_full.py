#!/usr/bin/env python3
"""M1 full-run analysis: cold start under 6G cap, step 6->4G, step 4->6G.
Convergence metric: 10-tick moving average of r_f enters the +-5% band of
the new target and stays for 20 consecutive MA points."""
import json

FS = "sgpu01/vf0>sgpu02/vf0|tcp"
rx = [json.loads(l) for l in open("rx_e_full.jsonl")]
t0 = float(open("mf_t0.txt").read())
t4 = float(open("mf_step4.txt").read())
t6 = float(open("mf_step6.txt").read())
t1 = float(open("mf_t1.txt").read())

run = [r for r in rx if t0 <= r["ts"] <= t1 and FS in r.get("r", {})]
series = [(r["ts"], r["r"][FS]) for r in run]


def engage(after, thresh):
    """First tick where e_f crosses thresh (policy actually live)."""
    for r in run:
        if r["ts"] >= after and 0 < r["e"].get(FS, 1e18) <= thresh:
            return r["ts"]
    return None


def conv(tstart, target):
    idx = next((i for i, (ts, _) in enumerate(series) if ts >= tstart), None)
    if idx is None:
        return None
    ma = []
    for i in range(idx, len(series) - 9):
        ma.append((series[i + 9][0], sum(v for _, v in series[i:i + 10]) / 10))
    for i in range(len(ma) - 20):
        if all(abs(v - target) <= .05 * target for _, v in ma[i:i + 20]):
            return ma[i][0] - tstart
    return None


def seg_stats(name, a, b, target):
    seg = sorted(r["r"][FS] for r in run if a <= r["ts"] <= b)
    if not seg:
        return
    n = len(seg)
    mean = sum(seg) / n
    print("%-18s mean=%.3fG (%+.1f%% of %.0fG) p5=%.2f p50=%.2f p95=%.2f "
          "tick-in-5%%=%d%%" % (name, mean / 1e9, (mean / target - 1) * 100,
          target / 1e9, seg[int(n * .05)] / 1e9, seg[n // 2] / 1e9,
          seg[int(n * .95)] / 1e9,
          round(100 * sum(1 for v in seg if abs(v - target) <= .05 * target) / n)))


start = next(r["ts"] for r in run if r["r"][FS] > 1e8)
c_cold = conv(start, 6e9)
print("cold start (cap 6G pre-set): conv=%s"
      % ("%.2fs = %.0f periods" % (c_cold, c_cold / .05) if c_cold else "none"))
seg_stats("steady @6G", start + 15, t4 - 1, 6e9)

e4 = engage(t4, 4.5e9)
c4 = conv(e4, 4e9)
print("step 6->4G: engage +%.2fs; conv=%s"
      % (e4 - t4, "%.2fs = %.1f periods" % (c4, c4 / .05) if c4 else "none"))
seg_stats("steady @4G", e4 + 10, t6 - 1, 4e9)

e6 = engage(t6, 1e18)  # e jumps up; find first e>4.6G after t6
for r in run:
    if r["ts"] >= t6 and r["e"].get(FS, 0) > 4.6e9:
        e6 = r["ts"]; break
c6 = conv(e6, 6e9)
print("step 4->6G: engage +%.2fs; conv=%s"
      % (e6 - t6, "%.2fs = %.1f periods" % (c6, c6 / .05) if c6 else "none"))
seg_stats("steady @6G(2)", e6 + 10, t1 - 2, 6e9)

print("\ndown-step transition (rel. engage):")
for r in run:
    if -0.1 <= r["ts"] - e4 <= 1.3:
        print("  %+5.2fs r=%5.2f e=%5.2f s=%.3f vq=%dM"
              % (r["ts"] - e4, r["r"][FS] / 1e9, r["e"].get(FS, 0) / 1e9,
                 r["s"].get(FS, 0), r["vq"].get(FS, 0) / 1e6))
