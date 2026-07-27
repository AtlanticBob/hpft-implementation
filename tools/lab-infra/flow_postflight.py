#!/usr/bin/env python3
"""Scan an rx_agent jsonl after a run and flag the silent failure modes.

Three checks, each of which has produced wrong conclusions before:

  dead   a flow-set present in the flow table whose wire stayed at zero
         through the steady window - a QP killed by an RC error. Every
         other layer reads healthy while this is true.
INFORMATIONAL, never a failure: the grant-vs-wire reconciliation. A
flow-set running below its pace is NOT a fault by itself - the target is
the ceiling, not a grant (design.md §3.4/§4.2), so an app-limited or
sender-tree-limited flow-set legitimately sits below it. It is printed
because it is the single most useful table in this system's failure
analysis: it separates "the allocator was unfair" from "the executor did
not deliver", and reading a fairness number without it is how one run got
within an inch of being reported as a fairness regression when the
allocator had in fact been exact.

Note the two references are BOTH insufficient on their own, which is why
this cannot be automated into a verdict: against the pace it fires on
every app-limited flow-set; against the demand-capped grant e it misses a
crushed one, because e follows the crush down. Distinguishing "would not"
from "could not" is precisely the ambiguity §3.2.2 exists to manage - a
script cannot resolve it, so it reports and lets the analyst judge.
  hot    the link sitting at saturation through the startup window. The
         overload that kills QPs lives in OFFERED load, which is not
         observable here (arrivals cannot exceed the link), so this is the
         observable signature of it rather than the thing itself: if the
         link is pinned at capacity while flows are still ramping, the
         offered load was above it.

usage: flow_postflight.py <tag_rx.jsonl> <t0_file> [--tx tag_tx.jsonl]
                          [--window 40,88] [--capacity-g 97]
"""
import json
import sys

args = sys.argv[1:]
rxp = args[0]
t0 = float(open(args[1]).read().strip())
txp = args[args.index("--tx") + 1] if "--tx" in args else None
lo, hi = (float(x) for x in
          (args[args.index("--window") + 1] if "--window" in args
           else "40,88").split(","))
# PHYSICAL link, not the ledger capacity C' - arrivals legitimately sit
# between C' and the link, that is what headroom is for.
cap = float(args[args.index("--capacity-g") + 1]
            if "--capacity-g" in args else 100) * 1e9

def _load(path):
    """Tolerate a truncated final line: these logs are copied while the
    agent is still appending, so the last record is often half-written."""
    out = []
    for l in open(path):
        l = l.strip()
        if not l:
            continue
        try:
            out.append(json.loads(l))
        except ValueError:
            continue
    return out


rows = _load(rxp)
win = [x for x in rows if lo <= x["ts"] - t0 <= hi]
if not win:
    print("postflight: no ticks in the window - run too short or t0 wrong")
    sys.exit(1)

present, zero = {}, {}
for x in win:
    for f in x.get("u", {}):            # membership = in the report
        present[f] = present.get(f, 0) + 1
        if x.get("r", {}).get(f, 0) == 0:
            zero[f] = zero.get(f, 0) + 1

bad = []
for f, n in sorted(present.items()):
    z = zero.get(f, 0)
    if z >= 0.98 * n:
        bad.append(("dead", f, "wire zero in %d/%d ticks" % (z, n)))

start = [x for x in rows if 0 <= x["ts"] - t0 <= 5.0]
if start:
    hot = sum(1 for x in start
              if sum(x.get("r", {}).values()) >= 0.98 * cap)
    if hot >= 0.5 * len(start):
        bad.append(("hot", "-",
                    "link at >=98%% of %.0fG for %d/%d startup ticks - "
                    "offered load was above capacity while flows ramped"
                    % (cap / 1e9, hot, len(start))))

info = []
if txp:
    import statistics as st
    per = {}
    for l in open(txp):
        l = l.strip()
        if not l:
            continue
        try:
            r = json.loads(l)
        except ValueError:
            continue
        if r.get("mode") != "track" or "pace" not in r:
            continue
        per.setdefault(r["fs"], []).append((r.get("pace", 0), r.get("r", 0)))
    for f, v in sorted(per.items()):
        p = st.mean([a for a, _ in v])
        w = st.mean([b for _, b in v])
        if p > 0:
            info.append((f, w, p))

for kind, f, msg in bad:
    print("  FAULT  %-6s %-34s %s" % (kind, f, msg))
if info:
    print("  -- grant vs wire (informational, judge it yourself) --")
    for f, w, p in sorted(info, key=lambda x: x[1] / max(x[2], 1)):
        print("     %-34s wire %6.2fG  pace %6.2fG  %3.0f%%"
              % (f, w / 1e9, p / 1e9, 100 * w / p))
print("postflight: %d flow-sets, %d FAULTS" % (len(present), len(bad)))
sys.exit(1 if bad else 0)
