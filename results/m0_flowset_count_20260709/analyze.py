#!/usr/bin/env python3
"""M0 acceptance analysis: flow-set meter (JSONL) vs self-reported rates and
representor vport counters.

Wire-overhead factors (self-report is payload/goodput; the meter counts L2
wire bytes):
  TCP  : MSS 1448 -> 1514 B frames (eth14+ip20+tcp32(ts))      = 1.045580
  RDMA : active_mtu 1024, msg 65536 = 64 pkts * 58 B hdr + RETH = 1.056885
         (eth14+ip20+udp8+bth12+icrc4 = 58; one 16 B RETH per message)
"""
import json

F_TCP = 1514 / 1448
F_RDMA = (65536 + 64 * 58 + 16) / 65536

FS_TCP0 = "sgpu01/vf0>sgpu02/vf0|tcp"
FS_RD0 = "sgpu01/vf0>sgpu02/vf0|rdma"
FS_RD1 = "sgpu01/vf1>sgpu02/vf1|rdma"

recs = [json.loads(l) for l in open("hpft_rxagent_m0.jsonl")]


def fs_bytes(fsid, t0, t1):
    return sum(r["bytes"].get(fsid, 0) for r in recs if t0 <= r["ts"] <= t1)


def vport_delta(pre, post, col=1):
    p0, p1 = open(pre).read().split(), open(post).read().split()
    return int(p1[col]) - int(p0[col])


def perftest_payload(log, dur_s):
    """-D mode: the iterations column is unreliable (~half the real message
    count); BW average [Gb/s] x duration is the ground truth."""
    for line in open(log):
        toks = line.split()
        if len(toks) >= 5 and toks[0] == "65536":
            return float(toks[3]) * 1e9 * dur_s / 8
    raise ValueError("no result line in " + log)


def report(name, meter_b, payload_b, factor, vport_b=None):
    exp = payload_b * factor
    line = "%-28s meter=%11.3fMB expect=%11.3fMB err=%+.2f%%" % (
        name, meter_b / 1e6, exp / 1e6, (meter_b / exp - 1) * 100)
    if vport_b:
        line += "   vs vport=%11.3fMB err=%+.2f%%" % (
            vport_b / 1e6, (meter_b / vport_b - 1) * 100)
    print(line)


def tcp_expected(j):
    """Wire bytes = received goodput x frame overhead + retransmitted frames
    (the meter counts wire truth; goodput excludes retransmissions)."""
    return (j["end"]["sum_received"]["bytes"] * F_TCP
            + j["end"]["sum_sent"]["retransmits"] * 1514)


# --- Run A: single TCP vf0 ---
ja = json.load(open("runA_iperf3.json"))
a0 = ja["start"]["timestamp"]["timesecs"]
a1 = a0 + ja["end"]["sum_received"]["seconds"]
report("A tcp vf0", fs_bytes(FS_TCP0, a0 - 2, a1 + 3), tcp_expected(ja), 1.0,
       vport_delta("runA_vport_pre.txt", "runA_vport_post.txt"))

# --- Run B: single RDMA vf0 ---
b0 = float(open("runB_t0.txt").read())
b1 = float(open("runB_t1.txt").read())
report("B rdma vf0", fs_bytes(FS_RD0, b0 - 1, b1 + 2),
       perftest_payload("runB_client.log", 20), F_RDMA,
       vport_delta("runB_vport_pre.txt", "runB_vport_post.txt"))

# --- Run C: concurrent vf0 tcp + vf0 rdma + vf1 rdma ---
c0 = float(open("runC_t0.txt").read())
c1 = float(open("runC_t1.txt").read())
jc = json.load(open("runC_iperf3.json"))
report("C tcp vf0", fs_bytes(FS_TCP0, c0 - 1, c1 + 2), tcp_expected(jc), 1.0)
report("C rdma vf0", fs_bytes(FS_RD0, c0 - 1, c1 + 2),
       perftest_payload("runC_rdma_vf0.log", 25), F_RDMA)
report("C rdma vf1", fs_bytes(FS_RD1, c0 - 1, c1 + 2),
       perftest_payload("runC_rdma_vf1.log", 25), F_RDMA)
tot0 = fs_bytes(FS_TCP0, c0 - 1, c1 + 2) + fs_bytes(FS_RD0, c0 - 1, c1 + 2)
v0 = vport_delta("runC_vport_pre.txt", "runC_vport_post.txt", 1)
v1 = vport_delta("runC_vport_pre.txt", "runC_vport_post.txt", 2)
print("%-28s meter=%11.3fMB vport=%11.3fMB err=%+.2f%%" %
      ("C vf0 tcp+rdma vs vport", tot0 / 1e6, v0 / 1e6, (tot0 / v0 - 1) * 100))
print("%-28s meter=%11.3fMB vport=%11.3fMB err=%+.2f%%" %
      ("C vf1 rdma vs vport",
       fs_bytes(FS_RD1, c0 - 1, c1 + 2) / 1e6, v1 / 1e6,
       (fs_bytes(FS_RD1, c0 - 1, c1 + 2) / v1 - 1) * 100))

# --- read latency over the whole session ---
rd = sorted(r["read_ms"] for r in recs)
n = len(rd)
print("\nread_ms: n=%d p50=%.1f p95=%.1f p99=%.1f max=%.1f (budget T/3=16.7)" %
      (n, rd[n // 2], rd[int(n * .95)], rd[int(n * .99)], rd[-1]))

# --- repro (b) timeline: vf0 rdma crush by coexisting tcp (1 s bins) ---
t0 = float(open("reproB_t0.txt").read()) - 2
print("\nrepro(b) vf0 rdma vs tcp, 1s bins (rdma starts t=2, tcp ~12-22s):")
bins = {}
for r in recs:
    if t0 <= r["ts"] <= t0 + 42:
        b = int(r["ts"] - t0)
        d = bins.setdefault(b, [0, 0])
        d[0] += r["bytes"].get(FS_RD0, 0)
        d[1] += r["bytes"].get(FS_TCP0, 0)
print("  t(s): " + " ".join("%4d" % b for b in sorted(bins)))
print("  rdma: " + " ".join("%4.1f" % (v[0] * 8 / 1e9) for _, v in sorted(bins.items())))
print("  tcp : " + " ".join("%4.1f" % (v[1] * 8 / 1e9) for _, v in sorted(bins.items())))
