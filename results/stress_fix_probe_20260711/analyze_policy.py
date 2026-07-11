#!/usr/bin/env python3
"""D3c analysis: convergence per online policy step.

For each step in policy_steps.tsv: targets are cap x w/sum(w) per class.
first_entry = first time both classes are within +-10% of target (for the
flap sequence only the final settle is judged); settle = last time either
class leaves the band before the next step (0 = never left after entry).
Also overshoot/undershoot of the total in the first 3s.
"""
import json
import sys
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
FS_R = "sgpu01/vf0>sgpu02/vf0|rdma"
FS_T = "sgpu01/vf0>sgpu02/vf0|tcp"

steps = []
for line in (DIR / "policy_steps.tsv").read_text().splitlines():
    tag, cap, w, ts = line.split("\t")
    rw, tw = (float(x) for x in w.split(":"))
    steps.append((tag, int(cap) / 1e9, rw, tw, float(ts)))

rx = []
for line in (DIR / "rx_pol.jsonl").read_text().splitlines():
    try:
        rec = json.loads(line)
        rx.append((rec["ts"], rec["r"].get(FS_R, 0) / 1e9,
                   rec["r"].get(FS_T, 0) / 1e9))
    except (json.JSONDecodeError, KeyError):
        pass
rx.sort()

print("%-10s %6s %6s %6s | %9s %8s %9s %9s"
      % ("step", "capG", "tgt_r", "tgt_t", "first_in", "settled",
         "min_tot3s", "max_tot3s"))
for k, (tag, cap, rw, tw, t0) in enumerate(steps):
    if tag == "init":
        continue
    t1 = steps[k + 1][4] if k + 1 < len(steps) else t0 + 30
    tr, tt = cap * rw / (rw + tw), cap * tw / (rw + tw)
    win = [(ts, r, t) for ts, r, t in rx if t0 <= ts < t1]
    inband = [ts for ts, r, t in win
              if abs(r - tr) <= 0.1 * tr and abs(t - tt) <= 0.1 * tt]
    first = inband[0] - t0 if inband else None
    settled = None
    if inband:
        out_after = [ts for ts, r, t in win if ts > inband[0]
                     and (abs(r - tr) > 0.1 * tr or abs(t - tt) > 0.1 * tt)]
        settled = (out_after[-1] - t0) if out_after else first
    tot3 = [r + t for ts, r, t in win if ts < t0 + 3]
    print("%-10s %6.1f %6.2f %6.2f | %9s %8s %9.2f %9.2f"
          % (tag, cap, tr, tt,
             "%.2fs" % first if first is not None else "never",
             "%.2fs" % settled if settled is not None else "-",
             min(tot3) if tot3 else float("nan"),
             max(tot3) if tot3 else float("nan")))
