#!/usr/bin/env python3
"""Sample the RDMA executor's per flow-set state on a sender DPU during a run.

Writes "0xded <slot>" queries into the RP FIFO and reads the HPFT_RSP lines
the RP host process prints to /tmp/pcc_rp.log. Each query costs one mailbox
round (about 16 ms) plus the wait for its answer, and it shares the FIFO with
the sender agent's budget pushes. Every slot is queried every pass: the whole
32-slot table costs about 0.53 s inside a 1 s period, so a flow set that
appears later (a join) is in the data from the very next pass. The per-QP
record dump is the one thing too big to do in a pass, so it is spread over the
passes that follow it.

The readback (rp_rtt_template_dev_main.c, 0xded) is, per flow set: id, R
(the permitted rate min(R, U) the sender agent wrote), RATE x TIME the set was paced at since the last read,
the sum of the CC rates over every QP on its list, the sum of the CC rates
over the QPs that drew tokens in the last millisecond, how many QPs that is,
and how many QPs are on the list. Rates are converted from the device's
2^20-of-line-rate units to Gb/s of a 200 G port.

The third field is a counter (rate x time), not a rate, and this is where it
becomes one: divided by the interval between THIS query of the slot and the
previous one. A once-a-second sample of the instantaneous rate would not do:
that quantity moves every few microseconds. The first query of a slot has no
interval yet, so it only starts the clock and emits nothing.

usage: rp_sample.py <duration_s> <out_jsonl> [interval_s=1.0] [line_gbps=200]
record: {"ts", "slot", "id", "R", "paced", "cc", "cc_live", "nlive", "nq"}
"""
import json, os, re, sys, time
FIFO, LOG = "/tmp/rp_fifo", "/tmp/pcc_rp.log"
dur = float(sys.argv[1]); out = sys.argv[2]
itv = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
line = float(sys.argv[4]) if len(sys.argv) > 4 else 200.0
# when the load stops. The sampler keeps running past it so the last
# samples are not clipped, but the binding evidence has to be taken while
# the traffic is still up: a QP silent for two seconds lets go of its set,
# so a dump after the load would show everything unbound and say nothing.
load_end = float(sys.argv[5]) if len(sys.argv) > 5 else dur
NSLOT = 32
QHOME = 128            # the executor's keyed window of the record table
DUMP_PER_PASS = 8      # record slots read per sampling pass (about 0.13 s)
RSP = re.compile(r"HPFT_RSP ft=0x([0-9a-f]+) bud=(\d+) lvl=(\d+) avg16=(\d+) r=(\d+) s16=(\d+) ep=(\d+)")


def gbps(u): return u * line / (1 << 20)


def paced_gbps(pacc, dt_s):
    """rate x time (fxp20 us >> 14) over an interval -> Gb/s"""
    return gbps((pacc << 14) / (dt_s * 1e6)) if dt_s > 0 else 0.0


def ask(word, arg):
    """One mailbox query, answer paired with it by file position. The RP log is
    flooded with a budget line every few ms while a run is on, so the answer
    has to be read from where the log ended before the query, not off the
    tail.

    The log is opened once and read forward, and the poll is 2 ms rather than
    10: a mailbox round is about 16 ms, so a 10 ms poll rounded every query up
    to 20-50 ms and made a query of the whole table cost more than the
    sampling period. That cost is the reason a joining flow-set used to be
    found slowly, which is what left holes in the executor figure."""
    with open(LOG, "rb") as lf:
        lf.seek(0, os.SEEK_END); pos = lf.tell()
    fd = os.open(FIFO, os.O_WRONLY)
    os.write(fd, ("%s %d\n" % (word, arg)).encode()); os.close(fd)
    with open(LOG, "rb") as lf:
        lf.seek(pos)
        tail = ""
        for _ in range(150):                 # 300 ms, same ceiling as before
            time.sleep(0.002)
            chunk = tail + lf.read().decode(errors="replace")
            lines = chunk.split("\n")
            tail = lines.pop()
            for l in lines:
                if "HPFT_RSP" in l:
                    return l.strip()
    return None


def write_qpdump(lines, path):
    """The binding evidence: every QP record the executor holds (collected a
    slice at a time by the sampling loop), the binding counters, and the
    sender agent's own map. A QP that never binds is not held by its flow
    set's pool at all, so the set's share is not enforced over it; having both
    sides in one file says whether such a QP was lost before the agent or
    after it."""
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
        try:
            with open("/tmp/hpft_txagent_qpmap.txt") as g:
                f.write(g.read())
        except OSError:
            f.write("agent qpmap: not published\n")


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
        with open(LOG, "rb") as lf:
            lf.seek(pos)
            tail = ""
            for _ in range(150):             # 300 ms for the answer
                time.sleep(0.002)
                # only the newly arrived bytes are scanned. The RP log gets a
                # budget line every few ms, so re-scanning the whole buffer on
                # every poll was quadratic in the answer's latency and pushed
                # a 0.66 s pass out to 1.25 s.
                chunk = tail + lf.read().decode(errors="replace")
                lines = chunk.split("\n")
                tail = lines.pop()
                for l in lines:
                    if "HPFT_RSP" in l:
                        g = RSP.search(l)
                        if g:
                            m = g
                if m:
                    break
        res.append((s, m))
    return res


# The evidence file lives at a fixed path, so a dump left by an earlier run
# would be collected again and attributed to this one - three dumps up to
# twenty minutes apart were once read as if they came from the same run
# (2026-09-09). Clear it before sampling starts.
try:
    os.unlink(out + ".qpdump")
