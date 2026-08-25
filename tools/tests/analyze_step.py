#!/usr/bin/env python3
"""Join/leave step run -> settle times, overshoot, steady jitter.

Receiver records are 20 ms apart. Shares: C'/8 with the incumbent alone,
C'/16 with the joiner present. A step is "settled" when every flow-set
of the group has been inside +-10% of its new share for 5 consecutive
records (100 ms). Aggregate overshoot is the peak 20 ms sum of arrivals
over C' in the second after the leave (all incumbents climb at once).
"""
import json
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
DIR = REPO / "results" / "regression"
BAND = 0.10


def root_bps():
    r = json.load(open(REPO / "config" / "lab-registry.json"))
    return r["line_rate_bps"] * (1.0 - r["e_params"]["headroom"])


def load(tag):
    t0 = float((DIR / f"{tag}_t0.txt").read_text())
    rows = []
    for l in (DIR / f"{tag}_rx.jsonl").read_text().splitlines():
        try:
            rec = json.loads(l)
        except ValueError:
            continue
        if rec["ts"] >= t0:
            rows.append((rec["ts"] - t0, rec["r"], rec.get("d", {})))
    return rows


def settle(rows, group, share, t_from, t_to):
    ok = 0
    for t, r, _ in rows:
        if t < t_from:
            continue
        if t > t_to:
            return None
        if all(abs(r.get(f, 0) - share) <= BAND * share for f in group):
            ok += 1
            if ok >= 5:
                return t - t_from - 0.08
        else:
            ok = 0
    return None


def main(tag):
    rows = load(tag)
    C = root_bps()
    keys = set()
    first, last = {}, {}
    for t, r, _ in rows:
        for f, v in r.items():
            if v > 5e7:
                keys.add(f)
                first.setdefault(f, t)
                last[f] = t
    inc = sorted(f for f in keys if first[f] < 5)
    joiner = sorted(f for f in keys if first[f] >= 20)
    if not inc or not joiner:
        print("groups: incumbents=%d joiner=%d - nothing to measure" % (len(inc), len(joiner)))
        return
    t_join = min(first[f] for f in joiner)
    # the joiner's classes end at different times (iperf runs dur-2 s from
    # connect, perftest -D from its own later connect), so "leave" is per
    # class and the shared plateau ends at the EARLIER of the two
    t_leave_c = {c: max(last[f] for f in joiner if f.endswith(c)) for c in ("rdma", "tcp")
                 if any(f.endswith(c) for f in joiner)}
    t_leave = min(t_leave_c.values())
    n1, n2 = len(inc), len(inc) + len(joiner)
    s1, s2 = C / n1, C / n2
    print("=== %s : %d incumbents, %d joiners; join %.1fs leave %.1fs; shares %.1fG -> %.1fG -> %.1fG"
          % (tag, n1, len(joiner), t_join, t_leave, s1 / 1e9, s2 / 1e9, s1 / 1e9))

    def cls(f):
        return f.rsplit("|", 1)[1]
    for name, grp, share, a, b in [
            ("incumbents down-step (join)", inc, s2, t_join, t_leave),
            ("joiner cold start", joiner, s2, t_join, t_leave)]:
        for c in ("rdma", "tcp"):
            g = [f for f in grp if cls(f) == c]
            st = settle(rows, g, share, a, b)
            print("  %-30s %-5s settle %s" % (name, c, "%.0f ms" % (st * 1e3) if st is not None else "never"))
    # up-step: when the joiner's class c leaves, the incumbents of class c
    # go back to C'/n1 within that class (the other class is unaffected
    # until its own joiners leave)
    for c in ("rdma", "tcp"):
        g = [f for f in inc if cls(f) == c]
        tl = t_leave_c.get(c)
        st = settle(rows, g, s1, tl, tl + 5) if tl else None
        print("  %-30s %-5s settle %s (leave at %.1fs)" % ("incumbents up-step (leave)", c,
              "%.0f ms" % (st * 1e3) if st is not None else "never", tl or 0))
    # aggregate overshoot after the last leave
    t_last = max(t_leave_c.values())
    agg = [(t, sum(v for f, v in r.items() if f in keys)) for t, r, _ in rows if t_last <= t <= t_last + 1.5]
    peak = max(v for _, v in agg) if agg else 0
    print("  aggregate after leave: peak %.1fG = %.0f%% of C' (%.0fG)" % (peak / 1e9, 100 * peak / C, C / 1e9))
    agg_j = [(t, sum(v for f, v in r.items() if f in keys)) for t, r, _ in rows if t_join <= t <= t_join + 1.5]
    peak_j = max(v for _, v in agg_j) if agg_j else 0
    print("  aggregate after join:  peak %.1fG = %.0f%% of C'" % (peak_j / 1e9, 100 * peak_j / C))
    # steady jitter per class in the three plateaus
    for name, a, b, grp in [("alone 10-28s", 10, t_join - 2, inc),
                            ("shared +8..-4s", t_join + 8, t_leave - 4, inc + joiner),
                            ("alone again +5s..85", t_last + 5, 85, inc)]:
        out = []
        for c in ("rdma", "tcp"):
            sds, means = [], []
            for f in grp:
                if cls(f) != c:
                    continue
                v = [r[f] / 1e9 for t, r, _ in rows if a <= t <= b and f in r]
                if len(v) > 10:
                    sds.append(statistics.pstdev(v))
                    means.append(statistics.mean(v))
            if sds:
                out.append("%s mean %.2fG sd %.2f" % (c, statistics.mean(means), statistics.mean(sds)))
        # signal liveness
        live = [1 if any(dd.get(f, 0) > 0 for f in grp) else 0 for t, _, dd in rows if a <= t <= b]
        print("  %-22s %s | some queue non-empty %.0f%% of ticks" % (name, " | ".join(out), 100 * sum(live) / max(len(live), 1)))


if __name__ == "__main__":
    main(sys.argv[1])
