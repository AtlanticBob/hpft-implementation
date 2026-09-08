"""Single-pass ceilings for bounded weighted water-filling.

waterfill_ceilings(C, items) == {k: waterfill(C, items with k's cap
replaced by repl_k)[k] for k in items}, computed in O(m log m) total
instead of m independent fills (O(m^2)). repl_k defaults to infinity
(the fair-share ceiling of design_e); a finite repl_k supports the
VM layer where the cap is replaced by MaxRate, not removed.

Math: with all caps, consumption at water level t is
F(t) = sum_j min(c_j, w_j t)  (piecewise linear, breakpoints t_j=c_j/w_j).
Replacing item i's cap by r_i changes only i's own term, so the new
level solves F(t) - min(c_i, w_i t) + min(r_i, w_i t) = C: binary-search
the same breakpoint grid with prefix sums. Ceiling_i = min(r_i, w_i t).
"""
import bisect
import ctypes
import os

# The C implementation of both primitives (fastfill.c). Water-filling is
# 77% of the receiver's per-tick cost at every scale measured, and the
# scaling is already near-linear, so the constant factor is the whole
# problem (measured on the 2026-07-27 scale sweep). Loaded through ctypes so the DPU
# Arm needs only gcc, not Python headers.
#
# Absent or unloadable .so falls back to the pure-Python path below, which
# stays as the reference the C is property-tested against (fastfill_test.py).
_LIB = None
try:
    _p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "libfastfill.so")
    if os.path.exists(_p):
        _LIB = ctypes.CDLL(_p)
        _dp = ctypes.POINTER(ctypes.c_double)
        _LIB.hpft_waterfill.restype = None
        _LIB.hpft_waterfill.argtypes = [ctypes.c_double, ctypes.c_int,
                                        _dp, _dp, _dp]
        _LIB.hpft_waterfill_ceilings.restype = None
        _LIB.hpft_waterfill_ceilings.argtypes = [ctypes.c_double,
                                                 ctypes.c_int,
                                                 _dp, _dp, _dp, _dp]
        _ip = ctypes.POINTER(ctypes.c_int)
        _LIB.hpft_entitlements.restype = None
        _LIB.hpft_entitlements.argtypes = [ctypes.c_int, _ip, _ip, _dp, _dp,
                                           ctypes.c_int, ctypes.c_int,
                                           _dp, _dp, _dp, ctypes.c_double,
                                           _dp, _dp]
except OSError:
    _LIB = None

USING_C = _LIB is not None


def _arr(vals):
    return (ctypes.c_double * len(vals))(*vals)


def entitlements_c(fsids, dst_idx, cls_idx, wfs, demand,
                   n_vm, n_cls, vm_w, vm_max, cls_w, c_root):
    """Whole three-layer allocation in one crossing of the boundary.

    Returns (e, ceil) as dicts keyed by fsid. Callers that cannot build
    the flat form should use the Scheduler's Python path; this exists
    because per-layer ctypes calls left marshalling, not arithmetic, as
    the dominant per-tick cost."""
    n = len(fsids)
    if n == 0:
        return {}, {}
    d = (ctypes.c_int * n)(*dst_idx)
    c = (ctypes.c_int * n)(*cls_idx)
    w = _arr(wfs)
    dm = _arr(demand)
    vw = _arr(vm_w)
    vm = _arr(vm_max)
    cw = _arr(cls_w)
    oe = (ctypes.c_double * n)()
    oc = (ctypes.c_double * n)()
    _LIB.hpft_entitlements(n, d, c, w, dm, n_vm, n_cls, vw, vm, cw,
                           float(c_root), oe, oc)
    return ({f: oe[i] for i, f in enumerate(fsids)},
            {f: oc[i] for i, f in enumerate(fsids)})


