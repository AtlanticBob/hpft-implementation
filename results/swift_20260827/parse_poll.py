#!/usr/bin/env python3
"""poll_<dpu>.log -> CSV rows (t_wall, pair, ft, cwnd, rtt_s, target, cc_rate,
n_dec, n_nack, qp, rtt_last, rtt_min, rtt_n). Each HPFT_RSP is followed by
the HPFT_SET line of the 0xdee query that produced it (pair index = rate=,
wall clock = t0=)."""
import re, sys
rsp = re.compile(r"HPFT_RSP ft=0x([0-9a-f]+) bud=(\d+) lvl=(\d+) avg16=(\d+) r=(\d+) s16=(\d+) ep=(\d+) evb32=(\d+) w8=(\d+) w9=(\d+) w10=(\d+)")
sett = re.compile(r"HPFT_SET ft=0xdee rate=(\d+) .* t0=([0-9.]+)")
def parse(path):
    out, pend = [], None
    for line in open(path, errors="replace"):
        m = rsp.search(line)
        if m:
            pend = m.groups(); continue
        m = sett.search(line)
        if m and pend:
            out.append((float(m.group(2)), int(m.group(1)), int(pend[0], 16)) + tuple(int(x) for x in pend[1:]))
            pend = None
    return out
if __name__ == "__main__":
    print("t,pair,ft,cwnd,rtt_s,target,cc_rate,n_dec,n_nack,qp,rtt_last,nslot,rtt_n")
    for r in parse(sys.argv[1]):
        print(",".join(str(x) for x in r))
