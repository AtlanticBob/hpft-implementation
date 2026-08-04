#!/usr/bin/env python3
"""Per-process CPU%%/RSS sampler for the DPU Arm (eval 2.3.2).

Scans /proc for processes whose cmdline matches any given pattern,
then samples utime+stime deltas and VmRSS at a fixed interval.
CSV to stdout: ts,name,pid,cpu_pct,rss_kb   (cpu_pct is single-core %%)

usage: arm_stat.py [--interval-ms 1000] [--duration 0] [pattern ...]
default patterns: rx_agent.py tx_agent_e.py vport_meter hpft_pace_shim
"""
import argparse, os, time, sys

CLK = os.sysconf("SC_CLK_TCK")

def find(patterns):
    out = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode()
        except OSError:
            continue
        for pat in patterns:
            if pat in cmd and "arm_stat" not in cmd:
                out[int(pid)] = pat
    return out

def cpu_ticks(pid):
    with open(f"/proc/{pid}/stat") as f:
        parts = f.read().rsplit(")", 1)[1].split()
    return int(parts[11]) + int(parts[12])   # utime + stime

def rss_kb(pid):
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    return 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval-ms", type=float, default=1000)
    ap.add_argument("--duration", type=float, default=0, help="seconds, 0=forever")
    ap.add_argument("patterns", nargs="*",
                    default=["rx_agent.py", "tx_agent_e.py", "vport_meter",
                             "hpft_pace_shim"])
    a = ap.parse_args()
    procs = find(a.patterns)
    if not procs:
        sys.exit("no matching processes")
    prev = {p: (cpu_ticks(p), time.monotonic()) for p in procs}
    print("ts,name,pid,cpu_pct,rss_kb", flush=True)
    t_end = time.monotonic() + a.duration if a.duration else None
    while t_end is None or time.monotonic() < t_end:
        time.sleep(a.interval_ms / 1e3)
        now_wall = time.time()
        for pid, name in list(procs.items()):
            try:
                t = cpu_ticks(pid); m = time.monotonic()
                pt, pm = prev[pid]
                pct = (t - pt) / CLK / (m - pm) * 100
                print(f"{now_wall:.3f},{name},{pid},{pct:.1f},{rss_kb(pid)}",
                      flush=True)
                prev[pid] = (t, m)
            except OSError:            # process died
                del procs[pid]
        if not procs:
            break

if __name__ == "__main__":
    main()
