#!/usr/bin/env python3
"""Sample the RDMA executor's per flow-set state on a sender DPU during a run.

Writes "0xded <slot>" queries into the RP FIFO and reads the HPFT_RSP lines
the RP host process prints to /tmp/pcc_rp.log. Each query costs one mailbox
send (13-22 ms), so only the slots that hold a flow set are queried after the
first full scan; every RESCAN_S seconds all slots are scanned again so a flow
set that appears later (a join) is picked up.

The readback (rp_rtt_template_dev_main.c, 0xded) is, per flow set: id, R
(the ledger's budget), the sum of the paced rates of its QPs, the sum of the
CC rates over every QP on its list, the sum of the CC rates over the QPs
that drew tokens in the last millisecond, how many QPs that is, and how many
QPs are on the list. Rates are converted from the device's 2^20-of-line-rate
units to Gb/s of a 200 G port.

usage: rp_sample.py <duration_s> <out_jsonl> [interval_s=1.0] [line_gbps=200]
record: {"ts", "slot", "id", "R", "paced", "cc", "cc_live", "nlive", "nq"}
"""
import json, os, re, sys, time
FIFO, LOG = "/tmp/rp_fifo", "/tmp/pcc_rp.log"
dur = float(sys.argv[1]); out = sys.argv[2]
itv = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
line = float(sys.argv[4]) if len(sys.argv) > 4 else 200.0
NSLOT = 32
RESCAN_S = 5.0
RSP = re.compile(r"HPFT_RSP ft=0x([0-9a-f]+) bud=(\d+) lvl=(\d+) avg16=(\d+) r=(\d+) s16=(\d+) ep=(\d+)")


def gbps(u): return u * line / (1 << 20)


def query(slots):
    """Query the given slots one at a time and wait for each answer, so a
    response is paired with its query by construction: the response carries
    the set id but not the slot, and pairing by order with a fixed wait let a
    late answer (the mailbox takes 13-22 ms and jitters) shift every later
    pair (one id showed up under three slots, 2026-09-08). Returns
    [(slot, match-or-None)]."""
    res = []
    for s in slots:
        with open(LOG, "rb") as lf:
            lf.seek(0, os.SEEK_END); pos = lf.tell()
        # O_WRONLY without O_CREAT: protected_fifos forbids a create-open of
        # a FIFO in sticky /tmp for anyone but its owner, even root
        fd = os.open(FIFO, os.O_WRONLY)
        os.write(fd, ("0xded %d\n" % s).encode()); os.close(fd)
        m = None
        for _ in range(20):                  # up to 200 ms for the answer
            time.sleep(0.01)
            with open(LOG, "rb") as lf:
                lf.seek(pos); new = lf.read().decode(errors="replace")
            ms = [RSP.search(l) for l in new.splitlines() if "HPFT_RSP" in l]
            if ms and ms[-1]:
                m = ms[-1]; break
        res.append((s, m))
    return res


t_end = time.time() + dur
live = None
t_scan = 0.0
with open(out, "w") as o:
    while time.time() < t_end:
        t = time.time()
        full = live is None or t - t_scan >= RESCAN_S
        slots = list(range(NSLOT)) if full else live
        if full:
            t_scan = t
        answered = []
        for s, m in query(slots):
            if not m:
                continue
            sid = int(m.group(1), 16)
            if not sid:
                continue
            answered.append(s)
            rec = {"ts": round(t, 3), "slot": s, "id": "0x%08x" % sid,
                   "R": round(gbps(int(m.group(2))), 3), "paced": round(gbps(int(m.group(3))), 3),
                   "cc": round(gbps(int(m.group(4))), 3), "cc_live": round(gbps(int(m.group(5))), 3),
                   "nlive": int(m.group(6)), "nq": int(m.group(7))}
            o.write(json.dumps(rec) + "\n")
        o.flush()
        if full and answered:
            live = answered
        time.sleep(max(0.0, itv - (time.time() - t)))
