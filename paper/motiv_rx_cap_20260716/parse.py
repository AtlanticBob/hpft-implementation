#!/usr/bin/env python3
"""Parse motivation-1.3 runs into perflow.csv + runs.csv.

Run dirs: runs/<arm>_run<N>   (arm = gbn | sr)

Columns of interest beyond bandwidth:
  ce_marked / cnp_sent / cnp_handled -- the DCQCN loop. A class-blind policer
      drops without marking, so these stay at 0: DCQCN never fires.
  rdma_off/dlv_gbps, rdma_drop_pct -- RDMA offered (sender port_xmit_data) vs
      delivered (receiver port_rcv_data); the gap is what the policer ate.
  seq_err -- RDMA NAK/out-of-order events (loss symptoms).
  tcp_retrans -- TCP retransmitted segments (loss symptoms).
  wire_in/meter_pass_gbps -- what reached the receiver DPU vs what the 20G
      meter let through to the VM.
"""
import csv, glob, json, os, re, sys

BASE = os.path.dirname(os.path.abspath(__file__))
RUN_RE = re.compile(r"^(?P<arm>gbn|sr|sr1024)_run(?P<run>\d+)$")
DUR = 60.0


def kv(p):
    d = {}
    if not os.path.exists(p):
        return d
    for ln in open(p):
        if "=" in ln:
            k, v = ln.strip().split("=", 1)
            try:
                d[k] = int(v)
            except ValueError:
                pass
    return d


def rdma_gbps(path):
    try:
        txt = open(path, errors="replace").read()
    except FileNotFoundError:
        return None
    m = re.search(r"^\s*65536\s+\d+\s+([\d.]+)\s+([\d.]+)", txt, re.M)
    return float(m.group(2)) if m else None


def tcp_gbps(path):
    try:
        d = json.load(open(path))
        return (d["end"]["sum_received"]["bits_per_second"] / 1e9,
                d["end"]["sum_sent"].get("retransmits"))
    except Exception:
        return None, None


def main():
    perflow, runs = [], []
    for d in sorted(glob.glob(os.path.join(BASE, "runs", "*"))):
        m = RUN_RE.match(os.path.basename(d))
        if not m:
            continue
        arm, run = m["arm"], int(m["run"])
        rd_tot = tc_tot = rtx_tot = 0.0
        for v in range(4):          # source VF (tenant)
            for i in range(4):
                bw = rdma_gbps(f"{d}/rdma_v{v}_f{i}.log")
                perflow.append([arm, run, "rdma", v, i,
                                round(bw, 3) if bw else "", ""])
                rd_tot += bw or 0.0
            for j in range(4):
                bw, rtx = tcp_gbps(f"{d}/tcp_v{v}_f{j}.json")
                perflow.append([arm, run, "tcp", v, j,
                                round(bw, 3) if bw else "",
                                rtx if rtx is not None else ""])
                tc_tot += bw or 0.0
                rtx_tot += rtx or 0
        hp, hq = kv(f"{d}/host_pre.txt"), kv(f"{d}/host_post.txt")
        pp, pq = kv(f"{d}/peer_pre.txt"), kv(f"{d}/peer_post.txt")
        dp, dq = kv(f"{d}/dpu2_pre.txt"), kv(f"{d}/dpu2_post.txt")

        ce = pq.get("mlx5_3.np_ecn_marked_roce_packets", 0) - pp.get("mlx5_3.np_ecn_marked_roce_packets", 0)
        cnp_s = pq.get("mlx5_3.np_cnp_sent", 0) - pp.get("mlx5_3.np_cnp_sent", 0)
        cnp_h = hq.get("mlx5_3.rp_cnp_handled", 0) - hp.get("mlx5_3.rp_cnp_handled", 0)
        seq = sum(hq.get(f"mlx5_{x}.packet_seq_err", 0) - hp.get(f"mlx5_{x}.packet_seq_err", 0)
                  for x in range(6, 10))
        off = sum(hq.get(f"mlx5_{x}.port_xmit_data", 0) - hp.get(f"mlx5_{x}.port_xmit_data", 0)
                  for x in range(6, 10)) * 4
        dlv = (pq.get("mlx5_6.port_rcv_data", 0) - pp.get("mlx5_6.port_rcv_data", 0)) * 4
        arr = dq.get("p1.rx_bytes_phy", 0) - dp.get("p1.rx_bytes_phy", 0)
        mb = dq.get("meter.byte_in", 0) - dp.get("meter.byte_in", 0)

        g = lambda b: round(b * 8 / DUR / 1e9, 2)
        runs.append([arm, run, round(rd_tot, 2), round(tc_tot, 2),
                     round(rd_tot + tc_tot, 2),
                     g(off), g(dlv),
                     round((1 - dlv / off) * 100, 1) if off else "",
                     g(arr), g(mb),
                     ce, cnp_s, cnp_h, seq, int(rtx_tot)])

    with open(f"{BASE}/perflow.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["arm", "run", "cls", "src_vf", "flow", "gbps", "tcp_retrans"])
        w.writerows(perflow)
    with open(f"{BASE}/runs.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["arm", "run", "rdma_gbps", "tcp_gbps", "total_gbps",
                    "rdma_off_gbps", "rdma_dlv_gbps", "rdma_drop_pct",
                    "wire_in_gbps", "meter_pass_gbps",
                    "ce_marked", "cnp_sent", "cnp_handled",
                    "seq_err", "tcp_retrans"])
        w.writerows(runs)
    print(f"parsed {len(runs)} runs")


if __name__ == "__main__":
    main()
