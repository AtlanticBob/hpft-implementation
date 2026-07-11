#!/usr/bin/env python3
"""V1 analysis: 240s repro under the realization-aware probe margin.

Old-anchor baseline (stress_ratio repro, same protocol): periodic RDMA
collapse 3s->8s->20s deepening, mean 1.96G vs 3G entitlement, min 0.14G.
Judge: collapse episodes (rdma < 1.5G sustained >= 2s), per-class band
residence vs the 3G/3G split, VQ-storm frequency (marks in rx jsonl).
"""
import json
import sys
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
FS_R = "sgpu01/vf0>sgpu02/vf0|rdma"
FS_T = "sgpu01/vf0>sgpu02/vf0|tcp"

t0 = float((DIR / "repro_t0.txt").read_text().strip())
per_sec = {}
for line in (DIR / "repro_rx.jsonl").read_text().splitlines():
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        continue
    if not (t0 + 15 <= rec["ts"] < t0 + 235):
        continue
    sec = int(rec["ts"] - t0)
    d = per_sec.setdefault(sec, {"r": [], "t": [], "sr": [], "st": []})
    d["r"].append(rec["r"].get(FS_R, 0))
    d["t"].append(rec["r"].get(FS_T, 0))
    d["sr"].append(rec["s"].get(FS_R, 0))
    d["st"].append(rec["s"].get(FS_T, 0))

secs = sorted(per_sec)
rser = [sum(per_sec[s]["r"]) / len(per_sec[s]["r"]) / 1e9 for s in secs]
tser = [sum(per_sec[s]["t"]) / len(per_sec[s]["t"]) / 1e9 for s in secs]
marks = [max(per_sec[s]["sr"]) for s in secs]

n = len(secs)
mean_r, mean_t = sum(rser) / n, sum(tser) / n
in_band = sum(1 for r, t in zip(rser, tser)
              if abs(r - 3.0) <= 0.3 and abs(t - 3.0) <= 0.3)
episodes, cur = [], 0
for r in rser:
    cur = cur + 1 if r < 1.5 else 0
    if cur == 2:
        episodes.append(1)
mark_secs = sum(1 for m in marks if m > 0.5)

print("window %ds  rdma mean=%.2fG min=%.2fG  tcp mean=%.2fG min=%.2fG"
      % (n, mean_r, min(rser), mean_t, min(tser)))
print("both-in-band(+-10%%): %d/%d s (%.0f%%)   collapse episodes(<1.5G>=2s): %d"
      % (in_band, n, 100 * in_band / n, len(episodes)))
print("heavy-mark seconds (s_rdma>0.5): %d/%d  util mean=%.2fG"
      % (mark_secs, n, mean_r + mean_t))
print("baseline(old anchor): mean_r 1.96G, min 0.14G, periodic collapses")
