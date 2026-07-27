#!/usr/bin/env python3
"""Property test: the C water-filling must agree with the pure-Python
reference on every input, including the awkward ones.

The pure-Python path is the reference precisely because it is the code
that has been running in the lab and producing every result so far. A
rewrite that is merely "close" is not acceptable here: these numbers are
policy shares, and a systematic difference would show up as a fairness
result nobody could explain.

usage: fastfill_test.py [--cases 4000]
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fastfill  # noqa: E402

if not fastfill.USING_C:
    print("libfastfill.so not loaded - nothing to compare against")
    sys.exit(1)

N = int(sys.argv[sys.argv.index("--cases") + 1]) if "--cases" in sys.argv else 4000
INF = float("inf")
rng = random.Random(20260727)

worst_fill = worst_ceil = 0.0
bad = []


def close(a, b, tol=1e-6):
    if a == b:
        return True
    if a == INF or b == INF:
        return False
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def gen_case(i):
    """Mix of ordinary and awkward shapes."""
    m = rng.choice([1, 1, 2, 2, 3, 4, 5, 8, 13, 20])
    style = i % 7
    items = {}
    for k in range(m):
        w = rng.choice([1.0, 1.0, 1.0, 2.0, 0.5, rng.uniform(0.1, 4)])
        if style == 0:            # everything saturates
            c = rng.uniform(0.1, 2.0)
        elif style == 1:          # nothing saturates
            c = rng.uniform(1e3, 1e4)
        elif style == 2:          # some zero caps (idle flow-sets)
            c = 0.0 if rng.random() < 0.4 else rng.uniform(1, 100)
        elif style == 3:          # equal caps, the tie case
            c = 10.0
        elif style == 4:          # huge dynamic range
            c = rng.choice([1e-3, 1.0, 1e3, 1e6])
        else:
            c = rng.uniform(0.5, 200)
        items["k%d" % k] = (w, c)
    total = sum(c for _, c in items.values())
    cap = rng.choice([0.0, total * rng.uniform(0.05, 0.95), total,
                      total * rng.uniform(1.0, 3.0)])
    return items, cap


for i in range(N):
    items, cap = gen_case(i)

    a = fastfill.waterfill(cap, items)
    b = fastfill._waterfill_py(cap, items)
    for k in items:
        d = abs(a[k] - b[k])
        worst_fill = max(worst_fill, d)
        if not close(a[k], b[k], 1e-6):
            bad.append(("waterfill", items, cap, k, a[k], b[k]))

    for repl_mode in ("inf", "finite", "mixed"):
        if repl_mode == "inf":
            repl = None
        elif repl_mode == "finite":
            repl = {k: items[k][1] * rng.uniform(1.0, 5.0) for k in items}
        else:
            repl = {k: (INF if rng.random() < 0.5
                        else items[k][1] * rng.uniform(1.0, 5.0))
                    for k in items}
        a = fastfill.waterfill_ceilings(cap, items, repl)
        b = fastfill._waterfill_ceilings_py(cap, items, repl)
        for k in items:
            d = abs(a[k] - b[k])
            worst_ceil = max(worst_ceil, d)
            if not close(a[k], b[k], 1e-6):
                bad.append(("ceilings/" + repl_mode, items, cap, k,
                            a[k], b[k]))

print("cases: %d   waterfill worst |C-Py| = %.3e   ceilings worst = %.3e"
      % (N, worst_fill, worst_ceil))
if bad:
    print("MISMATCHES: %d (showing 5)" % len(bad))
    for what, items, cap, k, x, y in bad[:5]:
        print("  %s cap=%.6g key=%s  C=%.10g  Py=%.10g" % (what, cap, k, x, y))
        print("     items=%s" % items)
    sys.exit(1)
print("C and Python agree on every case.")
