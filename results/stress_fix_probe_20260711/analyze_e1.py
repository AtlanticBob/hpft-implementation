#!/usr/bin/env python3
"""E1/E2 analysis: align per-second RDMA wire rate with RC hardware
counter deltas (sender ack-timeouts, receiver duplicates). Confirms or
kills the retransmit-bypass theory: timeouts/dups must spike at episode
onsets and be quiet elsewhere."""
import json
import sys
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else "e1")
FS_R = "sgpu01/vf0>sgpu02/vf0|rdma"

t0 = float((DIR / "repro_t0.txt").read_text().strip())

rate = {}
for line in (DIR / "repro_rx.jsonl").read_text().splitlines():
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        continue
    if rec["ts"] < t0:
        continue
    rate.setdefault(int(rec["ts"] - t0), []).append(rec["r"].get(FS_R, 0))

def load_ctr(path):
    out = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        ts = float(parts[0])
        out[int(ts - t0)] = {kv.split("=")[0]: int(kv.split("=")[1])
                             for kv in parts[1:]}
    return out

snd = load_ctr(DIR / "snd_ctr.txt")
rcv = load_ctr(DIR / "rcv_ctr.txt")

def delta(series, key, sec):
    a, b = series.get(sec - 1), series.get(sec)
    return b[key] - a[key] if a and b else 0

secs = sorted(s for s in rate if 15 <= s <= 235)
tot_tmo = tot_dup = 0
events = []
for s in secs:
    r = sum(rate[s]) / len(rate[s]) / 1e9
    tmo = delta(snd, "tmo", s)
    dup = delta(rcv, "dup", s)
    oos = delta(rcv, "oos", s)
    tot_tmo += tmo
    tot_dup += dup
    if tmo or dup > 10 or r < 1.5:
        events.append((s, r, tmo, dup, oos))

print("window totals: ack_timeouts=%d dup_requests=%d" % (tot_tmo, tot_dup))
print("eventful seconds (timeout, dup>10, or rdma<1.5G):")
print("  sec   rdmaG   tmo/s   dup/s   oos/s")
for s, r, tmo, dup, oos in events[:40]:
    print("  %3d  %6.2f  %6d  %6d  %6d" % (s, r, tmo, dup, oos))
if not events:
    print("  (none - clean run)")
