#!/usr/bin/env python3
"""What the 20x down-step did, per trial.

Three numbers decide whether wiring layer one was safe:
  survival   - RC error completions are terminal, so a killed QP shows up
               as a non-zero client exit and never recovers.
  hw latency - how long after the policy write the NIC was enforcing the
               new allowance. §5.1's argument that gradual software
               descent does not weaken the selling principle needs this
               to be small compared with the descent itself.
  descent    - how long the software tree took to reach the new cap, and
               how long the WIRE took, measured on the host's own vport
               RDMA counter rather than on either endpoint's opinion.

perftest's own BW average is deliberately not used: it reported 7.27G for
a run the host counter and the receiver both measured at 29.3G, with
"BW peak 0.00" alongside, so its report path is degraded under -D here.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "results")


def wire(trial):
    """Trial 1 predates the independent counter (it is what motivated it),
    so its wire columns are simply absent rather than guessed."""
    if not os.path.exists("%s/ds%d_txbytes.txt" % (OUT, trial)):
        return []
    t0 = float(open("%s/ds%d_t0.txt" % (OUT, trial)).read().split()[0])
    pts = []
    for line in open("%s/ds%d_txbytes.txt" % (OUT, trial)):
        p = line.split()
        if len(p) == 2:
            pts.append((float(p[0]) - t0, int(p[1])))
    out = []
    for i in range(1, len(pts)):
        dt = pts[i][0] - pts[i - 1][0]
        if dt > 0:
            out.append((pts[i][0], (pts[i][1] - pts[i - 1][1]) * 8 / dt))
    return out


def tx_rows(trial):
    t0 = float(open("%s/ds%d_t0.txt" % (OUT, trial)).read().split()[0])
    rows = []
    for line in open("%s/ds%d_tx.jsonl" % (OUT, trial)):
        if '"fs"' not in line:
            continue
        r = json.loads(line)
        if r["fs"].endswith("|rdma") and "vf0>" in r["fs"]:
            rows.append((r["ts"] - t0, r))
    return rows


def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


print("%-6s %-9s %-11s %-13s %-13s %s"
      % ("trial", "survived", "hw latency", "tree 30->1.5G", "wire follows",
         "counter hi/lo (G)"))
surv = 0
for t in (1, 2, 3):
    ev = open("%s/ds%d_events.txt" % (OUT, t)).read()
    down_ts = [float(l.split()[0]) for l in ev.splitlines() if "down" in l][-1]
    ok = "rc=0" in ev.splitlines()[-1]
    surv += ok

    hw = "-"
    hwf = "%s/ds%d_hw.log" % (OUT, t)
    if os.path.exists(hwf):
        txt = open(hwf).read().splitlines()
        rel = [l for l in txt if "policy reloaded" in l]
        setl = [l for l in txt if "-> 1.50G" in l]
        if rel and setl:
            def hms(line):
                h, m, s = line.split()[2].split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
            hw = "%.0f ms" % ((hms(setl[0]) - hms(rel[0])) * 1e3)

    rows = tx_rows(t)
    t_start = next((ts for ts, r in rows if r["tree"] < 29e9 and ts > 15), None)
    t_end = next((ts for ts, r in rows if r["tree"] <= 1.55e9 and ts > 15), None)
    tree = ("%.0f ms" % ((t_end - t_start) * 1e3)
            if t_start and t_end else "-")

    w = wire(t)
    hi = mean([g for ts, g in w if 5 < ts < 19]) / 1e9
    lo = mean([g for ts, g in w if 25 < ts < 43]) / 1e9
    rates = "-" if not w else "%.2f / %.2f" % (hi, lo)
    # How fast the WIRE followed the tree. The 1 Hz counter cannot resolve
    # this - it only ever bounds it at ~1 s - so it is taken from the 1 ms
    # telemetry, and the counter is used for what it is good for: an
    # independent check on the two STEADY LEVELS.
    r_settle = "-"
    if t_start:
        hit = next((ts for ts, r in rows
                    if ts > t_start and r["r"] <= 1.65e9), None)
        if hit:
            r_settle = "%.0f ms" % ((hit - t_start) * 1e3)

    print("%-6d %-9s %-11s %-13s %-13s %s"
          % (t, "yes" if ok else "NO", hw, tree, r_settle, rates))

print("\nsurvival %d/3   (software limiter alone, 2026-07-27: 3/3;"
      " no limiter at all: 2/3)" % surv)
sys.exit(0)