def waterfill(capacity, items):
    """Bounded weighted water-filling; items: {key: (weight, cap)}."""
    if not items:
        return {}
    if _LIB is None:
        return _waterfill_py(capacity, items)
    keys = list(items)
    w = _arr([float(items[k][0]) for k in keys])
    c = _arr([float(items[k][1]) for k in keys])
    out = (ctypes.c_double * len(keys))()
    _LIB.hpft_waterfill(float(capacity), len(keys), w, c, out)
    return {k: out[i] for i, k in enumerate(keys)}


def _waterfill_py(capacity, items):
    alloc = {k: 0.0 for k in items}
    active = {k: v for k, v in items.items() if v[1] > 0}
    remaining = capacity
    while active and remaining > 1e-3:
        wsum = sum(w for w, _ in active.values())
        t_sat = min((cap - alloc[k]) / w for k, (w, cap) in active.items())
        t = min(t_sat, remaining / wsum)
        for k, (w, cap) in list(active.items()):
            alloc[k] += w * t
            if alloc[k] >= cap - 1e-3:
                alloc[k] = cap
                del active[k]
        remaining = capacity - sum(alloc.values())
        if t < t_sat and t_sat != float("inf"):
            break
    return alloc


def waterfill_ceilings(capacity, items, repl=None):
    if not items:
        return {}
    if _LIB is None:
        return _waterfill_ceilings_py(capacity, items, repl)
    keys = list(items)
    w = _arr([float(items[k][0]) for k in keys])
    c = _arr([float(items[k][1]) for k in keys])
    out = (ctypes.c_double * len(keys))()
    if repl is None:
        rp = None
    else:
        rp = _arr([float(repl.get(k, float("inf"))) for k in keys])
    _LIB.hpft_waterfill_ceilings(float(capacity), len(keys), w, c, rp, out)
    return {k: out[i] for i, k in enumerate(keys)}


def _waterfill_ceilings_py(capacity, items, repl=None):
    if not items:
        return {}
    keys = list(items)
    ws = {k: items[k][0] for k in keys}
    cs = {k: items[k][1] for k in keys}
    # sorted breakpoints and prefix sums of the all-caps consumption F
    order = sorted(keys, key=lambda k: cs[k] / ws[k])
    ts = [cs[k] / ws[k] for k in order]
    pc = [0.0]          # prefix sum of caps saturated below segment
    for k in order:
        pc.append(pc[-1] + cs[k])
    sw = [0.0] * (len(order) + 1)   # suffix sum of weights (unsaturated)
    for i in range(len(order) - 1, -1, -1):
        sw[i] = sw[i + 1] + ws[order[i]]
    pos = {k: i for i, k in enumerate(order)}

    def F(t):
        j = bisect.bisect_right(ts, t)
        return pc[j] + sw[j] * t

    out = {}
    for k in keys:
        r = float("inf") if repl is None else repl.get(k, float("inf"))
        w, c = ws[k], cs[k]

        def g(t):
            # consumption with k's cap replaced by r
            return F(t) - min(c, w * t) + min(r, w * t)

        # find the level t* with g(t*) = capacity on the breakpoint grid
        # extended by r/w (the replacement's own breakpoint)
        grid = ts + ([r / w] if r != float("inf") else [])
        grid = sorted(set(grid))
        lo_t, lo_g = 0.0, 0.0
        t_star = None
        for bp in grid:
            gb = g(bp)
            if gb >= capacity - 1e-6:
                # linear segment [lo_t, bp]
                slope = (gb - lo_g) / (bp - lo_t) if bp > lo_t else 0.0
                t_star = lo_t + ((capacity - lo_g) / slope if slope > 0 else 0.0)
                break
            lo_t, lo_g = bp, gb
        if t_star is None:
            # capacity not exhausted even past all breakpoints
            j = len(ts)
            w_open = sw[j] - (0.0 if cs[k] / ws[k] > ts[-1] else 0.0)
            # remaining slope = weights never saturating = w (k, if r=inf)
            slope = w if r == float("inf") else 0.0
            if slope > 0:
                t_star = lo_t + (capacity - lo_g) / slope
            else:
                t_star = lo_t
        out[k] = min(r, w * t_star)
    return out
