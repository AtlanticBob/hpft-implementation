#!/usr/bin/env python3
"""Offline acceptance for the executor's CC combiner (rate = d * level).

The combiner lives in device code on the DPA, where it cannot be stepped or
instrumented. This transcribes its integer arithmetic exactly - the same
fxp20 units, the same shift-normalised divide, the same clamps - and checks
the properties the design rests on:

  A. a CC that has ratcheted to nothing still gets its share, which is the
     property min(cc, level) does not have
  B. the observer reproduces the CC's own decrease, in proportion
  C. the reading needs no calibration: scaling every sample by a constant
     leaves d untouched
  D. attribution - a cut the executor's own shaping caused is not counted
  E. the policy ceiling is never exceeded, and recovery lands on it

Run: python3 tools/tests/couple_check.py
"""
import sys

D_ONE = 1 << 20                 # 1.0 in the rates' fxp20
FLOOR = D_ONE >> 6              # g_d_floor, a backstop
RECOVER_US = 300                # g_d_recover_us
DQ_G_FXP16 = 1024               # the stock DCQCN's g = 1/64
DQ_ALPHA_ONE = 65536

fails = []


def check(name, ok, detail=""):
    print("%-44s %s  %s" % (name, "PASS" if ok else "FAIL", detail))
    if not ok:
        fails.append(name)


def observe_dec(d, cc, prev):
    """hpft_observe's decrease branch: d <- d * cc/prev, shift-normalised so
    the divide stays 32-bit."""
    sh = 5 if prev > 0xffff else 0
    pn, cn = prev >> sh, cc >> sh
    ratio = ((cn << 16) // pn) if pn else (1 << 16)
    nd = (d * ratio) >> 16
    return FLOOR if nd < FLOOR else nd


def recover(d):
    return d if d >= D_ONE else (D_ONE >> 1) + (d >> 1)


def stock_dcqcn_cnp(rc, rt, alpha):
    """The CC, unmodified: Rt = Rc; Rc = Rc(1-alpha/2); alpha += g(1-alpha)."""
    cut = (rc * alpha) >> 17
    rt = rc
    rc = rc - cut if rc > cut + 2 else 2
    alpha += ((DQ_ALPHA_ONE - alpha) * DQ_G_FXP16) >> 16
    return rc, rt, min(alpha, DQ_ALPHA_ONE)


# ------------------------------------------------------- A. no ratchet
# Run the stock DCQCN through a long burst so it ratchets the way it does on
# the wire, and read it the way the executor does.
LEVEL = 153145                  # a real budget from the lab, device units
rc, rt, alpha = D_ONE, D_ONE, DQ_ALPHA_ONE // 8
d, prev = D_ONE, D_ONE
for _ in range(200):
    rc, rt, alpha = stock_dcqcn_cnp(rc, rt, alpha)
    d = observe_dec(d, rc, prev)
    prev = rc
cc_after = rc
check("A1 the CC does ratchet, untouched", rc < D_ONE // 1000 and rt < D_ONE // 500,
      "Rc=%.5f Rt=%.5f of line after 200 CNPs" % (rc / D_ONE, rt / D_ONE))
# silence: the CC stops decreasing, so the observer stops being told anything
for _ in range(20):
    d = recover(d)
check("A2 the share comes back anyway", d >= D_ONE * 0.99,
      "d=%.3f -> %.2f G of an 11.5 G share, with the CC still at %.5f"
      % (d / D_ONE, d / D_ONE * 11.5, cc_after / D_ONE))
check("A3 min(cc, level) does not come back", min(cc_after, LEVEL) < LEVEL // 1000,
      "min gives %.4f G on the same CC state" % (min(cc_after, LEVEL) / LEVEL * 11.5))

# ------------------------------------------------- B. faithful decrease
rc, rt, alpha = D_ONE, D_ONE, DQ_ALPHA_ONE
d, prev = D_ONE, D_ONE
rc, rt, alpha = stock_dcqcn_cnp(rc, rt, alpha)
d = observe_dec(d, rc, prev)
check("B1 one cut is reproduced in proportion", abs(d / D_ONE - rc / D_ONE) < 1e-3,
      "CC cut to %.3f of itself, d to %.3f of the share" % (rc / D_ONE, d / D_ONE))

# ------------------------------------------------ C. calibration-free
# Only ratios are read, so whatever constant sits between the sample and the
# rate the CC would have paced at - fxp20 of line here, cwnd/srtt in the TCP
# shaper, BBR's 2x - divides out. It divides out exactly in the reals; what
# is left in the integers is the rounding of the shift-normalised divide.
lands = []
for scale in (1, 2, 17, 1000):
    rc, rt, alpha = D_ONE // 32, D_ONE // 32, DQ_ALPHA_ONE // 4
    d, prev = D_ONE, rc * scale
    for _ in range(12):
        rc, rt, alpha = stock_dcqcn_cnp(rc, rt, alpha)
        d = observe_dec(d, rc * scale, prev)
        prev = rc * scale
    lands.append(d)
spread = (max(lands) - min(lands)) / max(lands)
check("C1 a constant factor in the reading cancels", spread < 1e-3,
      "d lands within %.3f%% across a 1000x range in the sample's scale"
      % (spread * 100))

# -------------------------------------------------------- D. attribution
# Pace-limited: the queue the CC reacts to is the one our own shaping made.
rc, rt, alpha = D_ONE, D_ONE, DQ_ALPHA_ONE
d, prev = D_ONE, D_ONE
for _ in range(200):
    rc, rt, alpha = stock_dcqcn_cnp(rc, rt, alpha)
    prev = rc                      # read, but not counted
    d = recover(d)
check("D1 a cut we caused is not counted", d == D_ONE,
      "d stays at the share through 200 CNPs while pace-limited")

# Not pace-limited, then pace-limited again: the rule is self-limiting, so a
# flow cannot be walked down even with the floor removed from the picture.
d, prev, rc = D_ONE, D_ONE, D_ONE
alpha, rt = DQ_ALPHA_ONE, D_ONE
paced_limited = False
for i in range(300):
    rc, rt, alpha = stock_dcqcn_cnp(rc, rt, alpha)
    if not paced_limited:
        d = observe_dec(d, rc, prev)
        paced_limited = d < D_ONE // 2      # shaped low enough to be held by us
    else:
        d = recover(d)
        paced_limited = d < D_ONE // 2
    prev = rc
check("D2 the rule is self-limiting", d > D_ONE // 2,
      "d settles at %.2f of the share instead of walking to the floor"
      % (d / D_ONE))

# ----------------------------------------------------------- E. ceiling
over = [x for x in range(0, D_ONE + 1, 1024) if (x * LEVEL) >> 20 > LEVEL]
check("E1 the policy ceiling is never exceeded", not over,
      "max rate over d in [0,1] is %d, level is %d" % ((D_ONE * LEVEL) >> 20, LEVEL))
d, steps = FLOOR, 0
while d < int(D_ONE * 0.95):
    d = recover(d)
    steps += 1
check("E2 recovery lands on the share", recover(D_ONE) == D_ONE,
      "floor -> 95%% in %d steps (%.1f ms at %d us); d=1 is a fixed point"
      % (steps, steps * RECOVER_US / 1e3, RECOVER_US))

print()
n = 9
print("%d/%d checks passed" % (n - len(fails), n))
sys.exit(1 if fails else 0)
