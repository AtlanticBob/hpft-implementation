#!/usr/bin/env python3
"""Sample the RDMA executor's per-pair state on a sender DPU during a run.

Writes "0xded <idx>" queries into the RP FIFO (control lines survive the
host's latest-wins drain; each costs one mailbox send, ~13-22 ms, so keep
the pair count small) and reads the HPFT_RSP lines the RP host process
prints to /tmp/pcc_rp.log. First pass queries every slot to learn which
slots hold a flowtag; later passes only those. Rates are converted from
the device's 2^20-of-line-rate units to Gb/s, trust from fxp16.

usage: rp_probe.py <duration_s> <out_jsonl> [interval_s=1.0] [line_gbps=200]
record: {"ts", "ft", "level", "paced", "cc", "d", "pace_limited", "trust",
         "nack", "loss_ep", "sr_cuts"} - nack is how many NACKs this pair has
taken, loss_ep how many epochs the loss fast path granted the trust rise, and
sr_cuts how many slow-restart cuts the CC term applied because of loss.
"""
import json, os, re, sys, time
FIFO, LOG = "/tmp/rp_fifo", "/tmp/pcc_rp.log"
dur = float(sys.argv[1]); out = sys.argv[2]
itv = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
line = float(sys.argv[4]) if len(sys.argv) > 4 else 200.0
NSLOT = 16
RSP = re.compile(r"HPFT_RSP ft=0x([0-9a-f]+) bud=(\d+) lvl=(\d+) avg16=(\d+) r=(\d+) s16=(\d+) ep=(\d+) evb32=(\d+) w8=(\d+) w9=(\d+) w10=(\d+)")


def gbps(u): return u * line / (1 << 20)


def query(slots):
    """Query the given slots one by one; returns [(slot, match-or-None)],
    responses paired with queries by order (one HPFT_RSP per query)."""
    out = []
    for s in slots:
        with open(LOG, "rb") as lf:
            lf.seek(0, os.SEEK_END); pos = lf.tell()
        # O_WRONLY without O_CREAT: protected_fifos forbids a create-open of
        # a FIFO in sticky /tmp for anyone but its owner, even root
        fd = os.open(FIFO, os.O_WRONLY)
        os.write(fd, ("0xded %d\n" % s).encode()); os.close(fd)
        time.sleep(0.03)
        with open(LOG, "rb") as lf:
            lf.seek(pos); new = lf.read().decode(errors="replace")
        ms = [RSP.search(l) for l in new.splitlines() if "HPFT_RSP" in l]
        out.append((s, ms[-1] if ms and ms[-1] else None))
    return out


t_end = time.time() + dur
live = None
with open(out, "w") as o:
    while time.time() < t_end:
        t = time.time()
        slots = list(range(NSLOT)) if live is None else live
        answered = []
        for s, m in query(slots):
            if not m:
                continue
            ft = int(m.group(1), 16)
            if ft == 0:
                continue
            answered.append(s)
            rec = {"ts": t, "slot": s, "ft": "0x%08x" % ft, "level": round(gbps(int(m.group(2))), 3),
                   "paced": round(gbps(int(m.group(3))), 3), "cc": round(gbps(int(m.group(6))), 3),
                   "d": int(m.group(5)), "pace_limited": int(m.group(8)), "trust": round(int(m.group(11)) / 65536.0, 4),
                   "loss_ep": int(m.group(4)), "nack": int(m.group(7)),
                   "sr_cuts": int(m.group(10))}
            o.write(json.dumps(rec) + "\n")
        o.flush()
        if live is None and answered:
            live = answered
        time.sleep(max(0.0, itv - (time.time() - t)))
