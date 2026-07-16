#!/usr/bin/env python3
"""Parse an experiment's runs/ directory into perflow.csv + runs.csv.

Usage: parse.py <exp_dir>          # e.g. tools/parse.py exp1_ecn_band
Run dirs must be named  <config>_<arm>_run<N>   (arm = gbn | sr).

perflow.csv : one row per flow  (config, arm, run, cls, vf, flow, gbps, tcp_retrans)
runs.csv    : one row per run   (aggregates + mechanism counters)

Counter semantics
  *_gbps      application-reported goodput (perftest BW average / iperf3
              sum_received) -- payload only, retransmits not double counted.
  wire_*_gbps receiver-side hardware vport octets (vport-meter), i.e. what
              actually arrived on the wire: headers + (for GBN) discarded
              out-of-order arrivals. wire/goodput - 1 = "wire tax".
  seq_err     sender packet_seq_err delta = retransmission events.
  cnp / ecn_marked  DCQCN loop: switch CE marks -> receiver CNPs -> sender.
"""
import csv, glob, json, os, re, sys
from collections import defaultdict

RUN_RE = re.compile(r"^(?P<config>.+)_(?P<arm>gbn|sr)_run(?P<run>\d+)$")


def rdma_gbps(path):
    """perftest -D mode: use the BW-average column; the iterations column
    is unreliable in duration mode (known perftest quirk)."""
    try:
        txt = open(path, errors="replace").read()
    except FileNotFoundError:
        return None
    m = re.search(r"^\s*65536\s+\d+\s+([\d.]+)\s+([\d.]+)\s+[\d.]+\s*$", txt, re.M)
    return float(m.group(2)) if m else None


def tcp_gbps(path):
    try:
        d = json.load(open(path))
        return (d["end"]["sum_received"]["bits_per_second"] / 1e9,
                d["end"]["sum_sent"].get("retransmits"))
    except Exception:
        return None, None


def kv(path):
    out = {}
    if not os.path.exists(path):
        return out
    for ln in open(path):
        if "=" in ln:
            k, v = ln.strip().split("=", 1)
            try:
                out[k] = int(v)
            except ValueError:
                pass
    return out


def wire_gbps(d):
    """Class-level wire rate from the 1 Hz vport-meter series (4 VFs)."""
    p = os.path.join(d, "vpm_series.csv")
    if not os.path.exists(p):
        return None, None
    pts = defaultdict(list)
    for ln in open(p):
        if ln.startswith("ts"):
            continue
        _, vi, tn, ib, eth = ln.strip().split(",")
        pts[int(vi)].append((int(tn), int(ib), int(eth)))
    if not pts:
        return None, None
    ib = eth = 0
    span = 0.0
    for v in pts.values():
        span = max(span, (v[-1][0] - v[0][0]) / 1e9)
        ib += v[-1][1] - v[0][1]
        eth += v[-1][2] - v[0][2]
    if span <= 0:
        return None, None
    return ib * 8 / span / 1e9, eth * 8 / span / 1e9


def rtt_ms(d):
    p = os.path.join(d, "ping.txt")
    try:
        return float(open(p).read().strip().split("\n")[-1].split("/")[5 - 1])
    except Exception:
        return None


def main(exp):
    runs_dir = os.path.join(exp, "runs")
    perflow, runs = [], []
    for d in sorted(glob.glob(os.path.join(runs_dir, "*"))):
        if not os.path.isdir(d):
            continue
        m = RUN_RE.match(os.path.basename(d))
        if not m:
            print(f"  skip (name): {os.path.basename(d)}")
            continue
        cfg, arm, run = m["config"], m["arm"], int(m["run"])
        rd_tot = tc_tot = rtx_tot = 0.0
        dead = 0
        for v in range(4):
            for i in range(4):
                bw = rdma_gbps(os.path.join(d, f"rdma_v{v}_f{i}.log"))
                if bw is None:
                    dead += 1
                perflow.append([cfg, arm, run, "rdma", v, i,
                                round(bw, 3) if bw else "", ""])
                rd_tot += bw or 0.0
            for j in range(4):
                bw, rtx = tcp_gbps(os.path.join(d, f"tcp_v{v}_f{j}.json"))
                perflow.append([cfg, arm, run, "tcp", v, j,
                                round(bw, 3) if bw else "",
                                rtx if rtx is not None else ""])
                tc_tot += bw or 0.0
                rtx_tot += rtx or 0
        hp, hq = kv(f"{d}/host_pre.txt"), kv(f"{d}/host_post.txt")
        pp, pq = kv(f"{d}/peer_pre.txt"), kv(f"{d}/peer_post.txt")
        seq = sum(hq.get(k, 0) - hp.get(k, 0)
                  for k in hq if k.endswith("packet_seq_err"))
        cnp = hq.get("mlx5_3.rp_cnp_handled", 0) - hp.get("mlx5_3.rp_cnp_handled", 0)
        mrk = (pq.get("mlx5_3.np_ecn_marked_roce_packets", 0)
               - pp.get("mlx5_3.np_ecn_marked_roce_packets", 0))
        wib, weth = wire_gbps(d)
        runs.append([cfg, arm, run,
                     round(rd_tot, 2), round(tc_tot, 2), round(rd_tot + tc_tot, 2),
                     round(wib, 2) if wib else "", round(weth, 2) if weth else "",
                     rtt_ms(d), seq, cnp, mrk, int(rtx_tot), dead])

    with open(os.path.join(exp, "perflow.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["config", "arm", "run", "cls", "vf", "flow", "gbps", "tcp_retrans"])
        w.writerows(perflow)
    with open(os.path.join(exp, "runs.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["config", "arm", "run", "rdma_gbps", "tcp_gbps", "total_gbps",
                    "wire_ib_gbps", "wire_eth_gbps", "rtt_ms", "seq_err", "cnp",
                    "ecn_marked", "tcp_retrans", "dead_rdma_flows"])
        w.writerows(runs)
    print(f"{exp}: {len(runs)} runs -> perflow.csv ({len(perflow)} rows), runs.csv")


if __name__ == "__main__":
    main(sys.argv[1])
