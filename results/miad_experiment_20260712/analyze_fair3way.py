#!/usr/bin/env python3
"""28-fs three-way: per-class aggregate fairness + stability. Reference:
each dst VM (20G) has 4 TCP + 3 RDMA; class split 1:1 -> ~10/10 per VM.
Reports class totals, aggregate, and per-flow-set steady sd (oscillation)."""
import json
import sys
from pathlib import Path

DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")


def analyze(tag):
    try:
        t0 = float((DIR / f"{tag}_t0.txt").read_text().strip())
    except FileNotFoundError:
        return None
    persec = {}
    for jl in (DIR / f"{tag}_rx.jsonl").read_text().splitlines():
        try:
            rec = json.loads(jl)
        except json.JSONDecodeError:
            continue
        s = int(rec["ts"] - t0)
        if not (25 <= s < 90):
            continue
        for f, v in rec["r"].items():
            persec.setdefault(f, {}).setdefault(s, []).append(v)
    means, sds = {}, []
    for f, secs in persec.items():
        ser = [sum(x) / len(x) / 1e9 for _, x in sorted(secs.items())]
        m = sum(ser) / len(ser)
        means[f] = m
        sds.append((sum((x - m) ** 2 for x in ser) / len(ser)) ** 0.5)
    tcp = sum(m for f, m in means.items() if f.endswith("tcp"))
    rdma = sum(m for f, m in means.items() if f.endswith("rdma"))
    nfs = len(means)
    # per-dst-VM class ratio (avg over 4 dst VMs)
    dst_ratio = []
    for j in range(4):
        rt = sum(m for f, m in means.items() if f.endswith("vf%d|rdma" % j))
        tt = sum(m for f, m in means.items() if f.endswith("vf%d|tcp" % j))
        if tt > 0.1:
            dst_ratio.append(rt / tt)
    return nfs, rdma, tcp, sum(dst_ratio) / len(dst_ratio) if dst_ratio else 0, max(sds)


print("%-5s  nfs  rdmaG  tcpG  aggG  dst_class_ratio(tgt~1)  max_fs_sd" % "law")
for law, tag in (("aimd", "f3_aimd"), ("miad", "f3_miad"), ("mimd", "f3_mimd")):
    r = analyze(tag)
    if r is None:
        print("%-5s (no data)" % law); continue
    nfs, rdma, tcp, ratio, maxsd = r
    print("%-5s  %3d  %5.1f  %5.1f  %5.1f  %8.2f              %6.2f"
          % (law, nfs, rdma, tcp, rdma + tcp, ratio, maxsd))