except OSError:
    pass

t_start = time.time()
t_end = t_start + dur
last_q = {}                  # slot -> when it was last queried, for the interval
nq_seen = {}                 # slot -> its last reported member count
dumped = False               # the short-membership evidence is taken once
dump_todo, dump_lines, dump_why = [], [], ""
passes, slow, worst = 0, 0, 0.0
# When a slot first carries a flow set, the previous pass is a usable start
# for its interval: that pass queried the same slot and found it empty, so the
# counter it now reports was accumulated since then. Without this a joining
# flow set lost its first sample, and a set that only lives eight seconds lost
# an eighth of its window to the sampler rather than to the system.
prev_pass = None
# Every slot every pass. One query costs about 16 ms, so the whole 32-slot
# table costs 0.53 s inside a 1 s period, and a flow-set that joins mid-run is
# picked up on the very next pass. Sweeping only a few idle slots per pass was
# cheaper but found a join seconds late, and a flow-set that only lives
# eight seconds (V2's joiner) then lost most of its window to the distiller,
# which keeps a sample only while the set's row is scheduled.
with open(out, "w") as o:
    while time.time() < t_end:
        t = time.time()
        answers = query(range(NSLOT))
        # A set id in two slots at once is a leftover: the executor keeps a
        # set's slot until a zero budget retires it, so a slot from an earlier
        # run can still carry the id while the agent feeds the live one. The
        # stale copy reports no budget, and drawing it would put a second,
        # flat line on the figure under the same name.
        seen_id = {}
        for s, m in answers:
            sid = int(m.group(1), 16) if m else 0
            if sid:
                seen_id.setdefault(sid, []).append((int(m.group(2)), s))
        stale = set()
        for sid, lst in seen_id.items():
            if len(lst) > 1:
                lst.sort(reverse=True)       # largest budget wins
                stale.update(sl for _, sl in lst[1:])
        for s, m in answers:
            sid = int(m.group(1), 16) if m else 0
            if not sid or s in stale:
                last_q.pop(s, None); nq_seen.pop(s, None)
                continue
            prev = last_q.get(s, prev_pass)
            last_q[s] = t
            if prev is None:
                continue                 # no interval yet: this query only starts the clock
            rec = {"ts": round(t, 3), "slot": s, "id": "0x%08x" % sid,
                   "R": round(gbps(int(m.group(2))), 3),
                   "paced": round(paced_gbps(int(m.group(3)), t - prev), 3),
                   "cc": round(gbps(int(m.group(4))), 3), "cc_live": round(gbps(int(m.group(5))), 3),
                   "nlive": int(m.group(6)), "nq": int(m.group(7))}
            o.write(json.dumps(rec) + "\n")
            nq_seen[s] = rec["nq"]
        o.flush()
        # ---- the binding evidence, a slice of the record table per pass ----
        # Reading all HPFT_QHOME slots at once costs 2.2 s and would swallow
        # two sampling periods, which is exactly what broke the figure's
        # lines. The dump is therefore spread over the passes that follow it;
        # a QP's record and its binding do not move while it keeps sending, so
        # a dump smeared over some seconds still answers the question it is
        # there for.
        if not dumped and len(nq_seen) >= 3:
            common = max(set(nq_seen.values()), key=list(nq_seen.values()).count)
            short = [k for k, v in nq_seen.items() if v < common]
            if short:
                dumped = True
                dump_why = ("slots %s carry %s QPs, the others carry %d"
                            % (short, [nq_seen[k] for k in short], common))
                dump_todo = list(range(QHOME))
                # a partial sweep is still evidence if the run ends first
                write_qpdump([json.dumps({"reason": dump_why, "ts": round(time.time(), 3)}),
                              ask("0xdf3", 0) or "no answer"], out + ".qpdump")
        if not dumped and time.time() > t_start + load_end - 5.0:
            # Nothing looked wrong, so only the cheap half is taken: the
            # binding counters and the agent's own map, one query. Reading all
            # HPFT_QHOME record slots costs 2.2 s and is worth it only when a
            # set really is short, which is the branch above.
            dumped = True
            write_qpdump([json.dumps({"reason": "end of the load window, no set came up short",
                                      "ts": round(time.time(), 3)}),
                          ask("0xdf3", 0) or "no answer"], out + ".qpdump")
        if dump_todo:
            if not dump_lines:               # the header, taken once, up front
                dump_lines = [json.dumps({"reason": dump_why, "ts": round(time.time(), 3)}),
                              ask("0xdf3", 0) or "no answer"]
            for slot in dump_todo[:DUMP_PER_PASS]:
                l = ask("0xdee", slot)
                if l:
                    dump_lines.append("slot %d %s" % (slot, l))
            dump_todo = dump_todo[DUMP_PER_PASS:]
            write_qpdump(dump_lines, out + ".qpdump")
        prev_pass = t
        passes += 1
        took = time.time() - t
        worst = max(worst, took)
        if took > itv:
            slow += 1
        # reported as the run goes, not only at exit: the runner collects this
        # file while the sampler is still running, and a host whose passes are
        # stretching is exactly what explains a thin line in the figure
        if passes % 20 == 0:
            print("rp_sample: %d passes, worst %.2f s, %d over the %.1f s period"
                  % (passes, worst, slow, itv), flush=True)
        time.sleep(max(0.0, itv - (time.time() - t)))
# The sampler's own timing, so a run that could not keep up says so instead of
# leaving an unexplained hole in the figure.
print("rp_sample: %d passes, worst %.2f s, %d over the %.1f s period"
      % (passes, worst, slow, itv), flush=True)
